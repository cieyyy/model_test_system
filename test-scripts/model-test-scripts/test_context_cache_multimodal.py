#!/usr/bin/env python3
"""上下文、缓存、多模态和请求体边界专项测试。

--mode context 测 1M 上下文，cache 测 cached_tokens/命中率，mixed 测图片+视频，
--body-mb 测 50/70/80/90/95/100MB 入口限制。指标包括 HTTP、耗时、Token、缓存和 x-request-id。
运行：python3 test_context_cache_multimodal.py --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" --mode cache --rounds 10 --run --confirm-paid
"""

import argparse
import base64
import csv
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from sophnet_result_export import export_xlsx

ROOT = Path(__file__).resolve().parent


def args_parser():
    p = argparse.ArgumentParser(description="长上下文/缓存/多模态压测")
    p.add_argument("--base-url", default=os.getenv("API_BASE", "http://127.0.0.1:8000/v1"))
    p.add_argument("--key-env", default=os.getenv("API_KEY_ENV", "API_KEY"))
    p.add_argument("--model", default=os.getenv("MODEL", "test-model"))
    p.add_argument("--mode", choices=("context", "cache", "mixed"), default="context")
    p.add_argument("--context-tokens", type=int, default=1000000)
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--body-mb", type=float, default=0, help="mixed 模式的目标请求体 MB；填充写入 metadata，不计入模型提示词")
    p.add_argument("--max-completion-tokens", type=int, default=1024)
    p.add_argument("--image", type=Path)
    p.add_argument("--video", type=Path)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--output-dir", type=Path)
    p.add_argument("--run", action="store_true")
    p.add_argument("--confirm-paid", action="store_true")
    return p.parse_args()


def request(args, key, body):
    payload = None; header_file = None; start = time.perf_counter()
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
            json.dump(body, f, ensure_ascii=False); payload = f.name
        header_file = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".headers", delete=False).name
        p = subprocess.run(["curl", "-sS", "--max-time", str(args.timeout), "-H", f"Authorization: Bearer {key}", "-H", "Content-Type: application/json", "--data-binary", "@"+payload, "--dump-header", header_file, "--write-out", "\n__HTTP_STATUS__:%{http_code}", args.base_url.rstrip("/")+"/chat/completions"], text=True, capture_output=True, check=False)
        raw=p.stdout; marker="\n__HTTP_STATUS__:"; raw,status=raw.rsplit(marker,1) if marker in raw else (raw,"")
        try: out=json.loads(raw)
        except json.JSONDecodeError: out={"unparsed_response":raw}
        headers = Path(header_file).read_text(errors="replace") if header_file else ""
        header_id = re.search(r"(?im)^x-request-id:\s*(\S+)", headers)
        body_id = re.search(r"(?i)request id[:\\s]+([A-Za-z0-9_-]+)", raw)
        out["_x_request_id"] = header_id.group(1) if header_id else (body_id.group(1) if body_id else "")
        return int(status) if status.strip().isdigit() else None, round((time.perf_counter()-start)*1000,1), out
    finally:
        if payload:
            try: Path(payload).unlink()
            except FileNotFoundError: pass
        if header_file:
            try: Path(header_file).unlink()
            except FileNotFoundError: pass


def data_uri(path):
    mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,"+base64.b64encode(path.read_bytes()).decode("ascii")


def main():
    args=args_parser()
    if args.context_tokens<1000 or args.rounds<1: raise SystemExit("context-tokens 至少1000，rounds至少1")
    planned=args.rounds
    print(f"项目=Sophnet {args.mode}；模型={args.model}；上下文目标={args.context_tokens} tokens；计划={planned}请求")
    if args.mode=="mixed": print(f"混合视觉请求体目标={args.body_mb}MB；需要 --image 和/或 --video")
    if not(args.run and args.confirm_paid): print("仅预览；实际请求需同时添加 --run --confirm-paid"); return
    key=os.environ.get(args.key_env,"")
    if not key: raise SystemExit(f"未设置环境变量 {args.key_env}")
    out=(args.output_dir.resolve() if args.output_dir else ROOT/"results"/(datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-sophnet-load")); out.mkdir(parents=True,exist_ok=False)
    rows=[]
    for i in range(1,planned+1):
        if args.mode=="context":
            # 使用保守 ASCII 负载，避免中文字符分词膨胀；为输出预留 4096 tokens。
            target = max(1000, args.context_tokens - max(args.max_completion_tokens, 4096) - 10000)
            text = ("x " * target) + "\n核验码：CTX-GENERIC-731"
            body={"model":args.model,"messages":[{"role":"user","content":text}],"max_completion_tokens":args.max_completion_tokens}
        elif args.mode=="cache":
            prefix="固定前缀资料，用于自动 KV Cache 命中测试。"*1000
            body={"model":args.model,"messages":[{"role":"system","content":prefix},{"role":"user","content":f"第{i}轮只回复 CACHE-{i}"}],"max_completion_tokens":64}
        else:
            content=[]
            if args.image and args.image.is_file(): content.append({"type":"image_url","image_url":{"url":data_uri(args.image)}})
            if args.video and args.video.is_file(): content.append({"type":"video_url","video_url":{"url":data_uri(args.video)}})
            content.append({"type":"text","text":"概括这些视觉资料。"})
            body={"model":args.model,"messages":[{"role":"user","content":content}],"max_completion_tokens":args.max_completion_tokens}
            if args.body_mb>0:
                # 不把大段填充放进 messages，否则会被模型 tokenizer 解析并触发
                # 上下文超限 400。metadata 是 OpenAI 兼容请求的非提示词字段，
                # 用于验证网关请求体大小；真实视觉内容仍保持原始图片/视频。
                padding_bytes = max(0, int(args.body_mb * 1024 * 1024))
                body["metadata"] = {"request_body_padding": "A" * padding_bytes}
        status,elapsed,response=request(args,key,body); usage=response.get("usage") or {}; choice=(response.get("choices") or [{}])[0]
        row={"round":i,"http_status":status or "","elapsed_ms":elapsed,"input_tokens":usage.get("prompt_tokens",""),"output_tokens":usage.get("completion_tokens",""),"cached_tokens":(usage.get("prompt_tokens_details") or {}).get("cached_tokens",usage.get("cached_tokens","")),"finish_reason":choice.get("finish_reason",""),"response_model":response.get("model",""),"request_id":response.get("_x_request_id",""),"error":json.dumps(response.get("error", ""),ensure_ascii=False)}; rows.append(row)
        print(f"#{i} HTTP={status or '-'} TOTAL={elapsed}ms INPUT={row['input_tokens']} CACHE={row['cached_tokens']} FINISH={row['finish_reason'] or '-'}")
    with (out/"results.csv").open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    (out/"summary.json").write_text(json.dumps({"model":args.model,"base_url":args.base_url,"mode":args.mode,"context_tokens":args.context_tokens,"body_mb":args.body_mb,"requests":len(rows),"http_200":sum(r["http_status"]==200 for r in rows),"generated_at":datetime.now(timezone.utc).isoformat()},ensure_ascii=False,indent=2),encoding="utf-8")
    export_xlsx(out, rows, {"模型": args.model, "地址": args.base_url, "模式": args.mode, "上下文目标": args.context_tokens, "请求数": len(rows)})
    print(f"结果目录：{out}")


if __name__=="__main__": main()

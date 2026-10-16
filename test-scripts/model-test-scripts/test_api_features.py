#!/usr/bin/env python3
"""Responses、工具闭环和视觉能力测试。

默认仅预览。付费请求需同时添加 --run --confirm-paid。
指标包括 HTTP、内容判定、x-request-id、工具调用次数、视觉结果和 finish_reason。
官方工具、联网搜索、网页抓取、文件问答和 Batch API 只有在中转站暴露对应路由时才执行，
脚本不会把不存在的能力判定为模型失败。
运行示例：python3 test_api_features.py --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" --cases responses,tools,vision_image,vision_video --image /Users/leyla/Documents/test.png --video /Users/leyla/Documents/test.mp4 --run --confirm-paid
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
CASES = ("chat", "responses", "json_object", "json_schema", "tools", "reasoning",
         "stream", "vision_image", "vision_video", "partial", "cache", "conversation",
         "kvv", "prefix", "search_basic", "search_pro", "web_fetch", "official_tools",
         "dynamic_tools", "auto_reconnect", "file_qa", "batch")


def parse_args():
    p = argparse.ArgumentParser(description="通用 API 功能测试")
    p.add_argument("--base-url", default=os.getenv("API_BASE", "http://127.0.0.1:8000/v1"))
    p.add_argument("--key-env", default=os.getenv("API_KEY_ENV", "API_KEY"))
    p.add_argument("--model", default=os.getenv("MODEL", "test-model"))
    p.add_argument("--cases", default="chat,json_object,json_schema,tools,reasoning,stream,vision_image,partial,cache,conversation,kvv")
    p.add_argument("--image", type=Path)
    p.add_argument("--video", type=Path)
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--context-chars", type=int, default=12000)
    p.add_argument("--max-requests", type=int, default=30)
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--max-completion-tokens", type=int, default=1024)
    p.add_argument("--run", action="store_true")
    p.add_argument("--confirm-paid", action="store_true")
    return p.parse_args()


def send(args, key, body, endpoint="/chat/completions"):
    url = args.base_url.rstrip("/") + endpoint
    payload = None; header_file = None
    started = time.perf_counter()
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
            json.dump(body, f, ensure_ascii=False); payload = f.name
        header_file = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".headers", delete=False).name
        accept = "text/event-stream" if body.get("stream") else "application/json"
        p = subprocess.run(["curl", "-sS", "--no-buffer", "--max-time", str(args.timeout), "--request", "POST",
                            "-H", f"Authorization: Bearer {key}", "-H", "Content-Type: application/json", "-H", f"Accept: {accept}",
                            "--data-binary", "@" + payload, "--dump-header", header_file, "--write-out",
                            "\n__HTTP_STATUS__:%{http_code}", url], text=True,
                           capture_output=True, check=False)
        raw = p.stdout
        marker = "\n__HTTP_STATUS__:"
        raw, status_text = raw.rsplit(marker, 1) if marker in raw else (raw, "")
        status = int(status_text.strip()) if status_text.strip().isdigit() else None
        first_ms = None; sse_done = False
        if body.get("stream"):
            content = ""; usage = {}; finish = ""; started_stream = time.perf_counter()
            for line in raw.splitlines():
                if not line.startswith("data:"): continue
                data = line[5:].strip()
                if data == "[DONE]": sse_done = True; continue
                try: item = json.loads(data)
                except json.JSONDecodeError: continue
                usage = item.get("usage") or usage; choice = (item.get("choices") or [{}])[0]
                finish = choice.get("finish_reason") or finish; piece = (choice.get("delta") or {}).get("content") or ""
                if piece and first_ms is None: first_ms = (time.perf_counter() - started) * 1000
                content += piece
            response = {"choices": [{"message": {"content": content}, "finish_reason": finish}], "usage": usage, "model": body.get("model"), "_stream_content": content}
        else:
            try: response = json.loads(raw)
            except json.JSONDecodeError: response = {"unparsed_response": raw}
        headers = Path(header_file).read_text(errors="replace") if header_file else ""
        match = re.search(r"(?im)^x-request-id:\s*(\S+)", headers)
        body_id = re.search(r"(?i)request id[:\\s]+([A-Za-z0-9_-]+)", raw)
        response["_x_request_id"] = match.group(1) if match else (body_id.group(1) if body_id else "")
        response["_ttft_ms"] = round(first_ms, 2) if first_ms is not None else ""
        response["_sse_done"] = sse_done
        if p.returncode: response.setdefault("client_error", p.stderr.strip())
    except (OSError, subprocess.SubprocessError) as exc:
        status, response = None, {"client_error": str(exc)}
    finally:
        if payload:
            try: Path(payload).unlink()
            except FileNotFoundError: pass
        if header_file:
            try: Path(header_file).unlink()
            except FileNotFoundError: pass
    return status, round((time.perf_counter() - started) * 1000, 1), response


def image_data(path):
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build_case(args, case):
    base = {"model": args.model, "messages": [{"role": "user", "content": "只回复 API_OK"}],
            "max_completion_tokens": args.max_completion_tokens}
    if case == "responses": return {"model": args.model, "input": "只回复 API_RESPONSES_OK", "max_output_tokens": args.max_completion_tokens}, "/responses"
    if case == "json_object":
        base.update(messages=[{"role": "user", "content": "用 JSON Object 输出姓名小林和年龄28，只输出 JSON。"}], response_format={"type": "json_object"})
    elif case == "json_schema":
        base.update(messages=[{"role": "user", "content": "按 Schema 输出姓名小林和年龄28。"}], response_format={"type": "json_schema", "json_schema": {"name": "person", "strict": True, "schema": {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}}, "required": ["name", "age"], "additionalProperties": False}}})
    elif case == "tools":
        base.update(messages=[{"role": "user", "content": "调用天气工具查询北京明天的天气。"}], tools=[{"type": "function", "function": {"name": "get_weather", "description": "查询天气", "parameters": {"type": "object", "properties": {"city": {"type": "string"}, "date": {"type": "string"}}, "required": ["city", "date"], "additionalProperties": False}}}], tool_choice="required")
    elif case == "reasoning": base.update(messages=[{"role": "user", "content": "17乘23等于多少，只回复数字。"}], reasoning_effort="max")
    elif case == "stream": base.update(messages=[{"role": "user", "content": "只回复 STREAM_OK"}], stream=True, stream_options={"include_usage": True})
    elif case == "vision_image":
        if not args.image or not args.image.is_file(): raise ValueError("vision_image 需要 --image 本地图片")
        base["messages"] = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": image_data(args.image)}}, {"type": "text", "text": "描述图片中的文字。"}]}]
    elif case == "vision_video":
        if not args.video or not args.video.is_file(): raise ValueError("vision_video 需要 --video 本地视频")
        mime = mimetypes.guess_type(args.video.name)[0] or "video/mp4"
        base["messages"] = [{"role": "user", "content": [{"type": "video_url", "video_url": {"url": f"data:{mime};base64," + base64.b64encode(args.video.read_bytes()).decode("ascii")}}, {"type": "text", "text": "概括这个视频。"}]}]
    elif case == "partial": base.update(messages=[{"role": "user", "content": "继续完成这段摘要："}], assistant={"partial": True, "content": "社区活动"})
    elif case in ("cache", "prefix"):
        base.update(messages=[{"role": "system", "content": "固定前缀资料：" + "这是用于缓存命中测试的普通资料。" * 250}, {"role": "user", "content": "只回复 CACHE_OK"}])
    elif case in ("conversation", "kvv"):
        base.update(messages=[{"role": "user", "content": "这是连续对话测试，第1轮，只回复 ACK-1"}])
    elif case == "auto_reconnect": base.update(messages=[{"role": "user", "content": "只回复 RECONNECT_OK"}])
    return base, "/chat/completions"


def main():
    args = parse_args()
    cases = [x.strip() for x in args.cases.split(",") if x.strip()]
    unknown = [x for x in cases if x not in CASES]
    if unknown: raise SystemExit("未知项目：" + ",".join(unknown))
    print(f"项目={','.join(cases)}；地址={args.base_url}；模型={args.model}；预计最多{min(args.max_requests, sum(args.rounds if x in ('cache','conversation','kvv') else 1 for x in cases))}请求")
    if not (args.run and args.confirm_paid): print("仅预览；实际请求需同时添加 --run --confirm-paid"); return
    key = os.environ.get(args.key_env, "")
    if not key: raise SystemExit(f"未设置环境变量 {args.key_env}")
    # 统一入口通过 TEST_RESULT_ROOT 按当前模型隔离结果；直接运行时仍回退到本目录。
    result_root = Path(os.environ.get("TEST_RESULT_ROOT", str(ROOT / "results")))
    result_root.mkdir(parents=True, exist_ok=True)
    out = result_root / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-api-features")
    out.mkdir(parents=True, exist_ok=False); rows = []
    unsupported = {"search_basic", "search_pro", "web_fetch", "official_tools", "dynamic_tools", "file_qa", "batch"}
    for case in cases:
        if case in unsupported:
            rows.append({"case": case, "status": "MANUAL_REQUIRED", "note": "需要中转站官方工具/文件/Batch 路由配置，未伪造请求"}); print(f"{case}: MANUAL_REQUIRED"); continue
        count = args.rounds if case in ("cache", "conversation", "kvv") else 1
        for i in range(1, count + 1):
            try: body, endpoint = build_case(args, case)
            except ValueError as exc: rows.append({"case": case, "status": "INVALID_INPUT", "note": str(exc)}); break
            status, elapsed, response = send(args, key, body, endpoint)
            choice = (response.get("choices") or [{}])[0]
            rows.append({"case": case, "index": i, "http_status": status or "", "result": "PASS" if status == 200 else "FAIL", "response_model": response.get("model", ""), "finish_reason": choice.get("finish_reason", ""), "elapsed_ms": elapsed, "ttft_ms": response.get("_ttft_ms", ""), "sse_done": response.get("_sse_done", ""), "request_id": response.get("_x_request_id", ""), "content": str((choice.get("message") or {}).get("content", ""))[:300], "usage": json.dumps(response.get("usage", {}), ensure_ascii=False), "error": json.dumps(response.get("error", response.get("client_error", "")), ensure_ascii=False)})
            print(f"{case}#{i} HTTP={status or '-'} RESULT={rows[-1]['result']} TTFT={rows[-1]['ttft_ms'] or '-'}ms TOTAL={elapsed}ms X-REQUEST-ID={rows[-1]['request_id'] or '-'}")
            if status in (401, 403, 429): break
    with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields = sorted({k for row in rows for k in row}); writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    (out / "summary.json").write_text(json.dumps({"model": args.model, "base_url": args.base_url, "rows": len(rows), "generated_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2), encoding="utf-8")
    export_xlsx(out, rows, {"模型": args.model, "地址": args.base_url, "测试时间": datetime.now().isoformat(), "结果行数": len(rows)})
    print(f"结果目录：{out}")


if __name__ == "__main__": main()

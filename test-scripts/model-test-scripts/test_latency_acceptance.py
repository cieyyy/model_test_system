#!/usr/bin/env python3
"""多档输入/输出的严格延迟验收。

统计每档 HTTP、finish_reason、TTFT P50/P90/P95/P99 和 TPOT；TPOT 为空时表示
服务端或客户端没有提供可用的流式时间数据。运行：python3 test_latency_acceptance.py --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" --mode latency --requests 10 --concurrency 5 --run --confirm-paid
"""

import argparse
import concurrent.futures
import csv
import json
import os
import statistics
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    rank = (len(values) - 1) * p / 100
    lo, hi = int(rank), min(int(rank) + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (rank - lo), 2)


def parse_args():
    p = argparse.ArgumentParser(description="严格验收测试（默认不发请求）")
    p.add_argument("--base-url", default=os.getenv("API_BASE", "http://127.0.0.1:8000/v1"))
    p.add_argument("--key-env", default=os.getenv("API_KEY_ENV", "API_KEY"))
    p.add_argument("--model", default=os.getenv("MODEL", "test-model"))
    p.add_argument("--mode", choices=("latency", "throughput", "tools"), default="latency")
    p.add_argument("--input-tokens", default="1000,2000,4000")
    p.add_argument("--output-tokens", default="200,500,800")
    p.add_argument("--requests", type=int, default=3, help="每个输入/输出档位的请求数")
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--duration", type=int, default=10, help="throughput 模式持续秒数")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--reasoning-effort", choices=("low", "high", "max", "none"), default="low")
    p.add_argument("--output-dir", type=Path)
    p.add_argument("--run", action="store_true")
    p.add_argument("--confirm-paid", action="store_true")
    return p.parse_args()


def ints(value, name, low, high):
    try:
        out = [int(x.strip()) for x in value.split(",") if x.strip()]
    except ValueError as exc:
        raise SystemExit(f"{name} 必须是逗号分隔的整数") from exc
    if not out or any(x < low or x > high for x in out):
        raise SystemExit(f"{name} 范围为 {low}-{high}")
    return out


def make_prompt(tokens):
    # 中文字符约等于 1-2 tokens；留出指令和回答空间，实际 token 以 usage 为准。
    filler = "社区阅读活动资料：报名、场地、志愿者、物料、宣传、预算和复盘。"
    return "请阅读资料并用清晰的分级条目总结重点，严格只输出总结正文。\n" + filler * max(1, tokens // 18)


def call(args, key, input_tokens, output_tokens, index):
    if args.mode == "tools":
        messages = [{"role": "user", "content": "调用天气工具查询北京明天的天气，然后只回复工具结果。"}]
        tools = [{"type": "function", "function": {"name": "get_weather", "description": "查询天气",
                 "parameters": {"type": "object", "properties": {"city": {"type": "string"}, "date": {"type": "string"}},
                 "required": ["city", "date"], "additionalProperties": False}}}]
    else:
        messages = [{"role": "user", "content": make_prompt(input_tokens)}]
        tools = None
    body = {"model": args.model, "messages": messages, "max_completion_tokens": output_tokens,
            "reasoning_effort": args.reasoning_effort, "stream": True,
            "stream_options": {"include_usage": True}}
    if tools:
        body["tools"], body["tool_choice"] = tools, "required"
    payload = None
    started = time.perf_counter()
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
            json.dump(body, f, ensure_ascii=False); payload = f.name
        proc = subprocess.run(["curl", "-sS", "--no-buffer", "--max-time", str(args.timeout),
                               "-H", f"Authorization: Bearer {key}", "-H", "Content-Type: application/json",
                               "-H", "Accept: text/event-stream", "--data-binary", "@" + payload,
                               args.base_url.rstrip("/") + "/chat/completions"],
                              text=True, capture_output=True, check=False)
        raw = proc.stdout
        status = 200 if proc.returncode == 0 and raw else None
        chunks, done, content, tool_calls, usage, model, finish = [], False, "", 0, {}, "", ""
        first = None
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]": done = True; continue
            try: item = json.loads(data)
            except json.JSONDecodeError: continue
            chunks.append(item); model = item.get("model") or model; usage = item.get("usage") or usage
            choice = (item.get("choices") or [{}])[0]
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta") or {}
            piece = delta.get("content") or delta.get("reasoning_content") or ""
            if (piece or delta.get("tool_calls")) and first is None: first = (time.perf_counter() - started) * 1000
            content += piece
            tool_calls += len(delta.get("tool_calls") or [])
        if proc.returncode:
            status = None
        elapsed = (time.perf_counter() - started) * 1000
        output = (usage.get("completion_tokens") or 0)
        ttft = round(first, 2) if first is not None else None
        tpot = round((elapsed - ttft) / max(output - 1, 1), 2) if ttft and output else None
        return {"index": index, "input_target": input_tokens, "output_target": output_tokens,
                "http_status": status or "", "finish_reason": finish, "model": model,
                "input_tokens": usage.get("prompt_tokens", ""), "output_tokens": output,
                "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", ""),
                "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", ""),
                "ttft_ms": ttft or "", "tpot_ms": tpot or "", "elapsed_ms": round(elapsed, 2),
                "content_chars": len(content), "tool_calls": tool_calls, "sse_done": done,
                "client_error": proc.stderr.strip() if proc.returncode else ""}
    finally:
        if payload:
            try: Path(payload).unlink()
            except FileNotFoundError: pass


def main():
    args = parse_args()
    if not args.base_url.startswith(("http://", "https://")) or args.concurrency < 1 or args.requests < 1:
        raise SystemExit("地址必须为 http(s)，requests/concurrency 必须大于 0")
    inputs = ints(args.input_tokens, "--input-tokens", 1000, 4000)
    outputs = ints(args.output_tokens, "--output-tokens", 200, 800)
    planned = 1 if args.mode == "tools" else len(inputs) * len(outputs) * args.requests
    print(f"项目=严格验收；模式={args.mode}；输入={inputs}；输出={outputs}；计划约 {planned} 请求；并发={args.concurrency}")
    if args.mode == "throughput": print(f"吞吐窗口={args.duration}秒；需自行设置高并发和请求量进行 RPM/TPM 验收")
    if not (args.run and args.confirm_paid):
        print("仅预览；实际请求需同时添加 --run --confirm-paid"); return
    key = os.environ.get(args.key_env, "")
    if not key: raise SystemExit(f"未设置环境变量 {args.key_env}")
    out = (args.output_dir.resolve() if args.output_dir else ROOT / "results" /
           (datetime.now().strftime("%Y%m%d-%H%M%S") + "-acceptance"))
    out.mkdir(parents=True, exist_ok=False)
    jobs = [(i, o, n) for i in inputs for o in outputs for n in range(1, args.requests + 1)]
    if args.mode == "tools": jobs = [(0, 800, 1)]
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(call, args, key, i, o, n) for i, o, n in jobs]
        for future in concurrent.futures.as_completed(futures):
            row = future.result(); rows.append(row)
            print(f"#{row['index']}->{row['output_target']} HTTP={row['http_status'] or '-'} "
                  f"TTFT={row['ttft_ms'] or '-'}ms TPOT={row['tpot_ms'] or '-'}ms "
                  f"TOTAL={row['elapsed_ms']}ms FINISH={row['finish_reason'] or '-'}")
    with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields = list(rows[0].keys()) if rows else []
        writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    successful = [r for r in rows if r["http_status"] == 200]
    summary = {"model": args.model, "base_url": args.base_url, "mode": args.mode,
               "requests": len(rows), "http_200": len(successful),
               "success_rate": len(successful) / len(rows) if rows else 0,
               "ttft_ms": {f"P{p}": percentile([r["ttft_ms"] for r in successful if r["ttft_ms"] != ""], p) for p in (50, 90, 95, 99)},
               "tpot_ms": {f"P{p}": percentile([r["tpot_ms"] for r in successful if r["tpot_ms"] != ""], p) for p in (50, 90, 95, 99)},
               "generated_at": datetime.now(timezone.utc).isoformat()}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TTFT：" + "；".join(f"P{p}={summary['ttft_ms'][f'P{p}']}ms" for p in (50, 90, 95, 99)))
    print("TPOT：" + "；".join(f"P{p}={summary['tpot_ms'][f'P{p}']}ms/token" for p in (50, 90, 95, 99)))
    print(f"结果目录：{out}")


if __name__ == "__main__": main()

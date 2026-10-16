#!/usr/bin/env python3
"""固定 RPM 性能压测。

默认只预览。真实压测需显式 --run --confirm-paid。该脚本用于测实际请求速率，
指标包括实际 RPM/TPM、TTFT/TPOT 分位数、HTTP 成功率和 finish_reason；不承诺达到服务端限流，请从低速率逐级增加。
运行：python3 test_fixed_rate_performance.py --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" --rate-rpm 10 --duration 60 --concurrency 5 --run --confirm-paid
"""

import argparse
import concurrent.futures
import csv
import json
import os
import re
import statistics
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sophnet_result_export import export_xlsx

ROOT = Path(__file__).resolve().parent


def percentile(values, p):
    values = sorted(v for v in values if v is not None and v != "")
    if not values:
        return None
    pos = (len(values) - 1) * p / 100
    lo, hi = int(pos), min(int(pos) + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (pos - lo), 2)


def parse_args():
    p = argparse.ArgumentParser(description="固定速率 TPM/RPM/延迟压测")
    p.add_argument("--base-url", default=os.getenv("API_BASE", "http://127.0.0.1:8000/v1"))
    p.add_argument("--key-env", default=os.getenv("API_KEY_ENV", "API_KEY"))
    p.add_argument("--model", default=os.getenv("MODEL", "test-model"))
    p.add_argument("--input-tokens", type=int, default=1000)
    p.add_argument("--max-output-tokens", type=int, default=200)
    p.add_argument("--rate-rpm", type=float, default=10, help="目标发起速率，每分钟请求数")
    p.add_argument("--duration", type=int, default=10, help="持续时间，秒")
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--reasoning-effort", choices=("low", "high", "max"), default="low")
    p.add_argument("--output-dir", type=Path)
    p.add_argument("--run", action="store_true")
    p.add_argument("--confirm-paid", action="store_true")
    return p.parse_args()


def make_prompt(target_tokens):
    filler = "社区阅读活动涉及报名、场地、志愿者、物料、宣传、预算与复盘。"
    return "请阅读资料并用普通中文概括重点，不要讨论模型或接口。\n" + filler * max(1, target_tokens // 18)


def parse_sse(raw, started):
    content = ""
    usage = {}
    finish = ""
    model = ""
    done = False
    first_ms = None
    chunk_times = []
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            continue
        try:
            item = json.loads(data)
        except json.JSONDecodeError:
            continue
        now_ms = (time.perf_counter() - started) * 1000
        model = item.get("model") or model
        usage = item.get("usage") or usage
        choice = (item.get("choices") or [{}])[0]
        finish = choice.get("finish_reason") or finish
        delta = choice.get("delta") or {}
        piece = delta.get("content") or ""
        if piece:
            if first_ms is None:
                first_ms = now_ms
            chunk_times.append(now_ms)
            content += piece
    intervals = [b - a for a, b in zip(chunk_times, chunk_times[1:])]
    return {"model": model, "usage": usage, "finish_reason": finish,
            "sse_done": done, "content_chars": len(content),
            "ttft_ms": round(first_ms, 2) if first_ms is not None else None,
            "chunk_tpot_ms": round(statistics.mean(intervals), 2) if intervals else None}


def one_request(args, key, number):
    body = {"model": args.model, "messages": [{"role": "user", "content": make_prompt(args.input_tokens)}],
            "reasoning_effort": args.reasoning_effort, "max_completion_tokens": args.max_output_tokens,
            "stream": True, "stream_options": {"include_usage": True}}
    payload = None
    header_file = None
    started = time.perf_counter()
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
            json.dump(body, f, ensure_ascii=False); payload = f.name
        header_file = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".headers", delete=False).name
        command = ["curl", "-sS", "--no-buffer", "--max-time", str(args.timeout),
                   "-H", f"Authorization: Bearer {key}", "-H", "Content-Type: application/json",
                   "-H", "Accept: text/event-stream", "--data-binary", "@" + payload,
                   "--dump-header", header_file,
                   "--write-out", "\n__HTTP_STATUS__:%{http_code}",
                   args.base_url.rstrip("/") + "/chat/completions"]
        proc = subprocess.run(command, text=True, capture_output=True, check=False)
        raw = proc.stdout
        status = None
        marker = "\n__HTTP_STATUS__:"
        if marker in raw:
            raw, status_text = raw.rsplit(marker, 1)
            status = int(status_text.strip()) if status_text.strip().isdigit() else None
        response = parse_sse(raw, started)
        headers = Path(header_file).read_text(errors="replace") if header_file else ""
        match = re.search(r"(?im)^x-request-id:\s*(\S+)", headers)
        response["request_id"] = match.group(1) if match else ""
        if proc.returncode:
            response["error"] = proc.stderr.strip() or f"curl exit {proc.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        status, response = None, {"error": str(exc)}
    finally:
        if payload:
            try: Path(payload).unlink()
            except FileNotFoundError: pass
        if header_file:
            try: Path(header_file).unlink()
            except FileNotFoundError: pass
    elapsed = round((time.perf_counter() - started) * 1000, 2)
    usage = response.get("usage") or {}
    return {"request_no": number, "http_status": status or "", "elapsed_ms": elapsed,
            "input_tokens": usage.get("prompt_tokens", ""),
            "output_tokens": usage.get("completion_tokens", ""),
            "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", ""),
            "finish_reason": response.get("finish_reason", ""), "ttft_ms": response.get("ttft_ms", ""),
            "chunk_tpot_ms": response.get("chunk_tpot_ms", ""), "sse_done": response.get("sse_done", False),
            "model": response.get("model", ""), "request_id": response.get("request_id", ""), "content_chars": response.get("content_chars", 0),
            "error": response.get("error", "")}


def main():
    args = parse_args()
    if not args.base_url.startswith(("http://", "https://")) or args.rate_rpm <= 0 or args.duration <= 0 or args.concurrency <= 0:
        raise SystemExit("base-url 必须为 http(s) 地址，rate-rpm/duration/concurrency 必须大于 0")
    planned = max(1, int(args.rate_rpm * args.duration / 60 + 0.999))
    print(f"项目=固定速率压测；模型={args.model}；目标速率={args.rate_rpm} RPM；持续={args.duration}秒；计划约={planned}请求；并发上限={args.concurrency}")
    if not (args.run and args.confirm_paid):
        print("仅预览；真实压测需同时添加 --run --confirm-paid")
        return
    key = os.environ.get(args.key_env, "")
    if not key: raise SystemExit(f"未设置环境变量 {args.key_env}")
    out = (args.output_dir.resolve() if args.output_dir else ROOT / "results" /
           (datetime.now().strftime("%Y%m%d-%H%M%S") + "-load-test"))
    out.mkdir(parents=True, exist_ok=False)
    rows = []
    started = time.perf_counter()
    next_submit = started
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = []
        for n in range(1, planned + 1):
            wait = next_submit - time.perf_counter()
            if wait > 0: time.sleep(wait)
            futures.append(pool.submit(one_request, args, key, n))
            next_submit += 60 / args.rate_rpm
        for future in concurrent.futures.as_completed(futures):
            row = future.result(); rows.append(row)
            print(f"#{row['request_no']} HTTP={row['http_status'] or '-'} TTFT={row['ttft_ms'] or '-'}ms "
                  f"TOTAL={row['elapsed_ms']}ms FINISH={row['finish_reason'] or '-'}")
    wall_s = max((time.perf_counter() - started), 0.001)
    ok = [r for r in rows if r["http_status"] == 200]
    total_tokens = sum((int(r["input_tokens"] or 0) + int(r["output_tokens"] or 0)) for r in ok)
    summary = {"model": args.model, "base_url": args.base_url, "target_rpm": args.rate_rpm,
               "duration_s": round(wall_s, 2), "attempted": len(rows), "http_200": len(ok),
               "actual_rpm": round(len(ok) / wall_s * 60, 2), "actual_tpm": round(total_tokens / wall_s * 60, 2),
               "ttft_ms": {f"P{p}": percentile([r["ttft_ms"] for r in ok if r["ttft_ms"] != ""], p) for p in (50, 90, 95, 99)},
               "chunk_tpot_ms": {f"P{p}": percentile([r["chunk_tpot_ms"] for r in ok if r["chunk_tpot_ms"] != ""], p) for p in (50, 90, 95, 99)},
               "finish_counts": {x: sum(r["finish_reason"] == x for r in rows) for x in ("stop", "length")},
               "generated_at": datetime.now(timezone.utc).isoformat()}
    with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys())); writer.writeheader(); writer.writerows(rows)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    export_xlsx(out, rows, {"模型": args.model, "地址": args.base_url, "目标RPM": args.rate_rpm, "持续秒数": args.duration, "实际RPM": summary["actual_rpm"], "实际TPM": summary["actual_tpm"]})
    print(json.dumps(summary, ensure_ascii=False, indent=2)); print(f"结果目录：{out}")


if __name__ == "__main__": main()

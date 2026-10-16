#!/usr/bin/env python3
"""高吞吐/缓存命中率基准压测。

这是独立的高负载基准脚本，对齐压测面板中的指标：有效成功率、RPM、
Input/Output/Total TPM、KV Cache 命中率、TTFT 和 TPOT 分位数，并记录每个请求的 x-request-id。
默认仅预览；真实请求必须显式添加 --run --confirm-paid。
运行：python3 test_tpm_throughput_benchmark.py --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" --requests 292 --duration 96 --concurrency 80 --input-tokens 35000 --target-tpm 10000000 --run --confirm-paid
"""

import argparse
import concurrent.futures
import csv
import json
import os
import re
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def percentile(values, p):
    values = sorted(float(v) for v in values if v not in (None, ""))
    if not values:
        return None
    pos = (len(values) - 1) * p / 100
    lo, hi = int(pos), min(int(pos) + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (pos - lo), 2)


def parse_args():
    p = argparse.ArgumentParser(description="1000万 TPM 高吞吐基准压测")
    p.add_argument("--base-url", default=os.getenv("API_BASE", "http://127.0.0.1:8000/v1"))
    p.add_argument("--key-env", default=os.getenv("API_KEY_ENV", "API_KEY"))
    p.add_argument("--model", default=os.getenv("MODEL", "test-model"))
    p.add_argument("--requests", type=int, default=292, help="计划请求数，图片场景为292")
    p.add_argument("--duration", type=float, default=96, help="发起请求的目标窗口，秒")
    p.add_argument("--concurrency", type=int, default=80)
    p.add_argument("--input-tokens", type=int, default=35000)
    p.add_argument("--max-output-tokens", type=int, default=256)
    p.add_argument("--target-tpm", type=float, default=10_000_000)
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--output-dir", type=Path)
    p.add_argument("--run", action="store_true")
    p.add_argument("--confirm-paid", action="store_true")
    return p.parse_args()


def make_prefix(target_tokens):
    sentence = "这是用于高吞吐缓存压测的固定前缀，内容保持不变以观察 KV Cache 命中情况。"
    return sentence * max(1, target_tokens // 18)


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
        piece = (choice.get("delta") or {}).get("content") or ""
        if piece:
            if first_ms is None:
                first_ms = now_ms
            chunk_times.append(now_ms)
            content += piece
    intervals = [b - a for a, b in zip(chunk_times, chunk_times[1:])]
    return {
        "model": model,
        "usage": usage,
        "finish_reason": finish,
        "sse_done": done,
        "content_chars": len(content),
        "ttft_ms": round(first_ms, 2) if first_ms is not None else "",
        "tpot_ms": round(statistics.mean(intervals), 2) if intervals else "",
    }


def one_request(args, key, number, prefix):
    body = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": prefix},
            {"role": "user", "content": f"第 {number} 个请求只回复 BENCHMARK-{number}。"},
        ],
        "max_completion_tokens": args.max_output_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    payload = header_file = None
    started = time.perf_counter()
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
            json.dump(body, f, ensure_ascii=False)
            payload = f.name
        header_file = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".headers", delete=False).name
        command = [
            "curl", "-sS", "--no-buffer", "--max-time", str(args.timeout),
            "-H", f"Authorization: Bearer {key}", "-H", "Content-Type: application/json",
            "-H", "Accept: text/event-stream", "--data-binary", "@" + payload,
            "--dump-header", header_file, "--write-out", "\n__HTTP_STATUS__:%{http_code}",
            args.base_url.rstrip("/") + "/chat/completions",
        ]
        import subprocess
        proc = subprocess.run(command, text=True, capture_output=True, check=False)
        raw = proc.stdout
        marker = "\n__HTTP_STATUS__:"
        status = None
        if marker in raw:
            raw, status_text = raw.rsplit(marker, 1)
            status = int(status_text.strip()) if status_text.strip().isdigit() else None
        parsed = parse_sse(raw, started)
        headers = Path(header_file).read_text(errors="replace")
        match = re.search(r"(?im)^x-request-id:\s*(\S+)", headers)
        parsed["request_id"] = match.group(1) if match else ""
        parsed["error"] = proc.stderr.strip() if proc.returncode else ""
    except (OSError, subprocess.SubprocessError) as exc:
        status, parsed = None, {"error": str(exc), "request_id": "", "ttft_ms": "", "tpot_ms": ""}
    finally:
        for path in (payload, header_file):
            if path:
                try:
                    Path(path).unlink()
                except FileNotFoundError:
                    pass
    usage = parsed.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return {
        "request_no": number,
        "http_status": status or "",
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "input_tokens": usage.get("prompt_tokens", ""),
        "output_tokens": usage.get("completion_tokens", ""),
        "cached_tokens": details.get("cached_tokens", usage.get("cached_tokens", "")),
        "ttft_ms": parsed.get("ttft_ms", ""),
        "tpot_ms": parsed.get("tpot_ms", ""),
        "finish_reason": parsed.get("finish_reason", ""),
        "sse_done": parsed.get("sse_done", False),
        "request_id": parsed.get("request_id", ""),
        "error": parsed.get("error", ""),
    }


def main():
    args = parse_args()
    if not args.base_url.startswith(("http://", "https://")):
        raise SystemExit("base-url 必须使用 http:// 或 https://")
    if min(args.requests, args.duration, args.concurrency, args.input_tokens) <= 0:
        raise SystemExit("requests/duration/concurrency/input-tokens 必须大于 0")
    planned_rpm = args.requests / args.duration * 60
    print(f"项目=高吞吐 TPM/KV Cache 基准压测；模型={args.model}；计划={args.requests}请求；目标窗口={args.duration:g}秒；并发={args.concurrency}")
    print(f"目标 TPM={args.target_tpm:g}；预估 RPM={planned_rpm:.1f}；固定前缀约={args.input_tokens} tokens")
    if not (args.run and args.confirm_paid):
        print("仅预览；真实压测需同时添加 --run --confirm-paid")
        return
    key = os.environ.get(args.key_env, "")
    if not key:
        raise SystemExit(f"未设置环境变量 {args.key_env}")
    out = args.output_dir.resolve() if args.output_dir else ROOT / "results" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-tpm-benchmark")
    out.mkdir(parents=True, exist_ok=False)
    prefix = make_prefix(args.input_tokens)
    rows = []
    started = time.perf_counter()
    interval = args.duration / args.requests
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = []
        next_submit = started
        for number in range(1, args.requests + 1):
            wait = next_submit - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            futures.append(pool.submit(one_request, args, key, number, prefix))
            next_submit += interval
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            rows.append(row)
            print(f"#{row['request_no']} HTTP={row['http_status'] or '-'} TTFT={row['ttft_ms'] or '-'}ms TOTAL={row['elapsed_ms']}ms CACHE={row['cached_tokens'] or 0} X-REQUEST-ID={row['request_id'] or '-'}")
    wall_s = max(time.perf_counter() - started, 0.001)
    ok = [r for r in rows if r["http_status"] == 200]
    attempted = len(rows)
    input_tokens = sum(int(r["input_tokens"] or 0) for r in ok)
    output_tokens = sum(int(r["output_tokens"] or 0) for r in ok)
    cached_tokens = sum(int(r["cached_tokens"] or 0) for r in ok)
    tpm = lambda n: round(n / wall_s * 60, 2)
    summary = {
        "model": args.model, "base_url": args.base_url, "attempted": attempted,
        "http_200": len(ok), "effective_success_rate": round(len(ok) / attempted * 100, 2),
        "duration_s": round(wall_s, 2), "rpm": round(attempted / wall_s * 60, 2),
        "input_tpm": tpm(input_tokens), "output_tpm": tpm(output_tokens), "total_tpm": tpm(input_tokens + output_tokens),
        "cached_tpm": tpm(cached_tokens), "cache_hit_rate": round(cached_tokens / input_tokens * 100, 2) if input_tokens else None,
        "ttft_ms": {f"P{p}": percentile([r["ttft_ms"] for r in ok], p) for p in (50, 90, 95, 99)},
        "tpot_ms": {f"P{p}": percentile([r["tpot_ms"] for r in ok], p) for p in (50, 90, 95, 99)},
        "finish_counts": {x: sum(r["finish_reason"] == x for r in rows) for x in ("stop", "length")},
        "target_tpm": args.target_tpm, "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader(); writer.writerows(rows)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"结果目录：{out}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长上下文 TTFT 10 并发专项测试。

默认只预览，不发送真实请求。实际执行示例：

python3 test_ttft_long_context_concurrency.py \
  --base-url "$API_BASE" \
  --key-env "$API_KEY_ENV" \
  --model "$MODEL" \
  --run --confirm-paid

默认测试档位：16k、64k、100k 输入 token 目标；每档 10 请求、10 并发。
脚本通过 curl 逐行读取 SSE，在首个有效内容事件到达时记录 TTFT。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import re
import statistics
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_CASE_TOKENS = (16_000, 64_000, 100_000)


def percentile(values: list[Any], p: int) -> float | None:
    nums = sorted(float(v) for v in values if v not in ("", None))
    if not nums:
        return None
    pos = (len(nums) - 1) * p / 100
    lo = int(pos)
    hi = min(lo + 1, len(nums) - 1)
    return round(nums[lo] + (nums[hi] - nums[lo]) * (pos - lo), 2)


def mean_value(values: list[Any]) -> float | None:
    nums = [float(v) for v in values if v not in ("", None)]
    return round(statistics.mean(nums), 2) if nums else None


def parse_int_csv(text: str) -> list[int]:
    values: list[int] = []
    for part in text.split(","):
        part = part.strip().lower().replace("k", "000")
        if not part:
            continue
        values.append(int(part))
    if not values or any(v <= 0 for v in values):
        raise argparse.ArgumentTypeError("case-tokens 必须是正整数列表，例如 16000,64000,100000")
    return values


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TTFT 16k/64k/100k 输入、10 并发专项测试")
    p.add_argument("--base-url", default=os.getenv("API_BASE", "http://127.0.0.1:8000/v1"))
    p.add_argument("--key-env", default=os.getenv("API_KEY_ENV", "API_KEY"))
    p.add_argument("--model", default=os.getenv("MODEL", "test-model"))
    p.add_argument("--case-tokens", type=parse_int_csv, default=list(DEFAULT_CASE_TOKENS),
                   help="输入 token 目标档位，例如 16000,64000,100000 或 16k,64k,100k")
    p.add_argument("--requests-per-case", type=int, default=10, help="每个档位请求数，默认 10")
    p.add_argument("--concurrency", type=int, default=10, help="每个档位并发数，默认 10")
    p.add_argument("--max-output-tokens", type=int, default=32)
    p.add_argument("--temperature", type=float, default=1.0,
                   help="请求温度，默认 1.0")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--chars-per-token", type=float, default=1.0,
                   help="构造中文长文本时使用的近似字符/token 比例；实际 input_tokens 以 usage 为准")
    p.add_argument("--max-requests", type=int, default=30,
                   help="本次运行请求数上限，默认 3 档 x 10 请求 = 30")
    p.add_argument("--output-dir", type=Path)
    p.add_argument("--max-token-field", choices=("max_completion_tokens", "max_tokens"),
                   default="max_completion_tokens")
    p.add_argument("--shared-prefix", action="store_true",
                   help="使用相同长文本前缀，便于观察缓存影响；默认每个请求使用不同长文本以减少缓存干扰")
    p.add_argument("--run", action="store_true", help="允许发送真实请求")
    p.add_argument("--execute", action="store_true", help="同 --run")
    p.add_argument("--confirm-paid", action="store_true", help="确认可能产生费用")
    return p.parse_args()


def build_prompt(target_tokens: int, request_no: int, shared_prefix: bool, chars_per_token: float) -> str:
    target_chars = max(1, int(target_tokens * chars_per_token))
    variant = "shared" if shared_prefix else f"case-{target_tokens}-request-{request_no}"
    chunk = (
        f"长上下文 TTFT 测试资料片段 {variant}。"
        "本段用于填充上下文，包含项目背景、会议纪要、风险记录、排期、验收标准和复盘要点。"
        "请只把它当作资料，不需要逐条复述。"
    )
    repeats = max(1, target_chars // len(chunk) + 1)
    long_text = (chunk * repeats)[:target_chars]
    return (
        "你正在参与接口首字延迟测试。请阅读下面资料，最后只输出一行：TTFT-OK。\n\n"
        f"资料开始：\n{long_text}\n资料结束。\n\n"
        "只输出 TTFT-OK，不要解释。"
    )


def parse_stream_line(line: str, started: float, metrics: dict[str, Any]) -> None:
    if not line.startswith("data:"):
        return
    data = line[5:].strip()
    now_ms = (time.perf_counter() - started) * 1000
    if data == "[DONE]":
        metrics["sse_done"] = True
        return
    try:
        item = json.loads(data)
    except json.JSONDecodeError:
        metrics["parse_errors"] += 1
        return
    metrics["sse_event_count"] += 1
    metrics["first_event_ms"] = metrics["first_event_ms"] or round(now_ms, 2)
    metrics["response_model"] = item.get("model") or metrics["response_model"]
    metrics["usage"] = item.get("usage") or metrics["usage"]
    choice = (item.get("choices") or [{}])[0]
    metrics["finish_reason"] = choice.get("finish_reason") or metrics["finish_reason"]
    delta = choice.get("delta") or {}
    piece = delta.get("content") or delta.get("reasoning_content") or ""
    has_content = bool(piece or delta.get("tool_calls"))
    if has_content:
        metrics["first_content_ms"] = metrics["first_content_ms"] or round(now_ms, 2)
        metrics["chunk_times"].append(now_ms)
        metrics["content_chars"] += len(piece)


def run_one(args: argparse.Namespace, key: str, target_tokens: int, request_no: int) -> dict[str, Any]:
    prompt = build_prompt(target_tokens, request_no, args.shared_prefix, args.chars_per_token)
    body = {
        "model": args.model,
        "messages": [{"role": "user", "content": prompt}],
        args.max_token_field: args.max_output_tokens,
        "temperature": args.temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    payload_path = None
    header_path = None
    started = time.perf_counter()
    metrics: dict[str, Any] = {
        "first_event_ms": None,
        "first_content_ms": None,
        "chunk_times": [],
        "usage": {},
        "finish_reason": "",
        "response_model": "",
        "sse_done": False,
        "sse_event_count": 0,
        "content_chars": 0,
        "parse_errors": 0,
    }
    status = None
    stderr = ""
    raw_sample: list[str] = []
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
            json.dump(body, f, ensure_ascii=False)
            payload_path = f.name
        header_path = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".headers", delete=False).name
        command = [
            "curl", "-sS", "--no-buffer", "--max-time", str(args.timeout),
            "-H", f"Authorization: Bearer {key}",
            "-H", "Content-Type: application/json",
            "-H", "Accept: text/event-stream",
            "--data-binary", "@" + payload_path,
            "--dump-header", header_path,
            "--write-out", "\n__HTTP_STATUS__:%{http_code}\n",
            args.base_url.rstrip("/") + "/chat/completions",
        ]
        proc = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            clean = line.rstrip("\n")
            if clean.startswith("__HTTP_STATUS__:"):
                status_text = clean.split(":", 1)[1].strip()
                status = int(status_text) if status_text.isdigit() else None
                continue
            if len(raw_sample) < 20 and clean:
                raw_sample.append(clean[:500])
            parse_stream_line(clean, started, metrics)
        proc.wait()
        if proc.stderr is not None:
            stderr = proc.stderr.read().strip()
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = str(exc)
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        headers = ""
        if header_path:
            try:
                headers = Path(header_path).read_text(errors="replace")
            except OSError:
                headers = ""
        for path in (payload_path, header_path):
            if path:
                try:
                    Path(path).unlink()
                except FileNotFoundError:
                    pass

    usage = metrics["usage"] or {}
    details = usage.get("prompt_tokens_details") or {}
    intervals = [b - a for a, b in zip(metrics["chunk_times"], metrics["chunk_times"][1:])]
    request_id_match = re.search(r"(?im)^x-request-id:\s*(\S+)", headers)
    error_text = stderr
    if status != 200 and raw_sample:
        error_text = (error_text + " " + " ".join(raw_sample)).strip()
    return {
        "case": f"ttft_{target_tokens // 1000}k",
        "target_input_tokens": target_tokens,
        "request_no": request_no,
        "planned_concurrency": args.concurrency,
        "http_status": status or "",
        "request_model": args.model,
        "response_model": metrics["response_model"],
        "finish_reason": metrics["finish_reason"],
        "input_tokens": usage.get("prompt_tokens", ""),
        "output_tokens": usage.get("completion_tokens", ""),
        "cached_tokens": details.get("cached_tokens", usage.get("cached_tokens", "")),
        "first_event_ms": metrics["first_event_ms"] or "",
        "ttft_ms": metrics["first_content_ms"] or "",
        "tpot_ms": round(statistics.mean(intervals), 2) if intervals else "",
        "elapsed_ms": elapsed_ms,
        "sse_event_count": metrics["sse_event_count"],
        "sse_done": metrics["sse_done"],
        "content_chars": metrics["content_chars"],
        "request_id": request_id_match.group(1) if request_id_match else "",
        "parse_errors": metrics["parse_errors"],
        "error": error_text[:2000],
    }


def summarize_case(args: argparse.Namespace, rows: list[dict[str, Any]], target_tokens: int) -> dict[str, Any]:
    case_rows = [r for r in rows if int(r["target_input_tokens"]) == target_tokens]
    ok = [r for r in case_rows if r["http_status"] == 200]
    return {
        "case": f"ttft_{target_tokens // 1000}k",
        "target_input_tokens": target_tokens,
        "planned_concurrency": args.concurrency,
        "requests": len(case_rows),
        "http_200": len(ok),
        "success_rate": round(len(ok) / len(case_rows) * 100, 2) if case_rows else 0,
        "avg_input_tokens": mean_value([r["input_tokens"] for r in ok]),
        "avg_cached_tokens": mean_value([r["cached_tokens"] for r in ok]),
        "avg_ttft_ms": mean_value([r["ttft_ms"] for r in ok]),
        "p50_ttft_ms": percentile([r["ttft_ms"] for r in ok], 50),
        "p90_ttft_ms": percentile([r["ttft_ms"] for r in ok], 90),
        "p95_ttft_ms": percentile([r["ttft_ms"] for r in ok], 95),
        "p99_ttft_ms": percentile([r["ttft_ms"] for r in ok], 99),
        "avg_first_event_ms": mean_value([r["first_event_ms"] for r in ok]),
        "avg_elapsed_ms": mean_value([r["elapsed_ms"] for r in ok]),
        "p99_elapsed_ms": percentile([r["elapsed_ms"] for r in ok], 99),
        "finish_counts": {value: sum(r["finish_reason"] == value for r in case_rows)
                          for value in sorted({str(r["finish_reason"]) for r in case_rows})},
        "error_count": sum(bool(r["error"]) for r in case_rows),
    }


def main() -> None:
    args = parse_args()
    execute = args.run or args.execute
    planned_requests = len(args.case_tokens) * args.requests_per_case
    if not args.base_url.startswith(("http://", "https://")):
        raise SystemExit("base-url 必须使用 http:// 或 https://")
    if min(args.requests_per_case, args.concurrency, args.max_output_tokens, args.timeout) <= 0:
        raise SystemExit("requests-per-case/concurrency/max-output-tokens/timeout 必须大于 0")
    if args.chars_per_token <= 0:
        raise SystemExit("chars-per-token 必须大于 0")
    if planned_requests > args.max_requests:
        raise SystemExit(f"计划请求数 {planned_requests} 超过 max-requests={args.max_requests}")

    print(
        f"项目=长上下文 TTFT 专项；模型={args.model}；端点={args.base_url.rstrip('/')}/chat/completions"
    )
    print(
        f"档位={','.join(str(x) for x in args.case_tokens)}；每档请求={args.requests_per_case}；"
        f"并发={args.concurrency}；计划总请求={planned_requests}"
    )
    if not (execute and args.confirm_paid):
        print("仅预览；真实请求需添加 --run --confirm-paid")
        return

    key = os.environ.get(args.key_env, "")
    if not key:
        raise SystemExit(f"未设置环境变量 {args.key_env}")

    out = (
        args.output_dir.resolve()
        if args.output_dir
        else ROOT / "results" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-ttft-long-context")
    )
    out.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, Any]] = []
    started = time.perf_counter()

    for target_tokens in args.case_tokens:
        print(f"\n[ttft_{target_tokens // 1000}k] 开始：{args.requests_per_case} 请求，{args.concurrency} 并发")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [
                pool.submit(run_one, args, key, target_tokens, request_no)
                for request_no in range(1, args.requests_per_case + 1)
            ]
            for future in concurrent.futures.as_completed(futures):
                row = future.result()
                rows.append(row)
                print(
                    f"{row['case']}#{row['request_no']} HTTP={row['http_status'] or '-'} "
                    f"TTFT={row['ttft_ms'] or '-'}ms FIRST_EVENT={row['first_event_ms'] or '-'}ms "
                    f"INPUT={row['input_tokens'] or '-'} CACHE={row['cached_tokens'] or 0} "
                    f"TOTAL={row['elapsed_ms']}ms X-REQUEST-ID={row['request_id'] or '-'}"
                )

    fieldnames = list(rows[0].keys()) if rows else []
    if rows:
        with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    summary = {
        "model": args.model,
        "base_url": args.base_url,
        "case_tokens": args.case_tokens,
        "requests_per_case": args.requests_per_case,
        "concurrency": args.concurrency,
        "total_requests": len(rows),
        "http_200": sum(r["http_status"] == 200 for r in rows),
        "wall_seconds": round(time.perf_counter() - started, 2),
        "shared_prefix": args.shared_prefix,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases": [summarize_case(args, rows, target_tokens) for target_tokens in args.case_tokens],
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"结果目录：{out}")


if __name__ == "__main__":
    main()

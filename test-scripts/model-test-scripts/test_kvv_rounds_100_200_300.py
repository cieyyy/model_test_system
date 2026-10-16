#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KVV 100/200/300 轮连续对话专项测试。

每一档都会启动独立新会话，并且每轮携带完整历史，不能用独立重复请求替代。
默认只预览，不发送真实请求。

运行示例：

python3 test_kvv_rounds_100_200_300.py \
  --base-url "$API_BASE" \
  --key-env "$API_KEY_ENV" \
  --model "$MODEL" \
  --run --confirm-paid
"""

from __future__ import annotations

import argparse
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
DEFAULT_ROUNDS = (100, 200, 300)

RESULT_FIELDS = (
    "case", "round", "http_status", "result", "request_id", "request_model",
    "response_model", "finish_reason", "expected", "content", "exact_match",
    "thinking_leaked", "input_tokens", "output_tokens", "reasoning_tokens",
    "cached_tokens", "cache_creation_tokens", "cache_write_tokens",
    "total_tokens", "ttft_ms", "elapsed_ms", "raw_file", "error",
)


def parse_rounds(text: str) -> list[int]:
    values = [int(part.strip()) for part in text.split(",") if part.strip()]
    if not values or any(value <= 0 for value in values):
        raise argparse.ArgumentTypeError("rounds 必须是正整数列表，例如 100,200,300")
    return values


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KVV 100/200/300 轮专项测试")
    parser.add_argument("--base-url", default=os.getenv("API_BASE", "http://127.0.0.1:8000/v1"))
    parser.add_argument("--key-env", default=os.getenv("API_KEY_ENV", "API_KEY"))
    parser.add_argument("--model", default=os.getenv("MODEL", "test-model"))
    parser.add_argument("--rounds", type=parse_rounds, default=list(DEFAULT_ROUNDS),
                        help="测试轮数档位，默认 100,200,300")
    parser.add_argument("--max-requests", type=int, default=sum(DEFAULT_ROUNDS),
                        help="本次最多付费请求数，默认 600")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--temperature", type=float, default=1.0,
                        help="请求温度，默认 1.0")
    parser.add_argument("--reasoning-effort", choices=("low", "high", "max", "none"), default="low")
    parser.add_argument("--max-completion-tokens", type=int, default=120)
    parser.add_argument("--max-token-field", choices=("max_completion_tokens", "max_tokens"),
                        default="max_completion_tokens")
    parser.add_argument("--user-agent", default="curl/8.7.1")
    parser.add_argument("--stop-on-mismatch", action="store_true",
                        help="严格答案不匹配时立即停止该档位；默认继续统计命中率")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--execute", action="store_true", help="同 --run")
    parser.add_argument("--confirm-paid", action="store_true")
    return parser.parse_args()


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


def usage_values(response: dict[str, Any]) -> dict[str, Any]:
    usage = response.get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": usage.get("prompt_tokens", ""),
        "output_tokens": usage.get("completion_tokens", ""),
        "reasoning_tokens": completion_details.get("reasoning_tokens", ""),
        "cached_tokens": prompt_details.get("cached_tokens", usage.get("cached_tokens", "")),
        "cache_creation_tokens": (
            prompt_details.get("cache_creation_input_tokens")
            or usage.get("cache_creation_input_tokens")
            or ""
        ),
        "cache_write_tokens": (
            prompt_details.get("cache_write_tokens")
            or prompt_details.get("cache_creation_input_tokens")
            or usage.get("cache_write_tokens", "")
        ),
        "total_tokens": usage.get("total_tokens", ""),
    }


def build_body(args: argparse.Namespace, messages: list[dict[str, Any]]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": args.model,
        "messages": messages,
        args.max_token_field: args.max_completion_tokens,
        "temperature": args.temperature,
        "stream": False,
    }
    if args.reasoning_effort != "none":
        body["reasoning_effort"] = args.reasoning_effort
    return body


def send_request(
    args: argparse.Namespace,
    key: str,
    messages: list[dict[str, Any]],
) -> tuple[int | None, float | None, float, dict[str, Any], dict[str, Any], str]:
    body = build_body(args, messages)
    payload_path = None
    header_path = None
    started = time.perf_counter()
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as payload:
            json.dump(body, payload, ensure_ascii=False)
            payload_path = payload.name
        header_path = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".headers", delete=False).name
        curl_config = (
            f'header = "Authorization: Bearer {key}"\n'
            'header = "Content-Type: application/json"\n'
            'header = "Accept: application/json"\n'
            f'user-agent = "{args.user_agent.replace(chr(34), "")}"\n'
        )
        command = [
            "curl", "-sS", "--config", "-", "--max-time", str(args.timeout),
            "--request", "POST", "--data-binary", "@" + payload_path,
            "--dump-header", header_path,
            "--write-out", "\n__TTFT__:%{time_starttransfer}\n__HTTP_STATUS__:%{http_code}",
            args.base_url.rstrip("/") + "/chat/completions",
        ]
        proc = subprocess.run(command, input=curl_config, text=True, capture_output=True, check=False)
        raw = proc.stdout
        status = None
        marker = "\n__HTTP_STATUS__:"
        if marker in raw:
            raw, status_text = raw.rsplit(marker, 1)
            status = int(status_text.strip()) if status_text.strip().isdigit() else None
        ttft = None
        ttft_marker = "\n__TTFT__:"
        if ttft_marker in raw:
            raw, ttft_text = raw.rsplit(ttft_marker, 1)
            try:
                ttft = round(float(ttft_text.strip()) * 1000, 1)
            except ValueError:
                ttft = None
        try:
            response = json.loads(raw)
        except json.JSONDecodeError:
            response = {"unparsed_response": raw[:4000]}
        if proc.returncode:
            response.setdefault("client_error", proc.stderr.strip())
            response.setdefault("curl_exit", proc.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        status, ttft, response = None, None, {"client_error": str(exc)}
    finally:
        elapsed = round((time.perf_counter() - started) * 1000, 1)
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
    request_id = ""
    header_match = re.search(r"(?im)^x-request-id:\s*(\S+)", headers)
    if header_match:
        request_id = header_match.group(1)
    return status, ttft, elapsed, body, response, request_id


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def summarize_case(args: argparse.Namespace, case_rounds: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    http_200 = sum(row["http_status"] == 200 for row in rows)
    exact = sum(row["http_status"] == 200 and bool(row["exact_match"]) for row in rows)
    passed = sum(row["result"] == "pass" for row in rows)
    completed = bool(rows and rows[-1]["round"] == case_rounds and http_200 == len(rows))
    return {
        "case": f"kvv_{case_rounds}",
        "model": args.model,
        "base_url": args.base_url,
        "target_rounds": case_rounds,
        "attempted_requests": len(rows),
        "http_200": http_200,
        "api_success_rate": round(http_200 / len(rows) * 100, 2) if rows else 0,
        "pass_count": passed,
        "exact_matches": exact,
        "exact_match_rate": round(exact / http_200 * 100, 2) if http_200 else 0,
        "thinking_leak_rounds": sum(bool(row["thinking_leaked"]) for row in rows),
        "completed_target": completed,
        "last_round": rows[-1]["round"] if rows else 0,
        "last_input_tokens": rows[-1]["input_tokens"] if rows else "",
        "last_cached_tokens": rows[-1]["cached_tokens"] if rows else "",
        "max_cached_tokens": max((int(row["cached_tokens"] or 0) for row in rows), default=0),
        "avg_ttft_ms": mean_value([row["ttft_ms"] for row in rows if row["http_status"] == 200]),
        "p95_ttft_ms": percentile([row["ttft_ms"] for row in rows if row["http_status"] == 200], 95),
        "p99_ttft_ms": percentile([row["ttft_ms"] for row in rows if row["http_status"] == 200], 99),
        "avg_elapsed_ms": mean_value([row["elapsed_ms"] for row in rows if row["http_status"] == 200]),
        "p99_elapsed_ms": percentile([row["elapsed_ms"] for row in rows if row["http_status"] == 200], 99),
        "finish_counts": {
            value: sum(row["finish_reason"] == value for row in rows)
            for value in sorted({str(row["finish_reason"]) for row in rows})
        },
        "error_count": sum(bool(row["error"]) for row in rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def run_case(args: argparse.Namespace, key: str, case_rounds: int, case_dir: Path) -> dict[str, Any]:
    case_name = f"kvv_{case_rounds}"
    history: list[dict[str, Any]] = [{
        "role": "system",
        "content": (
            "这是 KVV 连续对话稳定性测试。每轮严格只回复用户指定的 KVV_ACK 编号，"
            "不要解释，不要添加标点，不要输出其他内容。"
        ),
    }]
    rows: list[dict[str, Any]] = []
    case_dir.mkdir(parents=True, exist_ok=False)
    print(f"\n[{case_name}] 开始：目标 {case_rounds} 轮")
    for round_no in range(1, case_rounds + 1):
        expected = f"KVV_ACK_{round_no:03d}"
        user_message = {"role": "user", "content": f"第 {round_no} 轮，只回复 {expected}"}
        status, ttft, elapsed, body, response, header_request_id = send_request(
            args, key, history + [user_message]
        )
        choice = (response.get("choices") or [{}])[0]
        assistant = choice.get("message") or {}
        content = (assistant.get("content") or "").strip()
        finish_reason = choice.get("finish_reason") or ""
        exact_match = content == expected
        lower_content = content.lower()
        thinking_leaked = "<thinking>" in lower_content or "</thinking>" in lower_content
        metrics = usage_values(response)
        error_obj = response.get("error", response.get("client_error", ""))
        error_text = json.dumps(error_obj, ensure_ascii=False) if error_obj else ""
        if status != 200 or not assistant:
            result = "error"
        elif finish_reason == "length" or not exact_match or thinking_leaked:
            result = "fail"
        else:
            result = "pass"

        raw_file = case_dir / f"{case_name}-{round_no:03d}.json"
        raw_file.write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "case": case_name,
            "request": body,
            "http_status": status,
            "header_request_id": header_request_id,
            "ttft_ms": ttft,
            "elapsed_ms": elapsed,
            "response": response,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        row = {
            "case": case_name,
            "round": round_no,
            "http_status": status if status is not None else "",
            "result": result,
            "request_id": header_request_id or response.get("id", ""),
            "request_model": args.model,
            "response_model": response.get("model", ""),
            "finish_reason": finish_reason,
            "expected": expected,
            "content": content[:500],
            "exact_match": exact_match,
            "thinking_leaked": thinking_leaked,
            **metrics,
            "ttft_ms": ttft if ttft is not None else "",
            "elapsed_ms": elapsed,
            "raw_file": raw_file.name,
            "error": error_text[:2000],
        }
        rows.append(row)
        write_csv(case_dir / "conversation.csv", rows)
        summary = summarize_case(args, case_rounds, rows)
        (case_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"{case_name} 第{round_no}/{case_rounds}轮 | HTTP={status or '-'} | "
            f"RESULT={result.upper()} | EXACT={exact_match} | "
            f"TTFT={ttft if ttft is not None else '-'}ms | TOTAL={elapsed}ms | "
            f"INPUT={metrics['input_tokens'] or 0} CACHE={metrics['cached_tokens'] or 0}"
        )
        if status != 200 or not assistant:
            print(f"{case_name} 请求错误或无有效 assistant message，停止该档位")
            break
        if finish_reason == "length":
            print(f"{case_name} 输出被截断，停止该档位")
            break
        if args.stop_on_mismatch and not exact_match:
            print(f"{case_name} 严格答案未命中，按 --stop-on-mismatch 停止该档位")
            break
        history.extend([user_message, assistant])
    return summarize_case(args, case_rounds, rows)


def main() -> None:
    args = parse_args()
    execute = args.run or args.execute
    planned_requests = sum(args.rounds)
    if not args.base_url.startswith(("http://", "https://")):
        raise SystemExit("--base-url 必须使用 http:// 或 https://")
    if any(rounds > 300 for rounds in args.rounds):
        raise SystemExit("该脚本单档上限为 300 轮")
    if min(args.timeout, args.max_completion_tokens, args.max_requests) <= 0:
        raise SystemExit("timeout/max-completion-tokens/max-requests 必须大于 0")
    if planned_requests > args.max_requests:
        raise SystemExit(f"计划请求数 {planned_requests} 超过 max-requests={args.max_requests}")

    print(
        f"项目=KVV 连续对话专项；模型={args.model}；端点={args.base_url.rstrip('/')}/chat/completions"
    )
    print(f"档位={','.join(str(x) for x in args.rounds)}；计划总请求={planned_requests}")
    if not (execute and args.confirm_paid):
        print("仅预览；真实请求需添加 --run --confirm-paid")
        return

    key = os.environ.get(args.key_env, "")
    if not key:
        raise SystemExit(f"未设置环境变量 {args.key_env}")

    out_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else ROOT / "results" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-kvv-100-200-300")
    )
    out_dir.mkdir(parents=True, exist_ok=False)
    summaries = []
    started = time.perf_counter()
    for case_rounds in args.rounds:
        summaries.append(run_case(args, key, case_rounds, out_dir / f"kvv-{case_rounds}"))

    total_summary = {
        "model": args.model,
        "base_url": args.base_url,
        "round_cases": args.rounds,
        "planned_requests": planned_requests,
        "attempted_requests": sum(item["attempted_requests"] for item in summaries),
        "http_200": sum(item["http_200"] for item in summaries),
        "completed_cases": sum(bool(item["completed_target"]) for item in summaries),
        "wall_seconds": round(time.perf_counter() - started, 2),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases": summaries,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(total_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (out_dir / "cases.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = list(summaries[0].keys()) if summaries else []
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summaries)
    if summaries:
        report_rows = []
        for item in summaries:
            report_rows.append({
                "case": item.get("case", ""),
                "http_status": 200 if item.get("http_200") else "",
                "result": "PASS" if item.get("completed_target") else "FAIL",
                "elapsed_ms": item.get("avg_elapsed_ms", ""),
                "ttft_ms": item.get("avg_ttft_ms", ""),
                "input_tokens": item.get("last_input_tokens", ""),
                "cached_tokens": item.get("last_cached_tokens", ""),
                "content": (
                    f"目标 {item.get('target_rounds')} 轮；完成到第 {item.get('last_round')} 轮；"
                    f"HTTP 200 {item.get('http_200')}/{item.get('attempted_requests')}；"
                    f"精确命中率 {item.get('exact_match_rate')}%"
                ),
                "error": f"error_count={item.get('error_count', 0)}",
            })
        with (out_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(report_rows[0]))
            writer.writeheader()
            writer.writerows(report_rows)
    print("\n" + json.dumps(total_summary, ensure_ascii=False, indent=2))
    print(f"结果目录：{out_dir}")


if __name__ == "__main__":
    main()

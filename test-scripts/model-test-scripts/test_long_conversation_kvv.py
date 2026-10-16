#!/usr/bin/env python3
"""100轮真实连续对话/KVV测试。

每轮携带完整历史，核对 ACK 顺序、HTTP 成功率、cache_read、思考泄漏和末轮耗时；
不能用独立重复请求替代。运行：python3 test_long_conversation_kvv.py --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" --rounds 100 --run --confirm-paid
"""

import argparse
import csv
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RESULT_FIELDS = (
    "round", "http_status", "result", "request_id", "request_model",
    "response_model", "finish_reason", "expected", "content", "exact_match",
    "thinking_leaked", "input_tokens", "output_tokens", "reasoning_tokens",
    "cached_tokens", "cache_creation_tokens", "total_tokens", "ttft_ms",
    "elapsed_ms", "raw_file",
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="长对话稳定性专项测试（默认仅预览，不发送请求）"
    )
    parser.add_argument("--base-url", default=os.getenv("API_BASE", "http://127.0.0.1:8000/v1"))
    parser.add_argument("--key-env", default=os.getenv("API_KEY_ENV", "API_KEY"))
    parser.add_argument("--model", default=os.getenv("MODEL", "test-model"))
    parser.add_argument("--rounds", type=int, default=50, help="目标总轮数，范围 1-200")
    parser.add_argument(
        "--max-requests", type=int, default=50,
        help="本次最多付费请求数；测试100轮时需显式设为100",
    )
    parser.add_argument("--timeout", type=int, default=150, help="每轮超时秒数")
    parser.add_argument(
        "--reasoning-effort", choices=("low", "high", "max"), default="low"
    )
    parser.add_argument("--max-completion-tokens", type=int, default=120)
    parser.add_argument("--user-agent", default="curl/8.7.1")
    parser.add_argument(
        "--resume-from", type=Path,
        help="之前的长对话结果目录；自动从最后一个有效 HTTP 200 轮次恢复",
    )
    parser.add_argument(
        "--stop-on-mismatch", action="store_true",
        help="严格答案不匹配时立即停止；默认继续以统计命中率",
    )
    parser.add_argument("--output-dir", type=Path, help="结果目录，默认写入项目 results")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--confirm-paid", action="store_true")
    return parser.parse_args()


def validate_args(args, parser_error):
    if not args.base_url.startswith(("http://", "https://")):
        parser_error("--base-url 必须使用 http:// 或 https://")
    if not 1 <= args.rounds <= 200:
        parser_error("--rounds 必须在 1-200 之间")
    if not 1 <= args.max_requests <= 200:
        parser_error("--max-requests 必须在 1-200 之间")
    if not 1 <= args.timeout <= 600:
        parser_error("--timeout 必须在 1-600 秒之间")
    if not 16 <= args.max_completion_tokens <= 2048:
        parser_error("--max-completion-tokens 必须在 16-2048 之间")
    if args.resume_from and not args.resume_from.is_dir():
        parser_error("--resume-from 必须是有效的结果目录")


def usage_values(response):
    usage = response.get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    cache_creation = (
        prompt_details.get("cache_creation_input_tokens")
        or usage.get("cache_creation_input_tokens")
        or 0
    )
    return {
        "input_tokens": usage.get("prompt_tokens", ""),
        "output_tokens": usage.get("completion_tokens", ""),
        "reasoning_tokens": completion_details.get("reasoning_tokens", ""),
        "cached_tokens": prompt_details.get(
            "cached_tokens", usage.get("cached_tokens", "")
        ),
        "cache_creation_tokens": cache_creation,
        "total_tokens": usage.get("total_tokens", ""),
    }


def send_request(args, key, messages):
    body = {
        "model": args.model,
        "messages": messages,
        "reasoning_effort": args.reasoning_effort,
        "max_completion_tokens": args.max_completion_tokens,
        "stream": False,
    }
    url = args.base_url.rstrip("/") + "/chat/completions"
    curl_config = (
        f'header = "Authorization: Bearer {key}"\n'
        'header = "Content-Type: application/json"\n'
        'header = "Accept: application/json"\n'
        f'user-agent = "{args.user_agent.replace(chr(34), "")}"\n'
    )
    payload_path = None
    started = time.perf_counter()
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", delete=False
        ) as payload:
            json.dump(body, payload, ensure_ascii=False)
            payload_path = payload.name
        command = [
            "curl", "-sS", "--config", "-", "--max-time", str(args.timeout),
            "--request", "POST", "--data-binary", "@" + payload_path,
            "--write-out", "\n__TTFT__:%{time_starttransfer}\n__HTTP_STATUS__:%{http_code}",
            url,
        ]
        proc = subprocess.run(
            command, input=curl_config, text=True, capture_output=True, check=False
        )
        raw = proc.stdout
        marker = "\n__HTTP_STATUS__:"
        raw, status_text = raw.rsplit(marker, 1) if marker in raw else (raw, "")
        status_text = status_text.strip()
        status = int(status_text) if status_text.isdigit() else None
        ttft = None
        ttft_marker = "\n__TTFT__:"
        if ttft_marker in raw:
            raw, ttft_text = raw.rsplit(ttft_marker, 1)
            try:
                ttft = round(float(ttft_text.strip()) * 1000, 1)
            except ValueError:
                pass
        try:
            response = json.loads(raw)
        except json.JSONDecodeError:
            response = {"unparsed_response": raw}
        if proc.returncode:
            response.setdefault("client_error", proc.stderr.strip())
            response.setdefault("curl_exit", proc.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        status, ttft, response = None, None, {"client_error": str(exc)}
    finally:
        if payload_path:
            try:
                Path(payload_path).unlink()
            except FileNotFoundError:
                pass
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    return status, ttft, elapsed, body, response


def conversation_message(message):
    return {
        "role": message.get("role", ""),
        "content": message.get("content", ""),
    }


def find_resume_point(source_dir, model):
    candidates = []
    for path in source_dir.glob("conversation-*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            request = record.get("request") or {}
            if request.get("model") != model or record.get("http_status") != 200:
                continue
            choice = ((record.get("response") or {}).get("choices") or [{}])[0]
            assistant = choice.get("message") or {}
            finish_reason = choice.get("finish_reason") or ""
            messages = request.get("messages") or []
            round_no = int(path.stem.rsplit("-", 1)[-1])
            if (
                assistant
                and messages
                and finish_reason != "length"
                and (assistant.get("content") or "").strip() == f"ACK-{round_no}"
            ):
                clean_messages = [conversation_message(message) for message in messages]
                candidates.append((
                    round_no, path, clean_messages, conversation_message(assistant)
                ))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    if not candidates:
        raise ValueError("恢复目录中没有该模型可用的 HTTP 200 长对话轮次")
    return max(candidates, key=lambda item: item[0])


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_summary(path, args, rows, start_round, resumed_from):
    http_200 = sum(row["http_status"] == 200 for row in rows)
    exact = sum(
        row["http_status"] == 200 and bool(row["exact_match"]) for row in rows
    )
    leaks = sum(bool(row["thinking_leaked"]) for row in rows)
    elapsed = [row["elapsed_ms"] for row in rows if row["http_status"] == 200]
    summary = {
        "model": args.model,
        "base_url": args.base_url,
        "target_rounds": args.rounds,
        "start_round": start_round,
        "attempted_requests": len(rows),
        "http_200": http_200,
        "api_success_rate": http_200 / len(rows) if rows else 0,
        "exact_matches": exact,
        "exact_match_rate": exact / http_200 if http_200 else 0,
        "thinking_leak_rounds": leaks,
        "average_elapsed_ms": sum(elapsed) / len(elapsed) if elapsed else None,
        "completed_target": bool(rows and rows[-1]["round"] == args.rounds and http_200 == len(rows)),
        "resumed_from": str(resumed_from) if resumed_from else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    args = parse_args()
    validate_args(args, lambda message: (_ for _ in ()).throw(ValueError(message)))

    history = [{
        "role": "system",
        "content": "这是长对话稳定性测试。每轮严格只回复用户指定的 ACK 编号，不要解释。",
    }]
    start_round = 1
    resume_file = None
    if args.resume_from:
        last_round, resume_file, saved_messages, assistant = find_resume_point(
            args.resume_from.resolve(), args.model
        )
        history = list(saved_messages) + [assistant]
        start_round = last_round + 1

    remaining = max(0, args.rounds - start_round + 1)
    planned = min(remaining, args.max_requests)
    print(
        f"项目=长对话稳定性；模型={args.model}；接口=/v1/chat/completions；"
        f"目标={args.rounds}轮；起始={start_round}轮；本次计划={planned}个请求；"
        f"最多执行={args.max_requests}个请求"
    )
    if remaining == 0:
        print("恢复点已经达到或超过目标轮数，无需发送请求")
        return
    if planned < remaining:
        print(f"注意：受 --max-requests 限制，本次最多运行到第 {start_round + planned - 1} 轮")
    if not (args.run and args.confirm_paid):
        print("仅预览；实际付费请求需同时添加 --run --confirm-paid")
        return

    key = os.environ.get(args.key_env, "")
    if not key:
        raise SystemExit(f"未设置环境变量 {args.key_env}")

    if args.output_dir:
        out_dir = args.output_dir.resolve()
        out_dir.mkdir(parents=True, exist_ok=False)
    else:
        out_dir = ROOT / "results" / (
            datetime.now().strftime("%Y%m%d-%H%M%S") + "-long-conversation"
        )
        out_dir.mkdir(parents=True, exist_ok=False)

    rows = []
    for round_no in range(start_round, start_round + planned):
        expected = f"ACK-{round_no}"
        user_message = {
            "role": "user", "content": f"第{round_no}轮，只回复 {expected}"
        }
        status, ttft, elapsed, body, response = send_request(
            args, key, history + [user_message]
        )
        choice = (response.get("choices") or [{}])[0]
        assistant = choice.get("message") or {}
        content = (assistant.get("content") or "").strip()
        finish_reason = choice.get("finish_reason") or ""
        exact_match = content == expected
        thinking_leaked = "<thinking>" in content.lower()
        metrics = usage_values(response)
        if status != 200 or not assistant:
            result = "error"
        elif finish_reason == "length" or not exact_match:
            result = "fail"
        else:
            result = "pass"

        raw_file = out_dir / f"conversation-{args.model}-{round_no:03d}.json"
        raw_file.write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "resumed_from": str(resume_file) if resume_file else None,
            "request": body,
            "http_status": status,
            "ttft_ms": ttft,
            "elapsed_ms": elapsed,
            "response": response,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        row = {
            "round": round_no,
            "http_status": status if status is not None else "",
            "result": result,
            "request_id": response.get("id", ""),
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
        }
        rows.append(row)
        write_csv(out_dir / "conversation.csv", rows)
        write_csv(out_dir / "results.csv", rows)
        write_summary(out_dir / "summary.json", args, rows, start_round, resume_file)

        print(
            f"第{round_no}轮 | HTTP={status or '-'} | RESULT={result.upper()} | "
            f"MODEL={args.model}->{response.get('model') or '-'} | "
            f"EXACT={exact_match} | THINKING_LEAK={thinking_leaked} | "
            f"TTFT={ttft if ttft is not None else '-'}ms | TOTAL={elapsed}ms",
            flush=True,
        )
        print(
            f"  TOKENS input={metrics['input_tokens'] or 0} "
            f"output={metrics['output_tokens'] or 0} "
            f"reasoning={metrics['reasoning_tokens'] or 0} "
            f"cache_read={metrics['cached_tokens'] or 0} "
            f"cache_creation={metrics['cache_creation_tokens'] or 0} "
            f"total={metrics['total_tokens'] or 0} | CONTENT={content[:120]!r}",
            flush=True,
        )

        if status != 200 or not assistant:
            print("请求错误或无有效 assistant message，停止后续轮次")
            break
        if finish_reason == "length":
            print("输出被截断，无法安全续接完整对话，停止后续轮次")
            break
        if args.stop_on_mismatch and not exact_match:
            print("严格答案未命中，按 --stop-on-mismatch 停止")
            break
        history.extend([user_message, conversation_message(assistant)])

    if rows:
        write_csv(out_dir / "results.csv", rows)
    write_summary(out_dir / "summary.json", args, rows, start_round, resume_file)
    print(f"结果目录：{out_dir}")


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(f"参数错误：{exc}") from exc

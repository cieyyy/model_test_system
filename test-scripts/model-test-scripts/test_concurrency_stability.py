#!/usr/bin/env python3
"""连续会话和并发稳定性测试。

同时统计连续轮次 EXACT/ACK、并发探测 HTTP 成功率、耗时和错误；用于发现高并发下
的内容错乱、超时和限流。运行：python3 test_concurrency_stability.py --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" --rounds 50 --concurrency 10 --probes 10 --run --confirm-paid
"""

import argparse
import concurrent.futures
import csv
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def args_parser():
    p = argparse.ArgumentParser(description="连续对话和并发稳定性测试")
    p.add_argument("--base-url", default=os.getenv("API_BASE", "http://127.0.0.1:8000/v1"))
    p.add_argument("--key-env", default=os.getenv("API_KEY_ENV", "API_KEY"))
    p.add_argument("--model", default=os.getenv("MODEL", "test-model"))
    p.add_argument("--rounds", type=int, default=50)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--probes", type=int, default=5, help="并发探测请求数")
    p.add_argument("--timeout", type=int, default=150)
    p.add_argument("--max-completion-tokens", type=int, default=120)
    p.add_argument("--output-dir", type=Path)
    p.add_argument("--run", action="store_true")
    p.add_argument("--confirm-paid", action="store_true")
    return p.parse_args()


def request(args, key, messages):
    body = {"model": args.model, "messages": messages, "reasoning_effort": "low",
            "max_completion_tokens": args.max_completion_tokens, "stream": False}
    payload = None
    started = time.perf_counter()
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
            json.dump(body, f, ensure_ascii=False); payload = f.name
        cfg = (f'header = "Authorization: Bearer {key}"\n'
               'header = "Content-Type: application/json"\n'
               'header = "Accept: application/json"\n')
        proc = subprocess.run([
            "curl", "-sS", "--config", "-", "--max-time", str(args.timeout),
            "--request", "POST", "--data-binary", "@" + payload,
            "--write-out", "\n__HTTP_STATUS__:%{http_code}",
            args.base_url.rstrip("/") + "/chat/completions"],
            input=cfg, text=True, capture_output=True, check=False)
        raw = proc.stdout
        if "\n__HTTP_STATUS__:" in raw:
            raw, status_text = raw.rsplit("\n__HTTP_STATUS__:", 1)
            status = int(status_text.strip()) if status_text.strip().isdigit() else None
        else:
            status = None
        try: response = json.loads(raw)
        except json.JSONDecodeError: response = {"unparsed_response": raw}
        if proc.returncode:
            response.setdefault("client_error", proc.stderr.strip())
    except (OSError, subprocess.SubprocessError) as exc:
        status, response = None, {"client_error": str(exc)}
    finally:
        if payload:
            try: Path(payload).unlink()
            except FileNotFoundError: pass
    return status, round((time.perf_counter() - started) * 1000, 1), body, response


def row_from(args, key, messages, expected, number, kind):
    status, elapsed, body, response = request(args, key, messages)
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = (message.get("content") or "").strip()
    usage = response.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return {
        "kind": kind, "number": number, "http_status": status or "",
        "response_model": response.get("model", ""), "finish_reason": choice.get("finish_reason", ""),
        "expected": expected, "content": content, "exact_match": content == expected,
        "input_tokens": usage.get("prompt_tokens", ""), "output_tokens": usage.get("completion_tokens", ""),
        "cached_tokens": details.get("cached_tokens", usage.get("cached_tokens", "")),
        "elapsed_ms": elapsed, "error": response.get("error", response.get("client_error", "")),
        "_assistant_message": message,
    }


def main():
    args = args_parser()
    if not args.base_url.startswith(("http://", "https://")) or args.rounds < 1 or args.concurrency < 1 or args.probes < 1:
        raise SystemExit("base-url 必须为 http(s) 地址，rounds/concurrency/probes 必须大于 0")
    planned = args.rounds + args.probes
    print(f"项目=连续对话+并发稳定性；模型={args.model}；目标={args.rounds}轮；并发探测={args.probes}；计划={planned}请求")
    if not (args.run and args.confirm_paid):
        print("仅预览；实际请求需同时添加 --run --confirm-paid"); return
    key = os.environ.get(args.key_env, "")
    if not key: raise SystemExit(f"未设置环境变量 {args.key_env}")
    out = (args.output_dir.resolve() if args.output_dir else
           ROOT / "results" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-stability-concurrency"))
    out.mkdir(parents=True, exist_ok=False)
    history = [{"role": "system", "content": "连续对话稳定性测试，只回复用户要求的 ACK，不要解释。"}]
    rows = []
    for n in range(1, args.rounds + 1):
        expected = f"ACK-{n}"
        user = {"role": "user", "content": f"第{n}轮，只回复 {expected}"}
        row = row_from(args, key, history + [user], expected, n, "conversation")
        rows.append(row); print(f"conversation#{n} HTTP={row['http_status'] or '-'} EXACT={row['exact_match']} TOTAL={row['elapsed_ms']}ms")
        # Preserve the assistant message only after a valid response, enabling true context carry-over.
        if row["http_status"] == 200:
            history.extend([user, row["_assistant_message"]])
        else:
            break
    def probe(n):
        return row_from(args, key, [{"role": "user", "content": f"只回复 CONCURRENT-{n}"}], f"CONCURRENT-{n}", n, "concurrency")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for future in concurrent.futures.as_completed([pool.submit(probe, n) for n in range(1, args.probes + 1)]):
            row = future.result(); rows.append(row)
            print(f"concurrency#{row['number']} HTTP={row['http_status'] or '-'} EXACT={row['exact_match']} TOTAL={row['elapsed_ms']}ms")
    csv_rows = [{k: v for k, v in row.items() if k != "_assistant_message"} for row in rows]
    with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys())); writer.writeheader(); writer.writerows(csv_rows)
    ok = [r for r in rows if r["http_status"] == 200]
    summary = {"model": args.model, "base_url": args.base_url, "target_rounds": args.rounds,
               "conversation_attempted": sum(r["kind"] == "conversation" for r in rows),
               "conversation_http_200": sum(r["kind"] == "conversation" and r["http_status"] == 200 for r in rows),
               "concurrency_probes": args.probes,
               "concurrency_http_200": sum(r["kind"] == "concurrency" and r["http_status"] == 200 for r in rows),
               "exact_matches": sum(bool(r["exact_match"]) for r in rows),
               "average_elapsed_ms": sum(r["elapsed_ms"] for r in ok) / len(ok) if ok else None,
               "generated_at": datetime.now(timezone.utc).isoformat()}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果目录：{out}")


if __name__ == "__main__": main()

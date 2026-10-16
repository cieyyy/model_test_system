#!/usr/bin/env python3
"""长输出截断与超时专项测试。

对多个 max output 档位统计 HTTP、TTFT、总耗时、finish_reason、核验码和输出 Token，
用于区分正常 stop、length 截断和网关超时。运行：python3 test_long_output_truncation.py --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" --targets 512,1024,1808 --run --confirm-paid
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
FIELDS = (
    "target_tokens", "http_status", "result", "request_id", "request_model",
    "response_model", "finish_reason", "input_tokens", "output_tokens",
    "reasoning_tokens", "cached_tokens", "total_tokens", "ttft_ms",
    "elapsed_ms", "output_chars", "markers_found", "raw_file",
)
MARKERS = ("LONG-ALPHA-731", "LONG-MIDDLE-284", "LONG-OMEGA-956")


def parse_args():
    p = argparse.ArgumentParser(description="长输出专项测试（默认不扣费）")
    p.add_argument("--base-url", default=os.getenv("API_BASE", "http://127.0.0.1:8000/v1"))
    p.add_argument("--key-env", default=os.getenv("API_KEY_ENV", "API_KEY"))
    p.add_argument("--model", default=os.getenv("MODEL", "test-model"))
    p.add_argument("--targets", default="512,1024,1808", help="目标 max_completion_tokens，逗号分隔")
    p.add_argument("--max-requests", type=int, default=3)
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--reasoning-effort", choices=("low", "high", "max"), default="low")
    p.add_argument("--user-agent", default="curl/8.7.1")
    p.add_argument("--output-dir", type=Path)
    p.add_argument("--run", action="store_true")
    p.add_argument("--confirm-paid", action="store_true")
    return p.parse_args()


def parse_targets(value):
    try:
        targets = [int(x.strip()) for x in value.split(",") if x.strip()]
    except ValueError as exc:
        raise ValueError("--targets 必须是逗号分隔的整数") from exc
    if not targets or any(x < 16 or x > 4096 for x in targets):
        raise ValueError("--targets 每项必须在 16-4096 之间")
    return targets


def usage_values(usage):
    prompt = usage.get("prompt_tokens_details") or {}
    completion = usage.get("completion_tokens_details") or {}
    return (
        usage.get("prompt_tokens", ""), usage.get("completion_tokens", ""),
        completion.get("reasoning_tokens", ""),
        prompt.get("cached_tokens", usage.get("cached_tokens", "")),
        usage.get("total_tokens", ""),
    )


def request(args, key, max_tokens):
    prompt = (
        "请写一篇面向普通读者的《城市社区阅读活动执行手册》，内容包括：背景、目标、"
        "活动流程、人员分工、预算、风险处理、宣传、复盘和改进建议。尽量提供具体例子、"
        "表格化清单和分级标题，充分展开，不要提前结束。必须在正文中原样保留三个核验码："
        "LONG-ALPHA-731、LONG-MIDDLE-284、LONG-OMEGA-956。不要讨论 API、模型或测试。"
    )
    body = {
        "model": args.model,
        "messages": [{"role": "user", "content": prompt}],
        "reasoning_effort": args.reasoning_effort,
        "max_completion_tokens": max_tokens,
        "stream": False,
    }
    url = args.base_url.rstrip("/") + "/chat/completions"
    cfg = (
        f'header = "Authorization: Bearer {key}"\n'
        'header = "Content-Type: application/json"\n'
        'header = "Accept: application/json"\n'
        f'user-agent = "{args.user_agent.replace(chr(34), "")}"\n'
    )
    payload = None
    started = time.perf_counter()
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
            json.dump(body, f, ensure_ascii=False)
            payload = f.name
        proc = subprocess.run(
            ["curl", "-sS", "--config", "-", "--max-time", str(args.timeout),
             "--request", "POST", "--data-binary", "@" + payload,
             "--write-out", "\n__TTFT__:%{time_starttransfer}\n__HTTP_STATUS__:%{http_code}", url],
            input=cfg, text=True, capture_output=True, check=False,
        )
        raw = proc.stdout
        status = None
        if "\n__HTTP_STATUS__:" in raw:
            raw, status_text = raw.rsplit("\n__HTTP_STATUS__:", 1)
            status = int(status_text.strip()) if status_text.strip().isdigit() else None
        ttft = None
        if "\n__TTFT__:" in raw:
            raw, ttft_text = raw.rsplit("\n__TTFT__:", 1)
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
        if payload:
            try:
                Path(payload).unlink()
            except FileNotFoundError:
                pass
    return status, ttft, round((time.perf_counter() - started) * 1000, 1), body, response


def main():
    args = parse_args()
    if not args.base_url.startswith(("http://", "https://")):
        raise SystemExit("--base-url 必须使用 http:// 或 https://")
    try:
        targets = parse_targets(args.targets)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if args.max_requests < 1 or args.timeout < 1:
        raise SystemExit("--max-requests 和 --timeout 必须大于 0")
    targets = targets[:args.max_requests]
    print(f"项目=长输出；模型={args.model}；目标档位={','.join(map(str, targets))}；计划={len(targets)}个请求")
    if not (args.run and args.confirm_paid):
        print("仅预览；实际付费请求需同时添加 --run --confirm-paid")
        return
    key = os.environ.get(args.key_env, "")
    if not key:
        raise SystemExit(f"未设置环境变量 {args.key_env}")
    out = (args.output_dir.resolve() if args.output_dir else ROOT / "results" /
           (datetime.now().strftime("%Y%m%d-%H%M%S") + "-long-output"))
    out.mkdir(parents=True, exist_ok=False)
    rows = []
    for target in targets:
        status, ttft, elapsed, body, response = request(args, key, target)
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        usage = response.get("usage") or {}
        inp, out_tokens, reasoning, cached, total = usage_values(usage)
        found = [marker for marker in MARKERS if marker in content]
        finish = choice.get("finish_reason", "")
        result = "PASS" if status == 200 and content and finish != "length" else (
            "TRUNCATED" if status == 200 and finish == "length" else "FAIL")
        raw_file = out / f"{len(rows) + 1:03d}-target-{target}.json"
        raw_file.write_text(json.dumps({"request": body, "response": response}, ensure_ascii=False, indent=2), encoding="utf-8")
        row = {"target_tokens": target, "http_status": status or "", "result": result,
               "request_id": response.get("id", ""), "request_model": args.model,
               "response_model": response.get("model", ""), "finish_reason": finish,
               "input_tokens": inp, "output_tokens": out_tokens, "reasoning_tokens": reasoning,
               "cached_tokens": cached, "total_tokens": total, "ttft_ms": ttft,
               "elapsed_ms": elapsed, "output_chars": len(content), "markers_found": len(found),
               "raw_file": raw_file.name}
        rows.append(row)
        print(f"目标={target} | HTTP={status or '-'} | RESULT={result} | FINISH={finish or '-'} | "
              f"OUTPUT={out_tokens or '-'} tokens | TTFT={ttft or '-'}ms | TOTAL={elapsed}ms | 核验码={len(found)}/3")
        if status in (401, 403, 429):
            print("鉴权、额度或限流错误，停止后续请求")
            break
    with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    summary = {"model": args.model, "base_url": args.base_url, "attempted": len(rows),
               "passed": sum(r["result"] == "PASS" for r in rows),
               "truncated": sum(r["result"] == "TRUNCATED" for r in rows),
               "generated_at": datetime.now(timezone.utc).isoformat()}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果目录：{out}")


if __name__ == "__main__":
    main()

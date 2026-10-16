#!/usr/bin/env python3
"""Thinking/temperature compatibility regression probe.

This script is intentionally small and focused. It checks the issues called out
by customers around disabling thinking, temperature behavior after thinking is
disabled, and nested thinking.effort compatibility.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Case:
    case_id: str
    category: str
    description: str
    body_extra: dict[str, Any]
    expected_http: int
    require_no_reasoning: bool = False
    require_reasoning: bool = False
    problem_when_failed: str = ""


CASES = [
    Case(
        "baseline_default_thinking",
        "baseline",
        "默认思考应可正常返回",
        {},
        200,
        require_reasoning=True,
        problem_when_failed="基线请求失败，后续参数判断不可靠",
    ),
    Case(
        "thinking_type_disabled",
        "disable_thinking",
        "官方 thinking.type=disabled 应可关闭思考并正常返回正文",
        {"thinking": {"type": "disabled"}},
        200,
        require_no_reasoning=True,
        problem_when_failed="P0 仍存在：thinking.type=disabled 不可用",
    ),
    Case(
        "enable_thinking_false",
        "disable_thinking",
        "兼容 enable_thinking=false 的关思考探针",
        {"enable_thinking": False},
        200,
        require_no_reasoning=True,
        problem_when_failed="兼容关思考参数 enable_thinking=false 仍不可用",
    ),
    Case(
        "enabled_temperature_1",
        "temperature",
        "开思考 temperature=1.0 应通过",
        {"thinking": {"type": "enabled"}, "temperature": 1.0},
        200,
        require_reasoning=True,
        problem_when_failed="temperature=1.0 在开思考下异常失败",
    ),
    Case(
        "enabled_temperature_0_6",
        "temperature",
        "开思考 temperature=0.6 按兼容性约束应拒绝",
        {"thinking": {"type": "enabled"}, "temperature": 0.6},
        400,
        problem_when_failed="开思考 temperature=0.6 未按约束拒绝",
    ),
    Case(
        "disabled_temperature_1",
        "temperature",
        "关思考 temperature=1.0 应通过",
        {"thinking": {"type": "disabled"}, "temperature": 1.0},
        200,
        require_no_reasoning=True,
        problem_when_failed="P0 仍存在：关思考后 temperature=1.0 不可达",
    ),
    Case(
        "disabled_temperature_0_6",
        "temperature",
        "关思考 temperature=0.6 应通过",
        {"thinking": {"type": "disabled"}, "temperature": 0.6},
        200,
        require_no_reasoning=True,
        problem_when_failed="P0 仍存在：关思考后 temperature=0.6 不可达",
    ),
    Case(
        "top_level_reasoning_effort_low",
        "effort",
        "顶层 reasoning_effort=low 作为对照应通过",
        {"reasoning_effort": "low"},
        200,
        require_reasoning=True,
        problem_when_failed="顶层 reasoning_effort=low 异常失败",
    ),
    Case(
        "nested_thinking_effort_low",
        "effort",
        "嵌套 thinking.effort=low 应被接受",
        {"thinking": {"type": "enabled", "effort": "low"}},
        200,
        require_reasoning=True,
        problem_when_failed="P1 仍存在：thinking.effort=low 不被接受",
    ),
    Case(
        "nested_thinking_effort_high",
        "effort",
        "嵌套 thinking.effort=high 应被接受",
        {"thinking": {"type": "enabled", "effort": "high"}},
        200,
        require_reasoning=True,
        problem_when_failed="P1 仍存在：thinking.effort=high 不被接受",
    ),
    Case(
        "nested_thinking_effort_max",
        "effort",
        "嵌套 thinking.effort=max 应被接受",
        {"thinking": {"type": "enabled", "effort": "max"}},
        200,
        require_reasoning=True,
        problem_when_failed="P1 仍存在：thinking.effort=max 不被接受",
    ),
]


def base_body(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "请简洁回答。"},
            {"role": "user", "content": "用一句话回答：2+3等于几？"},
        ],
        "max_completion_tokens": 64,
    }


def send(base_url: str, api_key: str, request_body: dict[str, Any], timeout: int, user_agent: str) -> tuple[int | None, float, dict[str, Any] | str]:
    url = base_url.rstrip("/") + "/chat/completions"
    curl_config = (
        f'header = "Authorization: Bearer {api_key}"\n'
        'header = "Content-Type: application/json"\n'
        f'user-agent = "{user_agent.replace(chr(34), "")}"\n'
    )
    payload_path = None
    start = time.perf_counter()
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as payload:
            payload.write(json.dumps(request_body, ensure_ascii=False))
            payload_path = payload.name
        command = [
            "curl",
            "-sS",
            "--config",
            "-",
            "--max-time",
            str(timeout),
            "--request",
            "POST",
            "--data-binary",
            "@" + payload_path,
            "--write-out",
            "\n__HTTP_STATUS__:%{http_code}",
            url,
        ]
        proc = subprocess.run(command, input=curl_config, text=True, capture_output=True, check=False)
        raw, status_text = proc.stdout.rsplit("\n__HTTP_STATUS__:", 1) if "\n__HTTP_STATUS__:" in proc.stdout else (proc.stdout, "")
        status = int(status_text) if status_text.isdigit() else None
        if proc.returncode and not raw:
            raw = json.dumps({"client_error": proc.stderr.strip(), "curl_exit": proc.returncode}, ensure_ascii=False)
        try:
            parsed: dict[str, Any] | str = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        return status, round((time.perf_counter() - start) * 1000, 1), parsed
    finally:
        if payload_path:
            try:
                Path(payload_path).unlink()
            except FileNotFoundError:
                pass


def response_parts(response: dict[str, Any] | str) -> tuple[str, str, dict[str, Any], str, str]:
    if not isinstance(response, dict):
        return "", "", {}, "", str(response)[:500]
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    usage = response.get("usage") or {}
    request_id = response.get("id") or ""
    error = response.get("error") or {}
    if isinstance(error, dict):
        error_text = json.dumps(error, ensure_ascii=False)
    else:
        error_text = str(error or "")
    return content, reasoning, usage, request_id, error_text


def evaluate(case: Case, status: int | None, content: str, reasoning: str, usage: dict[str, Any]) -> tuple[str, str]:
    reasons = []
    if status != case.expected_http:
        reasons.append(f"HTTP 期望 {case.expected_http}，实际 {status}")
    if status == 200 and case.expected_http == 200 and not content.strip():
        reasons.append("HTTP 200 但正文为空")
    reasoning_tokens = ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                        or usage.get("reasoning_tokens")
                        or 0)
    has_reasoning = bool(reasoning.strip()) or bool(reasoning_tokens)
    if case.require_no_reasoning and has_reasoning:
        reasons.append(f"期望无思考内容，但 reasoning_chars={len(reasoning)} reasoning_tokens={reasoning_tokens}")
    if case.require_reasoning and status == 200 and not has_reasoning:
        reasons.append("期望有思考内容或 reasoning_tokens，但未看到")
    if reasons:
        return "fail", "；".join(reasons)
    return "pass", "符合预期"


def main() -> None:
    parser = argparse.ArgumentParser(description="复测 thinking/temperature 参数兼容性")
    parser.add_argument("--base-url", default=os.getenv("API_BASE", "http://127.0.0.1:8000/v1"))
    parser.add_argument("--key-env", default=os.getenv("API_KEY_ENV", "API_KEY"))
    parser.add_argument("--model", default=os.getenv("MODEL", "test-model"))
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--user-agent", default="curl/8.7.1")
    parser.add_argument("--cases", default="all", help="逗号分隔 case_id；默认 all")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--confirm-paid", action="store_true")
    args = parser.parse_args()

    selected = CASES if args.cases == "all" else [case for case in CASES if case.case_id in {x.strip() for x in args.cases.split(",") if x.strip()}]
    if not selected:
        parser.error("没有匹配的 case_id")
    print("计划请求：")
    for case in selected:
        print(f"- {case.case_id}: {case.description}")
    if not (args.run and args.confirm_paid):
        print("仅预览；实际请求需同时添加 --run --confirm-paid")
        return
    if not args.base_url.startswith(("http://", "https://")):
        parser.error("Base URL 必须使用 http:// 或 https://")
    api_key = os.environ.get(args.key_env, "")
    if not api_key:
        parser.error(f"未设置环境变量 {args.key_env}")

    result_root = Path(os.environ.get("TEST_RESULT_ROOT", str(ROOT / "results")))
    out = (args.output_dir.resolve() if args.output_dir else
           result_root / datetime.now().strftime("%Y%m%d-%H%M%S-thinking-temperature-regression"))
    out.mkdir(parents=True, exist_ok=False)
    rows = []
    for idx, case in enumerate(selected, 1):
        request_body = base_body(args.model)
        request_body.update(case.body_extra)
        status, elapsed_ms, response = send(args.base_url, api_key, request_body, args.timeout, args.user_agent)
        content, reasoning, usage, request_id, error_text = response_parts(response)
        result, reason = evaluate(case, status, content, reasoning, usage)
        raw_file = f"{idx:03d}-{case.case_id}.json"
        (out / raw_file).write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request": request_body,
            "http_status": status,
            "elapsed_ms": elapsed_ms,
            "response": response,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        blocked_statuses = {None, 401, 403, 429, 502, 503, 504}
        issue_still_present = result == "fail" and bool(case.problem_when_failed) and status not in blocked_statuses
        blocked = result == "fail" and status in blocked_statuses
        row = {
            "case_id": case.case_id,
            "category": case.category,
            "description": case.description,
            "http_status": status,
            "expected_http": case.expected_http,
            "result": result,
            "blocked": blocked,
            "issue_still_present": issue_still_present,
            "reason": reason,
            "problem_when_failed": case.problem_when_failed if issue_still_present else "",
            "content_chars": len(content),
            "reasoning_chars": len(reasoning),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", usage.get("reasoning_tokens", "")),
            "request_id": request_id,
            "error": error_text[:600],
            "elapsed_ms": elapsed_ms,
            "raw_file": raw_file,
        }
        rows.append(row)
        marker = "阻塞" if blocked else "仍有问题" if issue_still_present else result.upper()
        print(f"{case.case_id} | HTTP={status} | {marker} | {reason}", flush=True)

    with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "model": args.model,
        "base_url": args.base_url,
        "requests": len(rows),
        "passed": sum(row["result"] == "pass" for row in rows),
        "failed": sum(row["result"] == "fail" for row in rows),
        "issues_still_present": [row for row in rows if row["issue_still_present"]],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果目录：{out}")


if __name__ == "__main__":
    main()

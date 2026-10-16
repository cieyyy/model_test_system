"""阶段 3：测试执行器与证据保存。

- 按 TestProject 分类自动生成标准测试请求（chat / stream / json / tools）。
- 通过 httpx 原生请求执行（保留真实请求头/体用于取证）。
- 证据目录按 TRD 结构写入，写入后不可被报告编辑覆盖。
- 判定遵循「HTTP 200 不等于测试通过」：识别静默失效。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx

from . import security
from .db import DATA_DIR, ModelEntity, Provider, TestProject

RUNS_DIR = DATA_DIR / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

REQUEST_TIMEOUT = 60.0

_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名"}},
                "required": ["city"],
            },
        },
    }
]


class ExecutionError(Exception):
    def __init__(self, error_class: str, message: str, http_status: int = 0):
        super().__init__(message)
        self.error_class = error_class
        self.http_status = http_status


def normalize_category(project: TestProject) -> str:
    """把项目分类映射到可执行的测试类型。"""
    c = (project.category or "chat").strip().lower()
    if c in ("stream", "json", "tools"):
        return c
    return "chat"


def build_request_body(project: TestProject, model_name: str) -> dict:
    """按分类生成标准测试请求体。"""
    category = normalize_category(project)
    body: dict[str, Any] = {"model": model_name}
    if category == "stream":
        body["messages"] = [{"role": "user", "content": "请用一句话介绍你自己。"}]
        body["stream"] = True
        body["max_tokens"] = 100
    elif category == "json":
        body["messages"] = [{
            "role": "user",
            "content": '请以 JSON 格式返回：{"name": "模型测试", "ok": true}。只输出 JSON，不要输出其他内容。',
        }]
        body["response_format"] = {"type": "json_object"}
        body["max_tokens"] = 200
    elif category == "tools":
        body["messages"] = [{"role": "user", "content": "北京今天天气怎么样？请调用工具查询。"}]
        body["tools"] = _TOOLS_SCHEMA
        body["tool_choice"] = "auto"
        body["max_tokens"] = 300
    else:  # chat
        body["messages"] = [{"role": "user", "content": "请用一句话介绍你自己。"}]
        body["max_tokens"] = 100
    return body


def build_request_plan(project: TestProject, provider: Provider, model_name: str) -> dict:
    """构建请求计划（预览模式使用，Header 已脱敏）。"""
    return {
        "project_id": project.id,
        "project_name": project.name,
        "category": normalize_category(project),
        "method": "POST",
        "url": provider.base_url.rstrip("/") + "/chat/completions",
        "headers": {
            "Authorization": "Bearer <已加密的 API Key，发送时自动注入>",
            "Content-Type": "application/json",
        },
        "body": build_request_body(project, model_name),
        "note": "预览模式：不发送请求。真实执行时使用相同请求体。",
    }


def _mask_headers(headers: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in headers.items():
        if k.lower() in ("authorization", "x-api-key", "api-key") and isinstance(v, str):
            parts = v.split(" ", 1)
            out[k] = (parts[0] + " " + security.mask_key(parts[1])) if len(parts) == 2 else security.mask_key(v)
        else:
            out[k] = v
    return out


def _write_evidence_file(path: Path, data: Any) -> None:
    if isinstance(data, (dict, list)):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        path.write_text(str(data), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_json_content(content: str | None) -> Any:
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if text.count("```") >= 2 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def execute_test(
    project: TestProject,
    provider: Provider,
    model_name: str,
    api_key: str,
    evidence_dir: Path,
    cancel_event=None,
) -> dict:
    """执行单个测试项目并写入证据目录。返回运行摘要。"""
    category = normalize_category(project)
    url = provider.base_url.rstrip("/") + "/chat/completions"
    body = build_request_body(project, model_name)
    is_stream = category == "stream"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "model-test-platform/0.3",
    }

    timing: dict[str, Any] = {"request_start": dt.datetime.now().isoformat(timespec="seconds")}
    started = time.perf_counter()
    ttft_ms = 0
    stream_events: list[dict] = []
    response_headers: dict = {}
    response_body: Any = None
    http_status = 0

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            if is_stream:
                with client.stream("POST", url, headers=headers, json=body) as resp:
                    http_status = resp.status_code
                    response_headers = dict(resp.headers)
                    if resp.status_code != 200:
                        response_body = resp.read().decode("utf-8", "replace")
                    else:
                        first_content = True
                        buffer: dict[str, str] = {}
                        finish_reason = None
                        for line in resp.iter_lines():
                            if cancel_event is not None and cancel_event.is_set():
                                raise ExecutionError("cancelled", "执行已被用户取消", http_status)
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                stream_events.append({"t_ms": elapsed_ms(), "event": "done"})
                                break
                            try:
                                chunk = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            choice = (chunk.get("choices") or [{}])[0]
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                if first_content:
                                    ttft_ms = elapsed_ms()
                                    first_content = False
                                buffer["content"] = buffer.get("content", "") + delta["content"]
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                            stream_events.append({
                                "t_ms": elapsed_ms(),
                                "event": "chunk",
                                "delta": delta.get("content") or "",
                            })
                        response_body = {
                            "model": model_name,
                            "content": buffer.get("content", ""),
                            "finish_reason": finish_reason,
                            "event_count": len(stream_events),
                        }
            else:
                resp = client.post(url, headers=headers, json=body)
                http_status = resp.status_code
                response_headers = dict(resp.headers)
                try:
                    response_body = resp.json()
                except (json.JSONDecodeError, ValueError):
                    response_body = resp.text
    except ExecutionError:
        raise
    except httpx.TimeoutException:
        raise ExecutionError("timeout", f"请求超时（>{REQUEST_TIMEOUT:.0f}s）")
    except httpx.ConnectError as e:
        raise ExecutionError("connect_error", f"连接失败：{e}")
    except httpx.HTTPError as e:
        raise ExecutionError("network_error", str(e))

    duration_ms = elapsed_ms()

    # ---- 判定：HTTP 200 不等于通过 ----
    error_class = ""
    verdict = ""
    passed = False

    if http_status != 200:
        error_class = {401: "auth_error", 403: "auth_error", 429: "rate_limit"}.get(
            http_status, "server_error" if http_status >= 500 else "http_error"
        )
        verdict = f"HTTP {http_status}"
    elif category == "chat":
        msg = ((response_body or {}).get("choices") or [{}])[0].get("message", {}) if isinstance(response_body, dict) else {}
        content = msg.get("content")
        passed = bool(content and content.strip())
        verdict = "返回了非空内容" if passed else "假成功：HTTP 200 但内容为空"
        if not passed:
            error_class = "silent_failure"
    elif category == "stream":
        if isinstance(response_body, dict) and ttft_ms:
            passed = bool(response_body.get("content"))
            verdict = f"流式正常，首 Token {ttft_ms}ms" if passed else "假成功：流式无内容"
        else:
            verdict = "假成功：响应非流式（可能被网关整包缓冲）"
        if not passed:
            error_class = "silent_failure"
    elif category == "json":
        msg = ((response_body or {}).get("choices") or [{}])[0].get("message", {}) if isinstance(response_body, dict) else {}
        parsed = _parse_json_content(msg.get("content"))
        passed = parsed is not None
        verdict = "返回了有效 JSON" if passed else "假成功：HTTP 200 但内容不是有效 JSON"
        if not passed:
            error_class = "silent_failure"
    elif category == "tools":
        msg = ((response_body or {}).get("choices") or [{}])[0].get("message", {}) if isinstance(response_body, dict) else {}
        tool_calls = msg.get("tool_calls")
        passed = bool(tool_calls)
        verdict = "返回了 tool_calls" if passed else "假成功：HTTP 200 但未返回 tool_calls"
        if not passed:
            error_class = "silent_failure"

    # ---- 证据落盘 ----
    evidence_dir.mkdir(parents=True, exist_ok=True)
    usage = (response_body or {}).get("usage") if isinstance(response_body, dict) else None
    _write_evidence_file(evidence_dir / "request.json", body)
    _write_evidence_file(evidence_dir / "request_headers.json", _mask_headers(headers))
    _write_evidence_file(evidence_dir / "response_headers.json", response_headers)
    _write_evidence_file(evidence_dir / "response.json", response_body)
    if is_stream and stream_events:
        with (evidence_dir / "stream.ndjson").open("w", encoding="utf-8") as f:
            for ev in stream_events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    _write_evidence_file(evidence_dir / "timing.json", {
        **timing,
        "duration_ms": duration_ms,
        "ttft_ms": ttft_ms,
    })

    summary = {
        "category": category,
        "http_status": http_status,
        "passed": passed,
        "duration_ms": duration_ms,
        "ttft_ms": ttft_ms,
        "error_class": error_class,
        "verdict": verdict,
        "usage": usage,
        "stream_event_count": len(stream_events),
    }
    return summary


def write_manifest(evidence_dir: Path, meta: dict) -> None:
    files = []
    for f in sorted(evidence_dir.iterdir()):
        if f.is_file():
            files.append({"name": f.name, "bytes": f.stat().st_size, "sha256": _sha256(f)})
    _write_evidence_file(evidence_dir / "manifest.json", {**meta, "files": files})

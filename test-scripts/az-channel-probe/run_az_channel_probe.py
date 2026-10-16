#!/usr/bin/env python3
"""AZ channel, model coverage, and concurrency probe for OpenAI-compatible APIs.

Default mode is a dry run. Real requests require both --execute and
--confirm-paid. API keys are read only from the configured environment variable
and are never written to result files.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_IMAGE_EXCLUDE = (
    r"(?i)(^|[-_.:/ ])("
    r"image|gpt-image|dall[-_ ]?e|sora|flux|stable[-_ ]?diffusion|sdxl|"
    r"midjourney|mj-|imagen|recraft|ideogram|kolors|wanx|seedream|doubao-seedream"
    r")($|[-_.:/ ])"
)
DEFAULT_AZ_HEADER_PATTERNS = (
    "x-ms-",
    "apim-request-id",
    "x-azure-",
    "azureml-",
    "ms-azure-",
    "azureai-",
)
AZ_BODY_MARKERS = (
    "azure",
    "openai|官转|az",
    "group az",
    "apim",
    "deploymentnotfound",
    "content_filter",
    "responsibleaipolicyviolation",
)


@dataclass
class Exchange:
    method: str
    path: str
    status: int | None
    elapsed_ms: float
    headers: dict[str, str]
    body: Any
    raw_preview: str
    error: str


def percentile(values: list[float], p: float) -> float | None:
    values = sorted(float(x) for x in values)
    if not values:
        return None
    pos = (len(values) - 1) * p / 100
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (pos - lo), 2)


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_int_csv(value: str, low: int, high: int, name: str) -> list[int]:
    try:
        result = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise SystemExit(f"{name} 必须是逗号分隔的整数") from exc
    if not result or any(item < low or item > high for item in result):
        raise SystemExit(f"{name} 的每一项必须在 {low}..{high} 范围内")
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AZ 渠道、全模型文本调用和并发能力探测")
    p.add_argument("--config", type=Path, help="JSON 配置文件，默认自动读取同目录 config.local.json")
    p.add_argument("--list", action="store_true", help="列出测试项，不发送请求")
    p.add_argument("--base-url", help="OpenAI 兼容 API 根地址，例如 https://example.com/v1")
    p.add_argument("--key-env", help="存放 API Key 的环境变量名")
    p.add_argument("--auth-header", choices=("authorization", "api-key", "both"), help="鉴权头类型")
    p.add_argument("--models", help="逗号分隔模型 ID；使用 all 表示从 /models 自动获取")
    p.add_argument("--models-file", type=Path, help="模型 ID 文件；支持一行一个、逗号分隔或 JSON 数组")
    p.add_argument("--include-regex", help="只测试匹配该正则的模型 ID")
    p.add_argument("--exclude-regex", help="排除匹配该正则的模型 ID，默认排除图片模型")
    p.add_argument("--model-limit", type=int, help="最多测试前 N 个模型，便于冒烟")
    p.add_argument("--cases", help="az,models,concurrency 或 all")
    p.add_argument("--endpoint", choices=("chat", "responses"), help="模型支持测试使用的接口")
    p.add_argument("--token-param", choices=("max_tokens", "max_completion_tokens", "both"), help="输出 token 参数")
    p.add_argument("--timeout", type=int, dest="timeout_seconds", help="单请求超时秒数")
    p.add_argument("--max-output-tokens", type=int, help="短答最大输出 token")
    p.add_argument("--temperature", type=float, help="采样温度")
    p.add_argument("--concurrency-levels", help="并发档位，例如 1,5,10")
    p.add_argument("--concurrency-requests", type=int, help="每个并发档位的请求倍数，实际请求数=档位*倍数")
    p.add_argument("--concurrency-models", help="并发测试模型：auto、all 或逗号分隔模型 ID")
    p.add_argument("--max-requests", type=int, default=300, help="本次运行真实请求硬上限，不含 /models")
    p.add_argument("--output-dir", type=Path, help="结果输出目录")
    p.add_argument("--execute", action="store_true", help="允许发送真实请求")
    p.add_argument("--confirm-paid", action="store_true", help="确认请求可能产生费用")
    return p


DEFAULTS: dict[str, Any] = {
    "base_url": "https://YOUR_PROVIDER/v1",
    "api_key_env": "AZ_CHANNEL_API_KEY",
    "auth_header": "authorization",
    "models": "all",
    "include_regex": "",
    "exclude_regex": DEFAULT_IMAGE_EXCLUDE,
    "model_limit": 0,
    "cases": ["az", "models", "concurrency"],
    "endpoint": "chat",
    "token_param": "max_tokens",
    "timeout_seconds": 90,
    "max_output_tokens": 32,
    "temperature": None,
    "concurrency_levels": [1, 5],
    "concurrency_requests": 2,
    "concurrency_models": "all",
}


def load_config(args: argparse.Namespace) -> dict[str, Any]:
    config = dict(DEFAULTS)
    config_path = args.config
    if config_path is None:
        candidate = ROOT / "config.local.json"
        config_path = candidate if candidate.exists() else None
    if config_path:
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SystemExit(f"配置文件不存在：{config_path}") from exc
        except json.JSONDecodeError as exc:
            raise SystemExit(f"配置文件不是有效 JSON：{config_path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise SystemExit("配置文件顶层必须是 JSON 对象")
        config.update({key: value for key, value in loaded.items() if key in DEFAULTS})

    cli_map = {
        "base_url": args.base_url,
        "api_key_env": args.key_env,
        "auth_header": args.auth_header,
        "models": args.models,
        "include_regex": args.include_regex,
        "exclude_regex": args.exclude_regex,
        "model_limit": args.model_limit,
        "cases": args.cases,
        "endpoint": args.endpoint,
        "token_param": args.token_param,
        "timeout_seconds": args.timeout_seconds,
        "max_output_tokens": args.max_output_tokens,
        "temperature": args.temperature,
        "concurrency_levels": args.concurrency_levels,
        "concurrency_requests": args.concurrency_requests,
        "concurrency_models": args.concurrency_models,
    }
    for key, value in cli_map.items():
        if value is not None:
            config[key] = value

    config["base_url"] = os.getenv("AZ_CHANNEL_BASE_URL", str(config["base_url"]))
    config["models"] = os.getenv("AZ_CHANNEL_MODELS", config["models"])
    config["cases"] = ["az", "models", "concurrency"] if config["cases"] == "all" else config["cases"]
    config["cases"] = parse_csv(config["cases"]) if isinstance(config["cases"], str) else config["cases"]
    config["concurrency_levels"] = (
        parse_int_csv(config["concurrency_levels"], 1, 128, "concurrency_levels")
        if isinstance(config["concurrency_levels"], str)
        else config["concurrency_levels"]
    )
    validate_config(config)
    return config


def load_models_file(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise SystemExit(f"模型文件不存在：{path}") from exc
    stripped = text.strip()
    if not stripped:
        raise SystemExit(f"模型文件为空：{path}")
    if stripped.startswith("["):
        try:
            loaded = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"模型文件不是有效 JSON 数组：{path}: {exc}") from exc
        if not isinstance(loaded, list) or not all(isinstance(item, str) and item.strip() for item in loaded):
            raise SystemExit("模型文件 JSON 数组必须只包含非空字符串")
        return [item.strip() for item in loaded]
    models: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        models.extend(parse_csv(line))
    if not models:
        raise SystemExit(f"模型文件没有可用模型 ID：{path}")
    return sorted(dict.fromkeys(models))


def validate_config(config: dict[str, Any]) -> None:
    base_url = str(config["base_url"])
    if not base_url.startswith(("http://", "https://")):
        raise SystemExit("base_url 必须为 http:// 或 https:// 地址")
    bad_cases = [case for case in config["cases"] if case not in {"az", "models", "concurrency"}]
    if bad_cases:
        raise SystemExit(f"未知测试项：{','.join(bad_cases)}；可选：az,models,concurrency")
    for key in ("timeout_seconds", "max_output_tokens", "concurrency_requests"):
        if int(config[key]) < 1:
            raise SystemExit(f"{key} 必须大于 0")
    if int(config["timeout_seconds"]) > 600:
        raise SystemExit("timeout_seconds 不能超过 600")
    if int(config["max_output_tokens"]) > 1024:
        raise SystemExit("max_output_tokens 不能超过 1024")
    if config["temperature"] is not None and (
        float(config["temperature"]) < 0 or float(config["temperature"]) > 2
    ):
        raise SystemExit("temperature 必须在 0..2 范围内")
    if int(config.get("model_limit") or 0) < 0:
        raise SystemExit("model_limit 不能小于 0")
    if not config["concurrency_levels"] or any(int(x) < 1 or int(x) > 128 for x in config["concurrency_levels"]):
        raise SystemExit("concurrency_levels 必须是 1..128 的整数列表")
    for regex_key in ("include_regex", "exclude_regex"):
        value = str(config.get(regex_key) or "")
        if value:
            try:
                re.compile(value)
            except re.error as exc:
                raise SystemExit(f"{regex_key} 不是有效正则：{exc}") from exc


def auth_headers(config: dict[str, Any], key: str) -> dict[str, str]:
    mode = str(config["auth_header"])
    headers = {"Accept": "application/json"}
    if mode in ("authorization", "both"):
        headers["Authorization"] = "Bearer " + key
    if mode in ("api-key", "both"):
        headers["api-key"] = key
    return headers


def parse_json_or_text(raw: bytes) -> tuple[Any, str]:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text), text[:4000]
    except json.JSONDecodeError:
        return {"unparsed_response": text[:4000]}, text[:4000]


def request_json(config: dict[str, Any], key: str, method: str, path: str, payload: dict[str, Any] | None) -> Exchange:
    url = str(config["base_url"]).rstrip("/") + path
    headers = auth_headers(config, key)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    status: int | None = None
    response_headers: dict[str, str] = {}
    body: Any = {}
    preview = ""
    error = ""
    try:
        with urllib.request.urlopen(req, timeout=int(config["timeout_seconds"])) as response:
            status = response.status
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            body, preview = parse_json_or_text(response.read())
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_headers = {k.lower(): v for k, v in exc.headers.items()}
        body, preview = parse_json_or_text(exc.read())
        error = extract_error(body) or f"HTTP {exc.code}"
    except Exception as exc:
        error = str(exc)
        body = {"client_error": error}
        preview = error[:4000]
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    return Exchange(method, path, status, elapsed_ms, response_headers, body, preview, error)


def extract_error(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, dict):
        pieces = [str(err.get(k) or "") for k in ("code", "type", "message")]
        return " | ".join(piece for piece in pieces if piece)[:1000]
    if isinstance(err, str):
        return err[:1000]
    return str(body.get("client_error") or "")[:1000]


def extract_models(body: Any) -> list[str]:
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    if not isinstance(data, list):
        return []
    result = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            result.append(item["id"])
        elif isinstance(item, str):
            result.append(item)
    return sorted(dict.fromkeys(result))


def wanted_models(config: dict[str, Any], listed: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    configured = str(config["models"])
    models = listed if configured.lower() == "all" else parse_csv(configured)
    include = re.compile(str(config.get("include_regex") or ".*"))
    exclude_text = str(config.get("exclude_regex") or "")
    exclude = re.compile(exclude_text) if exclude_text else None
    selected: list[str] = []
    skipped: list[dict[str, str]] = []
    for model in models:
        if not include.search(model):
            skipped.append({"model": model, "reason": "include_regex_not_matched"})
            continue
        if exclude and exclude.search(model):
            skipped.append({"model": model, "reason": "excluded_by_image_regex"})
            continue
        selected.append(model)
    limit = int(config.get("model_limit") or 0)
    if limit:
        skipped.extend({"model": model, "reason": "model_limit"} for model in selected[limit:])
        selected = selected[:limit]
    return selected, skipped


def marker_for(model: str, index: int = 0) -> str:
    digest = hashlib.sha1(f"{model}:{index}".encode("utf-8")).hexdigest()[:10].upper()
    return "AZPROBE-" + digest


def build_payload(config: dict[str, Any], model: str, expected: str) -> tuple[str, dict[str, Any]]:
    prompt = f"只回复以下字符串，不要添加空格、标点或解释：{expected}"
    if config["endpoint"] == "responses":
        body: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "max_output_tokens": int(config["max_output_tokens"]),
        }
        if config["temperature"] is not None:
            body["temperature"] = float(config["temperature"])
        return "/responses", body
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if config["temperature"] is not None:
        body["temperature"] = float(config["temperature"])
    token_param = str(config["token_param"])
    if token_param in ("max_tokens", "both"):
        body["max_tokens"] = int(config["max_output_tokens"])
    if token_param in ("max_completion_tokens", "both"):
        body["max_completion_tokens"] = int(config["max_output_tokens"])
    return "/chat/completions", body


def extract_text(config: dict[str, Any], body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    if config["endpoint"] == "responses":
        text = body.get("output_text")
        if isinstance(text, str):
            return text.strip()
        chunks: list[str] = []
        for item in body.get("output") or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    chunks.append(content["text"])
        return "".join(chunks).strip()
    choices = body.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        content = message.get("content")
        return content.strip() if isinstance(content, str) else ""
    return ""


def finish_reason(config: dict[str, Any], body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    if config["endpoint"] == "responses":
        return str(body.get("status") or body.get("incomplete_details", {}).get("reason") or "")
    choices = body.get("choices") or []
    if choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "")
    return ""


def response_model(body: Any) -> str:
    return str(body.get("model") or "") if isinstance(body, dict) else ""


def usage_tokens(body: Any) -> tuple[Any, Any, Any]:
    if not isinstance(body, dict):
        return "", "", ""
    usage = body.get("usage") or {}
    return (
        usage.get("prompt_tokens", usage.get("input_tokens", "")),
        usage.get("completion_tokens", usage.get("output_tokens", "")),
        usage.get("total_tokens", ""),
    )


def classify_support(status: int | None, body: Any, text: str, error: str) -> str:
    if status == 200 and isinstance(body, dict) and (
        body.get("choices") or body.get("output") or body.get("output_text")
    ):
        return "pass"
    low = error.lower()
    if (
        status in (400, 404, 503)
        and any(
            token in low
            for token in (
                "model_not_found",
                "no available channel",
                "deployment",
                "model",
                "not found",
                "does not exist",
                "unsupported",
            )
        )
    ):
        return "unsupported"
    if status == 200:
        return "fail"
    return "error"


def az_fingerprint(exchanges: list[Exchange]) -> dict[str, Any]:
    hits: list[dict[str, str]] = []
    marker_body_hits = 0
    for exchange in exchanges:
        for name, value in exchange.headers.items():
            lower_value = value.lower()
            if any(name.startswith(p) or name == p for p in DEFAULT_AZ_HEADER_PATTERNS) or "azure" in lower_value:
                hits.append({"path": exchange.path, "header": name, "value": value[:200]})
        preview = exchange.raw_preview.lower()
        if any(marker in preview for marker in AZ_BODY_MARKERS):
            marker_body_hits += 1
    header_names = {hit["header"] for hit in hits}
    if any(name.startswith("x-ms-") for name in header_names) or "apim-request-id" in header_names:
        confidence = "strong"
    elif hits:
        confidence = "medium"
    elif marker_body_hits:
        confidence = "weak"
    else:
        confidence = "none"
    return {
        "az_confidence": confidence,
        "header_hits": hits,
        "body_marker_hits": marker_body_hits,
        "note": "AZ 指纹基于响应头和错误形态推断；中转层可能隐藏或改写上游证据。",
    }


def evidence_record(kind: str, model: str, exchange: Exchange, request_body: dict[str, Any] | None) -> dict[str, Any]:
    safe_body = dict(request_body or {})
    if "messages" in safe_body:
        safe_body["messages"] = "[redacted prompt body]"
    if "input" in safe_body:
        safe_body["input"] = "[redacted prompt body]"
    return {
        "kind": kind,
        "model": model,
        "method": exchange.method,
        "path": exchange.path,
        "status": exchange.status,
        "elapsed_ms": exchange.elapsed_ms,
        "headers": exchange.headers,
        "request": safe_body,
        "response_preview": exchange.raw_preview,
        "error": exchange.error,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def probe_model(config: dict[str, Any], key: str, model: str, index: int = 0) -> tuple[dict[str, Any], Exchange, dict[str, Any]]:
    expected = marker_for(model, index)
    path, payload = build_payload(config, model, expected)
    exchange = request_json(config, key, "POST", path, payload)
    text = extract_text(config, exchange.body)
    error = extract_error(exchange.body) or exchange.error
    prompt_tokens, completion_tokens, total_tokens = usage_tokens(exchange.body)
    finish = finish_reason(config, exchange.body)
    verdict = classify_support(exchange.status, exchange.body, text, error)
    row = {
        "model": model,
        "endpoint": config["endpoint"],
        "http_status": exchange.status or "",
        "verdict": verdict,
        "strict_exact": text == expected,
        "finish_reason": finish,
        "response_quality": "exact" if text == expected else ("empty" if not text else "non_exact"),
        "expected": expected,
        "content_preview": text[:200],
        "response_model": response_model(exchange.body),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "elapsed_ms": exchange.elapsed_ms,
        "error": error,
    }
    return row, exchange, payload


def choose_concurrency_models(config: dict[str, Any], support_rows: list[dict[str, Any]], selected: list[str]) -> list[str]:
    value = str(config["concurrency_models"])
    if value.lower() == "all":
        return selected
    if value.lower() == "auto":
        passed = [row["model"] for row in support_rows if row["verdict"] == "pass"]
        return passed[:1]
    return [model for model in parse_csv(value) if model in selected or model]


def run_concurrency(config: dict[str, Any], key: str, models: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Exchange]]:
    rows: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    exchanges: list[Exchange] = []
    for model in models:
        for level in config["concurrency_levels"]:
            level = int(level)
            total = level * int(config["concurrency_requests"])
            print(f"[concurrency] model={model} level={level} requests={total}", flush=True)
            started_at = time.perf_counter()
            with ThreadPoolExecutor(max_workers=level) as pool:
                futures = [pool.submit(probe_model, config, key, model, i) for i in range(1, total + 1)]
                for future in as_completed(futures):
                    row, exchange, payload = future.result()
                    row.update({
                        "phase": "concurrency",
                        "planned_concurrency": level,
                        "actual_batch_requests": total,
                        "batch_elapsed_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    })
                    rows.append(row)
                    exchanges.append(exchange)
                    evidence.append(evidence_record("concurrency", model, exchange, payload))
                    print(
                        f"  #{len(rows)} HTTP={row['http_status'] or '-'} "
                        f"{row['verdict']} exact={row['strict_exact']} {row['elapsed_ms']}ms",
                        flush=True,
                    )
    return rows, evidence, exchanges


def planned_request_count(config: dict[str, Any], selected_count: int | None) -> str:
    if selected_count is None:
        return "未知：执行时先请求 /models 后计算"
    total = 0
    if "models" in config["cases"]:
        total += selected_count
    if "concurrency" in config["cases"]:
        levels_total = sum(int(level) * int(config["concurrency_requests"]) for level in config["concurrency_levels"])
        concurrency_models = str(config["concurrency_models"])
        if concurrency_models.lower() == "all":
            total += selected_count * levels_total
        else:
            total += levels_total
    return str(total)


def print_plan(config: dict[str, Any], selected_count: int | None, max_requests: int) -> None:
    print("测试计划")
    print(f"  base_url: {config['base_url']}")
    print(f"  key_env: {config['api_key_env']}")
    print(f"  auth_header: {config['auth_header']}")
    print(f"  cases: {','.join(config['cases'])}")
    print(f"  models: {config['models']}")
    print(f"  endpoint: {config['endpoint']}")
    print(f"  exclude_regex: {config.get('exclude_regex') or '(none)'}")
    print(f"  concurrency_levels: {','.join(str(x) for x in config['concurrency_levels'])}")
    print(f"  concurrency_models: {config['concurrency_models']}")
    print(f"  planned_requests_without_model_list: {planned_request_count(config, selected_count)}")
    print(f"  max_requests: {max_requests}")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def summarize_concurrency(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row.get("model") or ""), int(row.get("planned_concurrency") or 0))
        groups.setdefault(key, []).append(row)
    summaries = []
    for (model, level), group in sorted(groups.items()):
        available = [row for row in group if row.get("verdict") == "pass"]
        exact = [row for row in available if row.get("strict_exact")]
        latencies = [float(row["elapsed_ms"]) for row in available if row.get("elapsed_ms") not in ("", None)]
        summaries.append({
            "model": model,
            "planned_concurrency": level,
            "attempted": len(group),
            "available": len(available),
            "exact": len(exact),
            "availability_rate_pct": round(len(available) / len(group) * 100, 2) if group else None,
            "exact_rate_pct": round(len(exact) / len(group) * 100, 2) if group else None,
            "latency_ms": {
                "p50": percentile(latencies, 50),
                "p90": percentile(latencies, 90),
                "p95": percentile(latencies, 95),
                "p99": percentile(latencies, 99),
            },
            "http_status_counts": {
                str(status): sum(str(row.get("http_status") or "") == str(status) for row in group)
                for status in sorted({row.get("http_status") or "" for row in group}, key=str)
            },
        })
    return summaries


def ensure_under_limit(config: dict[str, Any], request_count: int, max_requests: int) -> None:
    if request_count > max_requests:
        raise SystemExit(f"计划真实请求数 {request_count} 超过 --max-requests {max_requests}，请缩小模型/并发范围或调高上限")


def main() -> None:
    args = parser().parse_args()
    config = load_config(args)
    if args.models_file:
        config["models"] = ",".join(load_models_file(args.models_file))
    if args.list:
        print("可选测试项：")
        print("  az           拉取 /models 并收集 AZ/Azure 响应头指纹")
        print("  models       逐个模型发短文本请求，判断是否支持文本 chat/responses 调用")
        print("  concurrency  按并发档位发短文本请求，统计成功率和延迟")
        return
    configured_models = None if str(config["models"]).lower() == "all" else parse_csv(str(config["models"]))
    print_plan(config, len(configured_models) if configured_models is not None else None, args.max_requests)
    if not (args.execute and args.confirm_paid):
        print("仅预览；真实请求需同时添加 --execute --confirm-paid")
        return

    key = os.environ.get(str(config["api_key_env"]) or "")
    if not key:
        raise SystemExit(f"未设置环境变量 {config['api_key_env']}")

    out = args.output_dir.resolve() if args.output_dir else ROOT / "results" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-az-channel-probe")
    out.mkdir(parents=True, exist_ok=bool(args.output_dir))
    evidence_path = out / "evidence.jsonl"

    exchanges: list[Exchange] = []
    evidence: list[dict[str, Any]] = []
    listed_models: list[str] = []
    if str(config["models"]).lower() == "all" or "az" in config["cases"]:
        exchange = request_json(config, key, "GET", "/models", None)
        exchanges.append(exchange)
        evidence.append(evidence_record("models_list", "", exchange, None))
        listed_models = extract_models(exchange.body)
        model_list_error = extract_error(exchange.body) or exchange.error
        print(f"[/models] HTTP={exchange.status or '-'} models={len(listed_models)} {exchange.elapsed_ms}ms")
        if model_list_error:
            print(f"[/models] error={model_list_error[:500]}")
        if str(config["models"]).lower() == "all" and not listed_models:
            write_jsonl(evidence_path, evidence)
            failure_summary = {
                "base_url": config["base_url"],
                "cases": config["cases"],
                "listed_models": 0,
                "model_list_status": exchange.status,
                "model_list_error": model_list_error,
                "az_fingerprint": az_fingerprint(exchanges),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "next_step": "该接口未返回可解析 /models 清单；请用 --models 或 --models-file 指定模型后重跑，可同时用 --cases models,concurrency 跳过 /models。",
            }
            (out / "summary.json").write_text(json.dumps(failure_summary, ensure_ascii=False, indent=2), encoding="utf-8")
            raise SystemExit(f"未能从 /models 提取模型列表；证据已保存到 {out}；可用 --models 或 --models-file 手动指定模型 ID")
    selected, skipped = wanted_models(config, listed_models)
    print(f"选中模型数={len(selected)}；过滤/跳过={len(skipped)}")

    request_count = 0
    if "models" in config["cases"]:
        request_count += len(selected)
    concurrency_models_for_limit = selected if str(config["concurrency_models"]).lower() == "all" else (selected[:1] if selected else [])
    if "concurrency" in config["cases"]:
        request_count += len(concurrency_models_for_limit) * sum(int(level) * int(config["concurrency_requests"]) for level in config["concurrency_levels"])
    ensure_under_limit(config, request_count, args.max_requests)

    support_rows: list[dict[str, Any]] = []
    if "models" in config["cases"]:
        for index, model in enumerate(selected, 1):
            row, exchange, payload = probe_model(config, key, model)
            support_rows.append(row)
            exchanges.append(exchange)
            evidence.append(evidence_record("model_probe", model, exchange, payload))
            print(
                f"[{index}/{len(selected)}] {model} HTTP={row['http_status'] or '-'} "
                f"{row['verdict']} exact={row['strict_exact']} {row['elapsed_ms']}ms",
                flush=True,
            )
    else:
        support_rows = [{"model": model, "verdict": "not_run"} for model in selected]

    concurrency_rows: list[dict[str, Any]] = []
    if "concurrency" in config["cases"] and selected:
        concurrency_models = choose_concurrency_models(config, support_rows, selected)
        if concurrency_models:
            concurrency_rows, conc_evidence, conc_exchanges = run_concurrency(config, key, concurrency_models)
            evidence.extend(conc_evidence)
            exchanges.extend(conc_exchanges)
        else:
            print("[concurrency] auto 模式未发现基础调用通过的模型，跳过并发请求；如需强制重试请设置 --concurrency-models MODEL_ID")

    write_jsonl(evidence_path, evidence)
    write_csv(out / "model_results.csv", support_rows)
    write_csv(out / "concurrency_results.csv", concurrency_rows)
    write_csv(out / "skipped_models.csv", skipped)

    passed = [row for row in support_rows if row.get("verdict") == "pass"]
    exact_passed = [row for row in passed if row.get("strict_exact")]
    conc_good = [row for row in concurrency_rows if row.get("verdict") == "pass"]
    conc_latencies = [float(row["elapsed_ms"]) for row in conc_good if row.get("elapsed_ms") not in ("", None)]
    summary = {
        "base_url": config["base_url"],
        "cases": config["cases"],
        "endpoint": config["endpoint"],
        "listed_models": len(listed_models),
        "selected_models": len(selected),
        "skipped_models": len(skipped),
        "model_support": {
            "attempted": len(support_rows),
            "pass": len(passed),
            "exact_pass": len(exact_passed),
            "non_exact_or_truncated": len(passed) - len(exact_passed),
            "unsupported": sum(row.get("verdict") == "unsupported" for row in support_rows),
            "fail": sum(row.get("verdict") == "fail" for row in support_rows),
            "error": sum(row.get("verdict") == "error" for row in support_rows),
            "support_rate_pct": round(len(passed) / len(support_rows) * 100, 2) if support_rows else None,
        },
        "concurrency": {
            "attempted": len(concurrency_rows),
            "pass": len(conc_good),
            "success_rate_pct": round(len(conc_good) / len(concurrency_rows) * 100, 2) if concurrency_rows else None,
            "latency_ms": {
                "p50": percentile(conc_latencies, 50),
                "p90": percentile(conc_latencies, 90),
                "p95": percentile(conc_latencies, 95),
                "p99": percentile(conc_latencies, 99),
            },
            "http_status_counts": {
                str(status): sum(str(row.get("http_status") or "") == str(status) for row in concurrency_rows)
                for status in sorted({row.get("http_status") or "" for row in concurrency_rows}, key=str)
            },
            "by_model_and_level": summarize_concurrency(concurrency_rows),
        },
        "az_fingerprint": az_fingerprint(exchanges),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"结果目录：{out}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("用户中断", file=sys.stderr)
        raise SystemExit(130)

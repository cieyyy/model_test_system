#!/usr/bin/env python3
"""独立 OpenAI Chat Completions 验收；默认预览，不发送请求。"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import platform
import re
import sys
import time
from collections import Counter
from functools import lru_cache
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = "1.3.1"
ROOT = Path(__file__).resolve().parent
CASES = {
    "diagnostic": ("接口与媒体诊断", "用独立小请求检查文本、图片、视频及混合媒体格式，并保存服务端错误原因"),
    "body_100mb": ("100MB 请求正文", "验证网关和服务接受实际 100MB JSON 正文并返回有效答案"),
    "context_1m": ("百万 Token 上下文稳定性", "重复验证百万输入 Token 的检索正确性、成功率和协议完整性"),
    "real_traffic": ("真实混合流量吞吐与延迟", "同时验证真实业务缓存、图片视频混合流量、1000万 TPM 及 TTFT 分位数"),
    "synthetic_traffic": ("公开素材混合负载", "验证公开素材构造的图片视频混合负载性能，不替代真实业务缓存验收"),
    "deployment": ("FP8 与 MoE 权重方案", "核验部署量化和 MoE 权重配置证据"),
}
DEFAULT = dict(base_url="http://127.0.0.1:8000/v1", model="test-model",
               provider="unspecified", api_key_env="MODEL_TEST_API_KEY",
               cases=["body_100mb", "context_1m", "real_traffic", "deployment"], concurrency=128, duration_seconds=180,
               timeout_seconds=600, max_requests=1000, max_output_tokens=512,
               context_repeats=10, body_repeats=3, body_bytes=100_000_000,
               target_context_tokens=1_000_000, target_tpm=10_000_000,
               tpm_basis="input", cache_basis="token", min_success_rate=0.99,
               min_samples=100, p50_ms=3000, p90_ms=5000,
               context_max_tokens=1_010_000, traffic_file=None,
               context_file=None, body_file=None, deployment_file=None,
               max_body_bytes=120_000_000, max_response_bytes=10_000_000,
               asset_dir=None, materials_manifest=None, media_transport="inline", body_mode="transport")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def number(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def percentile(values, q):
    if not values:
        return None
    a = sorted(values)
    x = (len(a) - 1) * q
    lo, hi = math.floor(x), math.ceil(x)
    if math.isinf(a[hi]):
        return None  # 失败按无限延迟计；JSON 使用 null，另记未达标
    return a[lo] + (a[hi] - a[lo]) * (x - lo)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def settings(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    public_preset = ROOT / "config.public.json"
    p.add_argument("--config", type=Path, default=public_preset if public_preset.is_file() else None)
    p.add_argument("--list", action="store_true")
    for key in ("base_url", "model", "provider", "traffic_file", "context_file",
                "body_file", "deployment_file", "tpm_basis", "cache_basis", "asset_dir", "materials_manifest", "media_transport", "body_mode"):
        p.add_argument("--" + key.replace("_", "-"))
    p.add_argument("--key-env", "--api-key-env", dest="api_key_env",
                   help="保存密钥的环境变量名，例如 san_API_KEY；这里传变量名，不传密钥值")
    p.add_argument("--models", help="单模型脚本；只接受一个模型 ID")
    p.add_argument("--cases", help=",".join(CASES))
    integer_options = ("concurrency", "max_requests", "max_output_tokens", "context_repeats", "body_repeats",
                "body_bytes", "target_context_tokens", "context_max_tokens", "min_samples",
                "max_body_bytes", "max_response_bytes")
    for key in integer_options:
        p.add_argument("--" + key.replace("_", "-"), type=int)
    for key in ("duration_seconds", "timeout_seconds", "target_tpm", "min_success_rate", "p50_ms", "p90_ms"):
        p.add_argument("--" + key.replace("_", "-"), type=float)
    p.add_argument("--output-dir", type=Path)
    p.add_argument("--run", "--execute", dest="execute", action="store_true", help="发送真实请求；须同时提供 --confirm-paid")
    p.add_argument("--confirm-paid", action="store_true")
    p.add_argument("--diagnose", action="store_true", help="最多4个小请求，分别诊断文本、图片、视频、混合媒体，不运行容量压测")
    p.add_argument("--make-context", type=Path, help="离线生成百万 Token 检索 fixture，不发送请求；需要 tiktoken")
    p.add_argument("--prepare-materials", action="store_true", help="下载校验公开素材，生成全部样本和 config.public.json，不调用模型")
    p.add_argument("--encoding-name", default="o200k_base", help="生成器 tokenizer 编码名；须确认与被测模型匹配")
    a = p.parse_args(argv)
    c = DEFAULT.copy()
    if a.config:
        local = read_json(a.config)
        if not isinstance(local, dict) or set(local) - set(c):
            p.error("配置必须是对象，且只能包含 config.template.json 中的字段")
        c.update(local)
        for key in ("traffic_file", "context_file", "body_file", "deployment_file", "asset_dir", "materials_manifest"):
            if c[key]:
                c[key] = str((a.config.resolve().parent / c[key]).resolve())
    for key, default in DEFAULT.items():
        env = os.getenv("ACCEPTANCE_" + key.upper())
        if env is not None:
            if isinstance(default, (int, float)):
                try:
                    c[key] = int(env) if key in integer_options else float(env)
                except ValueError:
                    p.error(f"环境变量 ACCEPTANCE_{key.upper()} 类型错误")
            else:
                c[key] = env.split(",") if key == "cases" else env
        if getattr(a, key, None) is not None:
            c[key] = getattr(a, key)
    if isinstance(c["cases"], str):
        c["cases"] = c["cases"].split(",")
    if a.models:
        if "," in a.models:
            p.error("每次只测试一个模型")
        c["model"] = a.models
    if a.diagnose:
        c["cases"] = ["diagnostic"]
    if a.list:
        print(dumps(CASES))
        return a, c
    from urllib.parse import urlsplit
    u = urlsplit(c["base_url"])
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        p.error("base_url 必须是不含凭证、查询参数的 HTTP(S) URL")
    if not isinstance(c["cases"], list) or not c["cases"] or set(c["cases"]) - set(CASES):
        p.error("未知或空测试项")
    limits = dict(concurrency=(1, 2048), max_requests=(1, 100000), max_output_tokens=(1, 8192),
                  context_repeats=(10, 1000), body_repeats=(3, 100), body_bytes=(100_000_000, 120_000_000),
                  target_context_tokens=(1_000_000, 2_000_000), context_max_tokens=(1_000_000, 2_100_000),
                  min_samples=(10, 100000), max_body_bytes=(100_000_000, 150_000_000),
                  max_response_bytes=(1024, 100_000_000), duration_seconds=(60, 3600),
                  timeout_seconds=(1, 3600), target_tpm=(10_000_000, 1e9),
                  min_success_rate=(0.99, 1), p50_ms=(1, 3000), p90_ms=(1, 5000))
    for key, (low, high) in limits.items():
        v = c[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not low <= v <= high:
            p.error(f"{key} 必须在 [{low}, {high}] 范围内")
        if key in integer_options and not isinstance(v, int):
            p.error(f"{key} 必须为整数")
    if c["context_max_tokens"] < c["target_context_tokens"]:
        p.error("context_max_tokens 不能小于目标")
    if c["tpm_basis"] not in ("input", "total") or c["cache_basis"] not in ("token", "request"):
        p.error("tpm_basis=input/total；cache_basis=token/request")
    if c["media_transport"] not in ("inline", "url"):
        p.error("media_transport=inline/url")
    if c["body_mode"] not in ("transport", "media"):
        p.error("body_mode=transport/media")
    if "real_traffic" in c["cases"] and "synthetic_traffic" in c["cases"]:
        p.error("同一次运行只能选择真实或合成流量中的一种")
    for key in ("model", "provider", "api_key_env"):
        if not isinstance(c[key], str) or not c[key].strip():
            p.error(f"{key} 必须是非空字符串")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c["api_key_env"]):
        p.error("--key-env 须为环境变量名，例如 san_API_KEY，不要加 $ 或传入密钥值")
    return a, c


def validate_fixture(row, c):
    if not isinstance(row, dict) or not isinstance(row.get("body"), dict):
        raise ValueError("每个样本须包含 body 对象")
    if not isinstance(row["body"].get("messages"), list) or not row["body"]["messages"]:
        raise ValueError("body.messages 必须是非空列表")
    expected = row.get("expected_contains", [])
    if not isinstance(expected, list) or any(not isinstance(x, str) or not x for x in expected):
        raise ValueError("expected_contains 必须为非空字符串列表")
    if len(expected) != len(set(expected)):
        raise ValueError("检索答案必须互不相同")
    if any(not isinstance(message, dict) for message in row["body"]["messages"]):
        raise ValueError("messages 每项必须是对象")
    if len(dumps(row["body"]).encode()) > c["max_body_bytes"]:
        raise ValueError("样本正文超过 max_body_bytes")


def modalities(body):
    found = set()
    for message in body.get("messages", []):
        content = message.get("content")
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict):
                kind = part.get("type", "")
                if kind in ("image_url", "input_image"):
                    found.add("image")
                if kind in ("video_url", "input_video", "video"):
                    found.add("video")
    return sorted(found) or ["text"]


def plan(c):
    tasks, missing, sources = [], {}, {}
    if "diagnostic" in c["cases"]:
        for label, kinds in (("text", []), ("image", ["image"]), ("video", ["video"]), ("mixed", ["image", "video"])):
            content = [{"type": "text", "text": "Reply exactly PROBE-OK."}]
            for kind in kinds:
                name = "fruits.jpg" if kind == "image" else "sample-5s-360p.mp4"
                fixture_path(name, c)
                content.append({"type": kind + "_url", kind + "_url": {"url": "fixture://" + name}})
            tasks.append(("diagnostic", dict(probe=label, body={"messages": [{"role": "user", "content": content}]},
                                               expected_contains=["PROBE-OK"]), 0))
            if not kinds:
                tasks[-1][1]["body"]["messages"][0]["content"] = content[0]["text"]
    def load(key):
        path = c[key]
        if not path:
            return None
        raw = Path(path).read_bytes()
        sources[key] = dict(path=str(Path(path).resolve()), sha256=sha(raw), bytes=len(raw))
        return raw
    if "body_100mb" in c["cases"]:
        raw = load("body_file") if c["body_mode"] == "media" else None
        row = json.loads(raw) if raw else dict(body={"messages": [{"role": "user", "content": "只回复 BODY-OK"}]}, expected_contains=["BODY-OK"])
        validate_fixture(row, c)
        if not row.get("expected_contains"):
            raise ValueError("body_file 必须提供 expected_contains")
        tasks += [("body_100mb", row, 0)] * c["body_repeats"]
    if "context_1m" in c["cases"]:
        raw = load("context_file")
        if raw:
            row = json.loads(raw)
            validate_fixture(row, c)
            if len(row.get("expected_contains", [])) < 3:
                raise ValueError("context_file 须提供至少三个检索答案（建议头/中/尾随机标记）")
            tasks += [("context_1m", row, 0)] * c["context_repeats"]
        else:
            missing["context_1m"] = "缺少 context_file，不能用字符数代替百万 Token"
    traffic_case = "synthetic_traffic" if "synthetic_traffic" in c["cases"] else "real_traffic"
    if traffic_case in c["cases"]:
        raw = load("traffic_file")
        if raw:
            rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
            if len(rows) < c["min_samples"]:
                raise ValueError("真实流量样本不足 min_samples")
            for row in rows:
                validate_fixture(row, c)
                if traffic_case == "real_traffic" and row.get("fixture_type") == "public_synthetic":
                    raise ValueError("公开合成样本不能用于真实业务验收，请选择 synthetic_traffic")
                if not isinstance(row.get("offset_seconds"), (int, float)) or not math.isfinite(row["offset_seconds"]) or row["offset_seconds"] < 0:
                    raise ValueError("每条流量须提供非负 offset_seconds")
            offsets = [r["offset_seconds"] for r in rows]
            if offsets != sorted(offsets) or offsets[-1] == offsets[0]:
                raise ValueError("流量必须按时间升序排列且时间跨度非零")
            for row in rows:
                offset = (row["offset_seconds"] - offsets[0]) / (offsets[-1] - offsets[0]) * c["duration_seconds"]
                tasks.append((traffic_case, row, offset))
        else:
            missing[traffic_case] = "缺少流量 JSONL；可先执行 --prepare-materials 准备公开合成负载"
    if len(tasks) > c["max_requests"]:
        raise ValueError(f"计划 {len(tasks)} 请求超过 max_requests={c['max_requests']}，不截断真实流量")
    return tasks, missing, sources


def fixture_path(name, c):
    if not c["asset_dir"]:
        raise ValueError("fixture 引用需要 asset_dir")
    root = Path(c["asset_dir"]).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("素材不存在或路径超出 asset_dir")
    return path


@lru_cache(maxsize=16)
def fixture_text(path, mtime_ns):
    return Path(path).read_text(encoding="utf-8")


@lru_cache(maxsize=8)
def fixture_data_uri(path, mtime_ns):
    import base64
    path = Path(path)
    mime = "image/jpeg" if path.suffix == ".jpg" else "video/mp4"
    return "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def resolve_fixtures(value, c):
    if isinstance(value, list):
        return [resolve_fixtures(v, c) for v in value]
    if isinstance(value, dict):
        return {k: resolve_fixtures(v, c) for k, v in value.items()}
    if isinstance(value, str) and value.startswith("fixture-text://"):
        path = fixture_path(value[15:], c)
        return fixture_text(str(path), path.stat().st_mtime_ns)
    if isinstance(value, str) and value.startswith("fixture://"):
        path = fixture_path(value[10:], c)
        if c["media_transport"] == "url":
            from prepare_materials import SOURCES
            return SOURCES[path.name]["url"]
        # 数据来源有固定哈希；运行前由材料清单核验。
        return fixture_data_uri(str(path), path.stat().st_mtime_ns)
    return value


def payload(case, row, c):
    if case == "body_100mb" and c["body_mode"] == "transport":
        row = dict(body={"messages": [{"role": "user", "content": "只回复 BODY-OK"}]})
    body = resolve_fixtures(row["body"], c)
    body.update(model=c["model"], stream=True, stream_options={"include_usage": True})
    # 防止真实历史样本携带无上限输出设置；保留其他业务参数和缓存键。
    field = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
    body.pop("max_tokens" if field == "max_completion_tokens" else "max_completion_tokens", None)
    body[field] = c["max_output_tokens"]
    raw = dumps(body).encode("utf-8")
    if case == "body_100mb":
        if row.get("fill_media_to_bytes"):
            if c["media_transport"] != "inline":
                raise ValueError("100MB媒体正文须使用 inline，不能以URL代替媒体字节")
            content = body["messages"][-1]["content"]
            image = next(p for p in content if p.get("type") == "image_url")
            step = len(dumps(image).encode("utf-8")) + 1
            repeats = max(0, (c["body_bytes"] - len(raw)) // step)
            content.extend([image] * repeats)
            raw = dumps(body).encode("utf-8")
            if len(raw) > c["body_bytes"]:
                raise ValueError("正文初始媒体已超过 body_bytes")
            raw += b" " * (c["body_bytes"] - len(raw))
        # JSON 末尾合法空白也属于 HTTP body；隔离网关大小限制与模型上下文限制。
        if c["body_mode"] == "media" and c["body_file"] and len(raw) < c["body_bytes"]:
            raise ValueError("自定义 body_file 的业务 payload 不足 body_bytes；请提供真实大正文")
        if c["body_mode"] == "transport" or not c["body_file"]:
            if len(raw) > c["body_bytes"]:
                raise ValueError("body_bytes 小于 JSON 正文")
            raw += b" " * (c["body_bytes"] - len(raw))
    if len(raw) > c["max_body_bytes"]:
        raise ValueError("实际请求超过 max_body_bytes")
    return raw


def context_length_ok(tokens, c):
    # 本项要求至少百万 Token；不同服务的 tokenizer 不应触发人为上限误判。
    return number(tokens) and tokens >= c["target_context_tokens"]


def usage_fields(usage):
    if not isinstance(usage, dict):
        usage = {}
    inp, out = usage.get("prompt_tokens"), usage.get("completion_tokens")
    prompt_details = usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    output_details = usage.get("completion_tokens_details")
    output_details = output_details if isinstance(output_details, dict) else {}
    cache = prompt_details.get("cached_tokens")
    if cache is None:
        cache = usage.get("cached_tokens")
    if cache is None:
        cache = usage.get("prompt_cache_hit_tokens")
    total = usage.get("total_tokens")
    valid = all(number(x) for x in (inp, out, total)) and total == inp + out and inp > 0
    cache_valid = number(cache) and number(inp) and cache <= inp
    return dict(input_tokens=inp, output_tokens=out, total_tokens=total, cached_tokens=cache,
                usage_valid=valid, cache_valid=cache_valid,
                reasoning_tokens=output_details.get("reasoning_tokens"))


def provider_error(text, status):
    """错误分类只依据返回正文；没有明确信息时保留通用类别。"""
    message = text.strip()
    candidates = [message] + [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        error = obj.get("error", obj)
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("detail") or error.get("code") or candidate)
        else:
            message = str(error)
        break
    lowered = message.lower()
    category = "parameter_error" if status in (400, 422) else "http_error"
    if any(x in lowered for x in ("insufficient user quota", "not enough quota", "insufficient_user_quota", "insufficient_quota", "余额不足", "额度不足")):
        category = "insufficient_quota"
    elif status in (401, 403):
        category = "authentication_or_permission"
    elif status == 413 or any(x in lowered for x in ("request body too large", "payload too large", "client intended to send too large")):
        category = "body_size_limit"
    elif "image" in lowered and any(x in lowered for x in ("too many", "maximum number", "at most", "number of images")):
        category = "image_count_limit"
    elif any(x in lowered for x in ("context length", "maximum context", "too many tokens", "token limit")):
        category = "context_limit"
    elif "video" in lowered and any(x in lowered for x in ("unsupported", "not support", "invalid", "not allowed", "unknown")):
        category = "video_format_or_support"
    elif status == 429:
        category = "rate_limit"
    elif status >= 500:
        category = "server_error"
    return dict(category=category, message=message[:2048])


async def capture_error(response, record, limit=65536):
    chunks, size = [], 0
    async for chunk in response.content.iter_chunked(8192):
        chunks.append(chunk[:max(0, limit + 1 - size)])
        size += len(chunks[-1])
        if size > limit:
            break
    raw = b"".join(chunks)
    record["error_body_truncated"] = len(raw) > limit
    raw = raw[:limit]
    try:
        import codecs
        text = codecs.getincrementaldecoder("utf-8")().decode(raw, final=not record["error_body_truncated"])
    except UnicodeDecodeError:
        record["error_body_hex"] = raw.hex()
        record["provider_error"] = dict(category="encoding_error", message="服务端错误响应不是有效UTF-8，原始字节已保存")
    else:
        record["error_body"] = text
        record["provider_error"] = provider_error(text, response.status)


def redact(value, key):
    if isinstance(value, dict):
        return {k: (v.replace(key.encode().hex(), b"[REDACTED]".hex()) if key and k.endswith("_hex") and isinstance(v, str)
                    else redact(v, key)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if isinstance(value, str) and key:
        return value.replace(key, "[REDACTED]")
    return value


async def request(session, sem, stop, key, c, case, row, index, due, evidence, case_stop=None):
    delay = due - time.perf_counter()
    if delay > 0:
        waits = [asyncio.create_task(asyncio.sleep(delay)), asyncio.create_task(stop.wait())]
        if case_stop is not None:
            waits.append(asyncio.create_task(case_stop.wait()))
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
    r = dict(index=index, case_id=case, timestamp=now(), result="error", ttft_ms=None,
             network_ttft_ms=None, ttfb_ms=None, elapsed_ms=0, http_status=None,
             modalities=modalities(row["body"]), finish_reason=None, response_model=None,
             error_type=None, content="", usage={}, sse_done=False, attempted=False, probe=row.get("probe"))
    r["evidence_file"] = f"evidence/{index:06d}.json"
    started = time.perf_counter()
    events = []
    async with sem:
        if stop.is_set():
            r.update(error_type="auth_stopped", result="skipped")
        elif case_stop is not None and case_stop.is_set():
            r.update(error_type="same_case_parameter_stopped", result="skipped")
        else:
            try:
                raw = payload(case, row, c)
                r.update(request_sha256=sha(raw), body_bytes=len(raw), queue_ms=(time.perf_counter() - due) * 1000)
                network_start = time.perf_counter()
                r["attempted"] = True
                async with session.post(c["base_url"].rstrip("/") + "/chat/completions", data=raw,
                                        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                                        allow_redirects=False) as response:
                    r.update(http_status=response.status, ttfb_ms=(time.perf_counter() - network_start) * 1000,
                             request_id=response.headers.get("x-request-id"),
                             content_type=response.headers.get("Content-Type"), decoding="utf-8")
                    if response.status in (401, 403):
                        stop.set()
                    if response.status in (400, 413, 422) and case_stop is not None:
                        case_stop.set()
                    if response.status != 200:
                        await capture_error(response, r)
                        raise ValueError(f"HTTP {response.status}")
                    if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                        await capture_error(response, r)
                        raise ValueError("响应不是 SSE")
                    pending, received = [], 0
                    async def event(lines):
                        data = "\n".join(x[5:].lstrip(" ") for x in lines if x.startswith("data:"))
                        if not data:
                            return
                        events.append(dict(elapsed_ms=(time.perf_counter() - network_start) * 1000, data=data))
                        if data == "[DONE]":
                            r["sse_done"] = True
                            return
                        def reject_constant(value):
                            raise ValueError("SSE 包含非法 JSON 常量")
                        obj = json.loads(data, parse_constant=reject_constant)
                        if not isinstance(obj, dict):
                            raise ValueError("SSE JSON 必须是对象")
                        if obj.get("error"):
                            r["provider_error"] = provider_error(data, response.status)
                            r["error_body"] = data[:65536]
                            raise ValueError("SSE error event")
                        r["response_model"] = obj.get("model") or r["response_model"]
                        if obj.get("usage") is not None:
                            r["usage"] = obj["usage"]
                        for choice in obj.get("choices", []):
                            if choice.get("index", 0) != 0:
                                continue
                            r["finish_reason"] = choice.get("finish_reason") or r["finish_reason"]
                            content = choice.get("delta", {}).get("content")
                            if isinstance(content, str) and content:
                                if r["ttft_ms"] is None:
                                    r["ttft_ms"] = (time.perf_counter() - due) * 1000
                                    r["network_ttft_ms"] = (time.perf_counter() - network_start) * 1000
                                r["content"] += content
                    # aiohttp 行读取保留跨网络 chunk 的 UTF-8 字节；整行严格解码。
                    async for line in response.content:
                        received += len(line)
                        if received > c["max_response_bytes"]:
                            raise ValueError("响应超过 max_response_bytes")
                        try:
                            line = line.decode("utf-8").rstrip("\r\n")
                        except UnicodeDecodeError:
                            r["encoding_error_line_hex"] = line.hex()
                            raise
                        if not line:
                            await event(pending)
                            pending = []
                        else:
                            pending.append(line)
                    if pending:
                        await event(pending)
                    r.update(usage_fields(r["usage"]))
                    valid = (r["sse_done"] and r["finish_reason"] in ("stop", "length")
                             and bool(r["content"].strip()) and bool(r["response_model"])
                             and r["usage_valid"])
                    valid = valid and all(x in r["content"] for x in row.get("expected_contains", []))
                    if case == "context_1m":
                        valid = valid and context_length_ok(r["input_tokens"], c)
                        valid = valid and r["finish_reason"] == "stop"
                    r["result"] = "pass" if valid else "fail"
                    if not valid:
                        assertions = dict(sse_done=r["sse_done"], finish_reason=r["finish_reason"] in ("stop", "length"),
                                          visible_text=bool(r["content"].strip()), response_model=bool(r["response_model"]),
                                          official_usage=r["usage_valid"], expected_answer=all(x in r["content"] for x in row.get("expected_contains", [])))
                        if case == "context_1m":
                            assertions.update(context_length=context_length_ok(r["input_tokens"], c),
                                              complete_answer=r["finish_reason"] == "stop")
                        r["failed_assertions"] = [name for name, passed in assertions.items() if not passed]
            except Exception as exc:
                # 不输出供应商原始错误，避免其回显凭证/业务请求。
                r.update(result="error", error_type=type(exc).__name__)
                r["error_message"] = str(exc)[:2048]
                if isinstance(exc, ValueError) and str(exc).startswith("HTTP "):
                    r["error_type"] = str(exc)
    r.update(usage_fields(r["usage"]))
    r["error_category"] = (r.get("provider_error") or {}).get("category")
    r["provider_message"] = (r.get("provider_error") or {}).get("message")
    if case == "body_100mb":
        r["body_mode"] = c["body_mode"]
    r["elapsed_ms"] = (time.perf_counter() - started) * 1000
    r["completed_monotonic"] = time.perf_counter()
    # 响应可能是业务敏感内容；证据目录只供本地审计，不保存凭证或超大输入。
    r.update(schema_version="1.0", title_zh=CASES[case][0], purpose_zh=CASES[case][1],
             impact_zh="当前响应满足本用例断言" if r["result"] == "pass" else "当前响应不能支持本用例通过结论",
             model=c["model"], provider=c["provider"], endpoint=c["base_url"].rstrip("/") + "/chat/completions")
    # 对完整证据脱敏，包含模型名、usage 等可能回显的字段。
    sanitized_record = redact(r, key)
    safe = dict(sanitized_record)
    safe["events"] = redact(events, key)
    save(evidence / f"{index:06d}.json", safe)
    print(dumps(dict(index=index, case=case, probe=r["probe"], result=r["result"], http_status=r["http_status"],
                     error_type=r["error_type"], error_category=r["error_category"],
                     message=(sanitized_record.get("provider_message") or "")[:300],
                     failed_assertions=r.get("failed_assertions"))), flush=True)
    return sanitized_record


def verdict(ok, detail):
    return dict(result="pass" if ok else "fail", detail=detail)


def summarize(records, c, missing, traffic_start=None, traffic_elapsed=None):
    result = {case: dict(result="skipped", detail=message) for case, message in missing.items()}
    for case in ("body_100mb", "context_1m"):
        rows = [r for r in records if r["case_id"] == case]
        if rows:
            good = sum(r["result"] == "pass" for r in rows)
            attempted = sum(r.get("attempted", r["result"] != "skipped") for r in rows)
            result[case] = verdict(good == len(rows), dict(requests=len(rows), successful=good,
                                  attempted_requests=attempted, skipped_requests=sum(r["result"] == "skipped" for r in rows),
                                  success_rate=good / attempted if attempted else None, input_tokens=[r["input_tokens"] for r in rows]))
    probes = [r for r in records if r["case_id"] == "diagnostic"]
    if probes:
        result["diagnostic"] = verdict(all(r["result"] == "pass" for r in probes),
            dict(probes=[{k: r.get(k) for k in ("probe", "result", "http_status", "attempted", "error_type", "provider_error", "failed_assertions", "evidence_file")}
                         for r in probes]))
    rows = [r for r in records if r["case_id"] == "real_traffic"]
    if rows:
        good = [r for r in rows if r["result"] == "pass"]
        sent_rows = [r for r in rows if r.get("attempted", r["result"] != "skipped")]
        success_rate = len(good) / len(sent_rows) if sent_rows else None
        complete_usage = all(r["usage_valid"] for r in rows)
        complete_cache = all(r["cache_valid"] for r in rows)
        token_sum = sum(r[c["tpm_basis"] + "_tokens"] for r in good)
        elapsed = max(c["duration_seconds"], traffic_elapsed)
        achieved = token_sum * 60 / elapsed if good else None
        cache_ratio = None
        if complete_cache:
            denom = sum(r["input_tokens"] for r in rows)
            cache_ratio = (sum(r["cached_tokens"] for r in rows) / denom if denom else None) if c["cache_basis"] == "token" else sum(r["cached_tokens"] > 0 for r in rows) / len(rows)
        latencies = [r["ttft_ms"] if r["result"] == "pass" and r["ttft_ms"] is not None else math.inf for r in rows]
        p50, p90 = percentile(latencies, .5), percentile(latencies, .9)
        media = {m: sum(m in r["modalities"] for r in sent_rows) for m in ("image", "video", "text")}
        planned_media = {m: sum(m in r["modalities"] for r in rows) for m in ("image", "video", "text")}
        media_good = {m: sum(m in r["modalities"] for r in good) for m in ("image", "video")}
        checks = dict(sufficient_samples=len(sent_rows) >= c["min_samples"],
                      success_rate=success_rate is not None and success_rate >= c["min_success_rate"],
                      mixed_image_video=all(media_good[m] > 0 for m in media_good),
                      official_usage_complete=complete_usage,
                      achieved_tpm=achieved is not None and achieved >= c["target_tpm"],
                      cache_85_percent=cache_ratio is not None and cache_ratio >= .85,
                      p50=p50 is not None and p50 <= c["p50_ms"],
                      p90=p90 is not None and p90 <= c["p90_ms"])
        per_media = {}
        for m in ("image", "video", "text"):
            group = [r for r in rows if m in r["modalities"]]
            ls = [r["ttft_ms"] if r["result"] == "pass" else math.inf for r in group]
            per_media[m] = dict(planned_samples=len(group), samples=sum(r.get("attempted", r["result"] != "skipped") for r in group),
                               p50_ms=percentile(ls, .5), p90_ms=percentile(ls, .9))
        result["real_traffic"] = verdict(all(checks.values()), dict(checks=checks,
            requests=len(rows), successful=len(good), success_rate=success_rate,
            measured_seconds=elapsed, actual_elapsed_seconds=traffic_elapsed, achieved_tpm=achieved, tpm_basis=c["tpm_basis"],
            cache_ratio=cache_ratio, cache_basis=c["cache_basis"], p50_ms=p50, p90_ms=p90,
            modalities=media, planned_modalities=planned_media, per_modality=per_media,
            note="TTFT 从计划发起时刻计算，包含客户端排队；失败按无限延迟计。TPM 包含排空时间，不使用估算 Token。"))
    synthetic = [dict(r, case_id="real_traffic") for r in records if r["case_id"] == "synthetic_traffic"]
    if synthetic:
        result["synthetic_traffic"] = summarize(synthetic, dict(c, cases=["real_traffic"]), {}, traffic_start, traffic_elapsed)["real_traffic"]
        result["synthetic_traffic"]["detail"].update(dataset_kind="public_synthetic",
            business_cache_acceptance="not_evaluated", note="公开素材、指定共享前缀及混合比例；这里只判断合成负载指标，不得推断正常业务缓存达到85%。")
    if "deployment" in c["cases"]:
        doc = read_json(c["deployment_file"]) if c["deployment_file"] else {}
        if not isinstance(doc, dict):
            raise ValueError("deployment_file 必须是 JSON 对象")
        fields = ("model", "served_model", "quantization", "moe_weight_scheme", "checkpoint_sha256",
                  "engine_version", "evidence_source", "evidence_sha256", "verified_by", "verified_at")
        evidence = {k: doc.get(k) for k in fields}
        result["deployment"] = dict(result="manual_review", detail=dict(
            declaration=evidence, fp8_declared=str(doc["quantization"]).lower() == "fp8" if doc.get("quantization") else None,
            missing_fields=[k for k in fields if not doc.get(k)],
            note="黑盒 API 无法证明 FP8 或 MoE 权重方案。请核对部署日志/模型配置/检查点，并核实与被测端点的对应关系；声明不自动判通过。"))
    for case, item in result.items():
        selected = [r for r in records if r["case_id"] == case]
        if selected and isinstance(item["detail"], dict):
            item["detail"].update(attempted_requests=sum(r.get("attempted", r["result"] != "skipped") for r in selected),
                skipped_requests=sum(r["result"] == "skipped" for r in selected),
                error_counts=dict(Counter(r["error_type"] for r in selected if r.get("error_type"))),
                provider_errors=[r["provider_error"] for r in selected if r.get("provider_error")])
    return result


def measurements(records, summary, c):
    """只从响应/计时生成观测值；目标值和通过断言不写进实测值列。"""
    output = []
    for case, result in summary.items():
        rows = [r for r in records if r["case_id"] == case]
        def add(name, value, unit="", samples=None, note="", state=None):
            output.append(dict(case_id=case, title_zh=CASES[case][0], metric=name, value=value,
                               unit=unit, samples=samples, status=state or ("已测得" if value is not None else "未测得"), note=note))
        if case == "deployment":
            declaration = result["detail"]["declaration"]
            for label, field in (("量化精度", "quantization"), ("MoE 权重方案", "moe_weight_scheme")):
                value = declaration.get(field)
                add(label, value, state="部署声明，待核验" if value else "未提供", note="来源为部署资料，不是 API 实测")
            continue
        if not rows:
            add("测试记录", None, note=str(result["detail"]), state="未执行")
            continue
        good = [r for r in rows if r["result"] == "pass"]
        attempted = [r for r in rows if r.get("attempted", r["result"] != "skipped")]
        add("计划请求数", len(rows), "个", len(rows))
        add("实际发送请求数", len(attempted), "个", len(rows))
        add("跳过请求数", sum(r["result"] == "skipped" for r in rows), "个", len(rows))
        add("成功请求数", len(good), "个", len(attempted))
        add("实际失败或未完成请求数", len(attempted) - len(good), "个", len(attempted))
        add("成功率", len(good) / len(attempted) * 100 if attempted else None, "%", len(attempted), "分母仅含实际发送请求；跳过不当作实测失败")
        for field, label, unit in (("body_bytes", "序列化请求正文", "字节"),
                                   ("input_tokens", "服务端输入 Token", "Token")):
            values = [r.get(field) for r in rows if number(r.get(field))]
            for name, value in (("最小值", min(values) if values else None), ("最大值", max(values) if values else None)):
                note = "正文已构造；是否被服务接受请结合 HTTP 状态和成功率" if field == "body_bytes" else "原样取自 usage，缺失不估算"
                add(label + name, value, unit, len(values), note)
        for field, label in (("ttft_ms", "观测 TTFT"), ("network_ttft_ms", "网络 TTFT"),
                             ("elapsed_ms", "单请求总耗时")):
            values = [r.get(field) for r in attempted if isinstance(r.get(field), (int, float)) and math.isfinite(r[field])]
            for q, suffix in ((.5, "P50"), (.9, "P90")):
                add(label + " " + suffix, percentile(values, q), "ms", len(values),
                    f"具有该计时值的样本 {len(values)}/{len(rows)}；无首字的失败请求单列，未伪造无限延迟为观测值")
        if case in ("real_traffic", "synthetic_traffic"):
            detail = result["detail"]
            seconds = detail["measured_seconds"]
            add("实际混合负载阶段耗时", detail["actual_elapsed_seconds"], "秒", len(attempted))
            add("TPM统计分母（至少配置窗口）", seconds, "秒", len(attempted))
            for field, name in (("input_tokens", "Input TPM"), ("output_tokens", "Output TPM"), ("total_tokens", "Total TPM")):
                valid = [r for r in good if number(r.get(field))]
                add(name, sum(r[field] for r in valid) * 60 / seconds if valid else None, "Token/分钟", len(valid),
                    "只累计成功且具有服务端 usage 的请求；不以目标 TPM 代替实测")
            cached = [r for r in rows if r.get("cache_valid") and number(r.get("input_tokens"))]
            denom = sum(r["input_tokens"] for r in cached)
            ratio = (sum(r["cached_tokens"] for r in cached) / denom if denom else None) if c["cache_basis"] == "token" else (
                sum(r["cached_tokens"] > 0 for r in cached) / len(cached) if cached else None)
            add("缓存命中率", ratio * 100 if ratio is not None else None, "%", len(cached),
                f"口径={c['cache_basis']}；缓存字段覆盖 {len(cached)}/{len(rows)}；合成负载和真实业务范围见测试项目",
                state="已测得" if len(cached) == len(rows) else "部分样本" if cached else "未返回")
            for media in ("image", "video", "text"):
                group = [r for r in attempted if media in r["modalities"]]
                label = {"image": "图片请求", "video": "视频请求", "text": "纯文本请求"}[media]
                add(label + "数量", len(group), "个", len(rows), "同请求含图片和视频时同时计入两组")
                values = [r["ttft_ms"] for r in group if r.get("ttft_ms") is not None]
                for q, suffix in ((.5, "P50"), (.9, "P90")):
                    add(label + "观测 TTFT " + suffix, percentile(values, q), "ms", len(values),
                        f"有首字样本 {len(values)}/{len(group)}；成功率另列")
    return output


def export(out, c, records, summary):
    measured = measurements(records, summary, c)
    with (out / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["case_id", "title_zh", "metric", "value", "unit", "samples", "status", "note"])
        writer.writeheader()
        writer.writerows(measured)
    rows = []
    for case, item in summary.items():
        impact = "本项实测满足验收条件" if item["result"] == "pass" else "本项尚不能支持满足验收要求的结论"
        if case == "synthetic_traffic":
            impact = "仅反映公开合成负载表现；不能证明真实业务缓存命中率达到85%"
        rows.append(dict(case_id=case, title_zh=CASES[case][0], purpose_zh=CASES[case][1],
                         impact_zh=impact,
                         model=c["model"], provider=c["provider"], endpoint=c["base_url"] + "/chat/completions",
                         result=item["result"], detail=dumps(item["detail"])))
    with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with (out / "requests.csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["index", "case_id", "title_zh", "purpose_zh", "impact_zh", "model", "provider", "endpoint", "result", "http_status", "error_type", "body_bytes", "ttfb_ms", "ttft_ms",
                  "network_ttft_ms", "queue_ms", "elapsed_ms", "input_tokens", "output_tokens", "total_tokens",
                  "cached_tokens", "reasoning_tokens", "finish_reason", "response_model", "attempted", "probe", "error_message", "error_category", "provider_message", "evidence_file"]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)
    # 运行时报告导出；不依赖项目中任何现有测试模块。
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = re.sub(r"[\\/*?:\[\]]", "_", c["model"])[:27] + "测试项"
    ws.merge_cells("A1:J1")
    ws["A1"] = c["model"] + "｜独立专项验收"
    ws["A1"].data_type = "s"
    ws["A1"].font = Font(name="宋体", size=14, bold=True)
    ws.row_dimensions[1].height = 28
    ws.merge_cells("A2:J2")
    ws["A2"] = f"{c['base_url']} | Key 已脱敏 | {now()[:10]}"
    ws["A2"].font = Font(name="宋体", size=10)
    ws.row_dimensions[2].height = 24
    headers = ["编号", "测试项目", "接口", "总耗时（秒）", "Input Tokens", "Output Tokens", "关键响应", "状态", "主要作用", "当前结果影响"]
    states = {"pass": "通过", "fail": "不通过", "error": "错误", "skipped": "未执行", "manual_review": "待人工核验"}
    for i, h in enumerate(headers, 1):
        cell = ws.cell(4, i, h)
        cell.font = Font(name="宋体", size=11, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4472C4")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[4].height = 32
    for i, row in enumerate(rows, 5):
        subset = [r for r in records if r["case_id"] == row["case_id"]]
        item = summary[row["case_id"]]
        detail = item["detail"]
        if row["case_id"] in ("real_traffic", "synthetic_traffic") and isinstance(detail, dict):
            def observed(name):
                value = next(x["value"] for x in measured if x["case_id"] == row["case_id"] and x["metric"] == name)
                return f"{value:.3f}" if value is not None else "未测得"
            text = (f"Input TPM={observed('Input TPM')}；缓存={observed('缓存命中率')}%；"
                    f"观测TTFT P50={observed('观测 TTFT P50')}ms；P90={observed('观测 TTFT P90')}ms；成功率={observed('成功率')}%")
        elif row["case_id"] == "deployment":
            text = f"量化声明={detail['declaration'].get('quantization') or '未提供'}；MoE={detail['declaration'].get('moe_weight_scheme') or '未提供'}；需核验部署证据"
        else:
            text = row["detail"][:240]
        values = [f"{i-4:02d}", row["title_zh"], row["endpoint"], sum(r["elapsed_ms"] for r in subset) / 1000 if subset else None,
                  sum(r["input_tokens"] for r in subset) if subset and all(number(r["input_tokens"]) for r in subset) else None,
                  sum(r["output_tokens"] for r in subset) if subset and all(number(r["output_tokens"]) for r in subset) else None,
                  text, states[row["result"]], row["purpose_zh"], row["impact_zh"]]
        for j, value in enumerate(values, 1):
            cell = ws.cell(i, j, value)
            if isinstance(value, str):
                cell.data_type = "s"  # 防止供应商名称/摘要被当作 Excel 公式
            cell.font = Font(name="宋体", size=11)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            edge = Side(style="thin", color="D9E1F2")
            cell.border = Border(left=edge, right=edge, top=edge, bottom=edge)
            cell.fill = PatternFill("solid", fgColor="EAF0FA" if i % 2 else "FFFFFF")
        ws.cell(i, 4).number_format = "0.000"
        ws.row_dimensions[i].height = 100
    for i, width in enumerate([9, 26, 29, 20, 17, 18, 48, 20, 32, 40], 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:J{ws.max_row}"
    ws.print_area = f"A1:J{ws.max_row}"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    metrics_sheet = wb.create_sheet("实测指标", 0)
    metric_headers = ["测试项目", "指标", "实测值", "单位", "有效样本数", "实测状态", "统计口径与数据覆盖"]
    metrics_sheet.append(metric_headers)
    for metric in measured:
        value = metric["value"]
        metrics_sheet.append([metric["title_zh"], metric["metric"], value if value is not None else metric["status"],
                              metric["unit"], metric["samples"], metric["status"], metric["note"]])
    for line in metrics_sheet:
        for cell in line:
            if isinstance(cell.value, str):
                cell.data_type = "s"
            cell.font = Font(name="宋体", size=11, bold=cell.row == 1, color="FFFFFF" if cell.row == 1 else "000000")
            cell.fill = PatternFill("solid", fgColor="4472C4" if cell.row == 1 else "EAF0FA" if cell.row % 2 else "FFFFFF")
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        metrics_sheet.row_dimensions[line[0].row].height = 48
    for i, width in enumerate([28, 30, 35, 16, 16, 24, 90], 1):
        metrics_sheet.column_dimensions[get_column_letter(i)].width = width
    for cell in metrics_sheet["C"][1:]:
        if isinstance(cell.value, (int, float)):
            cell.number_format = "0.###"
    metrics_sheet.freeze_panes = "A2"
    metrics_sheet.auto_filter.ref = f"A1:G{metrics_sheet.max_row}"
    metrics_sheet.print_area = metrics_sheet.auto_filter.ref
    metrics_sheet.sheet_properties.pageSetUpPr.fitToPage = True
    metrics_sheet.page_setup.orientation = "landscape"
    metrics_sheet.page_setup.fitToWidth, metrics_sheet.page_setup.fitToHeight = 1, 0
    wb.active = 0
    wb.save(out / "results.xlsx")


async def run(a, c, tasks, missing, sources):
    import aiohttp
    key = os.getenv(c["api_key_env"])
    if tasks and not key:
        raise ValueError(f"未设置密钥环境变量 {c['api_key_env']}")
    # 先验证导出依赖及所有 payload，避免付费请求完成后才发现配置错误。
    import openpyxl  # noqa: F401
    if c["materials_manifest"]:
        material_doc = read_json(c["materials_manifest"])
        for item in material_doc["files"]:
            path = fixture_path(item["file"], c)
            if sha(path.read_bytes()) != item["sha256"]:
                raise ValueError("素材哈希不匹配，请重新准备素材")
        sources["materials"] = material_doc
    for case, row, _ in tasks:
        payload(case, row, c)
    if c["deployment_file"]:
        if not isinstance(read_json(c["deployment_file"]), dict):
            raise ValueError("deployment_file 必须是 JSON 对象")
        raw = Path(c["deployment_file"]).read_bytes()
        sources["deployment_file"] = dict(sha256=sha(raw), bytes=len(raw))
    out = (a.output_dir or ROOT / "results") / (datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S-%f"))
    out.mkdir(parents=True, exist_ok=False)
    out.chmod(0o700)
    evidence = out / "evidence"
    evidence.mkdir()
    manifest = dict(schema_version="1.0", specification_version="1.1", script_version=VERSION,
                    script_sha256=sha(Path(__file__).read_bytes()), started_at=now(), timezone="Asia/Shanghai",
                    config=c, sources=sources, python=platform.python_version(), platform=platform.platform(),
                    planned_requests=len(tasks), key="[REDACTED]")
    save(out / "manifest.json", manifest)
    records, stop = [], asyncio.Event()
    traffic_start, traffic_elapsed = None, None
    timeout = aiohttp.ClientTimeout(total=c["timeout_seconds"], connect=min(30, c["timeout_seconds"]))
    connector = aiohttp.TCPConnector(limit=c["concurrency"])
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, read_bufsize=1024 * 1024) as session:
        index = 0
        for case in ("diagnostic", "body_100mb", "context_1m", "real_traffic", "synthetic_traffic"):
            selected = [(r, offset) for name, r, offset in tasks if name == case]
            if not selected:
                continue
            print(f"执行 {CASES[case][0]}：{len(selected)} 请求", flush=True)
            case_stop = asyncio.Event() if case != "diagnostic" else None
            if case not in ("real_traffic", "synthetic_traffic"):
                for row, _ in selected:
                    index += 1
                    records.append(await request(session, asyncio.Semaphore(1), stop, key, c, case, row, index, time.perf_counter(), evidence, case_stop))
            else:
                traffic_start = time.perf_counter()
                sem = asyncio.Semaphore(c["concurrency"])
                futures = []
                modality_stops = {}
                for row, offset in selected:
                    index += 1
                    # 视频格式400仅停止相同媒体组合，保留纯文本/图片负载的实测机会。
                    group_stop = modality_stops.setdefault(tuple(modalities(row["body"])), asyncio.Event())
                    futures.append(request(session, sem, stop, key, c, case, row, index, traffic_start + offset, evidence, group_stop))
                records.extend(await asyncio.gather(*futures))
                traffic_elapsed = time.perf_counter() - traffic_start
    summary = summarize(records, c, missing, traffic_start, traffic_elapsed)
    complete = all(x["result"] == "pass" for x in summary.values())
    report = dict(overall="pass" if complete else "not_passed", items=summary,
                  measurements=measurements(records, summary, c),
                  acceptance_scope="diagnostic_only" if "diagnostic" in c["cases"] else "public_synthetic_not_business" if "synthetic_traffic" in c["cases"] else "real_business",
                  limitation="真实流量代表性、FP8 和 MoE 部署证据需人工核验；混合媒体测试仅验接口和性能，非语义质量评测。")
    save(out / "summary.json", report)
    export(out, c, records, summary)
    manifest.update(ended_at=now(), actual_requests=sum(r["attempted"] for r in records),
                    received_http_responses=sum(r["http_status"] is not None for r in records), overall=report["overall"])
    save(out / "manifest.json", manifest)
    print(dumps(dict(output_dir=str(out.resolve()), **report)))
    return 0 if complete else 1


def main(argv=None):
    try:
        a, c = settings(argv)
        if a.list:
            return 0
        if a.prepare_materials:
            from prepare_materials import prepare
            prepare(ROOT, c, a.encoding_name)
            return 0
        if a.make_context:
            import tiktoken
            import random
            rng = random.Random(20261015)
            enc = tiktoken.get_encoding(a.encoding_name)
            # ASCII 填充使按 Token 分段解码不会切断中文多字节字符。
            chunk = "Archive background record. This line is filler, not an instruction.\n"
            unit = enc.encode(chunk)
            tokens = (unit * (c["target_context_tokens"] // len(unit) + 1))[:c["target_context_tokens"]]
            answers = ["".join(rng.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=24)) for _ in range(3)]
            parts = []
            boundaries = [0, len(tokens) // 2, len(tokens)]
            cursor = 0
            for i, boundary in enumerate(boundaries):
                parts.append(enc.decode(tokens[cursor:boundary]))
                parts.append(f"\nKEY_{i+1}={answers[i]}\n")
                cursor = boundary
            text = "".join(parts) + "\nReturn the exact values of KEY_1, KEY_2 and KEY_3 only."
            fixture = dict(body={"messages": [{"role": "user", "content": text}]},
                           expected_contains=answers, fixture_type="synthetic_context_retrieval",
                           seed=20261015, encoding=a.encoding_name, local_content_tokens=len(enc.encode(text)))
            with a.make_context.open("x", encoding="utf-8") as f:
                f.write(dumps(fixture))
            print(dumps(dict(context_file=str(a.make_context.resolve()), local_content_tokens=fixture["local_content_tokens"],
                             note="本地 tokenizer 计数仅用于构造，验收以服务端 usage 为准")))
            return 0
        tasks, missing, sources = plan(c)
        print(dumps(dict(mode="execute" if a.execute and a.confirm_paid else "preview", config=c,
                         planned_requests=len(tasks), missing=missing, sources=sources)))
        if not (a.execute and a.confirm_paid):
            print("预览完成。真实运行须同时提供 --run --confirm-paid（--execute 同 --run）。")
            return 0
        return asyncio.run(run(a, c, tasks, missing, sources))
    except (ValueError, OSError, ImportError, json.JSONDecodeError) as exc:
        # 不打印配置内容、环境变量值或原始异常正文。
        print(f"配置/运行错误 [{type(exc).__name__}]：请检查文件、参数、依赖；验证详情见 README。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

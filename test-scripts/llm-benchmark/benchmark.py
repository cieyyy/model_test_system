#!/usr/bin/env python3
"""Streaming Chat Completions benchmark and offline report generator."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import math
import os
import random
import statistics
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
DEFAULTS = {
    "name": "模型性能测试", "endpoint": "", "model": "",
    "api_key_env": "MODEL_TEST_API_KEY", "concurrency": 50,
    "total_requests": 160, "timeout_seconds": 60, "duration_seconds": 0,
    "prompt_mode": "generated", "prompt_tokens": 32000,
    "prompt": "请用中文解释缓存如何改善大语言模型的推理性能。",
    "max_tokens": 256, "max_tokens_field": "max_tokens", "temperature": None,
    "include_usage": True, "proxy": "", "demo": False,
}
CSV_FIELDS = [
    "sequence", "request_id", "status", "http_status", "started_at",
    "latency_ms", "ttft_ms", "tpot_ms", "input_tokens", "output_tokens",
    "cached_input_tokens", "total_tokens", "finish_reason", "provider_request_id",
    "error", "prompt_preview", "output_preview",
]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_endpoint(endpoint):
    endpoint = endpoint.strip()
    u = urlsplit(endpoint)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("endpoint 请填写 http(s) 基础地址或完整接口地址，不包含凭据、查询参数或片段")
    path = u.path.rstrip("/")
    if not path:
        path = "/v1/chat/completions"
    elif path.endswith("/v1"):
        path += "/chat/completions"
    else:
        return endpoint
    return urlunsplit((u.scheme, u.netloc, path, "", ""))


def validate_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("配置必须是 JSON 对象")
    cfg = {k: raw.get(k, v) for k, v in DEFAULTS.items()}
    for key, low, high in [
        ("concurrency", 1, 500), ("total_requests", 1, 100000),
        ("prompt_tokens", 1, 1000000), ("max_tokens", 1, 1000000),
    ]:
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} 必须是 {low}～{high} 的整数")
    for key, low, high in [("timeout_seconds", 0.1, 3600), ("duration_seconds", 0, 86400)]:
        v = cfg[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not low <= v <= high:
            raise ValueError(f"{key} 超出允许范围")
    for key in ["name", "endpoint", "model", "api_key_env", "prompt", "proxy"]:
        if not isinstance(cfg[key], str):
            raise ValueError(f"{key} 必须是字符串")
    for key in ["demo", "include_usage"]:
        if not isinstance(cfg[key], bool):
            raise ValueError(f"{key} 必须是布尔值")
    if cfg["temperature"] is not None:
        v = cfg["temperature"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 2:
            raise ValueError("temperature 必须为 null 或 0～2")
    if cfg["prompt_mode"] not in ("generated", "custom"):
        raise ValueError("prompt_mode 必须为 generated 或 custom")
    if cfg["prompt_mode"] == "custom" and not cfg["prompt"].strip():
        raise ValueError("自定义提示词不能为空")
    if cfg["max_tokens_field"] not in ("max_tokens", "max_completion_tokens"):
        raise ValueError("不支持的 max_tokens 字段")
    if not cfg["demo"]:
        cfg["endpoint"] = normalize_endpoint(cfg["endpoint"])
        if not cfg["model"].strip():
            raise ValueError("请填写模型 ID")
        if cfg["proxy"]:
            p = urlsplit(cfg["proxy"])
            if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
                raise ValueError("本版本支持无凭据的 HTTP/HTTPS 代理")
    return cfg


def prepare_prompt(cfg):
    if cfg["demo"]:
        return "本地模拟测试提示词", cfg["prompt_tokens"]
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
    if cfg["prompt_mode"] == "custom":
        return cfg["prompt"], len(enc.encode(cfg["prompt"], disallowed_special=()))
    target = cfg["prompt_tokens"]
    prefix = "Reference data:\n"
    suffix = "\nExplain briefly how prompt caching improves model inference performance."
    budget = max(0, target - len(enc.encode(prefix + suffix)))
    text = prefix + " benchmark" * budget + suffix
    tokens = enc.encode(text)
    # Validate the whole prompt rather than assuming concatenation preserves token counts.
    if len(tokens) >= target:
        text = enc.decode(tokens[:target])
    else:
        text += " benchmark" * (target - len(tokens))
    count = len(enc.encode(text))
    if count != target:
        raise ValueError("无法构造目标 Token 长度，请改用自定义提示词")
    return text, count


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def stats(values):
    return {"avg": statistics.mean(values) if values else None,
            "p50": percentile(values, .5), "p90": percentile(values, .9),
            "p95": percentile(values, .95), "samples": len(values)}


def token_number(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def parse_usage(usage):
    usage = usage or {}
    inp = token_number(usage.get("prompt_tokens", usage.get("input_tokens")))
    out = token_number(usage.get("completion_tokens", usage.get("output_tokens")))
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = token_number(details.get("cached_tokens", usage.get("prompt_cache_hit_tokens", usage.get("cached_tokens"))))
    total = token_number(usage.get("total_tokens"))
    if total is None and inp is not None and out is not None:
        total = inp + out
    return dict(input_tokens=inp, output_tokens=out, cached_input_tokens=cached, total_tokens=total)


async def sse_events(response):
    data = []
    async for line in response.aiter_lines():
        if line == "":
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield "\n".join(data)


def summary(rows, elapsed):
    successful = [r for r in rows if r["status"] == "success"]
    attempts = len(rows)
    rate = lambda count: count / elapsed if elapsed > 0 else 0
    sums = {}
    coverage = {}
    for key in ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens"):
        known = [r[key] for r in successful if r[key] is not None]
        coverage[key] = len(known)
        # A partial token sum would make throughput look authoritative; hide it instead.
        sums[key] = sum(known) if successful and len(known) == len(successful) else None
    cache_known = [r for r in successful if r["input_tokens"] is not None and r["cached_input_tokens"] is not None]
    cache_ratio = None
    if successful and len(cache_known) == len(successful):
        denom = sum(r["input_tokens"] for r in cache_known)
        cache_ratio = sum(r["cached_input_tokens"] for r in cache_known) / denom * 100 if denom else None
    result = dict(attempted=attempts, success=len(successful),
                  failed=sum(r["status"] == "failed" for r in rows),
                  cancelled=sum(r["status"] == "cancelled" for r in rows),
                  success_rate=len(successful) / attempts * 100 if attempts else None,
                  elapsed_seconds=elapsed, cache_hit_rate=cache_ratio,
                  req_tps=rate(len(successful)), rpm=rate(len(successful)) * 60,
                  token_coverage=coverage, **sums)
    for key in sums:
        result[key + "_tpm"] = rate(sums[key]) * 60 if sums[key] is not None else None
    result["system_gen_tps"] = rate(sums["output_tokens"]) if sums["output_tokens"] is not None else None
    for key in ("latency_ms", "ttft_ms", "tpot_ms"):
        result[key] = stats([r[key] for r in successful if r[key] is not None])
    return result


def csv_text(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe = dict(row)
        for k, v in safe.items():
            if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")):
                safe[k] = "'" + v
        writer.writerow(safe)
    return output.getvalue()


def report_html(report, embedded=False):
    template = (HERE / "web" / "index.html").read_text(encoding="utf-8")
    js = (HERE / "web" / "app.js").read_text(encoding="utf-8")
    css = (HERE / "web" / "style.css").read_text(encoding="utf-8")
    data = json.dumps(report, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    if embedded:
        css += "\nhtml,body{min-height:0!important;height:auto!important}body{display:block!important}.offline main{padding:0;max-width:none}.offline #report{width:100%}@media(min-width:980px){.analysis-grid{grid-template-columns:minmax(0,.88fr) minmax(0,1.2fr)}}"
    return template.replace('<link rel="stylesheet" href="/style.css">', f"<style>{css}</style>").replace(
        '<script src="/app.js" defer></script>', f"<script>window.BENCHMARK_EMBED={str(embedded).lower()};window.BENCHMARK_DATA={data};document.addEventListener('DOMContentLoaded',function(){{\n{js}\n}});</script>")


class Run:
    def __init__(self, cfg, api_key="", root=RESULTS):
        self.config = validate_config(cfg)
        self.id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
        self.directory = Path(root) / self.id
        self.api_key = api_key
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.rows = []
        self.status = "queued"
        self.created_at = utc_now()
        self.started_at = None
        self.completed_at = None
        self.error = ""
        self.start_clock = None
        self.elapsed = 0
        self.active = 0
        self.dispatched = 0
        self.prompt_count = None
        self.prompt_hash = ""

    def snapshot(self, include_rows=True):
        with self.lock:
            elapsed = time.perf_counter() - self.start_clock if self.status == "running" and self.start_clock else self.elapsed
            data = dict(id=self.id, config=dict(self.config), status=self.status,
                        created_at=self.created_at, started_at=self.started_at,
                        completed_at=self.completed_at, error=self.error,
                        active=self.active, dispatched=self.dispatched,
                        local_prompt_tokens=self.prompt_count, prompt_sha256=self.prompt_hash,
                        summary=summary(self.rows, elapsed))
            if include_rows:
                data["requests"] = sorted(self.rows, key=lambda r: r["sequence"])
            return data

    def redact(self, text):
        return str(text).replace(self.api_key, "[REDACTED]")[:2000] if self.api_key else str(text)[:2000]

    async def request(self, client, sequence, prompt):
        start = time.perf_counter()
        row = dict(sequence=sequence, request_id=uuid.uuid4().hex, status="failed",
                   http_status=None, started_at=utc_now(), latency_ms=None,
                   ttft_ms=None, tpot_ms=None, input_tokens=None, output_tokens=None,
                   cached_input_tokens=None, total_tokens=None, finish_reason="",
                   provider_request_id="", error="", prompt_preview=prompt[:100], output_preview="")
        first = last = None
        finished = False
        with self.lock:
            self.active += 1
        try:
            if self.config["demo"]:
                rng = random.Random(sequence)
                await asyncio.sleep(rng.uniform(.025, .11))
                first = time.perf_counter()
                await asyncio.sleep(rng.uniform(.07, .22))
                last = time.perf_counter()
                row.update(http_status=200, finish_reason="stop", output_preview="模拟输出：缓存可减少重复输入的预填充计算。",
                           **parse_usage({"prompt_tokens": self.prompt_count,
                                          "completion_tokens": min(self.config["max_tokens"], rng.randint(35, 100)),
                                          "prompt_cache_hit_tokens": int(self.prompt_count * .9988)}))
                finished = True
            else:
                body = dict(model=self.config["model"], messages=[dict(role="user", content=prompt)], stream=True)
                body[self.config["max_tokens_field"]] = self.config["max_tokens"]
                if self.config["temperature"] is not None:
                    body["temperature"] = self.config["temperature"]
                if self.config["include_usage"]:
                    body["stream_options"] = {"include_usage": True}
                async with client.stream("POST", self.config["endpoint"], json=body) as response:
                    row["http_status"] = response.status_code
                    row["provider_request_id"] = response.headers.get("x-request-id", "")
                    if response.status_code >= 300:
                        snippet = bytearray()
                        async for chunk in response.aiter_bytes():
                            snippet.extend(chunk[:2000 - len(snippet)])
                            if len(snippet) >= 2000:
                                break
                        raise ValueError(f"HTTP {response.status_code}: " + snippet.decode("utf-8", errors="replace"))
                    if "text/event-stream" not in response.headers.get("content-type", "").lower():
                        raise ValueError("接口未返回 SSE 流，无法测量 TTFT/TPOT")
                    async for payload in sse_events(response):
                        if payload.strip() == "[DONE]":
                            break
                        event = json.loads(payload)
                        if not isinstance(event, dict):
                            raise ValueError("无效的 SSE JSON 结构")
                        if event.get("error"):
                            raise ValueError(json.dumps(event["error"], ensure_ascii=False))
                        if event.get("usage"):
                            known = parse_usage(event["usage"])
                            row.update({k: v for k, v in known.items() if v is not None})
                        choices = event.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        text = "".join(v for v in [delta.get("content"), delta.get("reasoning_content")] if isinstance(v, str))
                        if text:
                            now = time.perf_counter()
                            first = now if first is None else first
                            last = now
                            row["output_preview"] = (row["output_preview"] + text)[:240]
                        if choice.get("finish_reason") is not None:
                            row["finish_reason"] = str(choice["finish_reason"])
                            finished = True
                if not finished:
                    raise ValueError("流提前结束：缺少 finish_reason")
                if first is None:
                    raise ValueError("未收到非空文本或 reasoning delta")
            row["status"] = "success"
        except asyncio.CancelledError:
            if self.stop.is_set():
                row.update(status="cancelled", error="测试已取消")
            else:
                row.update(status="failed", error="请求超过总超时")
        except (asyncio.TimeoutError, httpx.TimeoutException):
            row["error"] = "请求超过总超时或网络读取超时"
        except Exception as exc:
            row["error"] = self.redact(str(exc) or type(exc).__name__)
        finally:
            row["latency_ms"] = (time.perf_counter() - start) * 1000
            if first is not None:
                row["ttft_ms"] = (first - start) * 1000
            n = row["output_tokens"]
            if row["status"] == "success" and first is not None and last is not None and n is not None and n > 1:
                row["tpot_ms"] = (last - first) * 1000 / (n - 1)
            for key in ("prompt_preview", "output_preview", "provider_request_id", "finish_reason"):
                row[key] = self.redact(row[key])
            with self.lock:
                self.active -= 1
                self.rows.append(row)
                with (self.directory / "requests.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def execute(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            prompt, self.prompt_count = prepare_prompt(self.config)
            if self.stop.is_set():
                raise asyncio.CancelledError()
            self.prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
            headers = {"Authorization": "Bearer " + self.api_key} if self.api_key else {}
            async with httpx.AsyncClient(headers=headers, timeout=self.config["timeout_seconds"],
                                         limits=httpx.Limits(max_connections=self.config["concurrency"], max_keepalive_connections=self.config["concurrency"]),
                                         proxy=self.config["proxy"] or None, trust_env=False) as client:
                self.status = "running"
                self.started_at = utc_now()
                self.start_clock = time.perf_counter()
                (self.directory / "run.json").write_text(json.dumps(self.snapshot(False), ensure_ascii=False), encoding="utf-8")
                deadline = self.start_clock + self.config["duration_seconds"] if self.config["duration_seconds"] else None

                async def worker():
                    while not self.stop.is_set():
                        if deadline and time.perf_counter() >= deadline:
                            break
                        if self.dispatched >= self.config["total_requests"]:
                            break
                        self.dispatched += 1
                        seq = self.dispatched
                        await asyncio.wait_for(self.request(client, seq, prompt), self.config["timeout_seconds"])

                tasks = [asyncio.create_task(worker()) for _ in range(min(self.config["concurrency"], self.config["total_requests"]))]
                try:
                    while any(not t.done() for t in tasks):
                        if self.stop.is_set():
                            for t in tasks:
                                t.cancel()
                            break
                        await asyncio.sleep(.05)
                    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                    for outcome in outcomes:
                        if isinstance(outcome, Exception):
                            raise outcome
                except asyncio.CancelledError:
                    self.stop.set()
                    raise
                finally:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            self.status = "cancelled" if self.stop.is_set() else "completed"
        except asyncio.CancelledError:
            self.stop.set()
            self.status = "cancelled"
        except Exception as exc:
            self.status = "error"
            self.error = self.redact(str(exc))
        finally:
            self.elapsed = time.perf_counter() - self.start_clock if self.start_clock else 0
            self.completed_at = utc_now()
            self.api_key = ""
            self.save()
        return self.snapshot()

    def save(self):
        report = self.snapshot()
        tmp = self.directory / "report.json.tmp"
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.directory / "report.json")
        (self.directory / "requests.csv").write_text(csv_text(report["requests"]), encoding="utf-8-sig")
        (self.directory / "report.html").write_text(report_html(report), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="LLM 并发流式性能测试")
    parser.add_argument("--config", type=Path, help="JSON 配置文件")
    parser.add_argument("--demo", action="store_true", help="仅本地模拟，不调用真实模型")
    parser.add_argument("--output", type=Path, default=RESULTS)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    if args.demo:
        cfg.update(demo=True, name="DeepSeek · 本地模拟", model="demo-model")
    try:
        cfg = validate_config(cfg)
        key = os.environ.get(cfg["api_key_env"], "")
        if not cfg["demo"] and not key:
            parser.error("请设置 API Key 环境变量 " + cfg["api_key_env"])
    except ValueError as exc:
        parser.error(str(exc))
    run = Run(cfg, key, args.output)
    try:
        report = asyncio.run(run.execute())
    except KeyboardInterrupt:
        print("测试中断，已完成请求可在 requests.jsonl 中查看。")
        raise SystemExit(130)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print("报告：", run.directory / "report.html")
    if report["status"] != "completed" or report["summary"]["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

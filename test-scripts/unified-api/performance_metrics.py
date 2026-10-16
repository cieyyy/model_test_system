#!/usr/bin/env python3
"""OpenAI-compatible API performance and rate-limit probes.

Metrics:
  - TTFT: request start to the first non-empty generated text/reasoning delta.
  - TPOT: average milliseconds per output token after the first token.
  - output_tps: output tokens generated per second after TTFT.
  - RPM/TPM: successful requests/tokens normalized to one minute during a
    bounded, paced load window. A value is a tested lower bound unless a 429
    or an authoritative rate-limit response header is observed.
  - KV cache hit rate: request hit rate and cached-token ratio after warm-up.

The script previews by default. Paid requests require both --execute and
--confirm-paid.
"""

import argparse
import csv
import json
import math
import os
import statistics
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
METRICS = {"ttft", "tpot", "tpm", "rpm", "cache"}
RATE_HEADER_NAMES = (
    "x-ratelimit-limit-requests", "ratelimit-limit-requests",
    "x-ratelimit-limit-tokens", "ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests", "ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens", "ratelimit-remaining-tokens",
    "retry-after",
)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def usage_values(usage):
    usage = usage or {}
    prompt = number(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
    output = number(usage.get("completion_tokens", usage.get("output_tokens", 0)))
    total = number(usage.get("total_tokens", 0)) or prompt + output
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = number(details.get("cached_tokens", usage.get("cached_tokens", 0)))
    if not cached:
        cached = number(usage.get("prompt_cache_hit_tokens", 0))
    return int(prompt), int(output), int(total), int(cached)


def extract_text_delta(event):
    """Return generated text from common Chat Completions/Responses SSE shapes."""
    if not isinstance(event, dict):
        return ""
    choices = event.get("choices") or []
    if choices:
        delta = choices[0].get("delta") or {}
        for key in ("content", "reasoning_content"):
            value = delta.get(key)
            if isinstance(value, str) and value:
                return value
    if event.get("type") in (
        "response.output_text.delta", "response.reasoning_text.delta",
        "response.reasoning_summary_text.delta",
    ):
        return event.get("delta") if isinstance(event.get("delta"), str) else ""
    return ""


def rate_headers(headers):
    lowered = {k.lower(): v for k, v in headers.items()}
    return {name: lowered[name] for name in RATE_HEADER_NAMES if name in lowered}


def request_body(model, prompt, max_output_tokens, stream, max_tokens_field):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        max_tokens_field: max_output_tokens,
        "stream": stream,
    }
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body


def open_request(model_cfg, body, timeout):
    key = os.environ.get(model_cfg.get("api_key_env", "MODEL_TEST_API_KEY"), "")
    if not key:
        raise ValueError(f"未设置环境变量 {model_cfg.get('api_key_env', 'MODEL_TEST_API_KEY')}")
    url = model_cfg["base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if body.get("stream") else "application/json",
        },
        method="POST",
    )
    return req, timeout


def stream_request(model_cfg, body, timeout):
    req, timeout = open_request(model_cfg, body, timeout)
    started = time.perf_counter()
    status, first_event_ms, ttft_ms = None, None, None
    usage, response_headers, raw_lines, error = {}, {}, [], ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            ttfb_ms = round((time.perf_counter() - started) * 1000, 1)
            response_headers = rate_headers(response.headers)
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                raw_lines.append(line)
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                now_ms = round((time.perf_counter() - started) * 1000, 1)
                if first_event_ms is None:
                    first_event_ms = now_ms
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if ttft_ms is None and extract_text_delta(event):
                    ttft_ms = now_ms
                candidate = event.get("usage") or (event.get("response") or {}).get("usage")
                if candidate:
                    usage = candidate
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_headers = rate_headers(exc.headers)
        error = exc.read().decode("utf-8", errors="replace")
        ttfb_ms = round((time.perf_counter() - started) * 1000, 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        ttfb_ms = None
        error = str(exc)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    prompt_tokens, output_tokens, total_tokens, cached_tokens = usage_values(usage)
    tpot_ms = None
    output_tps = None
    if ttft_ms is not None and output_tokens > 1 and elapsed_ms >= ttft_ms:
        decode_ms = elapsed_ms - ttft_ms
        tpot_ms = round(decode_ms / (output_tokens - 1), 3)
        output_tps = round((output_tokens - 1) / (decode_ms / 1000), 3) if decode_ms > 0 else None
    return {
        "http_status": status, "ttfb_ms": ttfb_ms, "first_event_ms": first_event_ms,
        "ttft_ms": ttft_ms, "elapsed_ms": elapsed_ms, "input_tokens": prompt_tokens,
        "output_tokens": output_tokens, "total_tokens": total_tokens,
        "cached_tokens": cached_tokens, "tpot_ms_per_token": tpot_ms,
        "output_tokens_per_second": output_tps, "rate_headers": response_headers,
        "error": error, "raw_sse": "\n".join(raw_lines),
    }


def json_request(model_cfg, body, timeout):
    req, timeout = open_request(model_cfg, body, timeout)
    started_wall = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    status, parsed, response_headers, error = None, {}, {}, ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            response_headers = rate_headers(response.headers)
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_headers = rate_headers(exc.headers)
        raw = exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raw, error = "", str(exc)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {"unparsed": raw}
    usage = parsed.get("usage") or {}
    prompt_tokens, output_tokens, total_tokens, cached_tokens = usage_values(usage)
    return {
        "started_at": started_wall, "http_status": status, "elapsed_ms": elapsed_ms,
        "input_tokens": prompt_tokens, "output_tokens": output_tokens,
        "total_tokens": total_tokens, "cached_tokens": cached_tokens,
        "rate_headers": response_headers, "error": error,
        "response": parsed,
    }


def model_configs(cfg, selected):
    if cfg.get("models") and isinstance(cfg["models"][0], dict):
        models = cfg["models"]
    else:
        models = [{
            "name": model, "model": model, "base_url": cfg["base_url"],
            "api_key_env": cfg.get("api_key_env", "MODEL_TEST_API_KEY"),
        } for model in cfg.get("models", [])]
    if selected:
        wanted = {x.strip() for x in selected.split(",") if x.strip()}
        models = [m for m in models if m["model"] in wanted]
    return models


def fixed_prefix(chars):
    paragraph = (
        "这是虚构的社区读书会资料。活动每周六下午举行，设有安静阅读、书籍交换和儿童故事时间。"
        "报名只使用匿名编号，不收集电话、身份证或真实住址。组织者会检查照明并整理匿名反馈。"
    )
    return (paragraph * (chars // len(paragraph) + 1))[:chars]


def write_evidence(out, sequence, model, case, request, result):
    safe_model = model.replace("/", "_")
    name = f"{sequence:04d}-{safe_model}-{case}.json"
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "model": model,
        "case": case, "request": request, "result": result,
    }
    (out / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return name


def latency_probe(model_cfg, args, out, rows, sequence):
    for index in range(1, args.samples + 1):
        prompt = "请用约%d个汉字说明缓存和吞吐量的区别，直接回答。" % max(80, args.output_tokens)
        body = request_body(model_cfg["model"], prompt, args.output_tokens, True, args.max_tokens_field)
        result = stream_request(model_cfg, body, args.timeout)
        evidence = write_evidence(out, sequence, model_cfg["model"], f"latency-{index}", body, result)
        row = {"case": "latency", "model": model_cfg["model"], "index": index,
               **{k: v for k, v in result.items() if k not in ("raw_sse", "error", "rate_headers")},
               "rate_headers": json.dumps(result["rate_headers"], ensure_ascii=False),
               "error": result["error"], "evidence_file": evidence}
        rows.append(row)
        sequence += 1
        print(f"{model_cfg['model']} latency#{index}: HTTP {result['http_status']} "
              f"TTFT={result['ttft_ms']}ms TPS={result['output_tokens_per_second']}", flush=True)
    return sequence


def cache_probe(model_cfg, args, out, rows, sequence):
    prefix = fixed_prefix(args.cache_prefix_chars)
    for index in range(1, args.cache_rounds + 1):
        body = request_body(model_cfg["model"], "", 32, False, args.max_tokens_field)
        body["messages"] = [
            {"role": "system", "content": prefix},
            {"role": "user", "content": f"第{index}轮问题：只回复 CACHE_OK。"},
        ]
        result = json_request(model_cfg, body, args.timeout)
        evidence = write_evidence(out, sequence, model_cfg["model"], f"cache-{index}", body, result)
        row = {"case": "cache", "model": model_cfg["model"], "index": index,
               "http_status": result["http_status"], "elapsed_ms": result["elapsed_ms"],
               "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"],
               "total_tokens": result["total_tokens"], "cached_tokens": result["cached_tokens"],
               "cache_hit": index > 1 and result["cached_tokens"] > 0,
               "rate_headers": json.dumps(result["rate_headers"], ensure_ascii=False),
               "error": result["error"], "evidence_file": evidence}
        rows.append(row)
        sequence += 1
        print(f"{model_cfg['model']} cache#{index}: HTTP {result['http_status']} "
              f"cached={result['cached_tokens']}/{result['input_tokens']}", flush=True)
        if result["http_status"] in (401, 403, 429):
            break
    return sequence


def rate_probe(model_cfg, args, out, rows, sequence):
    prompt_padding = ("固定吞吐测试资料，不包含个人信息。" *
                      (args.rate_prompt_chars // 16 + 1))[:args.rate_prompt_chars]
    summaries = []
    for target_rpm in args.rpm_steps:
        planned = max(1, math.ceil(target_rpm * args.rate_duration_seconds / 60))
        interval = args.rate_duration_seconds / planned
        step_started = time.perf_counter()
        futures = []

        def one(index):
            prompt = prompt_padding + f"\n请求编号 {index}，只回复 RATE_OK。"
            body = request_body(model_cfg["model"], prompt, args.rate_output_tokens, False,
                                args.max_tokens_field)
            return body, json_request(model_cfg, body, args.timeout)

        with ThreadPoolExecutor(max_workers=args.rate_concurrency) as pool:
            for index in range(1, planned + 1):
                due = step_started + (index - 1) * interval
                delay = due - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                futures.append(pool.submit(one, index))
            step_results = []
            for index, future in enumerate(as_completed(futures), 1):
                body, result = future.result()
                step_results.append(result)
                evidence = write_evidence(out, sequence, model_cfg["model"],
                                          f"rate-{target_rpm}-{index}", body, result)
                rows.append({"case": "rate", "model": model_cfg["model"], "index": index,
                             "target_rpm": target_rpm, "http_status": result["http_status"],
                             "elapsed_ms": result["elapsed_ms"],
                             "input_tokens": result["input_tokens"],
                             "output_tokens": result["output_tokens"],
                             "total_tokens": result["total_tokens"],
                             "cached_tokens": result["cached_tokens"],
                             "rate_headers": json.dumps(result["rate_headers"], ensure_ascii=False),
                             "error": result["error"], "evidence_file": evidence})
                sequence += 1
        actual_seconds = max(args.rate_duration_seconds, time.perf_counter() - step_started)
        successes = [r for r in step_results if r["http_status"] == 200]
        limited = [r for r in step_results if r["http_status"] == 429]
        observed_rpm = len(successes) * 60 / actual_seconds
        observed_tpm = sum(r["total_tokens"] for r in successes) * 60 / actual_seconds
        headers = {}
        for result in step_results:
            headers.update(result["rate_headers"])
        summary = {
            "target_rpm": target_rpm, "planned_requests": planned,
            "successful_requests": len(successes), "rate_limited_requests": len(limited),
            "window_seconds": round(actual_seconds, 3),
            "observed_successful_rpm": round(observed_rpm, 3),
            "observed_successful_tpm": round(observed_tpm, 3),
            "rate_headers": headers,
            "conclusion": "limit_observed" if limited else "tested_lower_bound",
        }
        summaries.append(summary)
        print(f"{model_cfg['model']} rate target={target_rpm} RPM: "
              f"success={len(successes)}/{planned}, 429={len(limited)}, "
              f"observed={observed_rpm:.1f} RPM/{observed_tpm:.1f} TPM", flush=True)
        if limited:
            break
    return sequence, summaries


def summarize(model, rows, rate_summaries):
    latency = [r for r in rows if r["model"] == model and r["case"] == "latency"
               and r.get("http_status") == 200]
    cache = [r for r in rows if r["model"] == model and r["case"] == "cache"
             and r.get("index", 0) > 1 and r.get("http_status") == 200]
    ttft = [r["ttft_ms"] for r in latency if r.get("ttft_ms") is not None]
    tpot = [r["tpot_ms_per_token"] for r in latency if r.get("tpot_ms_per_token") is not None]
    tps = [r["output_tokens_per_second"] for r in latency
           if r.get("output_tokens_per_second") is not None]
    prompt_sum = sum(number(r.get("input_tokens")) for r in cache)
    cached_sum = sum(number(r.get("cached_tokens")) for r in cache)
    hits = sum(bool(r.get("cache_hit")) for r in cache)
    return {
        "model": model,
        "latency": {
            "successful_samples": len(latency),
            "ttft_ms_median": round(statistics.median(ttft), 3) if ttft else None,
            "ttft_ms_p95": percentile(ttft, 0.95),
            "tpot_ms_per_token_median": round(statistics.median(tpot), 3) if tpot else None,
            "output_tokens_per_second_median": round(statistics.median(tps), 3) if tps else None,
        },
        "kv_cache": {
            "warmup_excluded": True, "measured_requests": len(cache), "hit_requests": hits,
            "request_hit_rate": round(hits / len(cache), 6) if cache else None,
            "cached_tokens": cached_sum, "prompt_tokens": prompt_sum,
            "token_hit_rate": round(cached_sum / prompt_sum, 6) if prompt_sum else None,
        },
        "rate_limit": rate_summaries,
    }


def main():
    ap = argparse.ArgumentParser(description="TTFT、TPOT/TPS、TPM、RPM 和 KV Cache 命中率测试")
    ap.add_argument("--config", default=str(HERE / "config.template.json"))
    ap.add_argument("--models", help="仅测试指定模型 ID，逗号分隔")
    ap.add_argument("--metrics", default="ttft,tpot,tpm,rpm,cache")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--output-tokens", type=int, default=256)
    ap.add_argument("--cache-rounds", type=int, default=5)
    ap.add_argument("--cache-prefix-chars", type=int, default=12000)
    ap.add_argument("--rpm-steps", default="10,30,60")
    ap.add_argument("--rate-duration-seconds", type=int, default=30)
    ap.add_argument("--rate-concurrency", type=int, default=10)
    ap.add_argument("--rate-prompt-chars", type=int, default=1000)
    ap.add_argument("--rate-output-tokens", type=int, default=32)
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--max-tokens-field", choices=("max_tokens", "max_completion_tokens"),
                    default="max_completion_tokens")
    ap.add_argument("--max-requests", type=int, default=150)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--confirm-paid", action="store_true")
    args = ap.parse_args()

    selected_metrics = {x.strip() for x in args.metrics.split(",") if x.strip()}
    unknown = selected_metrics - METRICS
    if unknown:
        ap.error("未知指标：" + ",".join(sorted(unknown)))
    try:
        args.rpm_steps = sorted({int(x.strip()) for x in args.rpm_steps.split(",") if x.strip()})
    except ValueError:
        ap.error("--rpm-steps 必须是逗号分隔的正整数")
    if (not args.rpm_steps or min(args.rpm_steps) < 1 or max(args.rpm_steps) > 6000 or
            not 1 <= args.samples <= 20 or not 2 <= args.cache_rounds <= 100 or
            not 256 <= args.cache_prefix_chars <= 200000 or
            not 10 <= args.rate_duration_seconds <= 60 or
            not 1 <= args.rate_concurrency <= 100 or not 1 <= args.output_tokens <= 4096 or
            not 1 <= args.rate_output_tokens <= 4096 or not 0 <= args.rate_prompt_chars <= 200000):
        ap.error("测试参数超出安全范围")

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    models = model_configs(cfg, args.models)
    if not models:
        ap.error("配置中没有可测试的模型")
    args.timeout = args.timeout or int(cfg.get("timeout_seconds", (cfg.get("request") or {}).get("timeout_seconds", 90)))
    latency_count = args.samples if selected_metrics & {"ttft", "tpot"} else 0
    cache_count = args.cache_rounds if "cache" in selected_metrics else 0
    rate_count = (sum(math.ceil(rpm * args.rate_duration_seconds / 60) for rpm in args.rpm_steps)
                  if selected_metrics & {"tpm", "rpm"} else 0)
    planned = len(models) * (latency_count + cache_count + rate_count)
    print(f"计划最多 {planned} 个请求；模型={','.join(m['model'] for m in models)}；"
          f"指标={','.join(sorted(selected_metrics))}")
    if planned > args.max_requests:
        ap.error(f"计划请求数 {planned} 超过 --max-requests={args.max_requests}，请缩小测试或显式提高上限")
    if not (args.execute and args.confirm_paid):
        print("仅预览，不发送请求。真实执行必须同时添加 --execute --confirm-paid。")
        return
    for model in models:
        if (not str(model.get("base_url", "")).startswith("https://") and urlsplit(model["base_url"]).hostname not in ("localhost", "127.0.0.1", "::1")) or "YOUR_" in model["base_url"]:
            ap.error(f"{model['model']} 未配置实际 HTTPS base_url")
        if not os.environ.get(model.get("api_key_env", "MODEL_TEST_API_KEY")):
            ap.error(f"未设置 {model.get('api_key_env', 'MODEL_TEST_API_KEY')}")

    out = Path(os.environ.get("TEST_RESULT_ROOT", str(HERE / "results"))) / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-performance")
    out.mkdir(parents=True, exist_ok=False)
    rows, summaries, sequence = [], [], 1
    for model_cfg in models:
        if selected_metrics & {"ttft", "tpot"}:
            sequence = latency_probe(model_cfg, args, out, rows, sequence)
        if "cache" in selected_metrics:
            sequence = cache_probe(model_cfg, args, out, rows, sequence)
        rate_summaries = []
        if selected_metrics & {"tpm", "rpm"}:
            sequence, rate_summaries = rate_probe(model_cfg, args, out, rows, sequence)
        summaries.append(summarize(model_cfg["model"], rows, rate_summaries))

    fieldnames = sorted({key for row in rows for key in row})
    with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader(); writer.writerows(rows)
    (out / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print("结果目录：" + str(out))


if __name__ == "__main__":
    main()

"""Present source assertions without deriving model acceptance from HTTP success."""
from __future__ import annotations

import math
import statistics
import json
from collections import Counter

OUTCOMES = {
    "pass": "通过", "passed": "通过", "通过": "通过",
    "fail": "未通过", "failed": "未通过", "未通过": "未通过",
    "skip": "跳过", "skipped": "跳过", "manual_review": "人工复核",
    "review": "人工复核", "data_display": "数据展示", "metrics": "数据展示",
    "http_error": "未通过", "content_mismatch": "未通过", "invalid": "未通过",
}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def first(row, *names):
    return next((row[k] for k in names if row.get(k) not in (None, "")), "")


def detail(row, project, evidence, run_id):
    original = str(first(row, "result", "status", "verdict") or "未提供业务断言")
    return dict(project=project, run_id=run_id, case=str(first(row, "case", "case_id", "name", "round") or "请求"),
                outcome=OUTCOMES.get(original.lower(), "待复核"), original=original,
                http=first(row, "http_status", "status_code"), endpoint=first(row, "endpoint"),
                elapsed_ms=first(row, "elapsed_ms", "latency_ms", "total_ms"),
                ttft_ms=first(row, "ttft_ms"), tpot_ms=first(row, "tpot_ms"),
                input_tokens=first(row, "prompt_tokens", "input_tokens"),
                output_tokens=first(row, "completion_tokens", "output_tokens"),
                cached_tokens=first(row, "cached_tokens", "cache_read_input_tokens"),
                response=str(first(row, "content", "content_preview", "response", "error", "message", "note"))[:3000],
                checks={key: row[key] for key in ("exact_match", "expected", "finish_reason", "finish", "sse_done", "response_completed", "tool_calls", "error") if row.get(key) not in (None, "")},
                evidence=evidence)


def observed(item):
    def token(value):
        return "—" if value in (None, "") else value
    fields = [("HTTP", item["http"]), ("耗时 ms", item["elapsed_ms"]),
              ("输入 / 输出 Token", f'{token(item["input_tokens"])} / {token(item["output_tokens"])}'),
              ("TTFT ms（源脚本口径）", item["ttft_ms"]), ("TPOT ms", item["tpot_ms"]),
              ("缓存 Token", item["cached_tokens"]), ("响应", item["response"]),
              ("源脚本校验字段", json.dumps(item["checks"], ensure_ascii=False) if item["checks"] else "")]
    return "\n".join(f"{key}：{value}" for key, value in fields if value not in (None, ""))


def overview(details):
    counts = Counter(d["outcome"] for d in details)
    def mean(key):
        values = [n for d in details if (n := number(d.get(key))) is not None]
        return round(statistics.mean(values), 2) if values else None
    def total(key):
        values = [n for d in details if (n := number(d.get(key))) is not None]
        return sum(values) if values else None
    pairs = [(number(d.get("cached_tokens")), number(d.get("input_tokens"))) for d in details]
    pairs = [(cached, prompt) for cached, prompt in pairs if cached is not None and prompt is not None]
    denominator = sum(prompt for _, prompt in pairs)
    return dict(total=len(details), passed=counts["通过"], failed=counts["未通过"],
                review=sum(counts[k] for k in ("人工复核", "待复核")), skipped=counts["跳过"],
                data=counts["数据展示"], ttft_ms=mean("ttft_ms"), tpot_ms=mean("tpot_ms"),
                elapsed_ms=mean("elapsed_ms"), input_tokens=total("input_tokens"), output_tokens=total("output_tokens"),
                cache_token_ratio=round(sum(cached for cached, _ in pairs) / denominator * 100, 2) if denominator else None)

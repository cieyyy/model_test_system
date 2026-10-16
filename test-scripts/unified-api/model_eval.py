#!/usr/bin/env python3
"""Selective OpenAI-compatible model evaluation; never runs paid calls by default."""

import argparse
import csv
import json
import os
import time
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASE_NAMES = (
    "chat", "responses", "instruction", "self_correct", "json_object",
    "json_schema", "function", "parallel_tools", "chat_sse", "responses_sse",
    "conversation", "cache", "context", "concurrency",
)
WEATHER = {"type": "function", "function": {
    "name": "get_weather", "description": "查询城市和日期的天气",
    "parameters": {"type": "object", "properties": {
        "city": {"type": "string"}, "date": {"type": "string", "enum": ["今天", "明天"]}},
        "required": ["city", "date"], "additionalProperties": False}, "strict": True}}
CALCULATE = {"type": "function", "function": {
    "name": "calculate", "description": "计算两个整数的乘积",
    "parameters": {"type": "object", "properties": {
        "a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"], "additionalProperties": False}, "strict": True}}
PERSON_SCHEMA = {"type": "object", "properties": {
    "name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name", "age"], "additionalProperties": False}


def chat(model, prompt, **options):
    return {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": 256, "stream": False, **options}


def spec(name, model, index=1, context_chars=12000):
    if name == "responses":
        return "responses", {"model": model, "input": "只回复 RESP_OK", "max_output_tokens": 256}, "RESP_OK"
    if name == "responses_sse":
        return "responses", {"model": model, "input": "只回复 STREAM_OK", "max_output_tokens": 256, "stream": True}, "STREAM_OK"
    if name == "instruction":
        return "chat/completions", chat(model, "只回复 INSTRUCTION_OK，不要解释。"), "INSTRUCTION_OK"
    if name == "self_correct":
        body = chat(model, "请核对上一条错误回答，只回复正确金额。")
        body["messages"] = [{"role": "user", "content": "80元商品打八折后多少钱？"},
                            {"role": "assistant", "content": "72元"},
                            {"role": "user", "content": "请核对上一条错误回答，只回复正确金额。"}]
        return "chat/completions", body, "64"
    if name == "json_object":
        return "chat/completions", chat(model, "以 JSON 输出小林今年28岁，只含name和age。",
                                        response_format={"type": "json_object"}), "person"
    if name == "json_schema":
        fmt = {"type": "json_schema", "json_schema": {
            "name": "person", "strict": True, "schema": PERSON_SCHEMA}}
        return "chat/completions", chat(model, "以 JSON 提取小林今年28岁的姓名与年龄。",
                                        response_format=fmt), "person"
    if name == "function":
        return "chat/completions", chat(model, "调用工具查询北京明天天气。",
                                        tools=[WEATHER], tool_choice="required"), "weather"
    if name == "parallel_tools":
        return "chat/completions", chat(model, "调用两个工具：查上海今天天气，并算23乘47。",
                                        tools=[WEATHER, CALCULATE], tool_choice="required",
                                        parallel_tool_calls=True), "parallel"
    if name == "chat_sse":
        return "chat/completions", chat(model, "只回复 STREAM_OK", stream=True), "STREAM_OK"
    if name == "conversation":
        return "chat/completions", chat(model, f"第{index}轮，只回复 ACK-{index}"), f"ACK-{index}"
    if name == "cache":
        prefix = ("社区读书会每周六下午举行，提供安静阅读与书籍交换。" * (context_chars // 25 + 1))[:context_chars]
        body = chat(model, f"第{index}轮，只回复 CACHE_OK")
        body["messages"].insert(0, {"role": "system", "content": prefix})
        return "chat/completions", body, "CACHE_OK"
    if name == "context":
        fixture = ("社区阅读活动的预约采用编号，不收集私人地址。" * (context_chars // 23 + 1))[:context_chars]
        return "chat/completions", chat(model, "只回复末尾核验码：" + fixture + "\n核验码 CTX-731"), "CTX-731"
    if name == "concurrency":
        return "chat/completions", chat(model, f"只回复 PAR-{index}"), f"PAR-{index}"
    return "chat/completions", chat(model, "只回复 CHAT_OK"), "CHAT_OK"


def send(base, key, endpoint, body, timeout):
    url = base.rstrip("/") + "/" + endpoint
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "Accept": "text/event-stream" if body.get("stream") else "application/json"}, method="POST")
    start = time.perf_counter()
    status, raw, first_ms = None, "", None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            first_ms = round((time.perf_counter() - start) * 1000, 1)
            if body.get("stream"):
                chunks = []
                for line in response:
                    if line.strip() and not chunks:
                        first_ms = round((time.perf_counter() - start) * 1000, 1)
                    chunks.append(line.decode("utf-8", errors="replace"))
                raw = "".join(chunks)
            else:
                raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raw = json.dumps({"client_error": str(exc)}, ensure_ascii=False)
    return status, raw, first_ms, round((time.perf_counter() - start) * 1000, 1)


def unpack(raw, stream):
    if not stream:
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {"unparsed": raw}, [], False, False
        except json.JSONDecodeError:
            return {"unparsed": raw}, [], False, False
    events, done, chunks = [], False, []
    for line in raw.splitlines():
        if line.startswith("event:"):
            events.append(line[6:].strip())
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            continue
        try:
            chunks.append(json.loads(data))
        except json.JSONDecodeError:
            pass
    return {}, chunks, done, "response.completed" in events


def interpret(name, expected, obj, chunks, done, completed, status):
    if status != 200:
        return "http_error", "", "", "", {}, 0
    model, finish, content, calls, usage = "", "", "", [], {}
    if chunks:
        for chunk in chunks:
            model = chunk.get("model") or (chunk.get("response") or {}).get("model") or model
            usage = chunk.get("usage") or (chunk.get("response") or {}).get("usage") or usage
            choices = chunk.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                content += delta.get("content") or ""
                finish = choices[0].get("finish_reason") or finish
            if chunk.get("type") == "response.output_text.delta":
                content += chunk.get("delta") or ""
            if chunk.get("type") == "response.completed":
                response = chunk.get("response") or {}
                finish = response.get("status") or finish
                if not content:
                    content = responses_text(response)
        valid_end = completed if name == "responses_sse" else done
    else:
        model, usage = obj.get("model", ""), obj.get("usage") or {}
        if name == "responses":
            finish, content = obj.get("status", ""), responses_text(obj)
        else:
            choice = (obj.get("choices") or [{}])[0]
            finish = choice.get("finish_reason", "")
            message = choice.get("message") or {}
            content, calls = message.get("content") or "", message.get("tool_calls") or []
        valid_end = True
    if name in ("json_object", "json_schema"):
        try:
            person = json.loads(content)
            valid = isinstance(person, dict) and set(person) == {"name", "age"} and person["name"] == "小林" and type(person["age"]) is int and person["age"] == 28
        except (json.JSONDecodeError, TypeError, KeyError):
            valid = False
    elif name == "function":
        valid = any(tool_name(c) == "get_weather" and tool_args(c) == {"city": "北京", "date": "明天"} for c in calls)
    elif name == "parallel_tools":
        valid = len(calls) == 2 and {tool_name(c) for c in calls} == {"get_weather", "calculate"} and any(tool_args(c) == {"a": 23, "b": 47} for c in calls)
    elif name == "self_correct":
        valid = "64" in content and "72" not in content
    else:
        valid = content.strip() == expected
    if finish == "length":
        valid = False
    return "pass" if valid and valid_end else "fail", model, finish, content, usage, len(calls)


def responses_text(obj):
    return "".join(part.get("text", "") for item in obj.get("output", []) if item.get("type") == "message"
                   for part in item.get("content", []) if part.get("type") in ("output_text", "text"))


def tool_name(call):
    return (call.get("function") or {}).get("name")


def tool_args(call):
    try:
        return json.loads((call.get("function") or {}).get("arguments") or "{}")
    except json.JSONDecodeError:
        return None


def main():
    ap = argparse.ArgumentParser(description="统一模型 API 测试；默认仅预览，不发请求")
    ap.add_argument("--config", default=str(HERE / "config.template.json"))
    ap.add_argument("--cases", default="chat,responses,instruction,self_correct,json_object,json_schema,function,parallel_tools,chat_sse,responses_sse")
    ap.add_argument("--models", help="逗号分隔模型 ID，覆盖配置")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--confirm-paid", action="store_true")
    ap.add_argument("--max-requests", type=int, default=10)
    ap.add_argument("--rounds", type=int, default=5, help="conversation/cache 轮数")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--context-chars", type=int, default=12000)
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    models = [m.strip() for m in (args.models or ",".join(cfg["models"])).split(",") if m.strip()]
    cases = [x.strip() for x in args.cases.split(",") if x.strip()]
    invalid = set(cases) - set(CASE_NAMES)
    if invalid:
        ap.error("未知项目：" + ",".join(sorted(invalid)))
    if args.max_requests < 1 or not 1 <= args.rounds <= 200 or not 1 <= args.concurrency <= 20 or not 256 <= args.context_chars <= 120000:
        ap.error("max-requests、rounds、concurrency 或 context-chars 超出安全范围")
    if args.list:
        print("可选项目：" + ", ".join(CASE_NAMES))
        return
    plan = [(model, name, i) for name in cases for i in range(1, (args.rounds if name in ("conversation", "cache") else args.concurrency if name == "concurrency" else 1) + 1) for model in models]
    print(f"计划 {len(plan)} 个请求；模型={','.join(models)}；项目={','.join(cases)}；最多执行={args.max_requests}")
    if not (args.execute and args.confirm_paid):
        print("仅预览。真实调用必须同时添加 --execute --confirm-paid；不会自动扣费。")
        return
    if (not cfg["base_url"].startswith("https://") and urlsplit(cfg["base_url"]).hostname not in ("localhost", "127.0.0.1", "::1")) or "YOUR_PROVIDER" in cfg["base_url"]:
        ap.error("请在配置文件中填写实际 HTTPS base_url")
    key = os.environ.get(cfg.get("api_key_env", "MODEL_TEST_API_KEY"), "")
    if not key:
        ap.error("未设置配置指定的 Key 环境变量")
    timeout = int(cfg.get("timeout_seconds", 90))
    out = Path(os.environ.get("TEST_RESULT_ROOT", str(HERE / "results"))) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=False)
    rows, histories, halted = [], {model: [] for model in models}, False
    record_lock = threading.Lock()

    def run_one(model, name, index):
        endpoint, body, expected = spec(name, model, index, args.context_chars)
        if name == "conversation":
            body["messages"] = histories[model] + body["messages"]
        status, raw, first_ms, elapsed = send(cfg["base_url"], key, endpoint, body, timeout)
        obj, chunks, done, completed = unpack(raw, body.get("stream", False))
        verdict, returned, finish, content, usage, calls = interpret(name, expected, obj, chunks, done, completed, status)
        raw_name = f"{model}-{name}-{index}.json".replace("/", "_")
        evidence = {"timestamp": datetime.now(timezone.utc).isoformat(), "endpoint": endpoint,
                    "request": body, "http_status": status, "response_raw": raw,
                    "ttft_ms": first_ms, "elapsed_ms": elapsed}
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        row = {"case": name, "model": model, "index": index, "http_status": status,
               "result": verdict, "response_model": returned, "finish": finish,
               "expected": expected, "content": content[:300], "tool_calls": calls,
               "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens", "")),
               "output_tokens": usage.get("completion_tokens", usage.get("output_tokens", "")),
               "reasoning_tokens": (usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}).get("reasoning_tokens", ""),
               "cached_tokens": details.get("cached_tokens", usage.get("cached_tokens", "")),
               "cache_creation_tokens": details.get("cache_creation_input_tokens", ""),
               "ttft_ms": first_ms, "elapsed_ms": elapsed, "sse_done": done,
               "response_completed": completed, "raw_file": raw_name}
        with record_lock:
            (out / raw_name).write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            rows.append(row)
            if name == "conversation" and status == 200 and obj.get("choices"):
                histories[model].extend([body["messages"][-1], obj["choices"][0].get("message") or {}])
        print(f"{model} {name}#{index}: HTTP {status} {verdict}, {elapsed}ms", flush=True)
        return status

    for model, name, index in plan:
        if halted or len(rows) >= args.max_requests:
            break
        if name == "concurrency":
            # A small explicit probe is grouped by model; no other tests run concurrently.
            group = [(m, n, i) for m, n, i in plan if n == name and m == model]
            if index != 1:
                continue
            group = group[:min(args.concurrency, args.max_requests - len(rows))]
            with ThreadPoolExecutor(max_workers=len(group)) as pool:
                futures = [pool.submit(run_one, *item) for item in group]
                for future in as_completed(futures):
                    if future.result() in (401, 403, 429):
                        halted = True
        else:
            status = run_one(model, name, index)
            if status in (401, 403, 429):
                halted = True
        if halted:
            print("鉴权、额度或限流错误，停止后续请求。")
    if rows:
        with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader(); writer.writerows(rows)
    print("结果目录：" + str(out))


if __name__ == "__main__":
    main()

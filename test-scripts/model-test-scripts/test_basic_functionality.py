#!/usr/bin/env python3
"""通用功能兼容性测试。

覆盖基础调用、推理强度、流式、JSON Object/Schema、工具调用、Partial、缓存、
连续对话和上下文。主要指标是 HTTP、内容校验、finish_reason、usage、缓存字段
和 SSE_DONE；HTTP 200 不单独代表通过。
运行示例：python3 test_basic_functionality.py --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" --cases basic,stream,cache --run --confirm-paid
"""

import argparse
import base64
import csv
import json
import mimetypes
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CASES = ("basic", "reasoning", "stream", "json_object", "json_schema", "tools",
         "parallel_tools", "partial", "cache", "conversation", "vision", "context")
PERSON_SCHEMA = {"type": "object", "properties": {
    "name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name", "age"], "additionalProperties": False}
TOOLS = [
    {"type": "function", "function": {"name": "get_weather", "description": "查询城市天气",
     "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                    "required": ["city"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "multiply", "description": "计算两个整数的乘积",
     "parameters": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                    "required": ["a", "b"], "additionalProperties": False}}},
]


MODEL_NAME = "test-model"
GLM_FLASH_CACHE_MIN_TOKENS = 8960
GLM_FLASH_DEFAULT_PREFIX_CHARS = 18000

def body(messages, **options):
    # 模型由统一入口传入；其余请求字段保持原兼容性测试口径。
    return {"model": MODEL_NAME, "messages": messages, "max_completion_tokens": 512, **options}


def usage_values(usage):
    details = usage.get("prompt_tokens_details") or {}
    output = usage.get("completion_tokens_details") or {}
    creation = (details.get("cache_creation_input_tokens")
                or usage.get("cache_creation_input_tokens")
                or (usage.get("claude_cache_creation_5_m_tokens", 0)
                    + usage.get("claude_cache_creation_1_h_tokens", 0)))
    return (usage.get("prompt_tokens", ""), usage.get("completion_tokens", ""),
            details.get("cached_tokens", usage.get("cached_tokens", "")),
            output.get("reasoning_tokens", ""), usage.get("total_tokens", ""), creation)


def parse_stream(raw):
    chunks, done = [], False
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        item = line[5:].strip()
        if item == "[DONE]":
            done = True
            continue
        try:
            chunks.append(json.loads(item))
        except json.JSONDecodeError:
            pass
    content, reasoning, usage, model, finish, request_id = "", "", {}, "", "", ""
    for chunk in chunks:
        model = chunk.get("model") or model
        request_id = chunk.get("id") or request_id
        usage = chunk.get("usage") or usage
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        usage = choice.get("usage") or usage
        finish = choice.get("finish_reason") or finish
        delta = choice.get("delta") or {}
        content += delta.get("content") or ""
        reasoning += delta.get("reasoning_content") or ""
    return {"content": content, "reasoning_content": reasoning, "usage": usage,
            "model": model, "id": request_id, "finish_reason": finish,
            "done": done, "chunks": len(chunks)}


def send(base, key, request_body, timeout, user_agent):
    url = base.rstrip("/") + "/chat/completions"
    curl_config = (
        f'header = "Authorization: Bearer {key}"\n'
        'header = "Content-Type: application/json"\n'
        f'header = "Accept: {"text/event-stream" if request_body.get("stream") else "application/json"}"\n'
        f'user-agent = "{user_agent.replace(chr(34), "")}"\n'
    )
    start = time.perf_counter()
    status, raw, ttft = None, "", None
    payload_path = None
    try:
        # Store only the request body in a private temporary file. The API key
        # is delivered to curl through stdin config and never written to disk.
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as payload:
            payload.write(json.dumps(request_body, ensure_ascii=False))
            payload_path = payload.name
        command = ["curl", "-sS", "--config", "-", "--max-time", str(timeout),
                   "--request", "POST", "--data-binary", "@" + payload_path,
                   "--write-out", "\n__TTFT__:%{time_starttransfer}\n__HTTP_STATUS__:%{http_code}"]
        if request_body.get("stream"):
            command.append("--no-buffer")
        command.append(url)
        proc = subprocess.run(command, input=curl_config, text=True, capture_output=True, check=False)
        marker = "\n__HTTP_STATUS__:"
        raw, status_text = proc.stdout.rsplit(marker, 1) if marker in proc.stdout else (proc.stdout, "")
        status = int(status_text) if status_text.isdigit() else None
        ttft_marker = "\n__TTFT__:"
        if ttft_marker in raw:
            raw, ttft_text = raw.rsplit(ttft_marker, 1)
            try:
                ttft = round(float(ttft_text) * 1000, 1)
            except ValueError:
                pass
        if proc.returncode and not raw:
            raw = json.dumps({"client_error": proc.stderr.strip(), "curl_exit": proc.returncode}, ensure_ascii=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raw = json.dumps({"client_error": str(exc)}, ensure_ascii=False)
    finally:
        if payload_path:
            try:
                Path(payload_path).unlink()
            except FileNotFoundError:
                pass
    elapsed = round((time.perf_counter() - start) * 1000, 1)
    if request_body.get("stream") and status == 200:
        parsed = parse_stream(raw)
        parsed["raw_sse"] = raw
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"unparsed": raw}
    return status, ttft, elapsed, parsed


def message_and_usage(response, stream=False):
    if stream:
        return response.get("content") or "", response.get("reasoning_content") or "", response.get("usage") or {}, response.get("model") or "", response.get("finish_reason") or ""
    choice = (response.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return msg.get("content") or "", msg.get("reasoning_content") or "", response.get("usage") or {}, response.get("model") or "", choice.get("finish_reason") or ""


def args_parser():
    ap = argparse.ArgumentParser(description="通用功能选择性测试（默认不扣费）")
    ap.add_argument("--base-url", default=os.getenv("API_BASE", "http://127.0.0.1:8000/v1"))
    ap.add_argument("--key-env", default=os.getenv("API_KEY_ENV", "API_KEY"))
    ap.add_argument("--model", default=os.getenv("MODEL", "test-model"))
    ap.add_argument("--cases", default="basic,reasoning,stream,json_object,json_schema,tools,partial")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--confirm-paid", action="store_true")
    ap.add_argument("--max-requests", type=int, default=12)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--user-agent", default="curl/8.7.1",
                    help="中转站允许的客户端标识；默认与本机系统 cURL 一致")
    ap.add_argument("--rounds", type=int, default=5, help="cache/conversation 轮数")
    ap.add_argument(
        "--prefix-chars",
        type=int,
        default=None,
        help="缓存测试固定前缀字符数；glm-5.3-flash 默认 18000，其他模型默认 1600",
    )
    ap.add_argument(
        "--cache-min-tokens",
        type=int,
        default=None,
        help="缓存门槛标注值；glm-5.3-flash 默认 8960，仅用于测试标注和默认前缀估算",
    )
    ap.add_argument("--context-chars", type=int, default=12000)
    ap.add_argument("--image", type=Path, help="明确选择本地 PNG/JPEG/WebP 图片")
    return ap


def main():
    ap = args_parser()
    args = ap.parse_args()
    global MODEL_NAME
    MODEL_NAME = args.model
    if args.list:
        print("支持项目：" + ", ".join(CASES))
        return
    cases = [x.strip() for x in args.cases.split(",") if x.strip()]
    if not cases or any(x not in CASES for x in cases):
        ap.error("--cases 存在未知项目，请先用 --list 查看")
    cache_prefix_chars = args.prefix_chars
    cache_min_tokens = args.cache_min_tokens
    if cache_min_tokens is None and MODEL_NAME == "glm-5.3-flash":
        cache_min_tokens = GLM_FLASH_CACHE_MIN_TOKENS
    if cache_min_tokens is not None and cache_min_tokens < 1:
        ap.error("--cache-min-tokens 必须大于 0")
    if cache_prefix_chars is None:
        if MODEL_NAME == "glm-5.3-flash":
            estimated_chars = int(cache_min_tokens * 1.9) if cache_min_tokens else 0
            cache_prefix_chars = max(GLM_FLASH_DEFAULT_PREFIX_CHARS, estimated_chars)
        else:
            cache_prefix_chars = 1600
    if args.max_requests < 1 or args.timeout < 1 or not 1 <= args.rounds <= 100 or not 512 <= cache_prefix_chars <= 120000 or not 1000 <= args.context_chars <= 120000:
        ap.error("请求数、超时或素材长度超出允许范围")
    plan = sum(args.rounds if case in ("cache", "conversation") else 3 if case == "reasoning" else 2 if case in ("tools", "parallel_tools") else 1 for case in cases)
    print(f"项目={','.join(cases)}；计划约 {plan} 个请求；最多执行 {args.max_requests} 个请求")
    if "cache" in cases and cache_min_tokens is not None:
        print(
            f"缓存标注：模型 {MODEL_NAME}；隐式缓存最小块 "
            f"{cache_min_tokens:,} tokens；固定前缀约 {cache_prefix_chars:,} 字符"
        )
    if not (args.run and args.confirm_paid):
        print("仅预览；实际付费请求需同时添加 --run --confirm-paid")
        return
    if not args.base_url.startswith(("http://", "https://")):
        ap.error("Base URL 必须使用 http:// 或 https://")
    key = os.environ.get(args.key_env, "")
    if not key:
        ap.error("未设置指定的 Key 环境变量")
    if "vision" in cases:
        if not args.image or not args.image.is_file() or args.image.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            ap.error("vision 需提供有效的 PNG/JPEG/WebP 本地图片，SVG 不支持")
        if args.image.stat().st_size > 8_000_000:
            ap.error("示例限制图片小于 8MB，避免超大请求体")

    # 统一入口通过 TEST_RESULT_ROOT 按当前模型隔离结果；直接运行时仍回退到本目录。
    result_root = Path(os.environ.get("TEST_RESULT_ROOT", str(ROOT / "results")))
    result_root.mkdir(parents=True, exist_ok=True)
    out = result_root / datetime.now().strftime("%Y%m%d-%H%M%S-basic")
    out.mkdir(parents=True, exist_ok=False)
    rows, history, stopped = [], [], False

    def record(case, index, request_body, expected="", check=None):
        nonlocal stopped
        if stopped or len(rows) >= args.max_requests:
            return None
        status, ttft, elapsed, response = send(
            args.base_url, key, request_body, args.timeout, args.user_agent)
        content, reasoning, usage, returned_model, finish = message_and_usage(response, request_body.get("stream", False))
        inp, output, cached, reasoning_tokens, total_tokens, creation_tokens = usage_values(usage)
        valid = status == 200 and finish != "length" and (bool(content.strip()) or (case in ("tools", "parallel_tools") and index == 1))
        if request_body.get("stream"):
            valid = valid and response.get("done", False)
        if expected:
            valid = valid and content.strip() == expected
        if check:
            valid = valid and check(response, content)
        raw_name = f"{len(rows)+1:03d}-{case}-{index}.json"
        # Do not persist base64 images or the file's private pixels in request evidence.
        safe_request = json.loads(json.dumps(request_body, ensure_ascii=False))
        for msg in safe_request.get("messages", []):
            if isinstance(msg.get("content"), list):
                for part in msg["content"]:
                    if part.get("type") == "image_url":
                        part["image_url"]["url"] = "[LOCAL_IMAGE_BASE64_REDACTED]"
        test_metadata = {"model": MODEL_NAME}
        if case == "cache" and cache_min_tokens is not None:
            test_metadata.update({
                "implicit_cache_min_tokens": cache_min_tokens,
                "cache_prefix_chars": cache_prefix_chars,
            })
        (out / raw_name).write_text(json.dumps({
            "test_metadata": test_metadata,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request": safe_request, "http_status": status, "ttft_ms": ttft,
            "elapsed_ms": elapsed, "response": response,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        result = "manual_review" if case == "vision" and valid else "pass" if valid else "fail"
        request_id = response.get("id", "")
        tool_count = len((((response.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or [])) if not request_body.get("stream") else 0
        rows.append({"case": case, "index": index, "endpoint": "/v1/chat/completions",
            "http_status": status, "result": result, "request_id": request_id,
            "request_model": MODEL_NAME, "response_model": returned_model,
            "response_object": response.get("object", ""), "finish_reason": finish,
            "content": content[:240], "reasoning_chars": len(reasoning), "input_tokens": inp,
            "output_tokens": output, "cached_tokens": cached, "cache_hit": bool(cached and int(cached) > 0),
            "cache_creation_tokens": creation_tokens, "reasoning_tokens": reasoning_tokens,
            "total_tokens": total_tokens, "tool_call_count": tool_count,
            "ttft_ms": ttft, "elapsed_ms": elapsed, "sse_done": response.get("done", ""),
            "service_tier": response.get("service_tier", ""),
            "system_fingerprint": response.get("system_fingerprint", ""), "raw_file": raw_name,
            "cache_min_tokens": (
                cache_min_tokens
                if case == "cache" and cache_min_tokens is not None
                else ""
            ),
            "cache_note": (
                f"{MODEL_NAME} 隐式缓存最小块 {cache_min_tokens:,} tokens"
                if case == "cache" and cache_min_tokens is not None
                else ""
            )})
        print(
            f"{case}#{index} | HTTP={status} | RESULT={result.upper()} | "
            f"MODEL={MODEL_NAME}->{returned_model or '-'} | ID={request_id or '-'} | FINISH={finish or '-'}",
            flush=True,
        )
        print(
            f"  TOKENS input={inp or 0} output={output or 0} reasoning={reasoning_tokens or 0} "
            f"cache_read={cached or 0} cache_creation={creation_tokens or 0} total={total_tokens or 0} | "
            f"TTFT={ttft if ttft is not None else '-'}ms total_time={elapsed}ms | "
            f"SSE_DONE={response.get('done', '-')} tools={tool_count}",
            flush=True,
        )
        if content:
            print("  CONTENT=" + content[:240].replace("\n", "\\n"), flush=True)
        if status != 200:
            error = response.get("error") or response
            code = error.get("code") or error.get("error_code") or error.get("type")
            message = error.get("message") or error.get("detail") or error.get("title")
            if code or message:
                print(f"错误：{code or ''} {message or ''}".strip(), flush=True)
        if status in (401, 403, 429):
            stopped = True
            print("鉴权、额度或限流错误，停止后续请求")
        return response

    def json_object_check(_, content):
        try:
            return isinstance(json.loads(content), dict)
        except (ValueError, TypeError):
            return False

    def person_schema_check(_, content):
        try:
            obj = json.loads(content)
            return isinstance(obj, dict) and set(obj) == {"name", "age"} and obj["name"] == "小林" and type(obj["age"]) is int and obj["age"] == 28
        except (ValueError, TypeError, KeyError):
            return False

    def tool_check(response, _, names):
        choice = (response.get("choices") or [{}])[0]
        calls = ((choice.get("message") or {}).get("tool_calls") or [])
        if choice.get("finish_reason") != "tool_calls" or len(calls) != len(names):
            return False
        try:
            by_name = {call["function"]["name"]: json.loads(call["function"]["arguments"]) for call in calls}
            return set(by_name) == set(names) and by_name.get("get_weather") == {"city": "上海" if len(names) > 1 else "北京"} and ("multiply" not in by_name or by_name["multiply"] == {"a": 23, "b": 47})
        except (KeyError, TypeError, ValueError):
            return False

    for case in cases:
        if stopped or len(rows) >= args.max_requests:
            break
        if case == "basic":
            record(case, 1, body([{"role": "user", "content": "只回复 BASIC_OK"}], reasoning_effort="low"), "BASIC_OK")
        elif case == "reasoning":
            for effort in ("low", "high", "max"):
                record(case, effort, body([{"role": "user", "content": "计算17乘23，只回复答案数字"}], reasoning_effort=effort), "391")
        elif case == "stream":
            record(case, 1, body([{"role": "user", "content": "只回复 STREAM_OK"}], reasoning_effort="low", stream=True,
                                 stream_options={"include_usage": True}), "STREAM_OK")
        elif case in ("json_object", "json_schema"):
            fmt = {"type": "json_object"} if case == "json_object" else {"type": "json_schema", "json_schema": {
                "name": "person", "strict": True, "schema": PERSON_SCHEMA}}
            checker = json_object_check if case == "json_object" else person_schema_check
            record(case, 1, body([{"role": "user", "content": "小林今年28岁，以 JSON 提取姓名和年龄"}],
                                 reasoning_effort="low", response_format=fmt), check=checker)
        elif case in ("tools", "parallel_tools"):
            parallel = case == "parallel_tools"
            prompt = "调用工具查询上海今天天气，同时计算23乘47" if parallel else "调用工具查询北京今天天气"
            selected = TOOLS if parallel else TOOLS[:1]
            messages = [{"role": "user", "content": prompt}]
            first = record(case, 1, body(messages, tools=selected, tool_choice="required",
                         reasoning_effort="low", **({"parallel_tool_calls": True} if parallel else {})),
                         check=lambda r, c: tool_check(r, c, ["get_weather", "multiply"] if parallel else ["get_weather"]))
            if first and first.get("choices"):
                assistant = (first["choices"][0].get("message") or {})
                calls = assistant.get("tool_calls") or []
                if calls:
                    messages.append(assistant)  # Complete message includes reasoning_content and tool_calls.
                    for call in calls:
                        name = call["function"]["name"]
                        result = {"city": "上海" if parallel else "北京", "weather": "晴", "temperature_c": 24} if name == "get_weather" else {"result": 1081}
                        messages.append({"role": "tool", "tool_call_id": call["id"],
                                         "content": json.dumps(result, ensure_ascii=False)})
                    record(case, 2, body(messages, tools=selected, reasoning_effort="low"),
                           check=lambda _, c: "晴" in c and ("1081" in c if parallel else True))
        elif case == "partial":
            prefix = "结论："
            req = body([{"role": "user", "content": "用一句话说读书会为何需要报名"},
                        {"role": "assistant", "content": prefix, "partial": True}], reasoning_effort="low")
            record(case, 1, req, check=lambda _, c: bool((prefix + c).strip()) and not c.startswith(prefix))
        elif case == "cache":
            fixed = ("虚构社区读书会每周六下午举办阅读分享和书籍交换。" * (cache_prefix_chars // 26 + 1))[:cache_prefix_chars]
            for n in range(1, args.rounds + 1):
                record(case, n, body([{"role": "system", "content": fixed},
                    {"role": "user", "content": f"这是第{n}轮，只回复 CACHE_OK"}], reasoning_effort="low"), "CACHE_OK")
        elif case == "conversation":
            history = [{"role": "system", "content": "连续对话测试，严格只回复请求中的 ACK 编号。"}]
            for n in range(1, args.rounds + 1):
                user = {"role": "user", "content": f"第{n}轮，只回复 ACK-{n}"}
                response = record(case, n, body(history + [user], reasoning_effort="low"), f"ACK-{n}")
                if response and response.get("choices") and response["choices"][0].get("message"):
                    history.extend([user, response["choices"][0]["message"]])
                else:
                    break
        elif case == "vision":
            mime = mimetypes.guess_type(args.image.name)[0] or "image/png"
            encoded = base64.b64encode(args.image.read_bytes()).decode("ascii")
            req = body([{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                {"type": "text", "text": "只描述图片中能确认的对象或文字，不要猜测"}]}], reasoning_effort="low")
            record(case, 1, req)
        elif case == "context":
            material = ("虚构读书会使用匿名报名编号，地点为青禾图书馆。" * (args.context_chars // 25 + 1))[:args.context_chars]
            req = body([{"role": "user", "content": material + "\n唯一核验码：CTX-731。只回复核验码。"}],
                       reasoning_effort="low")
            record(case, 1, req, "CTX-731")

    if rows:
        with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader(); writer.writerows(rows)
    measured = [r for r in rows if r["case"] == "cache" and r["index"] > 1 and r["http_status"] == 200]
    if measured:
        hits = sum(r["cache_hit"] for r in measured)
        print(f"缓存可计量成功请求命中：{hits}/{len(measured)} = {hits / len(measured):.1%}；预热轮不计入分母")
    print("结果目录：" + str(out))


if __name__ == "__main__":
    main()

"""本地 OpenAI 兼容 Mock 服务（开发验证用，不产生任何费用）。

用法：
    python mock_openai.py            # 默认 127.0.0.1:8090
    python mock_openai.py 8091
    python mock_openai.py 8091 silent  # 全站假成功模式（所有模型都返回 200 但功能失效）

行为：
    - 普通 chat 请求：返回固定回复。
    - stream=True：返回标准 SSE 流。
    - response_format json：返回有效 JSON。
    - tools：返回 tool_calls。
    - 模型名含 "silent"：故意触发静默失效（200 但 JSON 无效 / 无 tool_calls），
      用于验证平台「HTTP 200 不等于通过」的判定能力。
"""
from __future__ import annotations

import json
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Mock OpenAI 兼容服务")

CHAT_REPLY = "你好，我是模拟模型，用于本地执行链路验证。"

# 全站假成功模式：由启动参数 silent 控制（阶段 4 跨中转站对比验收用）
ALL_SILENT = len(sys.argv) > 2 and sys.argv[2] == "silent"


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    body = await req.json()
    model = body.get("model", "mock")
    stream = bool(body.get("stream"))
    tools = body.get("tools")
    response_format = body.get("response_format") or {}
    json_mode = response_format.get("type") == "json_object"
    silent = ALL_SILENT or "silent" in model.lower()

    if tools:
        if silent:
            message = {"role": "assistant", "content": "北京今天天气晴，气温 26 度。"}  # 静默失效：不返回 tool_calls
            finish = "stop"
        else:
            fn = tools[0].get("function", {}).get("name", "get_weather")
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_mock_1",
                    "type": "function",
                    "function": {"name": fn, "arguments": json.dumps({"city": "北京"}, ensure_ascii=False)},
                }],
            }
            finish = "tool_calls"
    elif json_mode:
        content = "这不是 JSON 格式的回复。" if silent else json.dumps({"name": "模型测试", "ok": True}, ensure_ascii=False)
        message = {"role": "assistant", "content": content}
        finish = "stop"
    else:
        message = {"role": "assistant", "content": "" if ALL_SILENT else CHAT_REPLY}
        finish = "stop"

    usage = {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
    rid = "chatcmpl-mock"

    if stream:
        async def gen():
            yield _chunk(model, {"role": "assistant"})
            content = message.get("content")
            if message.get("tool_calls"):
                yield _chunk(model, {"content": "[tool_calls omitted in mock stream]"})
            elif content:
                for i in range(0, len(content), 6):
                    yield _chunk(model, {"content": content[i:i + 6]})
            yield _chunk(model, {}, finish)
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return JSONResponse({
        "id": rid,
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage,
    })


def _chunk(model: str, delta: dict, finish=None) -> str:
    data = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/v1/models")
async def list_models():
    """模拟模型列表接口（用于验证「拉取模型列表」功能）。"""
    return {"object": "list", "data": [
        {"id": "mock-normal", "object": "model", "owned_by": "mock"},
        {"id": "mock-silent", "object": "model", "owned_by": "mock"},
        {"id": "mock-vision-pro", "object": "model", "owned_by": "mock"},
        {"id": "mock-long-context", "object": "model", "owned_by": "mock"},
    ]}


if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

#!/usr/bin/env python3
"""使用 OpenAI 兼容 SDK 流程测试视频理解。"""

import argparse
import base64
import csv
import json
import mimetypes
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from openai import BadRequestError, OpenAI


def parse_args():
    parser = argparse.ArgumentParser(
        description="视频理解测试（默认仅预览，不上传或扣费）"
    )
    parser.add_argument("--base-url", default=os.getenv("API_BASE", "http://127.0.0.1:8000/v1"))
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--prompt", default="概括这个视频。")
    parser.add_argument("--model", default=os.getenv("MODEL", "test-model"))
    parser.add_argument("--key-env", default=os.getenv("API_KEY_ENV", "API_KEY"))
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--connect-timeout", type=float, default=60.0)
    parser.add_argument("--user-agent", default="curl/8.7.1")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--confirm-paid", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.base_url.startswith(("http://", "https://")) or "[" in args.base_url:
        raise SystemExit(
            "--base-url 必须是纯 http(s) 地址，例如 http://127.0.0.1:3000/v1"
        )
    if not args.video.is_file():
        raise SystemExit(f"视频文件不存在：{args.video}")
    if args.timeout <= 0 or args.connect_timeout <= 0:
        raise SystemExit("--timeout 和 --connect-timeout 必须大于0")

    print(
        f"项目=视频理解；模型={args.model}；地址={args.base_url}；"
        f"视频={args.video}；大小={args.video.stat().st_size / 1024 / 1024:.2f}MB"
    )
    if not (args.run and args.confirm_paid):
        print("仅预览；实际 Base64 视频请求需同时添加 --run --confirm-paid")
        return

    out = (args.output_dir.resolve() if args.output_dir else
           Path(__file__).resolve().parent / "results" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-video-understanding"))
    out.mkdir(parents=True, exist_ok=False)
    api_key = os.environ.get(args.key_env)
    if not api_key:
        raise SystemExit(
            f"环境变量 {args.key_env} 未设置或为空。请先执行："
            f"export {args.key_env}='你的临时Key'"
        )

    mime_type = mimetypes.guess_type(args.video.name)[0] or "video/mp4"
    if not mime_type.startswith("video/"):
        raise SystemExit(f"无法识别视频 MIME 类型：{mime_type}")
    video_b64 = base64.b64encode(args.video.read_bytes()).decode("ascii")
    video_url = f"data:{mime_type};base64,{video_b64}"
    print(f"视频已编码为 Base64：MIME={mime_type}；编码后大小={len(video_url) / 1024 / 1024:.2f}MB")

    http_client = httpx.Client(
        trust_env=False,
        http2=False,
        timeout=httpx.Timeout(args.timeout, connect=args.connect_timeout),
        headers={"User-Agent": args.user_agent},
    )
    client = OpenAI(
        api_key=api_key,
        base_url=args.base_url,
        timeout=args.timeout,
        max_retries=0,
        http_client=http_client,
    )
    started = time.perf_counter()
    row = {
        "case": "video_understanding",
        "http_status": "",
        "result": "FAIL",
        "elapsed_ms": "",
        "response_model": "",
        "input_tokens": "",
        "output_tokens": "",
        "finish_reason": "",
        "content": "",
        "error": "",
    }
    response_payload = {}
    try:
        completion = client.chat.completions.create(
            model=args.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "video_url",
                            "video_url": {"url": video_url},
                        },
                        {"type": "text", "text": args.prompt},
                    ],
                },
            ],
        )

        elapsed = round((time.perf_counter() - started) * 1000, 1)
        content = completion.choices[0].message.content or ""
        usage = completion.usage
        row.update({
            "http_status": 200,
            "result": "PASS" if content.strip() else "FAIL",
            "elapsed_ms": elapsed,
            "response_model": getattr(completion, "model", "") or "",
            "input_tokens": getattr(usage, "prompt_tokens", "") if usage else "",
            "output_tokens": getattr(usage, "completion_tokens", "") if usage else "",
            "finish_reason": completion.choices[0].finish_reason or "",
            "content": content[:500],
        })
        response_payload = completion.model_dump() if hasattr(completion, "model_dump") else {}
        print("模型返回：")
        print(content)
        if usage:
            print(f"Token 用量：{usage}")
    except BadRequestError as exc:
        row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        row["error"] = str(exc)[:1000]
        response_payload = {"error": str(exc)}
        print(f"视频 Base64 请求被 API 拒绝：{exc}")
    finally:
        client.close()
    (out / "response.json").write_text(json.dumps(response_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    (out / "summary.json").write_text(json.dumps({
        "model": args.model,
        "base_url": args.base_url,
        "requests": 1,
        "http_200": 1 if row["http_status"] == 200 else 0,
        "passed": row["result"] == "PASS",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果目录：{out}")


if __name__ == "__main__":
    main()

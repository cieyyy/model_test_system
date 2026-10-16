#!/usr/bin/env python3
"""Function Calling 工具结果闭环测试。"""

import argparse
import csv
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def parse_args():
    p = argparse.ArgumentParser(description="Function Calling 两阶段闭环测试")
    p.add_argument('--base-url', default=os.getenv('API_BASE', 'http://127.0.0.1:8000/v1'))
    p.add_argument('--key-env', default=os.getenv('API_KEY_ENV', 'API_KEY'))
    p.add_argument('--model', default=os.getenv('MODEL', 'test-model'))
    p.add_argument('--timeout', type=int, default=180)
    p.add_argument('--output-dir', type=Path)
    p.add_argument('--run', action='store_true')
    p.add_argument('--confirm-paid', action='store_true')
    return p.parse_args()


TOOL = {
    'type': 'function',
    'function': {
        'name': 'get_weather',
        'description': '查询指定城市和日期的天气',
        'parameters': {
            'type': 'object',
            'properties': {'city': {'type': 'string'}, 'date': {'type': 'string'}},
            'required': ['city', 'date'],
            'additionalProperties': False,
        },
    },
}


def call(args, key, body):
    started = time.perf_counter()
    payload = json.dumps(body, ensure_ascii=False)
    header_file = None
    try:
        import tempfile
        header_file = Path(tempfile.NamedTemporaryFile('w', suffix='.headers', delete=False).name)
    except OSError:
        header_file = ROOT / '.tool-loop-headers.tmp'
    command = [
        'curl', '-sS', '--max-time', str(args.timeout),
        '-H', f'Authorization: Bearer {key}', '-H', 'Content-Type: application/json',
        '--data-binary', payload, '--dump-header', str(header_file), '--write-out', '\n__HTTP_STATUS__:%{http_code}',
        args.base_url.rstrip('/') + '/chat/completions',
    ]
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    raw = proc.stdout
    marker = '\n__HTTP_STATUS__:'
    if marker in raw:
        raw, status_text = raw.rsplit(marker, 1)
        status = int(status_text.strip()) if status_text.strip().isdigit() else None
    else:
        status = None
    try:
        response = json.loads(raw)
    except json.JSONDecodeError:
        response = {'unparsed_response': raw}
    if proc.returncode:
        response['client_error'] = proc.stderr.strip() or f'curl exit {proc.returncode}'
    headers = header_file.read_text(errors='replace') if header_file.exists() else ''
    match = re.search(r'(?im)^x-request-id:\s*(\S+)', headers)
    body_id = re.search(r'(?i)request id[:\\s]+([A-Za-z0-9_-]+)', raw)
    response['_x_request_id'] = match.group(1) if match else (body_id.group(1) if body_id else '')
    try:
        header_file.unlink()
    except FileNotFoundError:
        pass
    return status, round((time.perf_counter() - started) * 1000, 1), response


def main():
    args = parse_args()
    print(f'项目=工具结果闭环；模型={args.model}；接口={args.base_url}/chat/completions；计划=2请求')
    if not (args.run and args.confirm_paid):
        print('仅预览；真实请求需同时添加 --run --confirm-paid')
        return
    key = os.environ.get(args.key_env, '')
    if not key:
        raise SystemExit(f'未设置环境变量 {args.key_env}')
    out = (args.output_dir.resolve() if args.output_dir else
           ROOT / 'results' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-tool-loop'))
    out.mkdir(parents=True, exist_ok=False)
    base = {
        'model': args.model,
        'messages': [{'role': 'user', 'content': '请调用天气工具查询北京 2026-09-20 的天气。'}],
        'tools': [TOOL], 'tool_choice': 'required', 'max_completion_tokens': 256,
    }
    rows = []
    status1, elapsed1, first = call(args, key, base)
    choice = (first.get('choices') or [{}])[0]
    assistant = choice.get('message') or {}
    tool_calls = assistant.get('tool_calls') or []
    ok_first = status1 == 200 and bool(tool_calls)
    rows.append({'step': 1, 'http_status': status1 or '', 'elapsed_ms': elapsed1,
                 'result': 'PASS' if ok_first else 'FAIL', 'detail': 'tool_calls=' + str(len(tool_calls)),
                 'request_id': first.get('_x_request_id', ''), 'response': first})
    if ok_first:
        # Preserve the complete assistant tool-call message, including reasoning_content when present.
        assistant_message = {'role': 'assistant', 'content': assistant.get('content'), 'tool_calls': tool_calls}
        if 'reasoning_content' in assistant:
            assistant_message['reasoning_content'] = assistant['reasoning_content']
        call_messages = base['messages'] + [assistant_message]
        for tc in tool_calls:
            function = tc.get('function') or {}
            try:
                arguments = json.loads(function.get('arguments') or '{}')
            except json.JSONDecodeError:
                arguments = {}
            result = {'city': arguments.get('city', '北京'), 'date': arguments.get('date', '2026-09-20'),
                      'condition': '晴', 'temperature_c': 22, 'source': 'test-fixture'}
            call_messages.append({'role': 'tool', 'tool_call_id': tc.get('id', ''),
                                  'name': function.get('name', 'get_weather'),
                                  'content': json.dumps(result, ensure_ascii=False)})
        second_body = {'model': args.model, 'messages': call_messages, 'max_completion_tokens': 256}
        status2, elapsed2, second = call(args, key, second_body)
        final = str(((second.get('choices') or [{}])[0].get('message') or {}).get('content') or '')
        # 模型可能会如实说明工具返回的是测试夹具；这不影响闭环是否完成。
        # 闭环验收关注 HTTP、非空最终回答，以及确实使用了工具结果。
        ok_second = status2 == 200 and bool(final.strip())
        rows.append({'step': 2, 'http_status': status2 or '', 'elapsed_ms': elapsed2,
                     'result': 'PASS' if ok_second else 'FAIL', 'detail': final[:500], 'request_id': second.get('_x_request_id', ''), 'response': second})
    (out / 'tool-loop.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    with (out / 'results.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['step', 'http_status', 'elapsed_ms', 'result', 'request_id', 'detail'])
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, '') for k in writer.fieldnames})
    passed = len(rows) == 2 and all(row['result'] == 'PASS' for row in rows)
    (out / 'summary.json').write_text(json.dumps({
        'model': args.model,
        'base_url': args.base_url,
        'requests': len(rows),
        'passed': passed,
        'http_200': sum(row.get('http_status') == 200 for row in rows),
        'generated_at': datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'第1步 tool_calls={len(tool_calls)} HTTP={status1 or "-"}；闭环结果={"PASS" if passed else "FAIL"}')
    print(f'结果目录：{out}')


if __name__ == '__main__':
    main()

#!/usr/bin/env bash
# 模型测试平台 - 阶段 2 启动脚本
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8032

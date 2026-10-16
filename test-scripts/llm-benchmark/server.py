#!/usr/bin/env python3
"""Local dashboard server; all benchmark requests run on the Python side."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from benchmark import HERE, RESULTS, Run, csv_text, report_html, summary, validate_config

RUNS = {}
REGISTRY_LOCK = threading.RLock()
ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{8}$")


def load_report(run_id):
    if not ID_PATTERN.fullmatch(run_id):
        raise ValueError("无效的报告 ID")
    with REGISTRY_LOCK:
        run = RUNS.get(run_id)
    if run:
        return run.snapshot()
    directory = RESULTS / run_id
    path = directory / "report.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    metadata = directory / "run.json"
    if not metadata.is_file():
        raise FileNotFoundError("报告不存在")
    data = json.loads(metadata.read_text(encoding="utf-8"))
    rows = []
    journal = directory / "requests.jsonl"
    if journal.is_file():
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    start = datetime.fromisoformat(data["started_at"]).timestamp()
    ends = [datetime.fromisoformat(r["started_at"]).timestamp() + r["latency_ms"] / 1000 for r in rows]
    elapsed = max(ends, default=start) - start
    data.update(status="interrupted", error="进程中断，仅恢复已落盘请求；有效时长为已知请求窗口。",
                requests=sorted(rows, key=lambda r: r["sequence"]), active=0, summary=summary(rows, elapsed))
    return data


def list_reports():
    ids = {p.name for p in RESULTS.iterdir() if p.is_dir() and ID_PATTERN.fullmatch(p.name)} if RESULTS.exists() else set()
    with REGISTRY_LOCK:
        ids.update(RUNS)
    reports = []
    for run_id in sorted(ids, reverse=True):
        try:
            with REGISTRY_LOCK:
                run = RUNS.get(run_id)
            report = run.snapshot(False) if run else load_report(run_id)
            reports.append({k: report[k] for k in ("id", "config", "status", "created_at", "summary")})
        except (OSError, ValueError, KeyError):
            continue
    return reports


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send_bytes(self, body, content_type, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, value, status=200):
        self.send_bytes(json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def valid_origin(self):
        expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host") not in expected:
            return False
        origin = self.headers.get("Origin")
        return not origin or origin in {"http://" + host for host in expected}

    def do_GET(self):
        if not self.valid_origin():
            self.send_json({"error": "仅允许本地控制台访问"}, 403)
            return
        path = urlsplit(self.path).path
        try:
            static = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "application/javascript"), "/style.css": ("style.css", "text/css")}
            if path in static:
                name, mime = static[path]
                self.send_bytes((HERE / "web" / name).read_bytes(), mime + "; charset=utf-8")
            elif path == "/api/runs":
                self.send_json(list_reports())
            elif path.startswith("/api/runs/"):
                parts = path.strip("/").split("/")
                run_id = parts[2]
                if not ID_PATTERN.fullmatch(run_id):
                    raise ValueError("无效的报告 ID")
                action = parts[3] if len(parts) == 4 else "report"
                if action == "events":
                    self.events(run_id)
                    return
                report = load_report(run_id)
                if action == "csv":
                    self.send_bytes(csv_text(report["requests"]).encode("utf-8-sig"), "text/csv; charset=utf-8", filename=run_id + ".csv")
                elif action == "html":
                    self.send_bytes(report_html(report).encode(), "text/html; charset=utf-8", filename=run_id + ".html")
                elif action in ("report", "json"):
                    if action == "json":
                        self.send_bytes(json.dumps(report, ensure_ascii=False, indent=2).encode(), "application/json; charset=utf-8", filename=run_id + ".json")
                    else:
                        self.send_json(report)
                else:
                    raise FileNotFoundError("路径不存在")
            else:
                self.send_json({"error": "路径不存在"}, 404)
        except FileNotFoundError as exc:
            self.send_json({"error": str(exc)}, 404)
        except (ValueError, KeyError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_POST(self):
        if not self.valid_origin():
            self.close_connection = True
            self.send_json({"error": "仅允许同源的本地请求"}, 403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 2 * 1024 * 1024:
                self.close_connection = True
                raise ValueError("请求体为空或超过 2 MB")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("请求体必须为 JSON 对象")
            path = urlsplit(self.path).path
            if path == "/api/runs":
                cfg = validate_config(body.get("config", {}))
                key = body.get("api_key", "") or os.environ.get(cfg["api_key_env"], "")
                if not isinstance(key, str):
                    raise ValueError("API Key 必须是字符串")
                if not cfg["demo"] and not key:
                    raise ValueError("请填写 API Key 或设置 " + cfg["api_key_env"])
                with REGISTRY_LOCK:
                    if any(r.status in ("queued", "running") for r in RUNS.values()):
                        self.send_json({"error": "已有测试运行中，请等待完成或取消"}, 409)
                        return
                    run = Run(cfg, key, root=RESULTS)
                    RUNS[run.id] = run
                    threading.Thread(target=lambda: asyncio.run(run.execute()), daemon=True).start()
                self.send_json({"id": run.id}, 201)
            elif path.endswith("/cancel") and path.startswith("/api/runs/"):
                run_id = path.strip("/").split("/")[2]
                with REGISTRY_LOCK:
                    run = RUNS.get(run_id)
                if not run:
                    raise FileNotFoundError("运行任务不存在")
                run.stop.set()
                self.send_json({"ok": True})
            else:
                raise FileNotFoundError("路径不存在")
        except FileNotFoundError as exc:
            self.send_json({"error": str(exc)}, 404)
        except (ValueError, TypeError, KeyError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def events(self, run_id):
        with REGISTRY_LOCK:
            run = RUNS.get(run_id)
        if not run:
            self.send_json({"error": "实时任务不存在，请直接读取历史报告"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                data = run.snapshot(False)
                with run.lock:
                    data["requests"] = sorted(run.rows[-500:], key=lambda r: r["sequence"])
                terminal = data["status"] not in ("queued", "running")
                kind = "complete" if terminal else "progress"
                payload = json.dumps(data, ensure_ascii=False)
                self.wfile.write(f"event: {kind}\ndata: {payload}\n\n".encode())
                self.wfile.flush()
                if terminal:
                    break
                time.sleep(.75)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def main():
    parser = argparse.ArgumentParser(description="启动本地 LLM 测试控制台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    else:
        parser.error("无法找到可用端口")
    url = f"http://127.0.0.1:{server.server_port}"
    print("测试控制台：" + url, flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        with REGISTRY_LOCK:
            active = [r for r in RUNS.values() if r.status in ("queued", "running")]
        for run in active:
            run.stop.set()
        limit = time.monotonic() + 5
        while any(r.status in ("queued", "running") for r in active) and time.monotonic() < limit:
            time.sleep(.1)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

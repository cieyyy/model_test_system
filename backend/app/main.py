"""模型测试平台 - 阶段 2 入口。

默认只监听 127.0.0.1（安全边界，见 docs/TRD.md 第 8 节）。
"""
from __future__ import annotations

from pathlib import Path
import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .record_imports import router as import_router
from .db import BASE_DIR, init_db
from .suite_api import router as suite_router
from .suite_reports import router as report_router
from .suite_jobs import manager as suite_manager, recover_interrupted
from .jobs import manager as legacy_manager
from .web_auth import router as auth_router

app = FastAPI(title="模型测试平台", version="0.2.0")
app.state.mutating_requests = 0


@app.middleware("http")
async def maintenance_guard(request: Request, call_next):
    marker = os.environ.get("MT_MAINTENANCE_FILE")
    if marker and request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path not in ("/auth/login", "/auth/logout"):
        if Path(marker).exists():
            return JSONResponse({"detail": "平台正在更新，请稍后重试"}, status_code=503)
    mutating = request.method not in ("GET", "HEAD", "OPTIONS")
    public_url = os.environ.get("MT_PUBLIC_URL", "").rstrip("/")
    if mutating and public_url and request.headers.get("origin") not in (None, public_url):
        return JSONResponse({"detail": "请求来源不被允许"}, status_code=403)
    if mutating:
        app.state.mutating_requests += 1
    try:
        return await call_next(request)
    finally:
        if mutating:
            app.state.mutating_requests -= 1


@app.get("/api/deployment-status")
def deployment_status():
    with suite_manager.lock:
        suites = sum(j.status == "running" for j in suite_manager.jobs.values())
    with legacy_manager._lock:
        legacy = sum(j["status"] == "running" for j in legacy_manager._jobs.values())
    return dict(mutating_requests=app.state.mutating_requests, active_jobs=suites + legacy)

app.include_router(router)
app.include_router(auth_router)
app.include_router(import_router)
app.include_router(suite_router)
app.include_router(report_router)

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.on_event("startup")
def startup() -> None:
    init_db()
    recover_interrupted()


@app.on_event("shutdown")
def shutdown() -> None:
    suite_manager.shutdown()


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND_DIR / "index.html")

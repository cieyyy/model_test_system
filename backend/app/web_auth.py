"""Browser login backed by the existing Nginx password file."""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .security import _load_fernet

router = APIRouter(include_in_schema=False)
COOKIE = "__Host-model-test-session"
SESSION_SECONDS = 8 * 60 * 60
LOGIN_FILE = Path(__file__).resolve().parents[2] / "frontend" / "login.html"


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


def valid_session(token: str | None) -> bool:
    if not token or len(token) > 4096:
        return False
    try:
        payload = json.loads(_load_fernet().decrypt(token.encode("ascii"), ttl=SESSION_SECONDS))
        return isinstance(payload, dict) and payload.get("purpose") == "web-session" and bool(payload.get("username"))
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        return False


async def check_password(username: str, password: str) -> bool:
    # Only the proxy can read htpasswd; its internal listener is not published.
    url = os.environ.get("MT_AUTH_VERIFY_URL", "http://proxy:8080/verify-basic")
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
            response = await client.get(url, auth=(username, password))
    except httpx.HTTPError:
        raise HTTPException(503, "登录服务暂时不可用，请稍后重试") from None
    if response.status_code == 401:
        return False
    if response.status_code != 200:
        raise HTTPException(503, "登录服务暂时不可用，请稍后重试")
    return True


@router.get("/login")
def login_page():
    return FileResponse(LOGIN_FILE, headers={"Cache-Control": "no-store"})


@router.post("/auth/login")
async def login(body: Login):
    if not await check_password(body.username, body.password):
        return JSONResponse({"detail": "账号或密码不正确"}, status_code=401, headers={"Cache-Control": "no-store"})
    token = _load_fernet().encrypt(json.dumps({"purpose": "web-session", "username": body.username}).encode()).decode("ascii")
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite="lax", path="/")
    return response


@router.get("/auth/session")
async def session(request: Request):
    if valid_session(request.cookies.get(COOKIE)):
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    # Retain command-line and deployment health checks using existing Basic credentials.
    authorization = request.headers.get("authorization", "")
    if len(authorization) <= 4096 and authorization.lower().startswith("basic "):
        try:
            username, password = base64.b64decode(authorization[6:], validate=True).decode("utf-8").split(":", 1)
        except (binascii.Error, ValueError, UnicodeError):
            return Response(status_code=401)
        if await check_password(username, password):
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return Response(status_code=401, headers={"Cache-Control": "no-store"})


@router.post("/auth/logout")
def logout():
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response

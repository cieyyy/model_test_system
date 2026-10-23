"""Offline login and session checks; no production credentials are read."""
import base64
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import web_auth


class WebAuthTests(unittest.TestCase):
    def setUp(self):
        self.fernet = Fernet(Fernet.generate_key())
        self.key_patch = patch.object(web_auth, "_load_fernet", return_value=self.fernet)
        self.key_patch.start()
        app = FastAPI()
        app.include_router(web_auth.router)
        self.client = TestClient(app, base_url="https://testserver")
        self.addCleanup(self.client.close)
        self.addCleanup(self.key_patch.stop)

    def test_login_page_has_no_basic_auth_challenge(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("www-authenticate", response.headers)
        self.assertIn('autocomplete="current-password"', response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_missing_and_malformed_sessions_are_rejected(self):
        for value in (None, "garbage", "!" * 5000, "中文"):
            self.assertFalse(web_auth.valid_session(value))
        self.assertEqual(self.client.get("/auth/session").status_code, 401)

    def test_wrong_password_does_not_create_cookie(self):
        with patch.object(web_auth, "check_password", new=AsyncMock(return_value=False)):
            response = self.client.post("/auth/login", json={"username": "admin", "password": "wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("set-cookie", response.headers)
        self.assertNotIn("www-authenticate", response.headers)

    def test_login_session_and_logout(self):
        with patch.object(web_auth, "check_password", new=AsyncMock(return_value=True)) as check:
            response = self.client.post("/auth/login", json={"username": "admin", "password": "offline-only"})
            check.assert_awaited_once_with("admin", "offline-only")
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"]
        for flag in ("Secure", "HttpOnly", "SameSite=lax", "Path=/", "Max-Age=28800"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Domain=", cookie)
        self.assertNotIn("offline-only", cookie)
        with patch.object(web_auth, "check_password", new=AsyncMock()) as check:
            self.assertEqual(self.client.get("/auth/session").status_code, 204)
            check.assert_not_called()
        self.assertEqual(self.client.post("/auth/logout").status_code, 200)
        self.assertEqual(self.client.get("/auth/session").status_code, 401)

    def test_expired_tampered_and_other_purpose_tokens_are_rejected(self):
        payload = json.dumps({"purpose": "web-session", "username": "admin"}).encode()
        expired = self.fernet.encrypt_at_time(payload, int(time.time()) - web_auth.SESSION_SECONDS - 1).decode()
        token = self.fernet.encrypt(payload).decode()
        other = self.fernet.encrypt(b'{"purpose":"api-key","username":"admin"}').decode()
        for value in (expired, token[:30] + "!" + token[31:], other, self.fernet.encrypt(b'[]').decode()):
            self.assertFalse(web_auth.valid_session(value))

    def test_basic_health_checks_remain_supported(self):
        header = "Basic " + base64.b64encode(b"admin:offline:password").decode()
        with patch.object(web_auth, "check_password", new=AsyncMock(return_value=True)) as check:
            self.assertEqual(self.client.get("/auth/session", headers={"Authorization": header}).status_code, 204)
            check.assert_awaited_once_with("admin", "offline:password")
        with patch.object(web_auth, "check_password", new=AsyncMock(return_value=False)):
            self.assertEqual(self.client.get("/auth/session", headers={"Authorization": header}).status_code, 401)
        for header in ("Basic !!!", "Basic bm9jb2xvbg==", "Basic /w==", "Bearer token"):
            self.assertEqual(self.client.get("/auth/session", headers={"Authorization": header}).status_code, 401)

    def test_password_verifier_fails_closed(self):
        for outcome in (httpx.Response(401), httpx.Response(503), httpx.ConnectError("offline")):
            method = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
            with patch("app.web_auth.httpx.AsyncClient.get", new=method):
                response = self.client.post("/auth/login", json={"username": "admin", "password": "offline-only"})
            self.assertEqual(response.status_code, 401 if isinstance(outcome, httpx.Response) and outcome.status_code == 401 else 503)
            self.assertNotIn("set-cookie", response.headers)


if __name__ == "__main__":
    unittest.main()

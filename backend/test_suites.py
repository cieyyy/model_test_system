"""Offline integration checks. Never reads or modifies the platform's real database."""
from __future__ import annotations

import json
import hashlib
import os
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

TEMP = tempfile.TemporaryDirectory(prefix="workbuddy-suite-test-")
os.environ["MT_DATA_DIR"] = TEMP.name
os.environ["MT_MEDIA_DIR"] = TEMP.name
# Preview checks hash fixtures but never send these bytes to a model.
(Path(TEMP.name) / "test.png").write_bytes(b"offline-image-fixture")
(Path(TEMP.name) / "test.mp4").write_bytes(b"offline-video-fixture")

from fastapi.testclient import TestClient
from app.db import Credential, ModelEntity, Provider, Run, SessionLocal
from app.main import app
from app.security import encrypt_secret
from app.suite_jobs import SuiteJob, manager, recover_interrupted, redact_artifact
from app.suites import COMMON_SUITES, REGISTRY, digest, script_command

KEY = "fake-secret-used-only-in-offline-tests"


class MockHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(dict(path=self.path, body=body, auth=self.headers.get("Authorization")))
        if body["model"] == "429":
            self.send_response(429)
            self.end_headers()
            self.wfile.write(b'{"error":"rate limited"}')
            return
        if body["model"] == "slow":
            time.sleep(2)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [dict(choices=[dict(delta=dict(content="hello"), finish_reason=None)]),
                  dict(choices=[dict(delta={}, finish_reason="stop")], usage=dict(prompt_tokens=12, completion_tokens=2,
                      total_tokens=14, prompt_tokens_details=dict(cached_tokens=6)))]
        try:
            for chunk in chunks:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


class SuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.context = TestClient(app)
        cls.client = cls.context.__enter__()
        with SessionLocal() as db:
            provider = Provider(name="local mock", base_url=f"http://127.0.0.1:{cls.server.server_port}/v1")
            db.add(provider)
            db.flush()
            model = ModelEntity(provider_id=provider.id, model_name="normal")
            credential = Credential(provider_id=provider.id, label="fake", encrypted_key=encrypt_secret(KEY))
            db.add_all([model, credential])
            db.flush()
            cls.pid, cls.mid, cls.cid = provider.id, model.id, credential.id
            db.commit()

    @classmethod
    def tearDownClass(cls):
        for job in list(manager.jobs.values()):
            job.cancel()
        cls.context.__exit__(None, None, None)
        cls.server.shutdown()
        cls.server.server_close()
        TEMP.cleanup()

    def request(self, **kw):
        return dict(suite_id="benchmark", provider_id=self.pid, model_id=self.mid, credential_id=self.cid,
                    concurrency_levels=[1, 2], input_token_levels=[16], output_token_levels=[8],
                    parameters=dict(total_requests=3), **kw)

    def execute(self, request):
        preview = self.client.post("/api/suites/preview", json=request)
        self.assertEqual(preview.status_code, 200, preview.text)
        result = self.client.post("/api/suites/execute", json=dict(request, confirmed=True, plan_hash=preview.json()["plan_hash"]))
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def wait(self, job_id):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            data = self.client.get("/api/suites/jobs/" + job_id).json()
            if data["status"] != "running":
                return data
            time.sleep(.03)
        self.fail("Job did not terminate")

    def test_result_writer_commits_while_report_reader_holds_snapshot(self):
        from app.db import engine
        with engine.connect() as reader:
            self.assertEqual(reader.exec_driver_sql("PRAGMA journal_mode").scalar(), "wal")
            reader.exec_driver_sql("BEGIN")
            before = reader.exec_driver_sql("SELECT count(*) FROM providers").scalar()
            with SessionLocal() as writer:
                writer.add(Provider(name="offline-concurrent-report-reader"))
                writer.commit()
            self.assertEqual(reader.exec_driver_sql("SELECT count(*) FROM providers").scalar(), before)
            reader.rollback()
            self.assertEqual(reader.exec_driver_sql("SELECT count(*) FROM providers").scalar(), before + 1)

    def test_source_assertions_and_missing_metrics(self):
        from app.report_results import detail, overview, observed
        passed = detail(dict(case="tools", result="pass", http_status="200", ttft_ms="0", input_tokens="0"), "工具", "results.csv", 1)
        unknown = detail(dict(case="http-only", http_status="200", elapsed_ms="20"), "功能", "results.csv", 1)
        failed = detail(dict(case="reasoning", result="fail", http_status="200"), "功能", "results.csv", 1)
        self.assertEqual((passed["outcome"], unknown["outcome"], failed["outcome"]), ("通过", "待复核", "未通过"))
        stats = overview([passed, unknown, failed])
        self.assertEqual((stats["passed"], stats["failed"], stats["review"]), (1, 1, 1))
        self.assertEqual(stats["ttft_ms"], 0)
        self.assertIsNone(stats["tpot_ms"])
        self.assertIn("TTFT", observed(passed))

    def test_unified_api_and_rate_metrics_execute_and_report(self):
        for sid, parameters in [("unified_api", dict(cases="chat_sse", max_requests=1)),
                                ("rate_cache_metrics", dict(metrics="ttft,tpot", samples=1, max_requests=1))]:
            req = self.request()
            req.update(suite_id=sid, parameters=parameters)
            job = self.wait(self.execute(req)["job_id"])
            self.assertEqual(job["status"], "completed", job)
            record = self.client.get(f'/api/suites/runs/{job["runs"][0]["id"]}').json()
            self.assertEqual(record["source_tables"][0]["rows"], 1)
            report = self.client.get(f'/api/suite-reports/{job["report_id"]}').json()
            self.assertEqual(report["source"]["overview"]["total"], 1)
            self.assertTrue(report["source"]["charts"])
            page = self.client.get(f'/api/suite-reports/{job["report_id"]}/view')
            self.assertEqual(page.status_code, 200)
            self.assertNotIn("attachment", page.headers.get("content-disposition", ""))
            self.assertIn("测试项目明细", page.text)
            self.assertIn("sandbox allow-scripts", page.headers["content-security-policy"])
            self.assertNotIn(KEY, page.text)

    def test_preview_combinations_and_no_records(self):
        before = len(self.client.get("/api/suites/runs").json())
        req = self.request()
        req.update(input_token_levels=[16, 32], output_token_levels=[8, 16], repeats=2)
        p = self.client.post("/api/suites/preview", json=req).json()
        self.assertEqual((p["stage_count"], p["request_budget"]), (8, 24))
        req["strategy"] = "cartesian"
        p = self.client.post("/api/suites/preview", json=req).json()
        self.assertEqual((p["stage_count"], p["request_budget"]), (16, 48))
        self.assertEqual(len(self.client.get("/api/suites/runs").json()), before)

    def test_deployment_maintenance_and_origin(self):
        marker = Path(TEMP.name) / "maintenance"
        with patch.dict(os.environ, MT_MAINTENANCE_FILE=str(marker), MT_PUBLIC_URL="https://example.test"):
            marker.touch()
            try:
                self.assertEqual(self.client.post("/api/suites/preview", json=self.request()).status_code, 503)
                with patch("app.web_auth.check_password", new=AsyncMock(return_value=False)):
                    self.assertEqual(self.client.post("/auth/login", json={"username": "admin", "password": "offline-only"}).status_code, 401)
                self.assertEqual(self.client.post("/auth/logout").status_code, 200)
                self.assertEqual(self.client.post("/auth/logout", headers={"Origin": "https://other.test"}).status_code, 403)
                status = self.client.get("/api/deployment-status").json()
                self.assertEqual(status["mutating_requests"], 0)
                self.assertEqual(self.client.get("/api/health").status_code, 200)
            finally:
                marker.unlink()
            self.assertEqual(self.client.post("/api/suites/preview", json=self.request(), headers={"Origin": "https://other.test"}).status_code, 403)
            self.assertEqual(self.client.post("/api/suites/preview", json=self.request(), headers={"Origin": "https://example.test"}).status_code, 200)

    def test_uploaded_folder_runs_with_relative_fixture_paths(self):
        import io
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("client-folder/main.py", "from pathlib import Path\nprint(Path('fixtures/value.txt').read_text())\n")
            bundle.writestr("client-folder/fixtures/value.txt", "offline-folder-upload-ok")
        uploaded = self.client.post("/api/files/upload-folder", content=buffer.getvalue())
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        project = self.client.post("/api/projects", json={"name": "uploaded folder fixture", "exec_mode": "script", "script_dir": uploaded.json()["path"]})
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        scripts = self.client.get(f"/api/projects/{project_id}/scripts").json()["scripts"]
        self.assertEqual([item["name"] for item in scripts], ["main.py"])
        result = self.client.post("/api/execute-scripts", json={"project_id": project_id, "script_names": ["main.py"]})
        self.assertEqual(result.status_code, 200, result.text)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.client.get("/api/jobs/" + result.json()["job_id"]).json()
            if job["status"] != "running":
                break
            time.sleep(.03)
        self.assertEqual(job["status"], "finished", job)
        self.assertEqual(job["runs"][0]["status"], "passed")
        self.assertIn("offline-folder-upload-ok", json.dumps(job["lines"]))

    def test_nested_uploaded_scripts_are_discovered_and_executed(self):
        import io
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as bundle:
            bundle.writestr('nested/python/check.PY', "from pathlib import Path\nprint(Path('value.txt').read_text())")
            bundle.writestr('nested/python/value.txt', 'nested-python-ok')
            bundle.writestr('nested/shell/check.bash', 'values=(nested-bash-ok)\necho "${values[0]}"')
            bundle.writestr('nested/shell/check.sh', '#!/bin/bash\nvalues=(nested-shell-ok)\necho "${values[0]}"')
            bundle.writestr('nested/report.html', '<html>support only</html>')
        uploaded = self.client.post('/api/files/upload-folder', content=buffer.getvalue()).json()
        self.assertEqual(uploaded['runnable_count'], 3)
        project_id = self.client.post('/api/projects', json={'name': 'nested script fixture', 'exec_mode': 'script', 'script_dir': uploaded['path']}).json()['id']
        result = self.client.get(f'/api/projects/{project_id}/scripts').json()
        self.assertEqual({item['name'] for item in result['scripts']}, {'python/check.PY', 'shell/check.bash', 'shell/check.sh'})
        for name in ('../escape.py', 'report.html', 'missing.py'):
            self.assertEqual(self.client.post('/api/execute-scripts', json={'project_id': project_id, 'script_names': [name]}).status_code, 400)
        job_id = self.client.post('/api/execute-scripts', json={'project_id': project_id}).json()['job_id']
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.client.get('/api/jobs/' + job_id).json()
            if job['status'] != 'running':
                break
            time.sleep(.03)
        self.assertEqual(job['status'], 'finished', job)
        self.assertEqual(len(job['runs']), 3)
        self.assertTrue(all(run['status'] == 'passed' for run in job['runs']), job)
        self.assertIn('nested-python-ok', json.dumps(job['lines']))
        self.assertIn('nested-bash-ok', json.dumps(job['lines']))
        self.assertIn('nested-shell-ok', json.dumps(job['lines']))

    def test_reject_stale_preview_and_cross_provider_key(self):
        req = self.request()
        self.assertEqual(self.client.post("/api/suites/execute", json=req).status_code, 400)
        req.update(confirmed=True, plan_hash="outdated")
        self.assertEqual(self.client.post("/api/suites/execute", json=req).status_code, 409)
        req["credential_id"] = 99999
        self.assertEqual(self.client.post("/api/suites/preview", json=req).status_code, 400)

    def test_real_mock_stream_and_evidence(self):
        req = self.request()
        job = self.execute(req)
        result = self.wait(job["job_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(result["runs"]), 2)
        for run in result["runs"]:
            self.assertEqual(run["summary"]["success"], 3)
            self.assertEqual(run["summary"]["input_tokens"], 36)
            self.assertEqual(run["summary"]["cache_hit_rate"], 50)
            detail = self.client.get(f"/api/suites/runs/{run['id']}").json()
            files = {f["path"] for f in detail["files"]}
            self.assertIn("manifest.json", files)
            html = next(f for f in files if f.endswith("/report.html"))
            report = self.client.get(f"/api/suites/runs/{run['id']}/files/{html}")
            self.assertEqual(report.status_code, 200)
            self.assertIn("sandbox allow-scripts", report.headers["content-security-policy"])
            self.assertNotIn(KEY, report.text)
            snapshot = self.client.get(f"/api/suites/runs/{run['id']}/benchmark")
            self.assertEqual(snapshot.status_code, 200)
            self.assertEqual(len(snapshot.json()["requests"]), 3)
            self.assertEqual(snapshot.json()["platform_run_id"], run["id"])
            self.assertNotIn(KEY, snapshot.text)
            view = self.client.get(f"/api/suites/runs/{run['id']}/view")
            self.assertEqual(view.status_code, 200)
            self.assertIn("window.BENCHMARK_EMBED=true", view.text)
            self.assertIn("benchmark:resize", view.text)
            self.assertIn("sandbox allow-scripts", view.headers["content-security-policy"])
        self.assertTrue(all(r["path"] == "/v1/chat/completions" for r in MockHandler.requests))
        self.assertEqual(MockHandler.requests[-1]["auth"], "Bearer " + KEY)
        self.assertNotIn(KEY, json.dumps(result))
        for p in Path(TEMP.name).rglob("*"):
            if p.is_file() and p.suffix in (".json", ".jsonl", ".html", ".csv", ".log"):
                self.assertNotIn(KEY.encode(), p.read_bytes(), str(p))

    def test_http_failure_is_not_compatibility_pass(self):
        with SessionLocal() as db:
            m = ModelEntity(provider_id=self.pid, model_name="429")
            db.add(m)
            db.commit()
            mid = m.id
        req = self.request()
        req.update(model_id=mid, concurrency_levels=[1])
        result = self.wait(self.execute(req)["job_id"])
        self.assertEqual(result["runs"][0]["summary"]["failed"], 3)
        with SessionLocal() as db:
            run = db.get(Run, result["runs"][0]["id"])
            self.assertFalse(run.passed)
            self.assertNotIn(run.status, ("passed", "failed"))

    def test_script_adapter_uses_stored_key_and_report(self):
        req = self.request()
        req.update(suite_id="stream_metrics", parameters=dict(requests=2, concurrency=1, input_tokens=16, max_output_tokens=8, timeout=5))
        p = self.client.post("/api/suites/preview", json=req).json()
        self.assertIn("--run", p["command"])
        result = self.wait(self.execute(req)["job_id"])
        self.assertEqual(result["status"], "completed", result)
        r = result["runs"][0]
        self.assertEqual(r["measurement_status"], "evidence_available")
        summary = r["source_summaries"][0]["data"]
        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["http_200"], 2)
        detail = self.client.get(f"/api/suites/runs/{r['id']}").json()
        self.assertTrue(any(f["path"] == "script_output.log" for f in detail["files"]))

    def test_basic_csv_evidence_and_environment_output_path(self):
        req = self.request()
        req.update(suite_id="basic", parameters=dict(cases="stream", rounds=1, max_requests=1, timeout=5))
        result = self.wait(self.execute(req)["job_id"])
        self.assertEqual(result["status"], "completed", result)
        run = result["runs"][0]
        self.assertEqual(run["measurement_status"], "evidence_available")
        self.assertEqual(run["source_tables"][0]["rows"], 1)
        self.assertEqual(run["acceptance_status"], "source_report_requires_review")

    def test_cancel_remaining_stages_and_task_timeout(self):
        req = self.request(demo=True)
        req["parameters"] = dict(total_requests=1000)
        job = self.execute(req)
        self.client.post(f"/api/suites/jobs/{job['job_id']}/cancel")
        result = self.wait(job["job_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(all(r["status"] == "cancelled" for r in result["runs"]))
        req["job_timeout_seconds"] = 1
        result = self.wait(self.execute(req)["job_id"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["status"], "error")

    def test_external_script_timeout_terminates_process_group(self):
        with SessionLocal() as db:
            model = ModelEntity(provider_id=self.pid, model_name="slow")
            db.add(model)
            db.commit()
            mid = model.id
        req = self.request()
        req.update(suite_id="stream_metrics", model_id=mid, job_timeout_seconds=1,
                   parameters=dict(requests=2, concurrency=1, input_tokens=16, max_output_tokens=8, timeout=10))
        job = self.execute(req)
        result = self.wait(job["job_id"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["status"], "error")
        self.assertIsNone(manager.jobs[job["job_id"]].proc)

    def test_live_curve_snapshot_before_completion(self):
        req = self.request(demo=True)
        req.update(concurrency_levels=[2], parameters=dict(total_requests=100))
        job = self.execute(req)
        rid = job["run_ids"][0]
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline:
                snapshot = self.client.get(f"/api/suites/runs/{rid}/benchmark")
                self.assertEqual(snapshot.status_code, 200, snapshot.text)
                data = snapshot.json()
                self.assertEqual(data["config"]["total_requests"], 100)
                if data["requests"]:
                    self.assertIn(data["status"], ("running", "completed"))
                    self.assertGreater(data["summary"]["attempted"], 0)
                    self.assertLessEqual(len(data["requests"]), 500)
                    break
                time.sleep(.05)
            else:
                self.fail("No live request samples available")
        finally:
            self.client.post(f"/api/suites/jobs/{job['job_id']}/cancel")
            self.wait(job["job_id"])

    def test_legacy_import_is_idempotent_and_path_scoped(self):
        legacy = Path(TEMP.name) / "legacy/llm-benchmark/results/example"
        legacy.mkdir(parents=True)
        (legacy / "report.json").write_text(json.dumps(dict(config=dict(model="historical", demo=True), summary=dict(success=1))))
        (legacy / "report.html").write_text("<h1>offline report</h1>")
        with patch("app.suite_api.SCRIPTS_ROOT", Path(TEMP.name) / "legacy"):
            first = self.client.post("/api/suites/legacy-reports/example/import").json()
            second = self.client.post("/api/suites/legacy-reports/example/import").json()
            self.assertTrue(first["imported"])
            self.assertFalse(second["imported"])
            self.assertEqual(first["run_id"], second["run_id"])
            detail = self.client.get(f"/api/suites/runs/{first['run_id']}").json()
            self.assertEqual(detail["status"], "imported")
            escaped = self.client.get(f"/api/suites/runs/{first['run_id']}/files/%2e%2e/%2e%2e/app.db")
            self.assertNotEqual(escaped.status_code, 200)

    def test_upload_html_and_table_are_deduplicated_and_enter_model_comparison(self):
        html = b"""<!doctype html><table><tr><th>case</th><th>result</th><th>http_status</th></tr>
        <tr><td>basic</td><td>pass</td><td>200</td></tr>
        <tr><td>tools</td><td>fail</td><td>200</td></tr></table>"""
        first = self.client.post(
            "/api/import/upload?filename=deepseek-v4.1-report.html&provider=relay-a&model=deepseek-v4.1&category=stream",
            content=html,
        )
        self.assertEqual(first.status_code, 200, first.text)
        imported = first.json()
        self.assertTrue(imported["imported"])
        self.assertEqual(imported["recognized"]["rows"], 2)
        duplicate = self.client.post(
            "/api/import/upload?filename=deepseek-v4.1-report.html&provider=relay-a&model=deepseek-v4.1&category=stream",
            content=html,
        )
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertTrue(duplicate.json()["duplicate"])
        table = self.client.post(
            "/api/import/upload?filename=deepseek-v4.1.csv&provider=relay-b&model=deepseek-v4.1&category=stream",
            content=b"case,result,http_status\nbasic,pass,200\n",
        )
        self.assertEqual(table.status_code, 200, table.text)
        self.assertEqual(table.json()["recognized"]["provider"], "relay-b")
        matrix = self.client.get("/api/matrix").json()
        self.assertTrue(any(row["provider_name"] == "relay-a" and row["model_name"] == "deepseek-v4.1" for row in matrix["rows"]))
        self.assertFalse(any(row["provider_name"] == "本地模拟" for row in matrix["rows"]))
        preview = self.client.get(f"/api/assets/{imported['asset_id']}/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["kind"], "html")

    def test_import_preserves_partial_review_and_multirelay_groups(self):
        html = """<!doctype html><table><tr><th>指标</th><th>数值</th></tr><tr><td>平均延迟</td><td>12</td></tr></table>
        <table><tr><th>测试项目</th><th>原始判定</th><th>HTTP</th></tr>
        <tr><td>基础调用</td><td>通过</td><td>200</td></tr>
        <tr><td>工具调用</td><td>部分通过</td><td>200</td></tr>
        <tr><td>长上下文</td><td>待复核</td><td>504</td></tr></table>""".encode()
        response = self.client.post(
            "/api/import/upload?filename=deepseek-v4.1-full.html&provider=relay-c&model=deepseek-v4.1&category=统一测试",
            content=html,
        )
        self.assertEqual(response.status_code, 200, response.text)
        group = response.json()["recognized"]["groups"][0]
        self.assertEqual((group["passed"], group["partial"], group["manual_review"]), (1, 1, 1))
        matrix = self.client.get("/api/matrix").json()
        row = next(r for r in matrix["rows"] if r["provider_name"] == "relay-c" and r["model_name"] == "deepseek-v4.1")
        self.assertEqual(row["cells"]["统一测试"]["effective_status"], "partial")
        detail = self.client.get("/api/matrix/detail", params={"provider_id": row["provider_id"], "model_name": row["model_name"], "category": "统一测试"}).json()
        evidence = self.client.get(f"/api/runs/{detail['runs'][0]['id']}").json()["evidence"]["source_rows"]
        self.assertEqual(evidence["records"], 3)
        self.assertEqual(evidence["source_rows"], [2, 3, 4])

    def test_http_success_without_assertion_is_review_and_xlsx_zero_is_kept(self):
        response = self.client.post(
            "/api/import/upload?filename=review.csv&provider=relay-review&model=deepseek-v4.1&category=raw",
            content=b"case,http_status\nonly-http,200\n",
        )
        self.assertEqual(response.status_code, 200, response.text)
        group = response.json()["recognized"]["groups"][0]
        self.assertEqual(group["manual_review"], 1)
        workbook_path = Path(TEMP.name) / "zero.xlsx"
        from openpyxl import Workbook
        workbook = Workbook()
        workbook.active.append(["case", "result", "latency_ms"])
        workbook.active.append(["zero", "pass", 0])
        workbook.save(workbook_path)
        xlsx = self.client.post(
            "/api/import/upload?filename=zero.xlsx&provider=relay-xlsx&model=deepseek-v4.1&category=raw",
            content=workbook_path.read_bytes(),
        )
        self.assertEqual(xlsx.status_code, 200, xlsx.text)
        self.assertEqual(xlsx.json()["recognized"]["groups"][0]["passed"], 1)
        self.assertEqual(xlsx.json()["recognized"]["groups"][0]["rows"], 1)

    def test_import_groups_rows_by_provider_and_model_without_duplication(self):
        csv_data = b"provider,model,case,result\nrelay-one,deepseek-v4.1,a,pass\nrelay-two,deepseek-v4.1,a,pass\n"
        response = self.client.post("/api/import/upload?filename=multi.csv&category=stream", content=csv_data)
        self.assertEqual(response.status_code, 200, response.text)
        groups = response.json()["recognized"]["groups"]
        self.assertEqual({(g["provider"], g["model"]) for g in groups}, {("relay-one", "deepseek-v4.1"), ("relay-two", "deepseek-v4.1")})
        digest = hashlib.sha256(csv_data).hexdigest()
        objects = list((Path(TEMP.name) / "import_objects").rglob(f"{digest}.*"))
        self.assertEqual(len(objects), 1)

    def test_restart_recovery_does_not_touch_old_runs(self):
        with SessionLocal() as db:
            suite = Run(status="running", summary_json='{"kind":"suite"}')
            old = Run(status="running", summary_json='{}')
            db.add_all([suite, old])
            db.commit()
            sid, oid = suite.id, old.id
        recover_interrupted()
        with SessionLocal() as db:
            self.assertEqual(db.get(Run, sid).status, "interrupted")
            self.assertEqual(db.get(Run, oid).status, "running")

    def test_redaction_across_file_chunks_and_xlsx_members(self):
        path = Path(TEMP.name) / "redaction.txt"
        path.write_bytes(b"x" * (1024 * 1024 - 5) + KEY.encode() + b" end")
        redact_artifact(path, KEY)
        self.assertNotIn(KEY.encode(), path.read_bytes())
        archive = Path(TEMP.name) / "redaction.xlsx"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("xl/sharedStrings.xml", "<t>" + KEY + "</t>")
        redact_artifact(archive, KEY)
        with zipfile.ZipFile(archive) as z:
            self.assertNotIn(KEY.encode(), z.read("xl/sharedStrings.xml"))

    def test_local_profile_and_all_common_stages(self):
        catalog = self.client.get("/api/suites").json()
        self.assertEqual(len(catalog["common_suite_ids"]), 18)
        self.assertEqual(REGISTRY["basic"]["fields"]["rounds"]["default"], 5)
        self.assertEqual(REGISTRY["long_conversation"]["fields"]["rounds"]["default"], 100)
        self.assertEqual(REGISTRY["tpm_benchmark"]["fields"]["requests"]["default"], 292)
        self.assertEqual(REGISTRY["tpm_benchmark"]["fields"]["concurrency"]["default"], 80)
        req = self.request()
        req.update(suite_ids=COMMON_SUITES, parameters={}, cooldown_seconds=70)
        before = len(self.client.get("/api/suites/runs").json())
        response = self.client.post("/api/suites/preview", json=req)
        self.assertEqual(response.status_code, 200, response.text)
        p = response.json()
        self.assertEqual((p["project_count"], p["stage_count"]), (18, 23))
        body = next(s for s in p["projects"] if s["suite_id"] == "body_100mb")
        self.assertEqual([s["config"]["body_mb"] for s in body["stages"]], [50, 70, 80, 90, 95, 100])
        for stage, command in zip(body["stages"], body["commands"]):
            self.assertEqual(float(command[command.index("--body-mb") + 1]), stage["config"]["body_mb"])
            self.assertNotIn("--body-mb-levels", command)
        self.assertEqual(len(self.client.get("/api/suites/runs").json()), before)
        req["suite_ids"] = [s["id"] for s in catalog["suites"]]
        adapted = self.client.post("/api/suites/preview", json=req)
        self.assertEqual(adapted.status_code, 200, adapted.text)
        acceptance = next(p for p in adapted.json()["projects"] if p["suite_id"] == "acceptance")
        self.assertEqual(acceptance["parameters"]["max_requests"], 1000)
        self.assertEqual(acceptance["parameters"]["duration_seconds"], 180)

    def test_batch_runs_distinct_scripts_and_generates_report(self):
        req = self.request()
        req.update(suite_ids=["benchmark", "stream_metrics", "basic"], concurrency_levels=[1], cooldown_seconds=0,
                   suite_parameters={"benchmark": dict(total_requests=2),
                       "stream_metrics": dict(requests=2, concurrency=1, input_tokens=16, max_output_tokens=8, timeout=5),
                       "basic": dict(cases="stream", rounds=1, max_requests=1, timeout=5)})
        job = self.execute(req)
        result = self.wait(job["job_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual([r["suite_id"] for r in result["runs"]], req["suite_ids"])
        self.assertTrue(result["report_id"])
        report = self.client.get(f"/api/suite-reports/{result['report_id']}").json()
        self.assertEqual(len(report["source"]["runs"]), 3)
        self.assertEqual(len(report["source"]["charts"][0]["samples"]), 2)
        self.assertIsNotNone(report["source"]["charts"][0]["samples"][0][2])
        self.assertTrue(any("源断言" in row["note"] for row in report["content"]["rows"]))
        self.assertTrue(all(row["conclusion"] == "待复核" for row in report["content"]["rows"] if row["case"] == "执行状态"))

    def test_batch_continue_or_stop_on_project_error(self):
        original = SuiteJob.execute_script

        def fail_basic(job, directory, plan=None):
            if plan["suite_id"] == "basic":
                raise ValueError("deliberate offline failure")
            return original(job, directory, plan)

        req = self.request()
        req.update(suite_ids=["basic", "stream_metrics"], cooldown_seconds=0,
                   suite_parameters={"basic": dict(cases="stream", max_requests=1),
                       "stream_metrics": dict(requests=1, concurrency=1, input_tokens=16, max_output_tokens=8, timeout=5)})
        with patch.object(SuiteJob, "execute_script", fail_basic):
            result = self.wait(self.execute(req)["job_id"])
            self.assertEqual([r["status"] for r in result["runs"]], ["error", "completed"])
            req["continue_on_error"] = False
            result = self.wait(self.execute(req)["job_id"])
            self.assertEqual([r["status"] for r in result["runs"]], ["error", "error"])
            self.assertIn("未继续执行", result["runs"][1]["verdict"])

    def test_batch_missing_asset_does_not_start_or_silently_skip(self):
        req = self.request()
        req.update(suite_ids=["basic", "api_features"], suite_parameters={"api_features": dict(video="/nonexistent/video.mp4")})
        before = len(self.client.get("/api/suites/runs").json())
        response = self.client.post("/api/suites/preview", json=req)
        self.assertEqual(response.status_code, 400)
        self.assertIn("video 文件不存在", response.text)
        self.assertEqual(len(self.client.get("/api/suites/runs").json()), before)

    def test_report_edit_preview_versions_exports_preserve_evidence(self):
        req = self.request(demo=True)
        req.update(parameters=dict(total_requests=2), concurrency_levels=[1])
        job = self.wait(self.execute(req)["job_id"])
        rid = job["runs"][0]["id"]
        detail_before = self.client.get(f"/api/suites/runs/{rid}").json()
        with SessionLocal() as db:
            root = Path(db.get(Run, rid).evidence_dir)
        hashes_before = {str(p.relative_to(root)): digest(p) for p in root.rglob("*") if p.is_file()}
        report_id = job["report_id"]
        self.assertEqual(self.client.post("/api/suite-reports", json=dict(job_id=job["id"])).json()["id"], report_id)
        original = self.client.get(f"/api/suite-reports/{report_id}").json()
        content = original["content"]
        content["title"] = '<script>alert("title")</script>'
        content["summary"] = '自定义结论'
        content["rows"][0]["observed"] = '=HYPERLINK("https://invalid")'
        content["rows"][0]["conclusion"] = '人工复核结论'
        preview = self.client.post(f"/api/suite-reports/{report_id}/preview", json=content)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(original["content"]["template"], "model_test")
        for marker in ("P99 TTFT", "P99 TPOT", "GEN. TOK/S", "SUCCESS RATE", "Detailed Results"):
            self.assertIn(marker, preview.text)
        self.assertNotIn('href="/#reports"', preview.text)
        self.assertIn("/api/suites/runs/", preview.text)
        self.assertIn('target="_blank"', preview.text)
        self.assertNotIn('<script>alert("title")</script>', preview.text)
        self.assertIn('&lt;script&gt;', preview.text)
        self.assertIn('class="edited"', preview.text)
        save = dict(base_version=1, content=content, editor="测试用户", change_summary="编辑验收结论")
        self.assertEqual(self.client.post(f"/api/suite-reports/{report_id}/versions", json=save).json()["version"], 2)
        self.assertEqual(self.client.post(f"/api/suite-reports/{report_id}/versions", json=save).status_code, 409)
        reopened = self.client.get(f"/api/suite-reports/{report_id}").json()
        self.assertEqual(reopened["content"]["summary"], '自定义结论')
        self.assertEqual(reopened["source"], original["source"])
        self.assertNotEqual(self.client.get(f"/api/suite-reports/{report_id}?version=1").json()["content"]["title"], content["title"])
        self.assertEqual(self.client.get(f"/api/suites/runs/{rid}").json(), detail_before)
        self.assertEqual({str(p.relative_to(root)): digest(p) for p in root.rglob("*") if p.is_file()}, hashes_before)
        for kind in ("html", "json", "csv"):
            exported = self.client.get(f"/api/suite-reports/{report_id}/export/{kind}")
            self.assertEqual(exported.status_code, 200)
            self.assertNotIn(KEY, exported.text)
            self.assertIn("attachment", exported.headers["content-disposition"])
            if kind == "csv":
                self.assertIn("'=HYPERLINK", exported.text)
        content["accent"] = "red;display:none"
        self.assertEqual(self.client.post(f"/api/suite-reports/{report_id}/preview", json=content).status_code, 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)

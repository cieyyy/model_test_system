import asyncio
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import benchmark as b


class ModelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    active = 0
    peak = 0
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.lock:
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)
        try:
            self.respond(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self.lock:
                type(self).active -= 1

    def respond(self, body):
        if self.path != "/v1/chat/completions":
            payload = b"404 page not found"
            self.send_response(404)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        mode = body["model"]
        if mode == "http-error":
            payload = b'quota exceeded secret-test-key'
            self.send_response(429)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.send_header("x-request-id", "mock-id")
        self.end_headers()
        self.close_connection = True

        def event(value):
            self.wfile.write(("data: " + json.dumps(value, ensure_ascii=False) + "\r\n\r\n").encode())
            self.wfile.flush()

        self.wfile.write(b": heartbeat\r\n\r\n")
        event({"choices": [{"delta": {"role": "assistant"}}]})
        time.sleep(.5 if mode == "timeout" else .02)
        event({"choices": [{"delta": {"reasoning_content": "思考"}}]})
        time.sleep(.025)
        # The two bytes of a Chinese character deliberately cross write boundaries.
        raw = ('data: ' + json.dumps({"choices": [{"delta": {"content": "中文"}}]}, ensure_ascii=False) + '\r\n\r\n').encode()
        cut = raw.index("中".encode()) + 1
        self.wfile.write(raw[:cut]); self.wfile.flush()
        time.sleep(.005)
        self.wfile.write(raw[cut:]); self.wfile.flush()
        if mode == "truncated":
            return
        event({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        time.sleep(.03)
        if mode != "no-usage":
            event({"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 1 if mode == "one-token" else 3,
                                             "prompt_tokens_details": {"cached_tokens": 0}}})
        self.wfile.write(b"data: [DONE]\r\n\r\n"); self.wfile.flush()


class BenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/v1/chat/completions"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def run_case(self, **config):
        cfg = dict(endpoint=self.endpoint, model="normal", prompt_mode="custom", prompt="中文测试", concurrency=2, total_requests=4)
        cfg.update(config)
        run = b.Run(cfg, "secret-test-key", self.temp.name)
        report = asyncio.run(run.execute())
        return run, report

    def test_real_stream_concurrency_metrics_and_exports(self):
        ModelHandler.peak = 0
        run, report = self.run_case(concurrency=3, total_requests=8)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["summary"]["success"], 8)
        self.assertLessEqual(ModelHandler.peak, 3)
        self.assertGreaterEqual(ModelHandler.peak, 2)
        for row in report["requests"]:
            self.assertGreater(row["ttft_ms"], 15)
            self.assertGreater(row["tpot_ms"], 8)
            self.assertLess(row["tpot_ms"], (row["latency_ms"] - row["ttft_ms"]) / 2)
            self.assertIn("中文", row["output_preview"])
        self.assertEqual(report["summary"]["cache_hit_rate"], 0)
        self.assertEqual(report["summary"]["input_tokens"], 160)
        self.assertEqual(report["summary"]["output_tokens"], 24)
        self.assertEqual(len((run.directory / "requests.jsonl").read_text().splitlines()), 8)
        html = (run.directory / "report.html").read_text()
        self.assertIn("window.BENCHMARK_DATA=", html)
        self.assertNotIn('src="/app.js"', html)
        self.assertNotIn("secret-test-key", html)
        self.assertTrue((run.directory / "requests.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_missing_usage_stays_unknown(self):
        _, report = self.run_case(model="no-usage", total_requests=1)
        self.assertEqual(report["summary"]["success"], 1)
        self.assertIsNone(report["summary"]["total_tokens"])
        self.assertIsNone(report["summary"]["cache_hit_rate"])
        self.assertIsNone(report["requests"][0]["tpot_ms"])

    def test_base_url_is_completed_and_reaches_chat_route(self):
        base = self.endpoint.removesuffix("/chat/completions")
        _, report = self.run_case(endpoint=base, total_requests=1)
        self.assertEqual(report["summary"]["success"], 1)
        self.assertEqual(report["config"]["endpoint"], self.endpoint)

    def test_endpoint_normalization_is_idempotent(self):
        expected = "https://tokenhub.tencentmaas.com/v1/chat/completions"
        for value in ["https://tokenhub.tencentmaas.com", "https://tokenhub.tencentmaas.com/",
                      "https://tokenhub.tencentmaas.com/v1", "https://tokenhub.tencentmaas.com/v1/", expected]:
            with self.subTest(value=value):
                actual = b.normalize_endpoint(value)
                self.assertEqual(actual, expected)
                self.assertEqual(b.normalize_endpoint(actual), expected)
        self.assertEqual(b.normalize_endpoint("https://example.com/proxy/v1/"), "https://example.com/proxy/v1/chat/completions")
        self.assertEqual(b.normalize_endpoint("https://example.com/custom/route/"), "https://example.com/custom/route/")
        for value in ["", "https://user:secret@example.com/v1", "https://example.com/v1?key=secret", "https://example.com/v1#fragment"]:
            with self.assertRaises(ValueError):
                b.normalize_endpoint(value)

    def test_one_token_tpot_is_undefined(self):
        _, report = self.run_case(model="one-token", total_requests=1)
        self.assertIsNone(report["requests"][0]["tpot_ms"])

    def test_truncated_stream_is_failure(self):
        _, report = self.run_case(model="truncated", total_requests=1)
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(report["summary"]["latency_ms"]["samples"], 0)

    def test_http_error_is_redacted_and_not_retried(self):
        run, report = self.run_case(model="http-error", total_requests=3)
        self.assertEqual(report["summary"]["failed"], 3)
        self.assertEqual(report["dispatched"], 3)
        self.assertEqual(report["requests"][0]["http_status"], 429)
        self.assertNotIn("secret-test-key", (run.directory / "report.json").read_text())

    def test_total_timeout_is_failure(self):
        _, report = self.run_case(model="timeout", total_requests=1, timeout_seconds=.1)
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(report["summary"]["cancelled"], 0)
        self.assertLess(report["summary"]["elapsed_seconds"], .4)

    def test_cancel_stops_dispatch_and_records_active_requests(self):
        async def scenario():
            run = b.Run(dict(demo=True, concurrency=2, total_requests=100), root=self.temp.name)
            task = asyncio.create_task(run.execute())
            await asyncio.sleep(.015)
            run.stop.set()
            return await task
        report = asyncio.run(scenario())
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(report["summary"]["cancelled"], 2)
        self.assertEqual(report["dispatched"], 2)

    def test_duration_stops_new_dispatch_and_drains(self):
        _, report = self.run_case(demo=True, duration_seconds=.03, total_requests=100, concurrency=2)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["summary"]["success"], 2)
        self.assertGreater(report["summary"]["elapsed_seconds"], .03)

    def test_percentiles_token_coverage_and_provider_shapes(self):
        self.assertEqual(b.percentile([10, 20, 30, 40], .5), 25)
        self.assertEqual(b.percentile([10, 20, 30, 40], .9), 37)
        self.assertIsNone(b.percentile([], .9))
        self.assertEqual(b.parse_usage({"prompt_tokens": 100, "completion_tokens": 3, "prompt_cache_hit_tokens": 90})["cached_input_tokens"], 90)
        rows = [dict(status="success", input_tokens=10, output_tokens=3, cached_input_tokens=None, total_tokens=13, latency_ms=100, ttft_ms=50, tpot_ms=20),
                dict(status="success", input_tokens=None, output_tokens=2, cached_input_tokens=None, total_tokens=None, latency_ms=200, ttft_ms=None, tpot_ms=None)]
        result = b.summary(rows, 2)
        self.assertIsNone(result["input_tokens"])
        self.assertEqual(result["output_tokens"], 5)
        self.assertEqual(result["system_gen_tps"], 2.5)
        self.assertEqual(result["ttft_ms"]["samples"], 1)

    def test_generated_prompt_count_and_html_escape(self):
        for target in (1, 64, 32000):
            cfg = b.validate_config(dict(endpoint=self.endpoint, model="normal", prompt_tokens=target))
            _, count = b.prepare_prompt(cfg)
            self.assertEqual(count, target)
        _, report = self.run_case(demo=True, total_requests=1, name="</script><script>alert(1)</script>")
        self.assertNotIn('"name": "</script>', b.report_html(report))

    def test_csv_formula_injection_and_validation(self):
        self.assertIn("'=evil", b.csv_text([dict(error="=evil")]))
        for cfg in (dict(concurrency=0), dict(total_requests=1.2), dict(temperature=float("nan")), dict(endpoint="https://key:pass@host/v1")):
            with self.assertRaises(ValueError):
                b.validate_config(dict(model="m", endpoint=self.endpoint, **{k:v for k,v in cfg.items() if k != "endpoint"}) if "endpoint" not in cfg else cfg)


if __name__ == "__main__":
    unittest.main()

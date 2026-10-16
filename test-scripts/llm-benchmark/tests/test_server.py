import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx

import server as dashboard
from benchmark import utc_now


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_results = dashboard.RESULTS
        dashboard.RESULTS = Path(self.temp.name)
        dashboard.RUNS.clear()
        self.server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.server.server_port}", trust_env=False)

    def tearDown(self):
        for run in dashboard.RUNS.values():
            run.stop.set()
        limit = time.monotonic() + 3
        while any(r.status in ("queued", "running") for r in dashboard.RUNS.values()) and time.monotonic() < limit:
            time.sleep(.02)
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        dashboard.RUNS.clear()
        dashboard.RESULTS = self.old_results
        self.temp.cleanup()

    def start_demo(self, **config):
        response = self.client.post('/api/runs', json=dict(config=dict(demo=True, total_requests=4, concurrency=2, **config)))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def test_http_api_live_stream_history_and_exports(self):
        run_id = self.start_demo()
        busy = self.client.post('/api/runs', json=dict(config=dict(demo=True)))
        self.assertEqual(busy.status_code, 409)
        with self.client.stream('GET', f'/api/runs/{run_id}/events') as response:
            self.assertEqual(response.status_code, 200)
            events = list(response.iter_lines())
        self.assertTrue(any(line == 'event: complete' for line in events))
        report = self.client.get(f'/api/runs/{run_id}').json()
        self.assertEqual(report['summary']['success'], 4)
        self.assertEqual(len(self.client.get('/api/runs').json()), 1)
        csv = self.client.get(f'/api/runs/{run_id}/csv')
        self.assertIn('attachment', csv.headers['content-disposition'])
        self.assertEqual(len(csv.text.strip().splitlines()), 5)
        html = self.client.get(f'/api/runs/{run_id}/html')
        self.assertIn('window.BENCHMARK_DATA=', html.text)
        self.assertEqual(self.client.get(f'/api/runs/{run_id}/json').json()['id'], run_id)
        dashboard.RUNS.clear()
        self.assertEqual(self.client.get(f'/api/runs/{run_id}').json()['summary']['success'], 4)

    def test_origin_validation_missing_key_and_invalid_input(self):
        cross = self.client.post('/api/runs', json=dict(config=dict(demo=True)), headers={'Origin':'http://untrusted.example'})
        self.assertEqual(cross.status_code, 403)
        config = dict(endpoint='https://example.com/v1/chat/completions', model='m', api_key_env='BENCHMARK_TEST_UNSET')
        self.assertEqual(self.client.post('/api/runs', json={'config':config}).status_code, 400)
        self.assertEqual(self.client.post('/api/runs', json={'config':dict(demo=True), 'api_key':123}).status_code, 400)
        self.assertEqual(self.client.post('/api/runs', json=[]).status_code, 400)
        self.assertEqual(self.client.get('/api/runs/invalid').status_code, 400)

    def test_cancel_api(self):
        run_id = self.start_demo()
        self.assertEqual(self.client.post(f'/api/runs/{run_id}/cancel', json={}).status_code, 200)
        with self.client.stream('GET', f'/api/runs/{run_id}/events') as response:
            list(response.iter_lines())
        self.assertEqual(self.client.get(f'/api/runs/{run_id}').json()['status'], 'cancelled')

    def test_recovery_keeps_only_durable_rows(self):
        run_id = '20261014T000000Z_1234abcd'
        directory = dashboard.RESULTS / run_id
        directory.mkdir()
        metadata = dict(id=run_id, created_at=utc_now(), started_at=utc_now(), config={'name':'interrupted'}, status='running', active=3)
        (directory / 'run.json').write_text(json.dumps(metadata))
        row = dict(sequence=1, status='success', started_at=metadata['started_at'], latency_ms=100,
                   ttft_ms=20, tpot_ms=10, input_tokens=10, output_tokens=3, total_tokens=13, cached_input_tokens=0)
        (directory / 'requests.jsonl').write_text(json.dumps(row) + '\n{"incomplete":')
        report = self.client.get(f'/api/runs/{run_id}').json()
        self.assertEqual(report['status'], 'interrupted')
        self.assertEqual(report['summary']['success'], 1)
        self.assertEqual(report['active'], 0)


if __name__ == '__main__':
    unittest.main()

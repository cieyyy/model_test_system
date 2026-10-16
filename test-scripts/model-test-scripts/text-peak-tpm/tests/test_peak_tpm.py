import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('peak',Path(__file__).resolve().parents[1]/'run_text_peak_tpm.py')
p=importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def row(n,t,tokens=100,ttft=1000,success=True):
    r=p.new_record(n,0)
    r.update(started_s=0,completed_s=t,success=success,ttft_ms=ttft,
             input_tokens=tokens,output_tokens=0,total_tokens=tokens,cached_tokens=tokens//2)
    return r


class Tests(unittest.TestCase):
    def test_complete_rolling_window_no_extrapolation(self):
        rows=[row(1,1),row(2,59),row(3,61)]
        self.assertEqual(p.peak_window(rows,'total',59)['tokens'],0)
        best=p.peak_window(rows,'total',120)
        self.assertEqual(best['tokens'],200)
        self.assertEqual(best['end_s']-best['start_s'],60)
        # 不同时计入相隔60秒的两笔
        self.assertEqual(p.peak_window([row(1,1),row(2,61)],'total',120)['tokens'],100)
        self.assertEqual(p.peak_window([row(1,1,success=False)],'total',120)['tokens'],0)

    def test_measured_values_and_missing_usage(self):
        a=p.options([])
        self.assertTrue(p.summarize([row(1,20)],a,120)['measurement_completed'])
        self.assertTrue(p.summarize([row(1,20,ttft=5000)],a,120)['measurement_completed'])
        self.assertFalse(p.summarize([row(1,20),row(2,21,success=False)],a,120)['success_rate_passed'])
        r=row(1,20)
        r['total_tokens']=None
        self.assertFalse(p.summarize([r],a,120)['measurement_completed'])
        self.assertEqual(p.metric(row(1,20),'uncached_total'),50)

    def test_first_text_only(self):
        r=p.new_record(1,0)
        r['started_s']=10
        p.parse_event('{"choices":[{"delta":{"role":"assistant","reasoning_content":"x"}}]}',r,11)
        self.assertIsNone(r['ttft_ms'])
        p.parse_event('{"choices":[{"delta":{"content":"PERF_OK"},"finish_reason":"stop"}]}',r,12)
        p.parse_event('[DONE]',r,13)
        self.assertEqual(r['ttft_ms'],2000)
        self.assertTrue(r['sse_done'])

    def test_confirmed_p99_defaults_and_slow_tail(self):
        a=p.options([])
        self.assertFalse(hasattr(a,'ttft_percentile'))
        self.assertFalse(hasattr(a,'target_tpm'))
        self.assertEqual(a.tpm_basis,'total')
        self.assertEqual(a.min_success_rate,.9)
        rows=[row(i,20,ttft=4999 if i<99 else 6000) for i in range(100)]
        self.assertEqual(p.summarize(rows,a,120)['p99_ttft_ms'],4999)
        rows[98]['ttft_ms']=5000
        summary=p.summarize(rows,a,120)
        self.assertEqual(summary['p99_ttft_ms'],5000)
        self.assertTrue(summary['measurement_completed'])
        rows[0]['ttft_ms']=12000
        self.assertEqual(p.summarize(rows,a,120)['p99_ttft_ms'],6000)

    def test_default_preview_and_validation(self):
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()),patch.object(p,'run',side_effect=AssertionError('network')):
            self.assertEqual(p.main([]),0)
            self.assertEqual(p.main(['--list']),0)
            self.assertEqual(p.main(['--run']),3)
            self.assertEqual(p.main(['--max-requests','2']),3)
            self.assertEqual(p.main(['--rate-rps','nan']),2)
        self.assertTrue(all(isinstance(m['content'],str) for m in p.body(p.options([]),1,'x ')['messages']))

    def test_shared_unique_prefix(self):
        a=p.options([])
        self.assertEqual(p.body(a,1,'x '),p.body(a,2,'x '))
        a.cache_mode='unique'
        self.assertNotEqual(p.body(a,1,'x '),p.body(a,2,'x '))

    def test_scheduler_caps_inflight_and_records_drops(self):
        async def exercise():
            a=p.options([])
            a.duration,a.rate_rps,a.concurrency=.08,100.,1
            inflight,maximum=0,0
            async def fake(session,a,key,index,scheduled,origin,prefix,payload):
                nonlocal inflight,maximum
                if index:
                    inflight+=1
                    maximum=max(maximum,inflight)
                    await asyncio.sleep(.03)
                    inflight-=1
                return row(index,scheduled+.03)
            with tempfile.TemporaryDirectory() as folder:
                out=Path(folder)
                (out/'evidence').mkdir()
                with patch.object(p,'request',side_effect=fake):
                    rows,_,_=await p.run(a,'test-secret',out)
                self.assertEqual(len(rows),8)
                self.assertEqual(maximum,1)
                self.assertTrue(any(r['error_type']=='ClientCapacity' for r in rows))
                self.assertEqual(len({r['index'] for r in rows}),8)
        asyncio.run(exercise())

    def test_local_sse_and_403_preflight_stop(self):
        async def exercise():
            import aiohttp
            from aiohttp import web
            hits=[]
            async def handler(request):
                hits.append(1)
                data=await request.json()
                self.assertTrue(all(isinstance(m['content'],str) for m in data['messages']))
                if len(hits)>1:
                    return web.json_response({'error':{'message':'No permission to access group'}},status=403)
                response=web.StreamResponse(headers={'Content-Type':'text/event-stream'})
                await response.prepare(request)
                await response.write(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
                await asyncio.sleep(.03)
                await response.write(b'data: {"choices":[{"delta":{"content":"PERF_OK"},"finish_reason":"stop"}]}\n\n')
                await asyncio.sleep(.04)
                await response.write(b'data: {"usage":{"prompt_tokens":100,"completion_tokens":2},"choices":[]}\n\ndata: [DONE]\n\n')
                return response
            app=web.Application()
            app.router.add_post('/v1/chat/completions',handler)
            runner=web.AppRunner(app)
            await runner.setup()
            site=web.TCPSite(runner,'127.0.0.1',0)
            await site.start()
            try:
                port=site._server.sockets[0].getsockname()[1]
                a=p.options(['--base-url',f'http://127.0.0.1:{port}/v1'])
                prefix='x '
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
                    import time
                    r=await p.request(session,a,'local-test-secret',1,0,time.perf_counter(),prefix,json.dumps(p.body(a,1,prefix)).encode())
                self.assertTrue(r['success'])
                self.assertGreater(r['ttft_ms'],20)
                self.assertGreater(r['elapsed_ms']-r['ttft_ms'],30)
                self.assertEqual(r['total_tokens'],102)
                with tempfile.TemporaryDirectory() as folder:
                    out=Path(folder)
                    (out/'evidence').mkdir()
                    rows,elapsed,preflight=await p.run(a,'local-test-secret',out)
                    self.assertEqual(rows,[])
                    self.assertEqual(preflight['http_status'],403)
                    self.assertEqual(len(hits),2)
                    summary=p.summarize(rows,a,elapsed)
                    p.report(out,rows,summary,a)
                    import openpyxl
                    wb=openpyxl.load_workbook(out/'results.xlsx')
                    self.assertEqual(wb['纯文本性能测试项'].max_column,10)
                    self.assertEqual(wb['纯文本性能测试项'].freeze_panes,'A5')
            finally: await runner.cleanup()
        asyncio.run(exercise())


if __name__=='__main__': unittest.main()

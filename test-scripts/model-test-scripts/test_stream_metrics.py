#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立流式性能指标采集。

统计 AVG/P99 TTFT、AVG/P99 TPOT、GEN TOK/S、成功率、SSE_DONE 和 x-request-id；
GEN TOK/S 必须与服务端使用记录交叉核对。运行：python3 test_stream_metrics.py --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" --requests 90 --concurrency 5 --run --confirm-paid
"""
import argparse, concurrent.futures, csv, json, os, re, statistics, subprocess, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def pct(values, p):
    values = sorted(float(x) for x in values if x not in ('', None))
    if not values: return None
    pos=(len(values)-1)*p/100; lo=int(pos); hi=min(lo+1,len(values)-1)
    return round(values[lo]+(values[hi]-values[lo])*(pos-lo),2)

def args():
    p=argparse.ArgumentParser(description='流式 TTFT/TPOT/生成速度采集')
    p.add_argument('--base-url',default=os.getenv('API_BASE','http://127.0.0.1:8000/v1')); p.add_argument('--key-env',default=os.getenv('API_KEY_ENV','API_KEY')); p.add_argument('--model',default=os.getenv('MODEL','test-model'))
    p.add_argument('--requests',type=int,default=90); p.add_argument('--concurrency',type=int,default=5); p.add_argument('--input-tokens',type=int,default=1000); p.add_argument('--max-output-tokens',type=int,default=200); p.add_argument('--timeout',type=int,default=180); p.add_argument('--output-dir',type=Path); p.add_argument('--run',action='store_true'); p.add_argument('--confirm-paid',action='store_true'); return p.parse_args()

def prompt(n):
    return '请阅读资料并用清晰的分级条目总结重点，严格只输出总结正文。' + '社区阅读活动资料：报名、场地、志愿者、物料、宣传、预算和复盘。' * max(1,n//18)

def one(a,key,no):
    body={'model':a.model,'messages':[{'role':'user','content':prompt(a.input_tokens)}],'max_completion_tokens':a.max_output_tokens,'stream':True,'stream_options':{'include_usage':True}}
    payload=tempfile.NamedTemporaryFile('w',suffix='.json',delete=False,encoding='utf-8').name; headers=tempfile.NamedTemporaryFile('w',suffix='.headers',delete=False,encoding='utf-8').name; started=time.perf_counter()
    try:
        Path(payload).write_text(json.dumps(body,ensure_ascii=False),encoding='utf-8')
        cmd=['curl','-sS','--no-buffer','--max-time',str(a.timeout),'-H',f'Authorization: Bearer {key}','-H','Content-Type: application/json','-H','Accept: text/event-stream','--data-binary','@'+payload,'--dump-header',headers,'--write-out','\n__HTTP_STATUS__:%{http_code}',a.base_url.rstrip('/')+'/chat/completions']
        proc=subprocess.run(cmd,text=True,capture_output=True,check=False); raw=proc.stdout; status=None; marker='\n__HTTP_STATUS__:'
        if marker in raw:
            raw,st=raw.rsplit(marker,1); status=int(st.strip()) if st.strip().isdigit() else None
        first=None; chunk_times=[]; usage={}; finish=''; done=False; content=''
        for line in raw.splitlines():
            if not line.startswith('data:'): continue
            data=line[5:].strip()
            if data=='[DONE]': done=True; continue
            try: item=json.loads(data)
            except json.JSONDecodeError: continue
            usage=item.get('usage') or usage; choice=(item.get('choices') or [{}])[0]; finish=choice.get('finish_reason') or finish
            delta=choice.get('delta') or {}; piece=delta.get('content') or delta.get('reasoning_content') or ''
            if piece or delta.get('tool_calls'):
                now=(time.perf_counter()-started)*1000; first=now if first is None else first; chunk_times.append(now); content+=piece
        elapsed=(time.perf_counter()-started)*1000; output=int(usage.get('completion_tokens') or 0); ttft=round(first,2) if first is not None else ''
        generation=max(elapsed-(first or elapsed),0); tok_s=round(output/(generation/1000),2) if output and generation>0 else ''
        intervals=[b-x for x,b in zip(chunk_times,chunk_times[1:])]; tpot=round(statistics.mean(intervals),2) if intervals else ''
        rid=''; m=re.search(r'(?im)^x-request-id:\s*(\S+)',Path(headers).read_text(errors='replace')); rid=m.group(1) if m else ''
        return {'request_no':no,'http_status':status or '','finish_reason':finish,'input_tokens':usage.get('prompt_tokens',''),'output_tokens':output,'ttft_ms':ttft,'tpot_ms':tpot,'generation_ms':round(generation,2),'gen_tok_s':tok_s,'elapsed_ms':round(elapsed,2),'sse_done':done,'request_id':rid,'error':proc.stderr.strip() if proc.returncode else ''}
    finally:
        for f in (payload,headers):
            try: Path(f).unlink()
            except FileNotFoundError: pass

def main():
    a=args()
    if not a.base_url.startswith(('http://', 'https://')): raise SystemExit('base-url 必须使用 http:// 或 https://')
    print(f'项目=流式性能指标采集；模型={a.model}；请求={a.requests}；并发={a.concurrency}')
    if not (a.run and a.confirm_paid): print('仅预览；真实请求需添加 --run --confirm-paid'); return
    key=os.environ.get(a.key_env,'')
    if not key: raise SystemExit(f'未设置环境变量 {a.key_env}')
    out=(a.output_dir.resolve() if a.output_dir else ROOT/'results'/(datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'-stream-metrics')); out.mkdir(parents=True,exist_ok=False); rows=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.concurrency) as pool:
        fs=[pool.submit(one,a,key,n) for n in range(1,a.requests+1)]
        for f in concurrent.futures.as_completed(fs):
            r=f.result(); rows.append(r); print(f"#{r['request_no']} HTTP={r['http_status'] or '-'} TTFT={r['ttft_ms'] or '-'}ms TPOT={r['tpot_ms'] or '-'}ms GEN={r['gen_tok_s'] or '-'} tok/s TOTAL={r['elapsed_ms']}ms")
    ok=[r for r in rows if r['http_status']==200]; avg=lambda k: round(statistics.mean([float(r[k]) for r in ok if r[k] not in ('',None)]),2) if any(r[k] not in ('',None) for r in ok) else None
    summary={'model':a.model,'base_url':a.base_url,'requests':len(rows),'http_200':len(ok),'success_rate':round(len(ok)/len(rows)*100,2) if rows else 0,'avg_ttft_ms':avg('ttft_ms'),'p99_ttft_ms':pct([r['ttft_ms'] for r in ok],99),'avg_tpot_ms':avg('tpot_ms'),'p99_tpot_ms':pct([r['tpot_ms'] for r in ok],99),'gen_tok_s':avg('gen_tok_s'),'generated_at':datetime.now(timezone.utc).isoformat()}
    with (out/'results.csv').open('w',encoding='utf-8-sig',newline='') as f: w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps(summary,ensure_ascii=False,indent=2)); print(f'结果目录：{out}')
if __name__=='__main__': main()

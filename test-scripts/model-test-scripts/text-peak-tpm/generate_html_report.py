#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从现有实测目录生成可离线浏览的 HTML 报告，不发送 API 请求。"""
import argparse
import bisect
import collections
import csv
import html
import json
import math
from pathlib import Path


def esc(v): return html.escape(str(v), quote=True)
def number(v): return f'{v:,}'
def percent(v): return f'{v:.2%}'
COLORS = ['#167b83','#b83c41','#d78534','#7461a8','#7091a7','#475569']


def svg_chart(series, xmax, ymax, ylabel, target=None):
    w,h,l,r,t,b=1000,280,82,22,24,48
    pw,ph=w-l-r,h-t-b
    x=lambda v:l+v/xmax*pw
    y=lambda v:t+ph-v/ymax*ph
    s=[f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(ylabel)}"><title>{esc(ylabel)}</title>']
    for i in range(5):
        value=ymax*i/4; py=y(value)
        s.append(f'<line x1="{l}" x2="{w-r}" y1="{py}" y2="{py}" stroke="#e3e9ee"/><text x="{l-12}" y="{py+4}" text-anchor="end">{value:,.0f}</text>')
    for i in range(6):
        value=xmax*i/5
        s.append(f'<text x="{x(value)}" y="{h-22}" text-anchor="middle">{value:.0f}</text>')
    s.append(f'<text x="{l}" y="15">{esc(ylabel)}</text><text x="{w-r}" y="{h-2}" text-anchor="end">正式负载开始后的秒数</text>')
    if target:
        s.append(f'<line x1="{l}" x2="{w-r}" y1="{y(target)}" y2="{y(target)}" stroke="#b83c41" stroke-dasharray="6 5"/><text x="{w-r}" y="{y(target)-7}" text-anchor="end">目标 {target:,.0f}</text>')
    for name,points,color in series:
        path=' '.join(f'{x(a):.2f},{y(v):.2f}' for a,v in points)
        s.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="{path}"/>')
        for a,v in points:
            s.append(f'<circle cx="{x(a):.2f}" cy="{y(v):.2f}" r="3" fill="{color}"><title>{esc(name)} · {a:g}秒：{v:,.2f}</title></circle>')
    s.append('</svg>')
    return ''.join(s)


def generate(folder, destination):
    summary=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    with (folder/'results.csv').open(encoding='utf-8-sig') as f: raw=list(csv.DictReader(f))
    rows=[]
    for r in raw:
        rows.append(dict(index=int(r['index']),success=r['success']=='True',
            status=int(r['http_status']) if r['http_status'] else None,error=r['error_type'],
            started=float(r['started_s']) if r['started_s'] else None,
            completed=float(r['completed_s']) if r['completed_s'] else None,
            ttft=float(r['ttft_ms'])/1000 if r['ttft_ms'] else None,
            elapsed=float(r['elapsed_ms'])/1000 if r['elapsed_ms'] else None,
            tokens=int(r['total_tokens']) if r['total_tokens'] else 0,
            cached=int(r['cached_tokens']) if r['cached_tokens'] else 0))
    backend_messages=collections.Counter()
    stream_messages=collections.Counter()
    for r in rows:
        if r['error'] not in ('HTTPError','ValueError'):
            continue
        evidence=folder/'evidence'/f"{r['index']:06}.json"
        if not evidence.exists():
            continue
        record=json.loads(evidence.read_text(encoding='utf-8'))
        if r['error']=='HTTPError':
            try:
                err=json.loads(record.get('error_response','{}')).get('error',{})
            except (ValueError,TypeError):
                err={}
            message=err.get('message','') if isinstance(err,dict) else str(err)
            if 'Too many connections' in message:
                backend_messages['连接数耗尽']+=1
            elif 'Database error' in message:
                backend_messages['数据库错误']+=1
            elif 'rate limit' in message.lower():
                backend_messages['上游限流']+=1
            elif 'pending requests' in message.lower():
                backend_messages['待处理请求过多']+=1
            elif r['status']==503:
                backend_messages['服务暂不可用']+=1
        else:
            for item in record.get('events',[]):
                err=item.get('error')
                if not err:
                    continue
                message=err.get('message','') if isinstance(err,dict) else str(err)
                if '繁忙' in message:
                    stream_messages['服务繁忙']+=1
                elif 'rate limit' in message.lower():
                    stream_messages['上游限流']+=1
                elif 'unavailable' in message.lower():
                    stream_messages['服务暂不可用']+=1
    error_details='；'.join(f'{esc(k)} {v:,} 次' for k,v in backend_messages.items())
    if stream_messages:
        error_details+='。HTTP 200 流内错误：'+'；'.join(f'{esc(k)} {v:,} 次' for k,v in stream_messages.items())
    error_details+='。Error 1040 通常表示数据库连接数耗尽，具体根因需渠道后台确认。'
    assert len(rows)==summary['scheduled_requests']
    assert sum(r['success'] for r in rows)==summary['success_requests']
    counts=collections.Counter()
    for r in rows:
        if r['success']: label='完整成功'
        elif r['error'] in ('ClientCapacity','ClientScheduleLate'): label=r['error']
        elif r['error']=='TimeoutError': label='请求超时'
        elif r['status']==200: label='HTTP 200 流内失败'
        else: label=f"HTTP {r['status']}"
        counts[label]+=1
        r['category']=label
    labels={'ClientCapacity':'客户端并发上限，未发送','ClientScheduleLate':'客户端调度落后，未发送'}
    errors=[('HTTP 503',counts['HTTP 503'],'服务暂不可用','渠道／上游'),
            ('HTTP 429',counts['HTTP 429'],'上游限流或待处理队列过多','渠道／上游'),
            ('HTTP 500',counts['HTTP 500'],'连接数耗尽／数据库错误','渠道后端'),
            ('HTTP 200 流内失败',counts['HTTP 200 流内失败'],'流内返回繁忙、限流或暂不可用','渠道／上游'),
            ('HTTP 502',counts['HTTP 502'],'openai_error / bad_response_status_code','网关／上游'),
            ('请求超时',counts['请求超时'],'客户端总时限内未完整返回；无法单凭超时定位根因','待定位'),
            ('ClientCapacity',counts['ClientCapacity'],'达到640个在途请求上限，未排队或补发','客户端限制'),
            ('ClientScheduleLate',counts['ClientScheduleLate'],'调度明显落后于计划，未发出该样本','客户端调度')]
    assert sum(v for _,v,_,_ in errors)+counts['完整成功']==len(rows)
    errors=[entry for entry in errors if entry[1]]
    target_tpm,target_ttft=200000000,5
    success=summary['success_requests'];sent=summary['sent_requests'];n=len(rows)
    peak=summary['peak_60s'];peak_n=peak['tokens'];p99=summary['p99_ttft_ms']/1000
    successful=[r for r in rows if r['success']]
    observations=sorted((r['completed'],r['tokens']) for r in successful)
    times=[v[0] for v in observations];totals=[0]
    for _,v in observations:totals.append(totals[-1]+v)
    window=lambda end:totals[bisect.bisect_right(times,end)]-totals[bisect.bisect_right(times,end-60)]
    assert window(peak['end_s'])==peak_n
    rolling=[(t,window(t)/1e6) for t in sorted(set(list(range(60,int(summary['observed_s'])+1))+[peak['end_s']]))]
    timeline=[]
    for start in range(0,math.ceil(summary['observed_s']/5)*5,5):
        rr=[r for r in rows if r['completed'] is not None and start<=r['completed']<start+5]
        timeline.append((start+5,sum(r['success'] for r in rr)/5,
                         sum(not r['success'] and r['started'] is not None for r in rr)/5,
                         sum(r['started'] is None for r in rr)/5))
    maxrate=max(max(v[1:]) for v in timeline)
    rate_chart=svg_chart([(label,[(v[0],v[i+1]) for v in timeline],COLORS[i]) for i,label in enumerate(['成功完成','已发送但失败','客户端未发送'])],160,math.ceil(maxrate/20)*20,'请求数 / 秒（5秒分箱）')
    tpm_chart=svg_chart([('滚动60秒总TPM',rolling,COLORS[0])],160,220,'百万 tokens / 分钟',200)
    quantiles=[(k,v/1000) for k,v in summary['ttft_ms'].items()]
    bar_max=math.ceil(max(v for _,v in quantiles)/5)*5
    bars=''
    for name,v in quantiles:
        bars+=f'<div class="hbar"><span>{name}</span><div class="track"><div style="width:{v/bar_max*100:.2f}%;background:{COLORS[1] if v>=5 else COLORS[0]}"></div></div><strong>{v:.3f} 秒</strong></div>'
    partition=[('完整成功',success,COLORS[0]),('已发送但失败',sent-success,COLORS[1]),('客户端未发送',n-sent,COLORS[2])]
    stack=''.join(f'<div style="width:{c/n*100:.6f}%;background:{col}" title="{label}：{c:,}（{c/n:.2%}）"></div>' for label,c,col in partition)
    partition_legend=''.join(f'<span><i style="background:{col}"></i>{label} <b>{c:,}</b> · {c/n:.2%}</span>' for label,c,col in partition)
    errtable=''.join(f'<tr><td>{esc(labels.get(k,k))}</td><td class="num">{v:,}</td><td class="num">{v/n:.2%}</td><td>{esc(source)}</td><td>{esc(msg)}</td></tr>' for k,v,msg,source in errors)
    basis=summary['peak_by_basis']
    basistable=''.join(f'<tr><td>{label}</td><td class="num">{b["tokens"]:,}</td><td>{b["start_s"]:.3f}–{b["end_s"]:.3f} 秒</td></tr>' for key,label in [('total','输入＋输出（含缓存输入）'),('input','仅输入（含缓存）'),('output','仅输出'),('uncached_total','输入＋输出－缓存输入')] for b in [basis[key]])
    cfg=manifest['config']
    meta=[('模型',cfg['model']),
          ('运行起止（上海）',manifest['started_at'][:19].replace('T',' ')+' → '+manifest['ended_at'][:19].replace('T',' ')),
          ('计划发起窗口',f"{cfg['duration']:g} 秒"),('正式观测时长',f"{summary['observed_s']:.3f} 秒"),
          ('计划速率／并发上限',f"{cfg['rate_rps']:g} RPS ／ {cfg['concurrency']}"),
          ('输入素材／输出预算',f"{cfg['input_units']:,} 个 x 单位 ／ {cfg['max_output_tokens']} tokens"),
          ('缓存条件','shared 固定前缀，探测可能预热缓存'),('单请求总超时',f"{cfg['timeout']:g} 秒"),
          ('实际发送（含探测）',f"{manifest['actual_requests']:,} 次"),('成功样本 usage 覆盖率',percent(summary['usage_coverage']))]
    metahtml=''.join(f'<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>' for k,v in meta)
    detaildata=[[r['index'],r['category'],r['status'],round(r['ttft'],3) if r['ttft'] is not None else None,
                 round(r['elapsed'],3) if r['elapsed'] is not None else None,r['tokens'],
                 round(r['completed'],3) if r['completed'] is not None else None,
                 r['started'] is not None] for r in rows]
    choices=''.join(f'<option value="{esc(k)}">{esc(labels.get(k,k))}（{v:,}）</option>' for k,v in counts.items())
    verdict='未满足本次验收要求'
    assertions=[peak_n>=target_tpm,p99<target_ttft,summary['success_rate']>=.9]
    if all(assertions):verdict='满足本次验收要求'
    content='''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Kimi K3 · 纯文本性能实测报告</title>
<style>
:root{--ink:#162b3b;--muted:#566978;--line:#dce5eb;--teal:#167b83;--red:#b83c41;--bg:#f3f6f8}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}main{max-width:1200px;margin:auto;padding:46px 34px 60px}a{color:#116e79;text-decoration:none}a:hover{text-decoration:underline}header{border-bottom:2px solid var(--ink);padding-bottom:28px;margin-bottom:28px}.eyebrow{font-size:12px;letter-spacing:.16em;color:var(--teal);font-weight:700}.top{display:flex;align-items:start;justify-content:space-between;gap:20px}h1{font-size:34px;line-height:1.25;margin:12px 0}h2{font-size:21px;margin:0 0 16px}h3{font-size:16px;margin:0 0 10px}p{margin:8px 0}.muted{color:var(--muted)}.badge{display:inline-block;border-radius:5px;padding:7px 12px;font-size:13px;font-weight:600;background:#fbeaec;color:var(--red);white-space:nowrap}.subtitle{color:var(--muted);max-width:840px}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin:24px 0}.metric,.panel{background:white;border:1px solid var(--line);border-radius:10px;padding:24px}.metric .label{color:var(--muted);font-size:13px}.metric .value{font-size:36px;line-height:1.3;letter-spacing:-.04em;font-weight:650;margin:9px 0;font-variant-numeric:tabular-nums}.metric .value small{font-size:16px;letter-spacing:0;font-weight:400}.metric .note{font-size:13px;color:var(--muted)}.metric .state{font-size:13px;color:var(--red);margin-top:10px}.callout{padding:18px 22px;border-left:4px solid var(--teal);background:#eaf4f4;margin:22px 0}.panel{margin:22px 0}.section-tag{color:var(--teal);font-size:12px;font-weight:700;margin-right:10px}.legend{display:flex;gap:12px 22px;flex-wrap:wrap;font-size:13px}.legend span{display:inline-flex;gap:7px;align-items:center}.legend i{width:10px;height:10px;display:inline-block;border-radius:2px}.stack{height:30px;display:flex;overflow:hidden;border-radius:4px;margin:18px 0}.small{font-size:13px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.grid .panel{margin:0}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;font-size:12px;font-weight:600;color:var(--muted);background:#f5f8fa}th,td{padding:12px 13px;border-bottom:1px solid var(--line);vertical-align:top}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}svg{display:block;width:100%;height:auto;margin-top:16px}svg text{fill:var(--muted);font:12px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}.hbar{display:grid;grid-template-columns:40px 1fr 98px;align-items:center;gap:12px;margin:16px 0;font-size:13px}.hbar strong{text-align:right;font-variant-numeric:tabular-nums;font-weight:500}.track{height:12px;background:#edf1f4;border-radius:3px;overflow:hidden}.track>div{height:100%;border-radius:3px}.meta{display:grid;grid-template-columns:1fr 1fr;gap:12px 26px}.meta>div{border-bottom:1px solid var(--line);padding:9px 0}dt{font-size:12px;color:var(--muted)}dd{margin:4px 0 0;overflow-wrap:anywhere;font-size:14px}.actions{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:16px 0}button,select{font:inherit;font-size:13px;padding:8px 12px;border:1px solid #bacbd5;border-radius:5px;background:white;color:var(--ink)}button{cursor:pointer}button:hover{background:#edf4f6}button:disabled{opacity:.45;cursor:default}.hash{font:11px/1.7 ui-monospace,monospace;word-break:break-all}.nowrap{white-space:nowrap}.endnote{border-top:1px solid var(--line);margin-top:28px;padding-top:20px;color:var(--muted);font-size:12px}.screenreader{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}li{margin:7px 0}code{background:#eff3f5;padding:2px 5px;border-radius:3px;font-size:13px}.threshold{color:var(--red)}
@media(max-width:760px){main{padding:24px 16px}.metrics,.grid,.meta{grid-template-columns:1fr}.top{display:block}h1{font-size:27px}.top .badge{margin-top:12px}.panel,.metric{padding:18px}.metric .value{font-size:32px}.hash{font-size:11px}}
@media print{body{background:white}main{max-width:none;padding:0}.panel,.metric{break-inside:avoid;border-radius:0}.actions,.no-print{display:none!important}.metrics{gap:10px}.metric .value{font-size:28px}h1{font-size:26px}.panel{margin:16px 0;padding:16px}.table-wrap{overflow:visible}#detail-panel{display:none}.endnote{font-size:10px}a{color:inherit}svg{max-height:260px}.grid{display:block}.grid .panel{margin:16px 0}@page{size:A4;margin:14mm}}
</style></head><body><main>
<header><div class="eyebrow">PERFORMANCE REVIEW / 2026.10.15</div><div class="top"><div><h1>Kimi K3 纯文本性能实测报告</h1><p class="subtitle">评估峰值吞吐、P99 首字延迟与请求成功率。仅包含文本；不涉及视频、图片或音频分析。</p></div><span class="badge">@@VERDICT@@</span></div><p class="muted small">测试日期：2026-10-15 · 上海时间 · <button class="no-print" onclick="window.print()">打印 / 另存 PDF</button></p></header>
<div class="metrics"><article class="metric"><div class="label">峰值总 TPM · 完整滚动 60 秒</div><div class="value">@@TPM@@ <small>亿 tokens / 分钟</small></div><div class="note">实测 @@TOKENS@@；目标 ≥2 亿</div><div class="state">未达标 · 达到目标的 @@ATTAIN@@</div></article><article class="metric"><div class="label">P99 首字延迟 · 成功响应</div><div class="value">@@P99@@ <small>秒</small></div><div class="note">要求 &lt;5 秒；实测为门槛的 @@MULTIPLE@@ 倍</div><div class="state">未达标 · 中位数也为 @@P50@@ 秒</div></article><article class="metric"><div class="label">成功率 · 全部正式计划请求</div><div class="value">@@SUCCESS_RATE@@</div><div class="note">@@SUCC@@ / @@PLANNED@@；要求 ≥90%</div><div class="state">未达标 · 仅已发送请求成功率 @@SENT_RATE@@</div></article></div>
<div class="callout"><strong>结论：当前渠道及测试条件下未满足要求。</strong><p>已发送请求存在大量服务端错误，具体分类见下方错误分析，表明渠道／上游在此负载下承载不足；同时客户端有 @@DROPPED@@ 个计划样本未发送。不能把所有失败都归因于渠道，也不能用提高客户端并发替代渠道排障。</p></div>
<section class="panel"><h2><span class="section-tag">01 /</span>请求去向与统计口径</h2><p>正式计划 <strong>@@PLANNED@@</strong> 次，实际发送 <strong>@@SENT@@</strong> 次，完整成功 <strong>@@SUCC@@</strong> 次。另有 1 次探测，不计入正式统计。</p><div class="stack" role="img" aria-label="计划请求分为成功、已发送失败及客户端未发送">@@STACK@@</div><div class="legend">@@PARTITION@@</div><p class="muted small">“完成”日志包含客户端跳过的计划样本，并不等于成功响应。HTTP 200 也不直接等于完整成功：必须满足有效 SSE、结束标记、正常结束原因及正文精确匹配。</p></section>
<section class="panel"><h2><span class="section-tag">02 /</span>峰值吞吐与时间趋势</h2><p>最高完整窗口为正式负载开始后 <strong>@@WINDOW@@ 秒</strong>，包含 @@PEAK_SUCC@@ 个成功请求，累计 @@TOKENS@@ tokens。窗口内另有 @@PEAK_FAIL@@ 个失败计划样本（含客户端未发送）。</p>@@TPM_CHART@@<p class="muted small">横轴为窗口结束时间；前 60 秒不计算完整窗口 TPM。按成功响应完成时刻累计官方输入＋输出计数，包含缓存输入，不对短时间突发外推。图中保留精确峰值采样点。</p><div class="table-wrap"><table><thead><tr><th>计数口径</th><th class="num">各自最高 60 秒 token 数</th><th>窗口范围</th></tr></thead><tbody>@@BASIS@@</tbody></table></div><p class="muted small">各口径的峰值窗口可能不同，因此“各自峰值”不能直接相加。扣除缓存后的数值不能替代本次约定的含缓存总 TPM。</p></section>
<section class="panel"><h2>请求结果随时间变化</h2><div class="legend"><span><i style="background:#167b83"></i>成功完成</span><span><i style="background:#b83c41"></i>已发送但失败</span><span><i style="background:#d78534"></i>客户端未发送</span></div>@@RATE_CHART@@<p class="muted small">5 秒分箱内的记录数除以 5，表示完成／丢弃事件速率，不是请求发起 RPS。120 秒后为在途请求收尾阶段。</p></section>
<div class="grid"><section class="panel"><h2><span class="section-tag">03 /</span>首字延迟分布</h2>@@BARS@@<p class="threshold small">验收门槛：P99 严格小于 5 秒。</p><p class="muted small">样本为 @@SUCC@@ 个完整成功响应；失败请求通过独立成功率门槛约束。TTFT 从开始发送至首个非空正文 delta.content，包含连接及等待，不把思考或心跳当正文。</p></section><section class="panel"><h2>如何理解这次结果</h2><ul><li>峰值吞吐为目标的 <strong>@@ATTAIN@@</strong>，本次未证明达到 2 亿 TPM。</li><li>P50 为 <strong>@@P50@@ 秒</strong>，说明首字超时限不只出现在少数极端长尾。</li><li>排除客户端未发送请求后，成功率仍仅 <strong>@@SENT_RATE@@</strong>，渠道问题不能用客户端丢弃解释掉。</li><li>客户端并发限制与调度落后同时存在，单机发压能力也限制了本次结论。</li></ul></section></div>
<section class="panel"><h2><span class="section-tag">04 /</span>失败归因与错误分析</h2><div class="table-wrap"><table><thead><tr><th>类别</th><th class="num">次数</th><th class="num">占计划请求</th><th>发生位置</th><th>错误说明</th></tr></thead><tbody>@@ERRORS@@</tbody></table></div><p class="muted small">@@ERROR_DETAILS@@</p><h3 style="margin-top:20px">建议处理顺序</h3><ol><li>渠道先排查 HTTP 500 的连接数耗尽，核对数据库连接池、连接释放与实例连接上限。</li><li>核对上游 TPM / RPM / 并发配额、网关待处理队列与可用容量后再加压。</li><li>从低速率逐档复测，保留相同 token 口径和缓存模式，同时监测客户端 CPU、网络、文件写入与在途请求。</li><li>渠道稳定后再提升负载；仅提高 640 并发上限，可能增加排队、错误和延迟。</li></ol></section>
<section class="panel"><h2><span class="section-tag">05 /</span>测试条件与结果边界</h2><dl class="meta">@@META@@</dl><p class="muted small">验收标准：含缓存输入＋输出峰值 ≥2 亿 TPM、P99 首字延迟 &lt;5 秒、成功率 ≥90%。完成数据采集不代表满足验收要求。</p><p class="muted small">这是固定文本前缀、短输出、共享缓存条件下的单次实测；不能外推为未缓存负载、长输出负载、持续稳定容量或模型服务端配额。观测时长不含探测及报告导出，运行起止时间覆盖这些步骤。</p></section>
<section class="panel" id="detail-panel"><h2><span class="section-tag">06 /</span>逐请求明细</h2><div class="actions"><label for="category">结果类别</label><select id="category"><option value="">全部（@@PLANNED@@）</option>@@CHOICES@@</select><button id="prev">上一页</button><button id="next">下一页</button><span id="page-info" class="muted small" aria-live="polite"></span></div><div class="table-wrap"><table><thead><tr><th>序号</th><th>结果</th><th>HTTP</th><th class="num">首字 / 秒</th><th class="num">总耗时 / 秒</th><th class="num">总 tokens</th><th class="num">完成偏移 / 秒</th></tr></thead><tbody id="detail-body"></tbody></table></div><p class="muted small">每页50条，可按结果类别筛选。未发送样本不显示 HTTP 状态和响应耗时。</p></section>
<footer class="endnote">统计范围：@@PLANNED@@ 个正式计划样本，1 次探测排除。图表与明细均为本次实测结果。报告支持离线浏览和打印。</footer>
</main><script>
const requestRows=@@DATA@@;
const names={ClientCapacity:'客户端并发上限',ClientScheduleLate:'客户端调度落后'};
let page=0;const size=50;
const select=document.getElementById('category'),tbody=document.getElementById('detail-body');
function render(){const filtered=requestRows.filter(r=>!select.value||r[1]===select.value),pages=Math.max(1,Math.ceil(filtered.length/size));page=Math.min(page,pages-1);tbody.replaceChildren();filtered.slice(page*size,(page+1)*size).forEach(r=>{const tr=document.createElement('tr');r.forEach((v,i)=>{if(i===7)return;const td=document.createElement('td');td.textContent=i===1?(names[v]||v):v===null?'—':i===0?'#'+v:i===2?v:i>=3&&i!==5?Number(v).toFixed(3):Number(v).toLocaleString('zh-CN');if(i>=3)td.className='num';tr.append(td);});tbody.append(tr);});document.getElementById('page-info').textContent=`第 ${page+1} / ${pages} 页 · ${filtered.length.toLocaleString()} 条`;document.getElementById('prev').disabled=page===0;document.getElementById('next').disabled=page===pages-1;}
select.addEventListener('change',()=>{page=0;render();});document.getElementById('prev').addEventListener('click',()=>{page--;render();});document.getElementById('next').addEventListener('click',()=>{page++;render();});render();
</script></body></html>'''
    replacements=dict(VERDICT=verdict,TPM=f'{peak_n/1e8:.3f}',TOKENS=number(peak_n),ATTAIN=percent(peak_n/target_tpm),
        P99=f'{p99:.3f}',MULTIPLE=f'{p99/5:.2f}',P50=f'{summary["ttft_ms"]["P50"]/1000:.3f}',SUCCESS_RATE=percent(summary['success_rate']),
        SUCC=number(success),PLANNED=number(n),SENT=number(sent),SENT_RATE=percent(success/sent),DROPPED=number(n-sent),
        STACK=stack,PARTITION=partition_legend,WINDOW=f'{peak["start_s"]:.3f}–{peak["end_s"]:.3f}',
        PEAK_SUCC=number(summary['peak_window_successes']),PEAK_FAIL=number(summary['peak_window_failures']),
        TPM_CHART=tpm_chart,BASIS=basistable,RATE_CHART=rate_chart,BARS=bars,ERRORS=errtable,ERROR_DETAILS=error_details,META=metahtml,
        CHOICES=choices,DATA=json.dumps(detaildata,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c'))
    for k,v in replacements.items():content=content.replace('@@'+k+'@@',v)
    assert '@@' not in content
    destination.write_text(content,encoding='utf-8')
    print(f'报告：{destination.resolve()}')
    print(f'数据校验通过：计划{n}；发送{sent}；成功{success}；精确峰值窗口{window(peak["end_s"])} tokens')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('result_dir',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    generate(args.result_dir,args.output or args.result_dir/'report.html')

"""Standalone HTML layout based on the local model-test report structure."""
import html
import json
import os
from urllib.parse import quote

from .report_results import overview


def render(content, source):
    c = content.model_dump()
    e = lambda value: html.escape(str(value), quote=True)
    original = {row["id"]: row for row in source["rows"]}
    columns = {"project": "测试项目", "case": "用例 / 指标", "observed": "实测 / 关键响应", "conclusion": "报告结论", "note": "判定说明 / 证据"}
    visible = [row for row in c["rows"] if row["visible"]]
    stats = source.get("overview") or overview(source.get("details", []))
    tiles = [("测试项目", len(source["runs"])), ("已归档用例", stats["total"]),
             ("通过", stats["passed"]), ("未通过", stats["failed"]),
             ("人工复核 / 待复核", stats["review"]), ("跳过 / 数据展示", f'{stats["skipped"]} / {stats["data"]}')]
    tiles_html = ''.join(f'<div class="tile"><span>{e(label)}</span><b>{e(value)}</b></div>' for label, value in tiles)
    rows = []
    for row in visible:
        changed = row["id"] not in original or any(row[k] != original[row["id"]][k] for k in columns)
        cells = ''.join(f'<td class="{"outcome" if k == "conclusion" else ""}">{e(row[k])}</td>' for k in c["columns"])
        rows.append(f'<tr class="{"edited" if changed else ""}">{cells}</tr>')
    table = '<div class="table-wrap"><table><thead><tr>' + ''.join(f'<th>{columns[k]}</th>' for k in c["columns"]) + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
    demo = any(run.get("demo") for run in source["runs"])
    parts = []
    if "summary" in c["sections"]:
        parts.append(f'<section><h2>测试总结 <small>Summary</small></h2><p class="prose">{e(c["summary"])}</p>{"<p class=warning>本报告包含模拟数据。</p>" if demo else ""}<div class="tiles">{tiles_html}</div><p class="muted">用例统计来自已归档源脚本断言；执行完成不代表模型通过验收。表格中的人工结论单独保存。</p></section>')
        run_rows = ''.join(f'<tr><td>#{r["run_id"]}</td><td>{e(r["name"])}</td><td>{e(r["model"])}</td><td>{e({"completed":"执行完成","error":"执行异常","cancelled":"已取消","queued":"排队","interrupted":"重启中断"}.get(r["status"],r["status"]))}</td><td>{"模拟数据" if r.get("demo") else "源脚本实测"}</td></tr>' for r in source["runs"])
        parts.append('<section><h2>项目执行总览 <small>Projects</small></h2><div class="table-wrap"><table><thead><tr><th>运行</th><th>测试项目</th><th>模型</th><th>执行状态</th><th>数据来源</th></tr></thead><tbody>' + run_rows + '</tbody></table></div></section>')
        metrics = [("平均首字延迟", stats["ttft_ms"], "ms · 源脚本口径"), ("平均每令牌时间", stats["tpot_ms"], "ms"), ("平均请求耗时", stats["elapsed_ms"], "ms"),
                   ("输入 Token", stats.get("input_tokens"), "归档样本合计"), ("输出 Token", stats.get("output_tokens"), "归档样本合计"),
                   ("缓存 Token 比率", stats.get("cache_token_ratio"), "% · 已返回缓存与输入 Usage 的样本")]
        parts.append('<section class="metric-strip">' + ''.join(f'<div class="metric"><span>{label}</span><b>{value if value is not None else "—"}</b><small>{unit}</small></div>' for label, value, unit in metrics) + '</section><p class="muted">仅统计归档明细中的已测值；缺失指标保持为空。各脚本的首字口径以原始证据为准。</p>')
    if "results" in c["sections"]:
        parts.append(f'<section><h2>测试项目明细 <small>Detailed Results</small></h2>{table}<p class="muted">主题色左线标记人工编辑或新增行。HTTP、耗时、Tokens、响应和源判定按实际记录展示。</p></section>')
    charts = source.get("charts", [])
    if "curves" in c["sections"] and charts:
        parts.append('<section><h2>性能指标曲线 <small>Performance</small></h2><p class="muted">单位 ms；每条曲线最多展示前 500 个归档样本；空值不绘制。TTFT 使用各源脚本口径。</p>' + ''.join(f'<h3>{e(ch["name"])}{" · 模拟数据" if ch.get("demo") else ""}</h3><canvas width="1000" height="280" aria-label="{e(ch["name"])}指标曲线"></canvas>' for ch in charts) + '</section>')
    if "evidence" in c["sections"]:
        base = os.environ.get("MT_PUBLIC_URL", "").rstrip("/")
        evidence = []
        for r in source["runs"]:
            files = ' · '.join(f'<a href="{e(base)}/api/suites/runs/{r["run_id"]}/files/{quote(path, safe="/")}">{e(path)}</a>' for path in r.get("files", []))
            evidence.append(f'<div class="evidence"><a href="{e(base)}/#reports">#{r["run_id"]} {e(r["name"])}</a><p>{e(r["provider"])} · {e(r["model"])}</p><p>{files}</p><small>源脚本 SHA256：{e(r["source_sha256"])}<br>证据清单 SHA256：{e(r["manifest_sha256"])}</small></div>')
        parts.append('<section><h2>原始证据 <small>Evidence</small></h2>' + ''.join(evidence) + '</section>')
    if source.get("truncated"):
        parts.append('<p class="warning">报告明细已达显示上限（每张 CSV 前 100 行、合计最多 1900 行）；统计仅针对已收集用例，完整结果见运行证据。</p>')
    data = json.dumps(charts, ensure_ascii=False, allow_nan=False).replace('<', '\\u003c')
    css = '''*{box-sizing:border-box;letter-spacing:0}body{margin:0;color:#252b2b;background:#f2f4f3;font:14px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}main{max-width:1240px;margin:auto;padding:44px 48px;background:#fff;min-height:100vh}header{border-bottom:2px solid var(--accent);padding-bottom:24px}.kicker{color:var(--accent);font-size:12px;font-weight:700}h1{font-size:30px;line-height:1.4;margin:10px 0;overflow-wrap:anywhere}h2{font-size:19px;margin:0 0 18px;border-bottom:1px solid #dfe5e2;padding-bottom:10px}h2 small{font-size:12px;font-weight:400;margin-left:8px}h3{font-size:14px}section{margin:30px 0}.muted,small{color:#68756f}.prose{white-space:pre-wrap}.chips{display:flex;gap:10px;flex-wrap:wrap}.chip{font-size:12px;background:#f1f5f3;border:1px solid #e2e8e4;padding:3px 10px;border-radius:4px}.tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:20px 0}.tile{padding:16px;border:1px solid #e0e6e2;border-radius:6px}.tile span,.metric span{display:block;color:#68756f;font-size:12px}.tile b{display:block;font-size:28px;color:var(--accent)}.metric-strip{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border-top:1px solid #dfe5e2;border-bottom:1px solid #dfe5e2}.metric{padding:18px}.metric b{display:block;font-size:24px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{text-align:left;vertical-align:top;padding:var(--cell-padding);border-bottom:1px solid #e2e7e4;white-space:pre-wrap;overflow-wrap:anywhere}th{background:#f3f6f4;font-size:12px}td{font-size:12px}.outcome{font-weight:600}tr.edited{border-left:3px solid var(--accent)}.evidence{padding:14px 0;border-bottom:1px solid #e2e7e4}.evidence p{margin:4px 0}p,small{overflow-wrap:anywhere}a{color:var(--accent)}canvas{width:100%;height:280px;display:block}.warning{color:#9d5f19;background:#fff8ec;padding:10px}footer{margin-top:36px;border-top:1px solid #dfe5e2;padding-top:16px;color:#68756f;font-size:12px}@media(max-width:700px){main{padding:24px 16px}h1{font-size:23px}.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.metric-strip{grid-template-columns:1fr}table{min-width:700px}}@media print{body{background:white}main{padding:0}header, .tiles{break-inside:avoid}thead{display:table-header-group}}'''
    script = '''document.querySelectorAll('canvas').forEach((c,i)=>{const x=c.getContext('2d'),s=charts[i].samples,colors=['#087f63','#ce8131','#4474a8'];const nums=s.flat().filter(v=>typeof v==='number'&&Number.isFinite(v)&&v>=0);const max=Math.max(1,...nums);x.font='12px sans-serif';for(let k=0;k<5;k++){let y=25+k*54;x.strokeStyle='#e1e6eb';x.beginPath();x.moveTo(65,y);x.lineTo(980,y);x.stroke();x.fillStyle='#63717e';x.fillText(Math.round(max*(1-k/4)),5,y+4)}colors.forEach((color,j)=>{x.strokeStyle=color;x.lineWidth=2;x.beginPath();let gap=true;s.forEach((row,n)=>{let v=row[j];if(typeof v!=='number'||!Number.isFinite(v)){gap=true;return}let px=65+n/Math.max(1,s.length-1)*915,py=241-v/max*216;if(gap)x.moveTo(px,py);else x.lineTo(px,py);x.fillStyle=color;x.fillRect(px-1,py-1,3,3);gap=false});x.stroke();x.fillStyle=color;x.fillText((charts[i].labels||['TTFT','TPOT','端到端延迟'])[j],65+j*260,270)})});'''
    return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(c["title"])}</title><style>:root{{--accent:{c["accent"]};--cell-padding:{"7px" if c["template"] == "compact" else "12px"}}}{css}</style></head><body><main><header><div class="kicker">API TEST REPORT</div><h1>{e(c["title"])}</h1><p class="muted">{e(c["subtitle"])}</p><div class="chips"><span class="chip">{"验收报告" if c["template"] == "acceptance" else "模型测试报告"}</span><span class="chip">{len(source["runs"])} 个测试项目</span></div></header>{"".join(parts)}<footer>{e(c["title"])}</footer></main><script>const charts={data};{script}</script></body></html>'

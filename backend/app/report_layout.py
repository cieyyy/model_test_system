"""Standalone HTML report layout for the local model-test report format."""
import html
import json
import os
from urllib.parse import quote

from .report_results import overview


def render(content, source):
    c = content.model_dump()
    escape = lambda value: html.escape(str(value), quote=True)
    original = {row["id"]: row for row in source["rows"]}
    columns = {
        "project": "测试项目",
        "case": "用例 / 指标",
        "observed": "实测 / 原始判定",
        "conclusion": "报告结论",
        "note": "备注",
    }
    visible = [row for row in c["rows"] if row["visible"]]
    stats = source.get("overview") or overview(source.get("details", []))

    def value(key, suffix=""):
        current = stats.get(key)
        if current is None or current == "":
            return "—"
        if isinstance(current, float):
            current = f"{current:.2f}".rstrip("0").rstrip(".")
        return f"{current}{suffix}"

    failed = stats.get("failed", 0)
    review = stats.get("review", 0)
    if failed:
        verdict_title = "本次测试存在未通过项目"
        verdict_icon = "!"
        verdict_class = "bad"
        verdict_note = f"源脚本记录 {failed} 个未通过结果；其余结果请结合原始证据和人工结论复核。"
    elif review:
        verdict_title = "本次测试部分完成"
        verdict_icon = "!"
        verdict_class = "warn"
        verdict_note = f"源脚本记录 {review} 个待复核结果；执行完成不等于模型验收通过。"
    else:
        verdict_title = "本次测试源脚本结果通过"
        verdict_icon = "✓"
        verdict_class = "good"
        verdict_note = "所有已归档结果均未报告失败；模型验收仍以测试项目的业务断言为准。"

    tiles = [
        ("测试项目", len(source["runs"]), "按本批次测试记录"),
        ("通过", stats.get("passed", 0), "源脚本业务断言"),
        ("未通过", stats.get("failed", 0), "需要定位或复测"),
        ("待复核 / 数据", f'{stats.get("review", 0)} / {stats.get("data", 0)}', "人工判断或仅展示数据"),
    ]
    tiles_html = "".join(
        f'<div class="tile"><div class="label">{escape(label)}</div>'
        f'<div class="value">{escape(number)}</div><div class="sub">{escape(note)}</div></div>'
        for label, number, note in tiles
    )

    rows = []
    for row in visible:
        changed = row["id"] not in original or any(
            row[key] != original[row["id"]][key] for key in columns
        )
        cells = "".join(
            f'<td class="{"outcome" if key == "conclusion" else ""}">{escape(row[key])}</td>'
            for key in c["columns"]
        )
        rows.append(f'<tr class="{"edited" if changed else ""}">{cells}</tr>')
    table = (
        '<div class="table-scroll"><table class="detail-table"><thead><tr>'
        + "".join(f"<th>{columns[key]}</th>" for key in c["columns"])
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )

    demo = any(run.get("demo") for run in source["runs"])
    parts = []
    if "summary" in c["sections"]:
        summary_note = '<p class="warning">本报告包含模拟数据，不能作为真实模型结论。</p>' if demo else ""
        parts.append(
            f'<section><h2>测试总结 <span class="en">Summary</span></h2>'
            f'<p class="section-desc prose">{escape(c["summary"])}</p>{summary_note}'
            f'<div class="tiles">{tiles_html}</div></section>'
        )

        metrics = [
            ("平均首字延迟", "AVG TTFT (MS)", value("ttft_ms")),
            ("P99 首字延迟", "P99 TTFT (MS)", value("p99_ttft_ms")),
            ("平均每令牌时间", "AVG TPOT (MS)", value("tpot_ms")),
            ("P99 每令牌时间", "P99 TPOT (MS)", value("p99_tpot_ms")),
            ("生成速度", "GEN. TOK/S", value("gen_tok_s")),
            ("成功率", "SUCCESS RATE", value("success_rate", "%")),
        ]
        parts.append(
            '<section class="metric-strip">'
            + "".join(
                f'<div class="metric"><div class="metric-label">{escape(label)}'
                f'<span class="metric-en">{escape(english)}</span></div>'
                f'<div class="metric-value">{escape(metric)}</div></div>'
                for label, english, metric in metrics
            )
            + '</section><p class="footnote">指标只统计归档明细中的已测值；缺失指标保持为空。TTFT / TPOT 使用源脚本口径，生成速度仅在输入、输出 Token 与耗时同时存在时计算。</p>'
        )

        status_labels = {"completed": "执行完成", "error": "执行异常", "cancelled": "已取消", "queued": "排队", "interrupted": "重启中断"}
        run_rows = "".join(
            f'<tr><td>#{run["run_id"]}</td><td><b>{escape(run["name"])}</b></td>'
            f'<td>{escape(run["model"])}</td><td>{escape(status_labels.get(run["status"], run["status"]))}</td>'
            f'<td>{"模拟数据" if run.get("demo") else "源脚本实测"}</td></tr>'
            for run in source["runs"]
        )
        parts.append(
            '<section><h2>执行项目 <span class="en">Projects</span></h2>'
            '<p class="section-desc">本报告覆盖本批次选中的全部测试项目，每个项目保留独立运行状态和证据入口。</p>'
            '<div class="card table-scroll"><table><thead><tr><th>运行</th><th>测试项目</th><th>模型</th><th>执行状态</th><th>数据来源</th></tr></thead><tbody>'
            + run_rows
            + '</tbody></table></div></section>'
        )

    if "results" in c["sections"]:
        parts.append(
            f'<section><h2>测试项目明细 <span class="en">Detailed Results</span></h2>'
            f'<div class="card">{table}</div>'
            '<p class="footnote">主题色左线标记人工编辑或新增行。HTTP、耗时、Tokens、响应和源判定按实际记录展示。</p></section>'
        )

    charts = source.get("charts", [])
    if "curves" in c["sections"] and charts:
        chart_html = []
        for chart in charts:
            labels = chart.get("labels") or ["TTFT", "TPOT", "端到端延迟"]
            legend = "".join(
                f'<span class="legend-item"><i style="background:{color}"></i>{escape(label)}</span>'
                for color, label in zip(("#2a78d6", "#eb6834", "#0ca30c"), labels)
            )
            chart_html.append(
                f'<div class="chart-block"><h3>{escape(chart["name"])}'
                f'{" · 模拟数据" if chart.get("demo") else ""}</h3>'
                f'<div class="legend">{legend}</div><canvas width="1000" height="280" aria-label="{escape(chart["name"])}指标曲线"></canvas></div>'
            )
        parts.append(
            '<section><h2>性能指标曲线 <span class="en">Performance</span></h2>'
            '<p class="section-desc">单位 ms；每条曲线最多展示前 500 个归档样本，空值不绘制。</p>'
            + "".join(chart_html)
            + '</section>'
        )

    if "evidence" in c["sections"]:
        base = os.environ.get("MT_PUBLIC_URL", "").rstrip("/")
        evidence = []
        for run in source["runs"]:
            files = " · ".join(
                f'<a href="{escape(base)}/api/suites/runs/{run["run_id"]}/files/{quote(path, safe="/")}">{escape(path)}</a>'
                for path in run.get("files", [])
            ) or "暂无文件清单"
            evidence.append(
                f'<div class="evidence"><a href="{escape(base)}/#reports">#{run["run_id"]} {escape(run["name"])}</a>'
                f'<p>{escape(run["provider"])} · {escape(run["model"])}</p><p>{files}</p>'
                f'<small>源脚本 SHA256：{escape(run["source_sha256"])}<br>证据清单 SHA256：{escape(run["manifest_sha256"])}</small></div>'
            )
        parts.append(
            '<section><h2>原始证据 <span class="en">Evidence</span></h2>'
            + "".join(evidence)
            + '</section>'
        )

    if source.get("truncated"):
        parts.append('<p class="warning">报告明细已达显示上限（每张 CSV 前 100 行、合计最多 1900 行）；统计仅针对已收集用例，完整结果见运行证据。</p>')

    data = json.dumps(charts, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    css = """
      :root { color-scheme: light; --surface-0:#f6f5f2; --surface-1:#fcfcfb; --surface-2:#f0efec; --border:#e2e1dc; --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#8a8880; --series-1:#2a78d6; --series-2:#eb6834; --good:#0ca30c; --good-text:#0b7a0b; --grid:#e8e7e2; }
      @media (prefers-color-scheme: dark) { :root { color-scheme:dark; --surface-0:#131312; --surface-1:#1a1a19; --surface-2:#242422; --border:#383835; --text-primary:#fff; --text-secondary:#c3c2b7; --text-muted:#8f8e85; --series-1:#3987e5; --series-2:#d95926; --good:#0ca30c; --good-text:#3fbf3f; --grid:#333330; } }
      * { margin:0; padding:0; box-sizing:border-box; letter-spacing:0; }
      body { background:var(--surface-0); color:var(--text-primary); font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif; padding:40px 20px 60px; }
      .wrap { max-width:1120px; margin:0 auto; }
      header { margin-bottom:28px; border-bottom:1px solid var(--border); padding-bottom:24px; }
      .kicker { font-size:12px; letter-spacing:2px; color:var(--text-muted); text-transform:uppercase; margin-bottom:6px; }
      h1 { font-size:28px; line-height:1.35; font-weight:700; overflow-wrap:anywhere; }
      .subtitle { color:var(--text-secondary); margin-top:7px; font-size:14px; overflow-wrap:anywhere; }
      .meta-chips { display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; }
      .chip { background:var(--surface-1); border:1px solid var(--border); border-radius:999px; padding:3px 12px; font-size:12.5px; color:var(--text-secondary); }
      .verdict { display:flex; align-items:center; gap:14px; background:var(--surface-1); border:1px solid var(--border); border-left:4px solid var(--good); border-radius:10px; padding:16px 20px; margin-bottom:22px; }
      .verdict.bad { border-left-color:#c53d32; } .verdict.warn { border-left-color:#c88a15; }
      .verdict .icon { flex:none; width:34px; height:34px; border-radius:50%; background:var(--good); color:#fff; font-size:19px; font-weight:700; display:flex; align-items:center; justify-content:center; }
      .verdict.bad .icon { background:#c53d32; } .verdict.warn .icon { background:#c88a15; }
      .verdict strong { font-size:16px; } .verdict p { color:var(--text-secondary); font-size:13.5px; margin-top:2px; }
      section { margin-bottom:34px; } h2 { font-size:18px; font-weight:700; margin-bottom:4px; display:flex; align-items:baseline; gap:10px; } h2 .en { font-size:12px; color:var(--text-muted); font-weight:500; }
      h3 { font-size:14px; margin-bottom:8px; } .section-desc { color:var(--text-secondary); font-size:13px; margin-bottom:14px; } .prose { white-space:pre-wrap; }
      .tiles { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin-bottom:30px; } .tile { background:var(--surface-1); border:1px solid var(--border); border-radius:10px; padding:16px 18px; }
      .tile .label { font-size:12.5px; color:var(--text-muted); } .tile .value { font-size:24px; font-weight:700; margin-top:4px; } .tile .sub { font-size:12px; color:var(--text-secondary); margin-top:2px; }
      .metric-strip { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); margin:18px 0 12px; border:1px solid var(--border); border-radius:8px; overflow:hidden; background:var(--surface-1); }
      .metric { padding:16px 18px; border-right:1px solid var(--border); min-width:0; } .metric:last-child { border-right:0; } .metric-label { font-size:13px; font-weight:700; line-height:1.35; } .metric-en { display:block; font-size:11px; font-weight:600; color:var(--text-secondary); margin-top:4px; overflow-wrap:anywhere; } .metric-value { font-size:22px; font-weight:700; margin-top:10px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
      .card { background:var(--surface-1); border:1px solid var(--border); border-radius:12px; padding:20px 22px; } .table-scroll { overflow:auto; } table { width:100%; border-collapse:collapse; font-size:13px; } th { text-align:left; font-weight:600; color:var(--text-secondary); font-size:12.5px; padding:9px 10px; border-bottom:1.5px solid var(--border); white-space:nowrap; } td { padding:10px; border-bottom:1px solid var(--border); vertical-align:top; overflow-wrap:anywhere; white-space:pre-wrap; } tr:last-child td { border-bottom:0; }
      .detail-table { min-width:760px; table-layout:fixed; } .detail-table td { font-size:12.5px; } .outcome { font-weight:600; } tr.edited { border-left:3px solid var(--accent); } .footnote,.muted,small { color:var(--text-muted); font-size:12px; margin-top:10px; overflow-wrap:anywhere; } a { color:var(--accent); } .warning { color:#9b6715; background:#fff8dc; border:1px solid #e6d18e; border-radius:6px; padding:10px 12px; }
      .chart-block { background:var(--surface-1); border:1px solid var(--border); border-radius:12px; padding:18px 20px 14px; margin-top:14px; } .legend { display:flex; flex-wrap:wrap; gap:18px; margin-bottom:8px; } .legend-item { display:flex; align-items:center; gap:6px; color:var(--text-secondary); font-size:12px; } .legend-item i { width:12px; height:12px; display:inline-block; border-radius:3px; } canvas { display:block; width:100%; height:280px; }
      .evidence { padding:14px 0; border-bottom:1px solid var(--border); } .evidence p { margin:4px 0; } footer { margin-top:44px; padding-top:16px; border-top:1px solid var(--border); font-size:12px; color:var(--text-muted); display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px; }
      @media(max-width:900px) { .tiles { grid-template-columns:repeat(2,minmax(0,1fr)); } .metric-strip { grid-template-columns:repeat(3,minmax(0,1fr)); } .metric { border-bottom:1px solid var(--border); } }
      @media(max-width:560px) { body { padding:24px 12px 40px; } h1 { font-size:22px; } .card { padding:14px; } .tiles { grid-template-columns:repeat(2,minmax(0,1fr)); gap:9px; } .tile { padding:13px; } .metric-strip { grid-template-columns:repeat(2,minmax(0,1fr)); } .metric { padding:13px; } .metric-value { font-size:19px; } .verdict { padding:14px; } }
      @media print { body { background:#fff; padding:10px; } .card,.tile,.verdict,.chart-block { break-inside:avoid; } }
    """
    script = """
      document.querySelectorAll('canvas').forEach((canvas,index)=>{const ctx=canvas.getContext('2d');const series=charts[index]?.samples||[];const colors=['#2a78d6','#eb6834','#0ca30c'];const numbers=series.flat().filter(v=>typeof v==='number'&&Number.isFinite(v)&&v>=0);const max=Math.max(1,...numbers);ctx.font='12px sans-serif';for(let row=0;row<5;row++){const y=25+row*54;ctx.strokeStyle=getComputedStyle(document.documentElement).getPropertyValue('--grid');ctx.beginPath();ctx.moveTo(65,y);ctx.lineTo(980,y);ctx.stroke();ctx.fillStyle=getComputedStyle(document.documentElement).getPropertyValue('--text-muted');ctx.fillText(String(Math.round(max*(1-row/4))),5,y+4)}colors.forEach((color,column)=>{ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();let gap=true;series.forEach((sample,n)=>{const v=sample[column];if(typeof v!=='number'||!Number.isFinite(v)){gap=true;return}const x=65+n/Math.max(1,series.length-1)*915,y=241-v/max*216;if(gap)ctx.moveTo(x,y);else ctx.lineTo(x,y);ctx.fillStyle=color;ctx.fillRect(x-1,y-1,3,3);gap=false});ctx.stroke()})});
    """
    template_label = "验收报告" if c["template"] == "acceptance" else "模型测试报告"
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(c["title"])}</title><style>:root{{--accent:{c["accent"]};}}{css}</style></head>'
        '<body><div class="wrap"><header><div class="kicker">API TEST REPORT · MODEL TEST</div>'
        f'<h1>{escape(c["title"])}</h1><div class="subtitle">{escape(c["subtitle"])}</div>'
        f'<div class="meta-chips"><span class="chip">{template_label}</span><span class="chip">{len(source["runs"])} 个测试项目</span></div></header>'
        f'<div class="verdict {verdict_class}"><div class="icon">{verdict_icon}</div><div><strong>{escape(verdict_title)}</strong><p>{escape(verdict_note)}</p></div></div>'
        + "".join(parts)
        + f'<footer><span>{escape(c["title"])}</span><span>原始证据只读 · 版本可在平台继续编辑</span></footer></div>'
        f'<script>const charts={data};{script}</script></body></html>'
    )

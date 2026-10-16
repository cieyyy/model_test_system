#!/usr/bin/env python3
"""从已保存的实测结果生成离线 HTML；不发送任何模型请求。"""
import argparse
from collections import Counter
from html import escape
import json
from pathlib import Path


def esc(value):
    return escape(str(value), quote=True)


def fmt(value):
    if value is None:
        return "未测得"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def render_report(out, config, records, summary, measured, manifest=None, related=()):
    out = Path(out)
    manifest = manifest or {}
    states = dict(pass_="通过", fail="未通过", error="错误", skipped="跳过", manual_review="待核验")

    def state(value):
        return states.get("pass_" if value == "pass" else value, value)

    def metric(case, name):
        return next((m for m in measured if m["case_id"] == case and m["metric"] == name), {})

    def value(case, name):
        return metric(case, name).get("value")

    traffic = "synthetic_traffic" if "synthetic_traffic" in summary else "real_traffic"
    detail = summary.get(traffic, {}).get("detail", {})
    synthetic = traffic == "synthetic_traffic"
    complete = all(item["result"] == "pass" for item in summary.values())
    scope = "公开素材合成负载；不能替代真实业务流量验收" if synthetic else "测试范围以各项目及素材来源为准"
    body = summary.get("body_100mb", {}).get("detail", {})
    context = summary.get("context_1m", {}).get("detail", {})
    cache = metric(traffic, "缓存命中率")
    tokens = [r["input_tokens"] for r in records if r["case_id"] == "context_1m" and r.get("input_tokens") is not None]

    def card(label, number, note, tone=""):
        return f'<article class="card {tone}"><span>{esc(label)}</span><strong>{esc(number)}</strong><small>{esc(note)}</small></article>'

    cards = [
        card("100MB HTTP 正文", f'{body.get("successful", 0)} / {body.get("requests", 0)} 成功',
             "短 JSON + 合法空白填充；不代表 100MB 有效媒体" if config.get("body_mode") == "transport" else "实际媒体正文测试"),
        card("百万上下文稳定性", f'{context.get("successful", 0)} / {context.get("requests", 0)} 成功',
             f'服务端输入：{fmt(min(tokens))} Token' if tokens else "未取得服务端 Token 数", "warn"),
        card("Input TPM", fmt(value(traffic, "Input TPM")), f'按 {fmt(detail.get("measured_seconds"))} 秒窗口统计成功请求 usage', "warn"),
        card("观测 TTFT · P50 / P90", " / ".join(fmt(value(traffic, "观测 TTFT " + q) / 1000) if value(traffic, "观测 TTFT " + q) is not None else "未测得" for q in ("P50", "P90")) + " 秒", "仅有首字样本；目标 ≤3 秒 / ≤5 秒", "warn"),
        card("样本缓存命中率", fmt(cache.get("value")) + ("%" if cache.get("value") is not None else ""), f'{cache.get("samples", 0)} 条有缓存字段；' + ("合成负载，不代表真实业务" if synthetic else "具体覆盖见指标表"), "warn"),
        card("混合负载成功率", fmt(value(traffic, "成功率")) + "%", f'成功 {detail.get("successful", 0)} / 实发 {detail.get("attempted_requests", 0)}；跳过 {detail.get("skipped_requests", 0)}', "warn"),
    ]
    quota = [r for r in records if r.get("error_category") == "insufficient_quota" or "quota" in (r.get("provider_message") or "").lower()]
    notice = (f'额度不足导致提前中止：{len(quota)} 条 HTTP 403。混合负载计划 {detail.get("requests", 0)} 条，实际发送 {detail.get("attempted_requests", 0)} 条，跳过 {detail.get("skipped_requests", 0)} 条；实际阶段耗时 {fmt(detail.get("actual_elapsed_seconds"))} 秒。上述 TPM 使用预设 {fmt(detail.get("measured_seconds"))} 秒分母，不能视为完成了该时长的持续压测。' if quota else "缺失值如实显示，未提供的数据不填 0。")

    rows = []
    for case, item in summary.items():
        label = next((m["title_zh"] for m in measured if m["case_id"] == case), case)
        reasons = {
            "body_100mb": "验证 HTTP 正文接收能力，独立于有效媒体大小。",
            "context_1m": f'成功 {context.get("successful", 0)}/{context.get("requests", 0)}；长度以服务端 usage 为准，且须找回全部检索答案。',
            traffic: "合成负载；额度不足提前停止。" if quota else scope,
            "deployment": "FP8 量化、MoE 权重方案及部署证据未提供；黑盒 API 无法证明。",
        }
        rows.append(f'<tr><td>{esc(label)}</td><td><span class="badge {esc(item["result"])}">{esc(state(item["result"]))}</span></td><td>{esc(reasons.get(case, "详见实测数据及响应记录"))}</td></tr>')

    metric_rows = []
    for m in measured:
        metric_rows.append('<tr>' + ''.join(f'<td>{esc(x)}</td>' for x in (
            m["title_zh"], m["metric"], fmt(m["value"]) if m["value"] is not None else m["status"],
            m["unit"], fmt(m["samples"]) if m["samples"] is not None else "—", m["status"], m["note"])) + '</tr>')

    request_rows = []
    for r in sorted(records, key=lambda r: r["index"]):
        message = r.get("provider_message") or r.get("error_message") or ("断言失败：" + ", ".join(r["failed_assertions"]) if r.get("failed_assertions") else r.get("error_type")) or "—"
        cells = (r["index"], r.get("title_zh", r["case_id"]), state(r["result"]), r.get("http_status") or "—", ", ".join(r.get("modalities", [])),
                 fmt(r.get("input_tokens")), fmt(r.get("cached_tokens")), fmt(r.get("ttft_ms")), message)
        request_rows.append(f'<tr data-status="{esc(r["result"])}">' + ''.join(f'<td>{esc(x)}</td>' for x in cells) + '</tr>')

    errors = Counter((r.get("error_category") or r.get("error_type") or "响应断言失败") for r in records if r["result"] in ("error", "fail"))
    error_rows = ''.join(f'<tr><td>{esc(k)}</td><td>{v}</td></tr>' for k, v in errors.most_common())
    supplements = []
    for folder in related:
        folder = Path(folder)
        other = json.loads((folder / "summary.json").read_text())
        for case, item in other["items"].items():
            if case == "diagnostic":
                probes = item["detail"].get("probes", [])
                text = "小请求诊断：" + "、".join(f'{p.get("probe")} {state(p["result"])}' for p in probes) + "。这些延迟不属于混合压测分位数。"
            else:
                messages = [p.get("message", "") for p in item["detail"].get("provider_errors", [])]
                text = "原始100MB媒体请求：" + state(item["result"]) + "；服务端消息：" + "；".join(messages) + "。未提供具体上游限制，不能推断是图片数量或正文上限。"
            supplements.append(f'<p>{esc(text)}<br><small>来源：{esc(folder.name)}</small></p>')

    audit = manifest.get("report_review", {})
    audit_note = audit.get("note", "无离线重判")
    review_count = len(audit.get("changes", []))
    title = str(config.get("model", "模型")) + " · 实测报告"
    html = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>
:root{color-scheme:light;--ink:#15243a;--muted:#617189;--line:#dde5ef;--paper:#f3f6fa;--accent:#2459ad}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}main{max-width:1360px;margin:auto;padding:40px 28px 60px}header{border-top:5px solid var(--accent);padding:24px 30px;background:white;border-radius:6px 6px 18px 18px}h1{margin:8px 0;font-size:32px;letter-spacing:-1px}h2{font-size:21px;margin:0 0 16px}p{margin:10px 0}small,.meta{color:var(--muted)}.eyebrow{font-size:12px;letter-spacing:2px;color:var(--accent);font-weight:750}.status{color:#a33425;background:#fff0eb;padding:6px 12px;border-radius:8px;font-weight:700;display:inline-block}.status.good{color:#126645;background:#e5f5eb}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:22px 0}.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:22px;display:flex;flex-direction:column;gap:10px}.card span{font-weight:650}.card strong{font-size:29px;line-height:1.25;font-variant-numeric:tabular-nums}.card small{line-height:1.5}.warn strong{color:#963e28}.notice{border-left:4px solid #dc9846;background:#fff7e9;padding:17px 22px;border-radius:4px 12px 12px 4px;margin:20px 0}section{background:white;border:1px solid var(--line);border-radius:14px;padding:24px;margin-top:20px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}th{text-align:left;background:#f0f4fa;color:#435775;white-space:nowrap}th,td{padding:12px 13px;border-bottom:1px solid var(--line);vertical-align:top}td{min-width:70px}td:last-child{min-width:240px;overflow-wrap:anywhere}tbody tr:hover{background:#f8faff}.badge{padding:3px 9px;border-radius:5px;background:#fff0e9;color:#963e28;white-space:nowrap}.badge.pass{color:#126645;background:#e5f5eb}.badge.manual_review{color:#686074;background:#f0edf6}details{margin-top:20px}summary{cursor:pointer;font-weight:650}button,select,input{font:inherit;border:1px solid #c9d6e6;border-radius:7px;padding:8px 12px;background:white;color:var(--ink)}button{cursor:pointer}header button{float:right}.toolbar{display:flex;gap:12px;margin:16px 0;align-items:center}.toolbar input{flex:1;min-width:100px}.footer{font-size:12px;color:var(--muted);padding:22px 4px}a{color:var(--accent)}.request-scroll{max-height:620px}thead{position:sticky;top:0;z-index:1}.request-scroll td:last-child{min-width:360px}.subtle{font-size:13px;color:var(--muted)}@media(max-width:850px){main{padding:18px 12px}.cards{grid-template-columns:repeat(2,1fr)}header,section{padding:20px}.card strong{font-size:24px}h1{font-size:26px}}@media(max-width:520px){.cards{grid-template-columns:1fr}.toolbar{flex-wrap:wrap}}@media print{body{background:white}main{padding:0}.cards{grid-template-columns:repeat(3,1fr)}section,.card,header{break-inside:avoid;border-color:#bbb}button,.toolbar{display:none}.scroll{overflow:visible}.request-scroll{max-height:none}details:not([open]){display:none}table{font-size:10px}a{color:inherit}section{padding:14px}}
</style></head><body><main>
<header><button onclick="window.print()">打印 / 保存 PDF</button><div class="eyebrow">MODEL ACCEPTANCE · OBSERVED RESULTS</div><h1>__TITLE__</h1><div class="status __GOOD__">__OVERALL__</div><p class="meta">__TIME__ · __ENDPOINT__</p><p>__SCOPE__</p></header>
<div class="cards">__CARDS__</div><div class="notice">__NOTICE__</div>
<section><h2>验收结果与证据边界</h2><div class="scroll"><table><thead><tr><th>项目</th><th>结论</th><th>说明</th></tr></thead><tbody>__ITEMS__</tbody></table></div><p class="subtle">观测 TTFT 只展示获得首字的样本；失败和跳过数量另列。缓存缺失不当作 0，部分样本命中率不代表全量，更不代表真实业务。FP8 和 MoE 需部署资料佐证。</p>__SUPPLEMENTS__</section>
<section><h2>全部实测指标</h2><p class="subtle">数值来自已保存的响应和 usage；目标值不替代实测值。各行列明单位、样本数和统计口径。</p><div class="scroll"><table id="metrics"><thead><tr><th>项目</th><th>指标</th><th>实测值</th><th>单位</th><th>样本数</th><th>数据状态</th><th>口径与覆盖</th></tr></thead><tbody>__METRICS__</tbody></table></div></section>
<section><h2>错误概览</h2><div class="scroll"><table><thead><tr><th>错误分类</th><th>实际失败请求数</th></tr></thead><tbody>__ERRORS__</tbody></table></div><p class="subtle">跳过请求不计入上表；供应商追踪 ID 和原始消息见逐请求明细。</p></section>
<section><h2>逐请求明细</h2><p class="subtle">全量 __COUNT__ 条记录已嵌入，可离线筛选。TTFT 单位为毫秒；未测得表示没有对应数据。</p><div class="toolbar"><select id="status"><option value="">全部状态</option><option value="pass">通过</option><option value="fail">未通过</option><option value="error">错误</option><option value="skipped">跳过</option></select><input id="search" aria-label="搜索请求" placeholder="搜索项目、HTTP 状态、错误消息或追踪 ID"><span id="count"></span></div><div class="scroll request-scroll"><table id="requests"><thead><tr><th>序号</th><th>项目</th><th>状态</th><th>HTTP</th><th>媒体</th><th>输入 Token</th><th>缓存 Token</th><th>TTFT ms</th><th>响应问题 / 消息</th></tr></thead><tbody>__REQUESTS__</tbody></table></div></section>
<section><h2>数据来源与报告修正</h2><p>测试开始：__START__<br>测试结束：__END__<br>原始执行脚本版本：__VERSION__</p><p>__AUDIT__；离线修正记录 __REVIEW_COUNT__ 条。修正前的原始目录保留，网络响应与 usage 不变。</p><p class="subtle">HTML 自包含，无外部字体、脚本或网络依赖，未嵌入 API 密钥。显示值适度四舍五入，完整精度保留在 JSON / CSV 中。</p></section>
<div class="footer">报告目录：__FOLDER__ · 数据口径：至少百万服务端输入 Token；TPM 基于成功请求 usage；部署方案须独立核验。</div>
</main><script>
const rows=Array.from(document.querySelectorAll('#requests tbody tr'));const statusSelect=document.getElementById('status'),search=document.getElementById('search');function filterRows(){let n=0;const term=search.value.toLowerCase();rows.forEach(row=>{const ok=(!statusSelect.value||row.dataset.status===statusSelect.value)&&row.textContent.toLowerCase().includes(term);row.hidden=!ok;if(ok)n++});document.getElementById('count').textContent=n+' 条'}statusSelect.addEventListener('change',filterRows);search.addEventListener('input',filterRows);filterRows();
</script></body></html>'''
    replacements = dict(TITLE=esc(title), GOOD="good" if complete else "", OVERALL="全部项目通过" if complete else "整体验收未通过 / 部分项目证据不足",
                        TIME=esc((manifest.get("started_at") or "时间见原始记录")[:10] + " · Asia/Shanghai"), ENDPOINT=esc(config.get("base_url", "")), SCOPE=esc(scope),
                        CARDS=''.join(cards), NOTICE=esc(notice), ITEMS=''.join(rows), METRICS=''.join(metric_rows), ERRORS=error_rows, REQUESTS=''.join(request_rows),
                        COUNT=str(len(records)), START=esc(manifest.get("started_at", "未提供")), END=esc(manifest.get("ended_at", "未提供")), VERSION=esc(manifest.get("script_version", "未提供")),
                        AUDIT=esc(audit_note), REVIEW_COUNT=str(review_count), FOLDER=esc(out.name), SUPPLEMENTS=''.join(supplements))
    # 只替换模板中的占位符，避免业务正文中的占位符被再次解释。
    import re
    html = re.sub(r"__([A-Z_]+)__", lambda match: replacements[match.group(1)], html)
    target = out / "report.html"
    target.write_text(html, encoding="utf-8")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_dir", type=Path)
    parser.add_argument("--related-report", type=Path, action="append", default=[])
    args = parser.parse_args()
    manifest = json.loads((args.result_dir / "manifest.json").read_text())
    report = json.loads((args.result_dir / "summary.json").read_text())
    records = [json.loads(f.read_text()) for f in sorted((args.result_dir / "evidence").glob("*.json"))]
    print(render_report(args.result_dir, manifest["config"], records, report["items"], report["measurements"], manifest, args.related_report).resolve())


if __name__ == "__main__":
    main()

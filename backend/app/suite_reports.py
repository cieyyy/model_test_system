"""Versioned editorial reports, separated from immutable execution evidence."""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import os
import re
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .db import Run, SessionLocal, SuiteReport, SuiteReportVersion
from .suite_jobs import SUITE_RUNS, run_dict

router = APIRouter(prefix="/api/suite-reports", tags=["可编辑报告"])
LOCK = threading.RLock()
COLUMNS = {"project": "测试项目", "case": "用例 / 指标", "observed": "实测 / 原始判定", "conclusion": "报告结论", "note": "备注"}
SECTIONS = ["summary", "results", "curves", "evidence"]


class ReportRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(max_length=100)
    project: str = Field(default="", max_length=500)
    case: str = Field(default="", max_length=500)
    observed: str = Field(default="", max_length=12000)
    conclusion: str = Field(default="待复核", max_length=2000)
    note: str = Field(default="", max_length=12000)
    visible: bool = True


class ReportContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="模型测试报告", min_length=1, max_length=300)
    subtitle: str = Field(default="", max_length=1000)
    summary: str = Field(default="", max_length=20000)
    template: Literal["standard", "compact", "acceptance"] = "standard"
    accent: str = Field(default="#087f63", pattern=r"^#[0-9a-fA-F]{6}$")
    sections: list[Literal["summary", "results", "curves", "evidence"]] = Field(default_factory=lambda: SECTIONS.copy(), max_length=4)
    columns: list[Literal["project", "case", "observed", "conclusion", "note"]] = Field(default_factory=lambda: list(COLUMNS), min_length=1, max_length=5)
    rows: list[ReportRow] = Field(default_factory=list, max_length=2000)


class ReportCreate(BaseModel):
    job_id: str = Field(default="", max_length=100)
    run_ids: list[int] = Field(default_factory=list, max_length=128)


class ReportSave(BaseModel):
    base_version: int = Field(ge=1)
    content: ReportContent
    editor: str = Field(default="本机用户", min_length=1, max_length=100)
    change_summary: str = Field(default="编辑报告", min_length=1, max_length=500)


def short(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False) if isinstance(value, (dict, list)) else str(value))[:12000]


def flat_metrics(data, prefix="", depth=0):
    if depth > 5 or not isinstance(data, dict):
        return []
    rows = []
    for key, value in data.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            rows.extend(flat_metrics(value, name, depth + 1))
        elif isinstance(value, (int, float, bool, str)) and value is not None:
            if isinstance(value, float) and not math.isfinite(value):
                continue
            rows.append((name, short(value)))
    return rows


def source_snapshot(runs):
    rows, charts, sources = [], [], []
    truncated = False
    for run in runs:
        record = run_dict(run)
        root = Path(run.evidence_dir).resolve() if run.evidence_dir else None
        if root and not root.is_relative_to(SUITE_RUNS.resolve()):
            raise ValueError("证据目录不在归档范围")
        manifest = root / "manifest.json" if root else None
        sources.append(dict(run_id=run.id, name=run.project_name, provider=run.provider_name, model=run.model_name,
                            status=run.status, demo=record.get("demo", False), source_sha256=record.get("source_sha256", ""),
                            manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest and manifest.is_file() else ""))

        def add(case, observed, conclusion="待复核", note="", suffix=""):
            nonlocal truncated
            if len(rows) >= 1900:
                truncated = True
                return
            rows.append(dict(id=f"{run.id}:{len(rows)}:{suffix}"[:100], project=run.project_name, case=str(case)[:500],
                             observed=short(observed), conclusion=str(conclusion)[:2000], note=str(note)[:12000], visible=True))

        add("执行状态", run.status, "模拟数据" if record.get("demo") else "待复核", run.verdict)
        for key, value in flat_metrics(record.get("summary", {})):
            add(key, value)
        for table in record.get("source_summaries", []):
            for key, value in flat_metrics(table.get("data", {})):
                add(key, value, note=table.get("file", ""))
        if not root or not root.is_dir():
            continue
        # Preserve source assertions verbatim. HTTP success and process exit are not acceptance.
        for table in record.get("source_tables", []):
            path = (root / table["file"]).resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 50000000:
                continue
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for index, row in enumerate(csv.DictReader(handle)):
                    if index >= 100 or len(rows) >= 1900:
                        truncated = True
                        break
                    original = row.get("result") or row.get("status") or "未提供业务断言"
                    conclusion = {"pass": "源脚本通过", "passed": "源脚本通过", "fail": "源脚本未通过", "failed": "源脚本未通过",
                                  "skip": "源脚本跳过", "skipped": "源脚本跳过", "manual_review": "待人工复核"}.get(str(original).lower(), "待复核")
                    add(row.get("case") or row.get("case_id") or f"请求 {index+1}", row, conclusion,
                        f"源断言：{original}；{table['file']}；行 {index+2}")
        if record.get("suite_id") == "benchmark":
            relative = record.get("report_dir")
            candidates = [root / relative / "report.json"] if relative else list(root.glob("*/report.json"))
            for path in candidates:
                if path.resolve().is_relative_to(root) and path.is_file() and path.stat().st_size < 50000000:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    series = []
                    for req in data.get("requests", [])[:500]:
                        series.append([req.get("ttft_ms"), req.get("tpot_ms"), req.get("latency_ms")])
                    charts.append(dict(name=f"#{run.id} {run.project_name}", samples=series, demo=record.get("demo", False)))
                    break
    if truncated:
        rows.append(dict(id="truncated", project="归档说明", case="明细显示上限", observed="报告最多收集 1900 行，每张 CSV 前 100 行。完整明细见原始证据。", conclusion="", note="", visible=True))
    return dict(runs=sources, rows=rows, charts=charts, truncated=truncated)


def generate_report(run_ids=None, job_id=""):
    with LOCK, SessionLocal() as db:
        query = select(Run).order_by(Run.id)
        query = query.where(Run.job_id == job_id) if job_id else query.where(Run.id.in_(run_ids or []))
        runs = list(db.scalars(query))
        if not runs or (not job_id and {r.id for r in runs} != set(run_ids or [])):
            raise ValueError("运行记录不存在")
        if len(runs) > 128 or any(json.loads(r.summary_json or "{}").get("kind") != "suite" for r in runs):
            raise ValueError("请选择 1～128 个套件运行")
        if any(r.status in ("running", "queued") for r in runs):
            raise ValueError("请等待测试结束后生成报告")
        scope = "job:" + job_id if job_id else "runs:" + ",".join(str(r.id) for r in runs)
        existing = db.scalar(select(SuiteReport).where(SuiteReport.scope_key == scope))
        if existing:
            return existing.id
        source = source_snapshot(runs)
        content = ReportContent(title=f"{runs[0].model_name} 测试报告", subtitle=f"{runs[0].provider_name} · {runs[0].started_at:%Y-%m-%d %H:%M}",
                                summary="测试执行结果与验收结论分开记录；请结合源脚本断言复核。", rows=source["rows"])
        report = SuiteReport(scope_key=scope, source_json=json.dumps(source, ensure_ascii=False, allow_nan=False))
        db.add(report)
        db.flush()
        db.add(SuiteReportVersion(report_id=report.id, version=1, content_json=content.model_dump_json(),
                                  editor="系统", change_summary="根据原始测试结果自动生成"))
        db.commit()
        return report.id


def get_report(db, rid):
    report = db.get(SuiteReport, rid)
    if not report:
        raise HTTPException(404, "报告不存在")
    return report


def report_version(db, rid, version=None):
    query = select(SuiteReportVersion).where(SuiteReportVersion.report_id == rid)
    if version is not None:
        query = query.where(SuiteReportVersion.version == version)
    result = db.scalar(query.order_by(SuiteReportVersion.version.desc()))
    if not result:
        raise HTTPException(404, "报告版本不存在")
    return result


def validate_content(content, source):
    ids = [r.id for r in content.rows]
    if len(set(ids)) != len(ids) or len(set(content.columns)) != len(content.columns) or len(set(content.sections)) != len(content.sections):
        raise HTTPException(400, "行 ID、栏目和章节不可重复")
    # Original rows are identified by stable IDs. New rows get an explicit custom ID.
    known = {r["id"] for r in source["rows"]}
    if any(r.id not in known and not r.id.startswith("custom:") for r in content.rows):
        raise HTTPException(400, "新增行需使用 custom: 前缀")


@router.get("")
def list_reports():
    with SessionLocal() as db:
        result = []
        for r in db.scalars(select(SuiteReport).order_by(SuiteReport.id.desc()).limit(200)):
            v = report_version(db, r.id)
            result.append(dict(id=r.id, title=json.loads(v.content_json)["title"], version=v.version,
                               scope_key=r.scope_key, created_at=r.created_at.isoformat()))
        return result


@router.post("")
def create_report(request: ReportCreate):
    if bool(request.job_id) == bool(request.run_ids):
        raise HTTPException(400, "请选择一个任务或一组运行记录")
    try:
        return dict(id=generate_report(request.run_ids, request.job_id))
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{rid}")
def read_report(rid: int, version: int | None = None):
    with SessionLocal() as db:
        report = get_report(db, rid)
        current = report_version(db, rid, version)
        versions = list(db.scalars(select(SuiteReportVersion).where(SuiteReportVersion.report_id == rid).order_by(SuiteReportVersion.version.desc())))
        return dict(id=rid, version=current.version, latest_version=versions[0].version, scope_key=report.scope_key,
                    content=json.loads(current.content_json), source=json.loads(report.source_json),
                    versions=[dict(version=v.version, editor=v.editor, change_summary=v.change_summary, created_at=v.created_at.isoformat()) for v in versions])


@router.post("/{rid}/versions")
def save_version(rid: int, request: ReportSave):
    with LOCK, SessionLocal() as db:
        report = get_report(db, rid)
        latest = report_version(db, rid)
        if request.base_version != latest.version:
            raise HTTPException(409, "报告已有新版本，请重新打开后保存")
        validate_content(request.content, json.loads(report.source_json))
        version = latest.version + 1
        db.add(SuiteReportVersion(report_id=rid, version=version, content_json=request.content.model_dump_json(),
                                  editor=request.editor, change_summary=request.change_summary))
        db.commit()
        return dict(id=rid, version=version)


def report_html(content, source):
    c = content.model_dump()
    e = lambda v: html.escape(str(v), quote=True)
    original = {r["id"]: r for r in source["rows"]}
    rows = []
    for row in c["rows"]:
        if not row["visible"]:
            continue
        changed = row["id"] not in original or any(row[k] != original[row["id"]][k] for k in COLUMNS)
        cells = "".join(f"<td>{e(row[k])}</td>" for k in c["columns"])
        rows.append(f'<tr class="{"edited" if changed else ""}">{cells}</tr>')
    table = '<div class="table-wrap"><table><thead><tr>' + ''.join(f'<th>{e(COLUMNS[k])}</th>' for k in c["columns"]) + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
    parts = []
    if "summary" in c["sections"]:
        parts.append(f'<section><h2>测试结论</h2><p class="prose">{e(c["summary"])}</p></section>')
    if "results" in c["sections"]:
        parts.append(f'<section><h2>测试结果</h2>{table}<p class="muted">绿色左线标记人工编辑或新增行；原始记录在证据章节中可追溯。</p></section>')
    if "curves" in c["sections"]:
        if source["charts"]:
            parts.append('<section><h2>请求指标曲线</h2><p class="muted">TTFT / TPOT / 端到端延迟，单位 ms；各运行最多展示前 500 个样本。</p>' + ''.join(f'<h3>{e(ch["name"])}{" · 模拟数据" if ch["demo"] else ""}</h3><canvas width="1000" height="280" aria-label="请求延迟曲线"></canvas>' for ch in source["charts"]) + '</section>')
        else:
            parts.append('<section><h2>请求指标曲线</h2><p>所选项目没有流式压测曲线，请查看源脚本报告中的专项指标。</p></section>')
    if "evidence" in c["sections"]:
        platform_url = e(os.environ.get("MT_PUBLIC_URL", "").rstrip("/") + "/#reports")
        parts.append('<section><h2>原始证据</h2>' + ''.join(f'<p>#{r["run_id"]} {e(r["name"])} · {e(r["status"])}{" · 模拟数据" if r["demo"] else ""}<br><a href="{platform_url}">平台运行 #{r["run_id"]}</a><br><small>源脚本 SHA256：{e(r["source_sha256"])}<br>证据清单 SHA256：{e(r["manifest_sha256"])}</small></p>' for r in source["runs"]) + '</section>')
    data = json.dumps(source["charts"], ensure_ascii=False, allow_nan=False).replace('<', '\\u003c')
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(c["title"])}</title>
<style>*{{box-sizing:border-box;letter-spacing:0}}body{{margin:0;color:#202a33;background:white;font:14px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;padding:28px}}main{{max-width:1160px;margin:auto}}h1{{font-size:26px;margin:0;color:{c["accent"]};overflow-wrap:anywhere}}h2{{font-size:18px;border-bottom:1px solid #dce3e7;padding-bottom:8px}}h3{{font-size:14px}}section{{margin:28px 0}}p{{overflow-wrap:anywhere}}.muted,small{{color:#63717e}}small{{overflow-wrap:anywhere}}.prose{{white-space:pre-wrap}}.table-wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;table-layout:fixed}}th,td{{padding:{"7px" if c["template"] == "compact" else "12px"};border-bottom:1px solid #e1e6eb;vertical-align:top;text-align:left;overflow-wrap:anywhere;white-space:pre-wrap}}th{{background:#f6f8fa}}tr.edited{{border-left:3px solid {c["accent"]}}}canvas{{display:block;width:100%;height:280px}}a{{color:{c["accent"]}}}@media(max-width:600px){{body{{padding:16px}}h1{{font-size:22px}}table{{min-width:660px}}}}@media print{{body{{padding:0}}section{{break-inside:avoid}}}}</style>
<main><header><small>{"验收报告" if c["template"] == "acceptance" else "模型测试"}</small><h1>{e(c["title"])}</h1><p class="muted">{e(c["subtitle"])}</p></header>{''.join(parts)}</main>
<script>const charts={data};document.querySelectorAll('canvas').forEach((c,i)=>{{const x=c.getContext('2d'),s=charts[i].samples,colors=['#087f63','#e38a32','#397bcd'];const nums=s.flat().filter(v=>typeof v==='number'&&Number.isFinite(v)&&v>=0);let max=Math.max(1,...nums);x.font='12px sans-serif';for(let k=0;k<5;k++){{let y=25+k*54;x.strokeStyle='#e1e6eb';x.beginPath();x.moveTo(65,y);x.lineTo(980,y);x.stroke();x.fillStyle='#63717e';x.fillText(Math.round(max*(1-k/4)),5,y+4)}}colors.forEach((color,j)=>{{x.strokeStyle=color;x.lineWidth=2;x.beginPath();let gap=true;s.forEach((row,n)=>{{let v=row[j];if(typeof v!=='number'||!Number.isFinite(v)){{gap=true;return}}let px=65+n/Math.max(1,s.length-1)*915,py=241-v/max*216;if(gap)x.moveTo(px,py);else x.lineTo(px,py);x.fillStyle=color;x.fillRect(px-1,py-1,3,3);gap=false}});x.stroke();x.fillStyle=color;x.fillText(['TTFT','TPOT','端到端延迟'][j],65+j*180,270)}})}});</script></html>'''


@router.post("/{rid}/preview", response_class=HTMLResponse)
def preview_report(rid: int, content: ReportContent):
    with SessionLocal() as db:
        source = json.loads(get_report(db, rid).source_json)
        validate_content(content, source)
        return HTMLResponse(report_html(content, source), headers={"Content-Security-Policy": "sandbox allow-scripts"})


@router.get("/{rid}/export/{kind}")
def export_report(rid: int, kind: Literal["html", "json", "csv"], version: int | None = None):
    with SessionLocal() as db:
        report = get_report(db, rid)
        v = report_version(db, rid, version)
        content = ReportContent.model_validate_json(v.content_json)
        source = json.loads(report.source_json)
        if kind == "html":
            value, media = report_html(content, source), "text/html"
        elif kind == "json":
            value, media = json.dumps(dict(content=content.model_dump(), source=source, version=v.version), ensure_ascii=False, indent=2), "application/json"
        else:
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer)
            writer.writerow([COLUMNS[k] for k in content.columns])
            for row in content.rows:
                if row.visible:
                    # Prevent report text from becoming a spreadsheet formula on open.
                    writer.writerow([("'" + str(getattr(row, k)) if str(getattr(row, k)).lstrip().startswith(("=", "+", "-", "@")) else getattr(row, k)) for k in content.columns])
            value, media = "\ufeff" + buffer.getvalue(), "text/csv"
        target = source["runs"][0]
        slug = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", target["provider"] + "-" + target["model"])[:100]
        name = f"{report.created_at:%Y%m%d-%H%M%S}-{slug}-report-{rid}-v{v.version}.{kind}"
        return Response(value, media_type=media, headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name), "Content-Security-Policy": "sandbox allow-scripts"})

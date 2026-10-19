"""Versioned editorial reports, separated from immutable execution evidence."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
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
from .report_results import detail, observed, overview, number

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
    template: Literal["model_test", "standard", "compact", "acceptance"] = "model_test"
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
    if depth > 5:
        return []
    if isinstance(data, list):
        return [row for index, item in enumerate(data[:100]) for row in flat_metrics(item, f"{prefix}[{index}]", depth + 1)]
    if not isinstance(data, dict):
        return []
    rows = []
    for key, value in data.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, (dict, list)):
            rows.extend(flat_metrics(value, name, depth + 1))
        elif isinstance(value, (int, float, bool, str)) and value is not None:
            if isinstance(value, float) and not math.isfinite(value):
                continue
            rows.append((name, short(value)))
    return rows


def source_snapshot(runs):
    rows, charts, sources, details = [], [], [], []
    truncated = False
    for run in runs:
        record = run_dict(run)
        root = Path(run.evidence_dir).resolve() if run.evidence_dir else None
        if root and not root.is_relative_to(SUITE_RUNS.resolve()):
            raise ValueError("证据目录不在归档范围")
        manifest = root / "manifest.json" if root else None
        source_files = [table["file"] for table in record.get("source_tables", [])]
        if record.get("suite_id") == "benchmark" and record.get("report_dir"):
            report_root = record["report_dir"].strip("/")
            source_files.extend([f"{report_root}/report.html", f"{report_root}/report.json", f"{report_root}/requests.csv"])
        sources.append(dict(run_id=run.id, name=run.project_name, provider=run.provider_name, model=run.model_name,
                            status=run.status, demo=record.get("demo", False), suite_id=record.get("suite_id", ""),
                            config=record.get("config", {}), files=source_files,
                            source_sha256=record.get("source_sha256", ""),
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
        samples = []
        for table in record.get("source_tables", []):
            path = (root / table["file"]).resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 50000000:
                continue
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for index, row in enumerate(csv.DictReader(handle)):
                    if index >= 100 or len(rows) >= 1900:
                        truncated = True
                        break
                    item = detail(row, run.project_name, f"{table['file']}；行 {index+2}", run.id)
                    details.append(item)
                    add(item["case"], observed(item), item["outcome"],
                        f"源断言：{item['original']}；{item['evidence']}")
                    samples.append([number(item[k]) for k in ("ttft_ms", "tpot_ms", "elapsed_ms")])
        if samples and any(v is not None for sample in samples for v in sample):
            charts.append(dict(name=f"#{run.id} {run.project_name}", samples=samples[:500],
                               demo=record.get("demo", False), labels=["源脚本 TTFT", "TPOT", "请求总耗时"]))
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
    return dict(runs=sources, rows=rows, charts=charts, details=details, overview=overview(details), truncated=truncated)


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
                                template="model_test",
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
    from .report_layout import render
    return render(content, source)


@router.get("/{rid}/view", response_class=HTMLResponse)
def view_report(rid: int, version: int | None = None):
    with SessionLocal() as db:
        report = get_report(db, rid)
        current = report_version(db, rid, version)
        return HTMLResponse(report_html(ReportContent.model_validate_json(current.content_json), json.loads(report.source_json)),
                            headers={"Content-Security-Policy": "sandbox allow-scripts"})


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

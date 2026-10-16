"""Suite preview, execution, read-only legacy reports and scoped evidence downloads."""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .db import Credential, ModelEntity, Provider, Run, SessionLocal
from .security import decrypt_secret
from .suite_jobs import SUITE_RUNS, manager, run_dict, save_json, suite_records
from .suites import SCRIPTS_ROOT, benchmark_module, catalog, digest, make_plan

router = APIRouter(prefix="/api/suites", tags=["测试套件"])


class SuiteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite_id: str = "benchmark"
    suite_ids: list[str] = Field(default_factory=list, max_length=22)
    suite_parameters: dict[str, dict] = Field(default_factory=dict)
    continue_on_error: bool = True
    cooldown_seconds: int = Field(default=70, ge=0, le=600)
    provider_id: int | None = None
    model_id: int | None = None
    credential_id: int | None = None
    demo: bool = False
    parameters: dict = Field(default_factory=dict)
    concurrency_levels: list[StrictInt] = Field(default_factory=lambda: [1, 5, 10, 20, 50])
    input_token_levels: list[StrictInt] = Field(default_factory=lambda: [1000])
    output_token_levels: list[StrictInt] = Field(default_factory=lambda: [256])
    strategy: Literal["one_factor", "cartesian"] = "one_factor"
    repeats: int = Field(default=1, ge=1, le=10)
    job_timeout_seconds: int = Field(default=3600, ge=1, le=86400)
    confirmed: bool = False
    plan_hash: str = ""


def resolve_plan(request, execute=False):
    with SessionLocal() as db:
        provider = db.get(Provider, request.provider_id) if request.provider_id else None
        model = db.get(ModelEntity, request.model_id) if request.model_id else None
        key = ""
        if not request.demo:
            if not provider or not model or model.provider_id != provider.id:
                raise HTTPException(400, "请选择同一渠道下登记的模型")
            if not provider.enabled:
                raise HTTPException(400, "渠道已禁用")
            credential = db.get(Credential, request.credential_id) if request.credential_id else None
            if not credential or credential.provider_id != provider.id:
                raise HTTPException(400, "请选择该渠道的密钥")
            if execute:
                key = decrypt_secret(credential.encrypted_key)
                if not key.strip():
                    raise HTTPException(400, "密钥为空")
        try:
            return make_plan(request, None if request.demo else provider, None if request.demo else model), key
        except (ValueError, OSError, ImportError) as exc:
            raise HTTPException(400, str(exc)) from exc


@router.get("")
def list_suites():
    return catalog()


@router.post("/preview")
def preview(request: SuiteRequest):
    plan, _ = resolve_plan(request)
    return plan


@router.post("/execute")
def execute(request: SuiteRequest):
    if not request.confirmed:
        raise HTTPException(400, "请先预览并确认本次计划")
    plan, key = resolve_plan(request, execute=True)
    if not request.plan_hash or request.plan_hash != plan["plan_hash"]:
        raise HTTPException(409, "计划已变更，请重新预览后执行")
    try:
        job = manager.start(plan, key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return dict(job_id=job.id, run_ids=job.run_ids)


@router.get("/jobs/{job_id}")
def job_status(job_id: str):
    if job_id in manager.jobs:
        return manager.jobs[job_id].snapshot()
    with SessionLocal() as db:
        records = [run_dict(r) for r in suite_records(db) if r.job_id == job_id]
    if not records:
        raise HTTPException(404, "任务不存在")
    return dict(id=job_id, status="interrupted" if any(r["status"] == "interrupted" for r in records) else "archived",
                runs=records, lines=["服务已重启，日志可从各运行证据下载"], current=None)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = manager.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "运行中的任务不存在")
    job.cancel()
    return dict(cancel_requested=job.stop.is_set())


@router.get("/runs")
def list_runs():
    with SessionLocal() as db:
        return [run_dict(r) for r in suite_records(db)[:200]]


def get_record(db, run_id):
    run = db.get(Run, run_id)
    if not run or json.loads(run.summary_json or "{}").get("kind") != "suite":
        raise HTTPException(404, "套件运行不存在")
    return run


def evidence_root(run):
    if not run.evidence_dir:
        return None
    root = Path(run.evidence_dir).resolve()
    if not root.is_relative_to(SUITE_RUNS.resolve()):
        raise HTTPException(403, "证据目录不在归档范围")
    return root


@router.get("/runs/{run_id}")
def report_details(run_id: int):
    with SessionLocal() as db:
        run = get_record(db, run_id)
        result = run_dict(run)
        root = evidence_root(run)
        files = []
        if root and root.is_dir() and run.status not in ("running", "queued"):
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
                    files.append(dict(path=str(path.relative_to(root)), bytes=path.stat().st_size))
        result["files"] = files
        return result


def benchmark_snapshot(run_id):
    with SessionLocal() as db:
        run = get_record(db, run_id)
        data = json.loads(run.summary_json)
        if data.get("suite_id") != "benchmark":
            raise HTTPException(400, "此运行没有流式压测数据")
        root = evidence_root(run)
        job = manager.jobs.get(run.job_id)
        report = None
        if job:
            with job.lock:
                current = job.current
                if current and root and current.directory.parent.resolve() == root:
                    report = current.snapshot(True)
                    report["requests"] = report["requests"][-500:]
        if report is None and root:
            relative = data.get("report_dir")
            candidates = [root / relative / "report.json"] if relative else sorted(root.glob("*/report.json"))
            for path in candidates:
                if path.resolve().is_relative_to(root) and path.is_file() and path.stat().st_size <= 50000000:
                    report = json.loads(path.read_text(encoding="utf-8"))
                    break
        if report is None:
            cfg = benchmark_module().validate_config(data["config"])
            report = dict(id=str(run.id), config=cfg, status=run.status, summary=benchmark_module().summary([], 0),
                          requests=[], created_at=run.started_at.isoformat(), error=data.get("error", ""), local_prompt_tokens=None)
        if run.status in ("interrupted", "cancelled", "error"):
            report["status"] = run.status
            report["error"] = data.get("error", "") or run.verdict
        report.update(platform_run_id=run.id, job_id=run.job_id, stage=data.get("stage"), stage_count=data.get("stage_count"))
        return report


@router.get("/runs/{run_id}/benchmark")
def benchmark_data(run_id: int):
    return benchmark_snapshot(run_id)


@router.get("/runs/{run_id}/view")
def benchmark_view(run_id: int):
    html = benchmark_module().report_html(benchmark_snapshot(run_id), embedded=True)
    return HTMLResponse(html, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
        "Content-Security-Policy": "sandbox allow-scripts allow-downloads allow-modals; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'"})


@router.get("/runs/{run_id}/files/{filename:path}")
def download(run_id: int, filename: str):
    with SessionLocal() as db:
        run = get_record(db, run_id)
        if run.status in ("running", "queued"):
            raise HTTPException(409, "请等待证据归档完成")
        root = evidence_root(run)
        path = (root / filename).resolve() if root else None
        if not path or not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
            raise HTTPException(404, "证据文件不存在")
        is_html = path.suffix.lower() == ".html"
        headers = {"X-Content-Type-Options": "nosniff"}
        if is_html:
            headers["Content-Security-Policy"] = "sandbox allow-scripts allow-downloads allow-modals; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'"
        return FileResponse(path, filename=None if is_html else path.name,
                            media_type="text/html" if is_html else "application/octet-stream", headers=headers)


def legacy_report(report_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", report_id):
        raise HTTPException(400, "报告 ID 无效")
    root = (SCRIPTS_ROOT / "llm-benchmark/results").resolve()
    directory = (root / report_id).resolve()
    path = directory / "report.json"
    if not directory.is_relative_to(root) or not path.is_file() or path.stat().st_size > 50000000:
        raise HTTPException(404, "历史压测报告不存在")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or not isinstance(report.get("config"), dict) or not isinstance(report.get("summary"), dict):
            raise ValueError()
    except ValueError as exc:
        raise HTTPException(400, "历史报告格式无效") from exc
    return directory, path, report


@router.get("/legacy-reports")
def legacy_reports():
    root = SCRIPTS_ROOT / "llm-benchmark/results"
    results = []
    if root.is_dir():
        for entry in sorted(root.iterdir(), reverse=True):
            if not entry.is_dir() or entry.is_symlink():
                continue
            try:
                _, path, data = legacy_report(entry.name)
                results.append(dict(id=entry.name, model=data["config"].get("model"), status=data.get("status"),
                    demo=data["config"].get("demo", False), summary=data["summary"], sha256=digest(path)))
            except HTTPException:
                continue
            if len(results) >= 100:
                break
    return results


@router.post("/legacy-reports/{report_id}/import")
def import_legacy(report_id: str):
    directory, path, report = legacy_report(report_id)
    source_hash = digest(path)
    source_path = str(path)
    with manager.lock, SessionLocal() as db:
        for existing in suite_records(db):
            info = json.loads(existing.summary_json)
            if info.get("legacy_source") == source_path and info.get("legacy_sha256") == source_hash:
                return dict(run_id=existing.id, imported=False)
        metadata = dict(kind="suite", suite_id="benchmark", legacy_source=source_path, legacy_sha256=source_hash,
            demo=report["config"].get("demo", False), summary=report["summary"], config=report["config"],
            measurement_status="historical", acceptance_status="not_evaluated", report_dir="artifacts")
        run = Run(project_name="历史压测报告", model_name=report["config"].get("model", ""),
            status="imported", verdict="从旧目录只读导入，未重新测试", summary_json=json.dumps(metadata, ensure_ascii=False),
            finished_at=dt.datetime.now())
        db.add(run)
        db.flush()
        dest = SUITE_RUNS / "imported" / str(run.id)
        (dest / "artifacts").mkdir(parents=True, exist_ok=False)
        try:
            for name in ("report.json", "report.html", "requests.csv", "requests.jsonl", "run.json"):
                src = directory / name
                if src.is_file() and not src.is_symlink():
                    shutil.copy2(src, dest / "artifacts" / name)
            save_json(dest / "import.json", dict(source=source_path, sha256=source_hash))
            save_json(dest / "manifest.json", dict(files=[dict(path=str(p.relative_to(dest)), sha256=digest(p), bytes=p.stat().st_size)
                for p in sorted(dest.rglob("*")) if p.is_file()]))
            run.evidence_dir = str(dest)
            db.commit()
        except Exception:
            db.rollback()
            shutil.rmtree(dest)
            raise
        return dict(run_id=run.id, imported=True)

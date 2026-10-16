"""Suite execution with durable per-stage records and process-group cancellation."""
from __future__ import annotations

import asyncio
import codecs
import csv
from collections import Counter
import datetime as dt
import hashlib
import json
import os
import signal
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path

from sqlalchemy import select

from .db import DATA_DIR, Run, SessionLocal
from .suites import KEY_ENV, benchmark_module, digest, script_command

SUITE_RUNS = DATA_DIR / "suite_runs"


def save_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def process_family(proc):
    """Read PIDs only; command lines may contain curl authorization headers."""
    result = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,pgid="], capture_output=True, text=True, timeout=5)
    rows = [tuple(map(int, line.split())) for line in result.stdout.splitlines() if len(line.split()) == 3]
    family = {proc.pid}
    while True:
        expanded = family | {pid for pid, parent, group in rows if parent in family or group == proc.pid}
        if expanded == family:
            return family
        family = expanded


def signal_family(proc, members, sig):
    try:
        os.killpg(proc.pid, sig)
        return
    except ProcessLookupError:
        pass
    except PermissionError:
        # Some macOS hosts reject group signals but permit signals to individual child PIDs.
        pass
    for pid in sorted(members, reverse=True):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def redact_artifact(path, key):
    secret = key.encode()
    temporary = path.with_name(path.name + ".redacting")
    if path.suffix.lower() == ".xlsx":
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                target.writestr(info, source.read(info.filename).replace(secret, b"[REDACTED]"))
    else:
        with path.open("rb") as source, temporary.open("wb") as target:
            pending = b""
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                pending = (pending + chunk).replace(secret, b"[REDACTED]")
                cut = max(0, len(pending) - len(secret) + 1)
                target.write(pending[:cut])
                pending = pending[cut:]
            target.write(pending.replace(secret, b"[REDACTED]"))
    temporary.replace(path)


def run_dict(run):
    data = json.loads(run.summary_json or "{}")
    return dict(id=run.id, job_id=run.job_id, name=run.project_name, provider=run.provider_name,
                model=run.model_name, status=run.status, verdict=run.verdict, duration_ms=run.duration_ms,
                started_at=run.started_at.isoformat(), finished_at=run.finished_at.isoformat() if run.finished_at else None,
                **data)


def suite_records(db):
    return [r for r in db.scalars(select(Run).order_by(Run.id.desc()))
            if json.loads(r.summary_json or "{}").get("kind") == "suite"]


def recover_interrupted():
    with SessionLocal() as db:
        for run in suite_records(db):
            if run.status in ("running", "queued"):
                run.status = "interrupted"
                run.verdict = "服务重启中断；保留已有证据，未自动重试"
                run.finished_at = dt.datetime.now()
        db.commit()


class SuiteJob:
    def __init__(self, plan, key):
        self.id = uuid.uuid4().hex
        self.plan = plan
        self.key = key
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.status = "running"
        self.timed_out = False
        self.lines = []
        self.run_ids = []
        self.current = None
        self.proc = None
        self.thread = None

    def log(self, value):
        value = str(value)
        if self.key:
            value = value.replace(self.key, "[REDACTED]")
        with self.lock:
            self.lines.append(value[:4000])
            self.lines = self.lines[-300:]
        return value

    def cancel(self, timeout=False):
        with self.lock:
            if self.status != "running":
                return
            self.timed_out |= timeout
            self.stop.set()
            if self.current:
                self.current.stop.set()

    def snapshot(self):
        with self.lock:
            current = self.current.snapshot(False) if self.current else None
            lines = list(self.lines)
            status = self.status
        with SessionLocal() as db:
            records = [run_dict(db.get(Run, rid)) for rid in self.run_ids]
        return dict(id=self.id, status=status, cancel_requested=self.stop.is_set(), timed_out=self.timed_out,
                    lines=lines, runs=records, current=current, plan=self.plan, report_id=getattr(self, "report_id", None))

    def prepare(self):
        with SessionLocal() as db:
            for index, stage in enumerate(self.plan["stages"], 1):
                plan = self.stage_plan(index - 1)
                metadata = dict(kind="suite", suite_id=plan["suite_id"], demo=plan["demo"],
                    stage=index, stage_count=len(self.plan["stages"]), config=stage["config"],
                    source=plan["source"], source_sha256=plan["source_sha256"],
                    endpoint=plan["endpoint"], plan_hash=self.plan["plan_hash"],
                    measurement_status="pending", acceptance_status="not_evaluated")
                run = Run(project_name=plan["name"], provider_id=plan["provider_id"],
                    provider_name=plan["provider_name"], model_name=plan["model"], job_id=self.id,
                    status="queued", summary_json=json.dumps(metadata, ensure_ascii=False))
                db.add(run)
                db.flush()
                self.run_ids.append(run.id)
            db.commit()

    def stage_plan(self, index):
        stage = self.plan["stages"][index]
        plan = self.plan["projects"][stage["project_index"]] if self.plan["kind"] == "batch" else self.plan
        return {**plan, "parameters": stage["config"]}

    def execute_script(self, directory, plan=None):
        plan = plan or self.plan
        output = directory / "artifacts"
        command = script_command(plan, output)
        save_json(directory / "command.json", command)
        # Explicit credential mapping; omit unrelated inherited API credentials.
        env = {k: v for k, v in os.environ.items() if not k.startswith("ACCEPTANCE_") and not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
        env.update({KEY_ENV: self.key, "API_KEY_ENV": KEY_ENV, "API_BASE": plan["endpoint"].removesuffix("/chat/completions"),
                    "MODEL": plan["model"], "TEST_RESULT_ROOT": str(output), "PYTHONUNBUFFERED": "1"})
        with (directory / "script_output.log").open("w", encoding="utf-8") as logfile:
            with self.lock:
                if self.stop.is_set():
                    return {"process_exit": None, "measurement_status": "cancelled"}
                self.proc = subprocess.Popen(command, cwd=str(Path(plan["source"]).parent), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
                proc = self.proc

            def read_output():
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                pending = ""
                written = 0
                limit = 10000000

                def emit(value):
                    nonlocal written
                    value = self.log(value)
                    if written < limit:
                        logfile.write(value[:limit - written])
                        logfile.flush()
                        written += len(value)
                        if written >= limit:
                            logfile.write("\n[日志达到 10MB 上限，后续输出仅保留末 300 段]\n")

                for chunk in iter(lambda: proc.stdout.read1(4096), b""):
                    pending += decoder.decode(chunk)
                    if self.key:
                        pending = pending.replace(self.key, "[REDACTED]")
                    retain = max(len(self.key) - 1, 0)
                    cut = max(0, len(pending) - retain)
                    if cut:
                        emit(pending[:cut])
                        pending = pending[cut:]
                emit(pending + decoder.decode(b"", final=True))

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            members = {proc.pid}
            while proc.poll() is None:
                if self.stop.wait(.1):
                    members = process_family(proc)
                    signal_family(proc, members, signal.SIGTERM)
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        signal_family(proc, members, signal.SIGKILL)
                    break
            code = proc.wait()
            if self.stop.is_set():
                signal_family(proc, members, signal.SIGKILL)
            reader.join(timeout=5)
            if reader.is_alive():
                signal_family(proc, process_family(proc) | members, signal.SIGKILL)
                reader.join(timeout=2)
            proc.stdout.close()
            self.proc = None
        summaries = []
        tables = []
        if output.exists():
            for path in sorted(output.rglob("summary.json")):
                if path.stat().st_size <= 5000000:
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                        summaries.append(dict(file=str(path.relative_to(directory)), data=data))
                    except (ValueError, OSError):
                        pass
            for path in sorted(output.rglob("*.csv")):
                if path.stat().st_size > 50000000:
                    continue
                try:
                    with path.open(encoding="utf-8-sig", newline="") as handle:
                        rows = csv.DictReader(handle)
                        count = 0
                        outcomes, statuses = Counter(), Counter()
                        for row in rows:
                            count += 1
                            if row.get("result"):
                                outcomes[row["result"]] += 1
                            if row.get("http_status"):
                                statuses[row["http_status"]] += 1
                        tables.append(dict(file=str(path.relative_to(directory)), rows=count,
                                           result_counts=dict(outcomes), http_status_counts=dict(statuses)))
                except (OSError, UnicodeError, csv.Error):
                    pass
        return dict(process_exit=code, measurement_status="evidence_available" if summaries or tables else "unverified",
                    acceptance_status="source_report_requires_review", source_summaries=summaries, source_tables=tables)

    def worker(self):
        timer = threading.Timer(self.plan["timeout_seconds"], lambda: self.cancel(timeout=True))
        timer.daemon = True
        timer.start()
        try:
            for index, rid in enumerate(self.run_ids):
                if self.stop.is_set():
                    break
                plan = self.stage_plan(index)
                if index and self.plan["kind"] == "batch":
                    previous = self.stage_plan(index - 1)
                    if previous["suite_id"] in ("tpm_benchmark", "stream_metrics") and previous["suite_id"] != plan["suite_id"]:
                        cooldown = self.plan.get("cooldown_seconds", 70)
                        self.log(f"限流冷却 {cooldown} 秒")
                        if self.stop.wait(cooldown):
                            break
                started = time.monotonic()
                directory = SUITE_RUNS / self.id / str(rid)
                directory.mkdir(parents=True, exist_ok=True)
                with SessionLocal() as db:
                    run = db.get(Run, rid)
                    run.status = "running"
                    run.evidence_dir = str(directory)
                    run.started_at = dt.datetime.now()
                    db.commit()
                save_json(directory / "plan.json", self.plan)
                self.log(f"阶段 {index + 1}/{len(self.run_ids)} 开始，运行 #{rid}")
                result = {}
                error = ""
                try:
                    if digest(Path(plan["source"])) != plan["source_sha256"]:
                        raise ValueError("源脚本在预览后发生变化，请重新预览")
                    for asset in plan.get("assets", []):
                        if digest(Path(asset["path"])) != asset["sha256"]:
                            raise ValueError("素材在预览后发生变化，请重新预览")
                    if plan["kind"] == "benchmark":
                        task = benchmark_module().Run(self.plan["stages"][index]["config"], self.key, root=directory)
                        with self.lock:
                            self.current = task
                            if self.stop.is_set():
                                task.stop.set()
                        report = asyncio.run(task.execute())
                        result = dict(report_dir=str(task.directory.relative_to(directory)), summary=report["summary"],
                            measurement_status="simulated" if self.plan["demo"] else "measured",
                            acceptance_status="not_evaluated", benchmark_status=report["status"],
                            metric_basis=dict(ttft="first content or reasoning", tpot="first-to-last generated delta / (output_tokens-1)",
                                tpm="whole-run extrapolated per minute", percentile="linear interpolation", cache="cached input / input tokens"))
                        if report["status"] == "error":
                            error = report.get("error") or "压测引擎异常"
                        with self.lock:
                            self.current = None
                    else:
                        result = self.execute_script(directory, plan)
                        if result.get("process_exit") not in (0, None):
                            error = "脚本退出码 " + str(result["process_exit"])
                except Exception as exc:
                    error = self.log(str(exc))
                finally:
                    with self.lock:
                        self.current = None
                status = "error" if error else "completed"
                if self.stop.is_set():
                    status = "error" if self.timed_out else "cancelled"
                    error = "任务达到总超时" if self.timed_out else "用户取消"
                verdict = error or ("本地模拟完成" if self.plan["demo"] else "执行完成；请查看实测值和原始断言")
                self.scrub_and_manifest(directory, plan)
                with SessionLocal() as db:
                    run = db.get(Run, rid)
                    metadata = json.loads(run.summary_json)
                    metadata.update(result)
                    if error:
                        metadata["error"] = error
                    serialized = json.dumps(metadata, ensure_ascii=False)
                    run.summary_json = serialized.replace(self.key, "[REDACTED]") if self.key else serialized
                    run.status = status
                    run.verdict = verdict
                    run.duration_ms = int((time.monotonic() - started) * 1000)
                    run.finished_at = dt.datetime.now()
                    db.commit()
                self.log(f"运行 #{rid}: {verdict}")
                if status == "error" and not self.plan.get("continue_on_error", False):
                    break
        except Exception as exc:
            self.log(str(exc))
        finally:
            timer.cancel()
            with SessionLocal() as db:
                for rid in self.run_ids:
                    run = db.get(Run, rid)
                    if run.status in ("queued", "running"):
                        run.status = "cancelled" if self.stop.is_set() and not self.timed_out else "error"
                        run.verdict = "未继续执行：" + ("取消" if self.stop.is_set() else "前序阶段异常")
                        run.finished_at = dt.datetime.now()
                db.commit()
                statuses = [db.get(Run, rid).status for rid in self.run_ids]
            with self.lock:
                try:
                    from .suite_reports import generate_report
                    self.report_id = generate_report(job_id=self.id)
                except Exception as exc:
                    self.log(f"汇总报告生成失败，可在报告页重试：{exc}")
                self.status = "error" if "error" in statuses else "cancelled" if "cancelled" in statuses else "completed"
                self.key = ""

    def scrub_and_manifest(self, directory, plan=None):
        files = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if self.key and path.suffix.lower() in (".json", ".jsonl", ".csv", ".html", ".txt", ".log", ".ndjson", ".sse", ".xlsx"):
                redact_artifact(path, self.key)
            h = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    h.update(chunk)
            files.append(dict(path=str(path.relative_to(directory)), bytes=path.stat().st_size, sha256=h.hexdigest()))
        save_json(directory / "manifest.json", {"files": files, "source_sha256": (plan or self.plan)["source_sha256"]})


class SuiteManager:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, plan, key):
        with self.lock:
            if any(j.status == "running" for j in self.jobs.values()):
                raise ValueError("已有套件正在运行，请等待完成或取消后再启动")
            job = SuiteJob(plan, key)
            job.prepare()
            self.jobs[job.id] = job
            # Keep active jobs plus the most recent in-memory log buffers; records remain in SQLite.
            while len(self.jobs) > 50:
                del self.jobs[next(iter(self.jobs))]
            job.thread = threading.Thread(target=job.worker, daemon=True)
            job.thread.start()
        return job

    def shutdown(self):
        for job in list(self.jobs.values()):
            job.cancel()
        for job in list(self.jobs.values()):
            if job.thread and job.thread.is_alive():
                job.thread.join(timeout=10)


manager = SuiteManager()

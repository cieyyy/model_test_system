"""阶段 3：受控终端任务管理。

- 后台线程顺序执行选中的测试项目（标准四类测试）。
- 后台线程在服务器终端运行脚本文件夹内的脚本 / 自定义命令（脚本执行模式）。
- 实时输出行缓冲，前端轮询展示（终端效果）。
- 支持取消；任务状态：running / passed / failed / cancelled / error。
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import threading
import uuid

from . import runner, security
from .db import Credential, ModelEntity, Provider, Run, SessionLocal, TestProject


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---------- 对外接口 ----------

    def start(self, project_ids: list[int]) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "status": "running",
            "lines": [],
            "cancel": threading.Event(),
            "run_ids": [],
        }
        with self._lock:
            self._jobs[job_id] = job
        t = threading.Thread(target=self._worker, args=(job, project_ids), daemon=True)
        t.start()
        return job_id

    def get(self, job_id: str) -> dict | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job["cancel"].set()
        return True

    # ---------- 脚本执行（在服务器终端运行脚本文件夹） ----------

    def start_scripts(self, project_id: int, script_names: list[str], command: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "status": "running",
            "lines": [],
            "cancel": threading.Event(),
            "run_ids": [],
            "kind": "script",
        }
        with self._lock:
            self._jobs[job_id] = job
        t = threading.Thread(target=self._script_worker, args=(job, project_id, script_names, command), daemon=True)
        t.start()
        return job_id

    def _script_worker(self, job: dict, project_id: int, script_names: list[str], command: str) -> None:
        try:
            self._run_scripts(job, project_id, script_names, command)
        except Exception as e:  # noqa: BLE001
            self._log(job, f"脚本任务异常终止：{e}", "err")
            job["status"] = "error"
        finally:
            if job["status"] == "running":
                job["status"] = "finished"

    def _run_scripts(self, job: dict, project_id: int, script_names: list[str], command: str) -> None:
        import json as _json

        db = SessionLocal()
        try:
            project = db.get(TestProject, project_id)
            if project is None:
                self._log(job, f"项目 {project_id} 不存在。", "err")
                job["status"] = "error"
                return
            folder = os.path.expanduser(project.script_dir)
            if not os.path.isdir(folder):
                self._log(job, f"✗ 脚本文件夹不存在：{folder}", "err")
                job["status"] = "error"
                return

            targets: list[tuple[str, list[str]]] = []  # (label, shell_cmd_list)
            if command.strip():
                self._log(job, f"▶ 运行自定义命令：{command}")
                targets.append((command.strip(), ["/bin/sh", "-c", command.strip()]))
            else:
                entries = sorted(
                    e for e in os.listdir(folder)
                    if os.path.isfile(os.path.join(folder, e)) and not e.startswith(".")
                )
                if script_names:
                    want = set(script_names)
                    entries = [e for e in entries if e in want]
                if not entries:
                    self._log(job, "文件夹内没有可运行的脚本（.py/.sh/.bash 或带执行权限的文件）。", "warn")
                    job["status"] = "finished"
                    return
                for name in entries:
                    ext = os.path.splitext(name)[1].lower()
                    if ext == ".py":
                        cmd = [sys.executable, name]
                    elif ext in (".sh", ".bash"):
                        cmd = ["/bin/sh", name]
                    elif os.access(os.path.join(folder, name), os.X_OK):
                        cmd = [os.path.join(folder, name)]
                    else:
                        self._log(job, f"  ℹ 跳过不可运行文件：{name}", "dim")
                        continue
                    targets.append((name, cmd))

            overall = "finished"
            for label, cmd in targets:
                if job["cancel"].is_set():
                    self._log(job, "收到取消信号，停止后续脚本。", "warn")
                    job["status"] = "cancelled"
                    overall = "cancelled"
                    break
                self._run_one_script(db, job, project, folder, label, cmd)
            else:
                job["status"] = overall
        finally:
            db.close()

    def _run_one_script(self, db, job: dict, project: TestProject, folder: str, label: str, cmd: list[str]) -> None:
        import json as _json

        self._log(job, f"▶ 运行：{label}")
        started = dt.datetime.now()
        run = Run(
            project_id=project.id, project_name=project.name,
            provider_id=project.provider_id or 0,
            provider_name=project.provider_ref.name if project.provider_ref else "脚本执行",
            model_name="", job_id=job["id"], status="running",
        )
        db.add(run)
        db.commit()
        job["run_ids"].append(run.id)

        exit_code = -1
        try:
            evidence_dir = runner.RUNS_DIR / f"run_{run.id:04d}"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            log_path = evidence_dir / "script_output.log"
            with log_path.open("w", encoding="utf-8") as lf:
                lf.write(f"$ cd {folder}\n$ {' '.join(cmd)}\n\n")
                proc = subprocess.Popen(
                    cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env=dict(os.environ),
                )
                assert proc.stdout is not None
                for raw in proc.stdout:
                    try:
                        text = raw.decode("utf-8", errors="replace").rstrip("\n")
                    except Exception:  # noqa: BLE001
                        text = raw.decode("latin-1", errors="replace").rstrip("\n")
                    lf.write(text + "\n")
                    lf.flush()
                    self._log(job, text, "info")
                proc.wait(timeout=600)
                exit_code = proc.returncode
                lf.write(f"\n[退出码] {exit_code}\n")

            passed = exit_code == 0
            run.status = "passed" if passed else "failed"
            run.passed = passed
            run.http_status = 0
            run.error_class = "script_ok" if passed else "script_exit_nonzero"
            run.verdict = f"脚本 {label}：{'通过' if passed else '失败'}（退出码 {exit_code}）"
            run.duration_ms = int((dt.datetime.now() - started).total_seconds() * 1000)
            run.summary_json = _json.dumps({
                "kind": "script",
                "label": label,
                "command": " ".join(cmd),
                "cwd": folder,
                "exit_code": exit_code,
            }, ensure_ascii=False)
            run.evidence_dir = str(evidence_dir)
            run.finished_at = dt.datetime.now()
            db.commit()
            runner.write_manifest(evidence_dir, {
                "run_id": run.id,
                "project": project.name,
                "label": label,
                "command": " ".join(cmd),
                "cwd": folder,
                "exit_code": exit_code,
                "started_at": str(started),
            })
            mark = "✓" if passed else "✗"
            kind = "ok" if passed else "err"
            self._log(
                job,
                f"  {mark} {label}：{'通过' if passed else '失败'}（退出码 {exit_code}），证据 run_{run.id:04d}/",
                kind,
            )
        except subprocess.TimeoutExpired:
            run.status = "failed"
            run.error_class = "script_timeout"
            run.verdict = f"脚本 {label}：超时（>600s）"
            run.finished_at = dt.datetime.now()
            db.commit()
            self._log(job, f"  ✗ {label}：执行超时（>600s）", "err")
        except Exception as e:  # noqa: BLE001
            run.status = "error"
            run.error_class = "internal_error"
            run.verdict = str(e)
            run.finished_at = dt.datetime.now()
            db.commit()
            self._log(job, f"  ✗ {label} 内部错误：{e}", "err")

    # ---------- 内部 ----------

    def _log(self, job: dict, text: str, kind: str = "info") -> None:
        job["lines"].append({
            "ts": dt.datetime.now().strftime("%H:%M:%S"),
            "kind": kind,
            "text": text,
        })

    def _worker(self, job: dict, project_ids: list[int]) -> None:
        try:
            self._run_all(job, project_ids)
        except Exception as e:  # noqa: BLE001
            self._log(job, f"任务异常终止：{e}", "err")
            job["status"] = "error"
        finally:
            if job["status"] == "running":
                job["status"] = "finished"

    def _run_all(self, job: dict, project_ids: list[int]) -> None:
        db = SessionLocal()
        try:
            self._log(job, f"共选择 {len(project_ids)} 个测试项目，开始执行。")
            for pid in project_ids:
                if job["cancel"].is_set():
                    self._log(job, "收到取消信号，停止后续项目。", "warn")
                    job["status"] = "cancelled"
                    break
                project = db.get(TestProject, pid)
                if project is None:
                    self._log(job, f"项目 {pid} 不存在，跳过。", "warn")
                    continue
                self._execute_one(db, job, project)
            else:
                job["status"] = "finished"
        finally:
            db.close()

    def _execute_one(self, db, job: dict, project: TestProject) -> None:
        name = project.name
        self._log(job, f"▶ [{name}] 开始。")

        # ---- 执行前检查 ----
        if not project.provider_ref:
            self._log(job, f"  ✗ [{name}] 未关联中转站，跳过。", "err")
            return
        if project.provider_ref.protocol != "openai":
            self._log(job, f"  ✗ [{name}] 阶段 3 仅支持 OpenAI 兼容协议，跳过。", "err")
            return
        model = project.model_ref
        if model is None:
            models = project.provider_ref.models
            if not models:
                self._log(job, f"  ✗ [{name}] 未关联模型且中转站下无模型，跳过。", "err")
                return
            model = models[0]
            self._log(job, f"  ℹ [{name}] 未关联模型，使用中转站首个模型 {model.model_name}。", "warn")
        cred = next(iter(project.provider_ref.credentials), None)
        if cred is None:
            self._log(job, f"  ✗ [{name}] 中转站没有 API Key，跳过（先在中转站页添加 Key）。", "err")
            return

        run = Run(
            project_id=project.id, project_name=name,
            provider_id=project.provider_id, provider_name=project.provider_ref.name,
            model_name=model.model_name, job_id=job["id"], status="running",
        )
        db.add(run)
        db.commit()
        job["run_ids"].append(run.id)

        try:
            api_key = security.decrypt_secret(cred.encrypted_key)  # 仅内存中，不写日志
            evidence_dir = runner.RUNS_DIR / f"run_{run.id:04d}"
            summary = runner.execute_test(
                project, project.provider_ref, model.model_name, api_key,
                evidence_dir, cancel_event=job["cancel"],
            )
            if job["cancel"].is_set() and not summary["passed"]:
                run.status = "cancelled"
            else:
                run.status = "passed" if summary["passed"] else "failed"
            run.http_status = summary["http_status"]
            run.passed = summary["passed"]
            run.duration_ms = summary["duration_ms"]
            run.ttft_ms = summary["ttft_ms"]
            run.error_class = summary["error_class"]
            run.verdict = summary["verdict"]
            import json as _json
            run.summary_json = _json.dumps(summary, ensure_ascii=False)
            run.evidence_dir = str(evidence_dir)
            run.finished_at = dt.datetime.now()
            db.commit()
            runner.write_manifest(evidence_dir, {
                "run_id": run.id,
                "project": name,
                "provider": project.provider_ref.name,
                "model": model.model_name,
                "category": summary["category"],
                "started_at": str(run.started_at),
                "key_hint": cred.key_hint,
            })
            mark = "✓" if run.status == "passed" else "✗"
            kind = "ok" if run.status == "passed" else "err"
            ttft = f"，首Token {run.ttft_ms}ms" if run.ttft_ms else ""
            self._log(
                job,
                f"  {mark} [{name}] {run.status}：HTTP {run.http_status}，{run.verdict}，"
                f"耗时 {run.duration_ms}ms{ttft}，证据 run_{run.id:04d}/",
                kind,
            )
        except runner.ExecutionError as e:
            run.status = "error" if e.error_class != "cancelled" else "cancelled"
            run.http_status = e.http_status
            run.error_class = e.error_class
            run.verdict = str(e)
            run.finished_at = dt.datetime.now()
            db.commit()
            self._log(job, f"  ✗ [{name}] {e.error_class}：{e}", "err")
        except Exception as e:  # noqa: BLE001
            run.status = "error"
            run.error_class = "internal_error"
            run.verdict = str(e)
            run.finished_at = dt.datetime.now()
            db.commit()
            self._log(job, f"  ✗ [{name}] 内部错误：{e}", "err")


manager = JobManager()

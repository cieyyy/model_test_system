"""Bounded historical-record ingestion; original files remain immutable."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import math
import re
import secrets
import shutil
import threading
import zipfile
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select

from .db import DATA_DIR, ImportedAsset, ModelEntity, Provider, Run, SessionLocal
from .suite_jobs import SUITE_RUNS

router = APIRouter(prefix="/api")
LOCK = threading.RLock()
OBJECTS = DATA_DIR / "import_objects"
STAGING = DATA_DIR / "import_staging"
LIMIT = 20 * 1024 * 1024
MAX_ROWS = 2000
EXTENSIONS = {".html", ".htm", ".csv", ".tsv", ".xlsx", ".json"}


def value(row, *names):
    def key(s):
        return re.sub(r"[\s_\-/\\:：().（）]+", "", str(s).lower())
    wanted = {key(n) for n in names}
    return next((str(v).strip() for k, v in row.items() if key(k) in wanted and v not in (None, "")), "")


def outcome(row):
    # Raw assertions take priority over an editorial conclusion in exported reports.
    raw = value(row, "original", "原始判定", "实测 / 原始判定", "result", "outcome", "passed", "状态", "status", "判定", "结论", "报告结论")
    text = raw.strip().lower()
    if any(t in text for t in ("部分", "partial")):
        return "partial", raw
    if any(t in text for t in ("待", "复核", "未测", "未执行", "跳过", "review", "unknown", "skip", "not_evaluated", "模拟")):
        return "manual_review", raw
    if any(t in text for t in ("未通过", "未达", "不通过", "失败", "不支持", "不兼容", "error", "fail", "timeout", "超时")) or text == "false":
        return "failed", raw
    if text in {"pass", "passed", "true", "ok", "success", "succeeded", "通过", "成功", "兼容", "支持", "全部通过"}:
        return "passed", raw
    return "manual_review", raw or "未识别业务断言"


def number(row, *names):
    try:
        n = float(value(row, *names))
        return n if math.isfinite(n) and n >= 0 else None
    except (ValueError, TypeError):
        return None


class Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.current, self.row, self.cell = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.current = []
        elif tag == "tr" and self.current is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag in ("br", "div") and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.current.append(self.row)
            self.row = None
        elif tag == "table" and self.current is not None:
            self.tables.append(self.current)
            self.current = None


def decode(path):
    if path.stat().st_size > LIMIT:
        raise HTTPException(413, "文件解析上限为 20MB")
    raw = path.read_bytes()
    encodings = ("utf-16", "utf-8-sig", "gb18030") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise HTTPException(400, "文本编码无法识别，请转为 UTF-8")


def matrix_rows(matrix):
    clean = [[str(c).strip() if c is not None else "" for c in row] for row in matrix]
    clean = [r for r in clean if any(r)]
    if not clean:
        return []
    header = next((i for i, r in enumerate(clean[:20]) if sum(bool(c) for c in r) >= 2), 0)
    headers = [h or f"列{i+1}" for i, h in enumerate(clean[header])]
    if len(set(headers)) != len(headers):
        headers = [f"{h} {i+1}" if headers.count(h) > 1 else h for i, h in enumerate(headers)]
    return [dict(zip(headers, r + [""] * max(0, len(headers)-len(r)))) for r in clean[header+1:] if r != headers]


def parse_file(path, extension=None):
    ext = (extension or path.suffix).lower()
    if ext in (".html", ".htm"):
        parser = Tables()
        parser.feed(decode(path))
        tables = [matrix_rows(t) for t in parser.tables]
        # Summary/metric tables are not extra test cases. Prefer assertion tables.
        assertion_names = ("result", "outcome", "passed", "状态", "status", "判定", "结论", "报告结论", "原始判定", "实测 / 原始判定")
        selected = [t for t in tables if t and any(value({k: "x" for k in t[0]}, n) for n in assertion_names)]
        rows = [r for t in (selected or tables) for r in t]
    elif ext in (".csv", ".tsv"):
        text = decode(path)
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters="\t,;")
        except csv.Error:
            dialect = csv.excel_tab if ext == ".tsv" else csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        rows = []
        for row in reader:
            rows.append(row)
            if len(rows) > MAX_ROWS:
                break
    elif ext == ".xlsx":
        try:
            with zipfile.ZipFile(path) as archive:
                if sum(x.file_size for x in archive.infolist()) > 50 * 1024 * 1024:
                    raise HTTPException(413, "Excel 解压后超过 50MB，请拆分工作簿")
            from openpyxl import load_workbook
            workbook = load_workbook(path, read_only=True, data_only=True)
            try:
                rows = []
                for sheet in workbook.worksheets:
                    if (sheet.max_column or 0) > 80:
                        raise HTTPException(400, "Excel 最多支持 80 列")
                    rows.extend(matrix_rows(list(sheet.iter_rows(max_row=MAX_ROWS+22, max_col=min(sheet.max_column or 80, 80), values_only=True))))
                    if len(rows) > MAX_ROWS:
                        break
            finally:
                workbook.close()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, "XLSX 无法解析，请检查文件是否损坏或加密") from exc
    elif ext == ".json":
        try:
            data = json.loads(decode(path))
        except ValueError as exc:
            raise HTTPException(400, "JSON 格式无效") from exc
        rows = data if isinstance(data, list) else next((data[k] for k in ("requests", "results", "data", "rows", "details") if isinstance(data, dict) and isinstance(data.get(k), list)), [])
    else:
        raise HTTPException(400, "不支持的文件类型")
    if len(rows) > MAX_ROWS:
        raise HTTPException(413, "每个文件最多识别 2000 行，请拆分后上传；未保存部分结果")
    rows = [r for r in rows if isinstance(r, dict) and any(v not in (None, "") for v in r.values())]
    if not rows:
        raise HTTPException(400, "文件中没有可识别的数据行")
    if any(len(r) > 80 or any(len(str(v)) > 12000 for v in r.values()) for r in rows):
        raise HTTPException(413, "每行最多 80 列，单元格最多 12000 字符")
    return rows


def identity(filename, row, provider, model, category):
    p = provider.strip() or value(row, "provider", "provider_name", "channel", "渠道", "中转站")
    m = model.strip() or value(row, "model", "model_name", "模型", "模型名称")
    if not p:
        raise HTTPException(400, "未识别到中转站，请填写中转站名称后重新上传")
    if not m:
        stem = Path(filename).stem
        match = re.search(r"(?i)(?:deepseek|gemini|claude|gpt|qwen|kimi|glm)[a-z0-9._-]*", stem)
        m = re.sub(r"[-_](?:report|reports|测试报告|all|\d{8}).*$", "", match[0]) if match else ""
    if not m:
        raise HTTPException(400, "未识别到模型，请填写完整模型名称后重新上传")
    c = category.strip() or value(row, "category", "测试类型", "测试分类", "分类", "type")
    c = c or "导入测试"
    return p[:100], m[:200], c[:100]


def import_staged(temporary, filename, sha256, size, provider, model, category):
    rows = parse_file(temporary, Path(filename).suffix.lower())
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        groups[identity(filename, row, provider, model, category)].append((index, row))
    if len(groups) > 128:
        raise HTTPException(400, "文件中模型/中转站/项目组合超过 128 个，请拆分文件")
    made, created_object = [], False
    stored = None
    with LOCK, SessionLocal() as db:
        try:
            asset = db.scalar(select(ImportedAsset).where(ImportedAsset.sha256 == sha256))
            if asset:
                stored = DATA_DIR / asset.storage_path
            else:
                stored = OBJECTS / sha256[:2] / (sha256 + Path(filename).suffix.lower())
                stored.parent.mkdir(parents=True, exist_ok=True)
                temporary.replace(stored)
                created_object = True
                asset = ImportedAsset(rel_path=filename, file_kind="html" if Path(filename).suffix.lower() in (".html", ".htm") else "table",
                    size_bytes=size, sha256=sha256, storage_path=str(stored.relative_to(DATA_DIR)))
                db.add(asset)
                db.flush()
            recognized = []
            run_ids = []
            for (p, m, c), items in groups.items():
                key = "import:" + hashlib.sha256(json.dumps([sha256, p, m, c], ensure_ascii=False).encode()).hexdigest()
                existing = db.scalar(select(Run).where(Run.job_id == key))
                counts = Counter(outcome(row)[0] for _, row in items)
                summary = {k: counts[k] for k in ("passed", "failed", "partial", "manual_review")}
                summary["total"] = len(items)
                recognized.append(dict(provider=p, model=m, category=c, rows=len(items), **{k: counts[k] for k in ("passed", "failed", "partial", "manual_review")}))
                if existing:
                    run_ids.append(existing.id)
                    continue
                relay = db.scalar(select(Provider).where(Provider.name == p))
                if not relay:
                    relay = Provider(name=p, enabled=False, description="由历史测试记录导入，配置接口后可启用")
                    db.add(relay)
                    db.flush()
                if not db.scalar(select(ModelEntity).where(ModelEntity.provider_id == relay.id, ModelEntity.model_name == m)):
                    db.add(ModelEntity(provider_id=relay.id, model_name=m, display_name=m))
                all_passed = counts["passed"] == len(items)
                metadata = dict(kind="suite", suite_id="imported_record", imported=True, historical=True,
                    import_sha256=sha256, asset_id=asset.id, source_file=filename, category=c,
                    measurement_status="historical", acceptance_status="source_assertions", summary=summary,
                    source_storage=str(stored.relative_to(DATA_DIR)))
                run = Run(project_name=c, provider_id=relay.id, provider_name=p, model_name=m, job_id=key,
                    status="passed" if all_passed else "completed", passed=all_passed,
                    verdict=f"通过 {counts['passed']} / 失败 {counts['failed']} / 部分 {counts['partial']} / 待复核 {counts['manual_review']}",
                    summary_json=json.dumps(metadata, ensure_ascii=False), finished_at=dt.datetime.now())
                db.add(run)
                db.flush()
                directory = SUITE_RUNS / "comparison-imports" / str(run.id)
                directory.mkdir(parents=True, exist_ok=False)
                made.append(directory)
                # Original bytes live only in the content-addressed store; this is a derived index.
                # Keep only row indexes here. The content-addressed source remains the single copy.
                (directory / "records.json").write_text(json.dumps({"source_rows": [i + 2 for i, _ in items], "records": len(items)}), encoding="utf-8")
                (directory / "manifest.json").write_text(json.dumps(dict(source_sha256=sha256, source_storage=metadata["source_storage"], records=len(items))), encoding="utf-8")
                run.evidence_dir = str(directory)
                run_ids.append(run.id)
            db.commit()
            return dict(imported=bool(made), duplicate=not made, run_id=run_ids[0], run_ids=run_ids,
                asset_id=asset.id, recognized=dict(rows=len(rows), groups=recognized, **{k: recognized[0][k] for k in ("provider", "model", "category", "passed", "failed", "partial", "manual_review")}))
        except Exception:
            db.rollback()
            for directory in made:
                shutil.rmtree(directory, ignore_errors=True)
            if created_object and stored:
                stored.unlink(missing_ok=True)
            raise


@router.post("/import/upload")
async def upload(request: Request, filename: str = "", provider: str = "", model: str = "", category: str = ""):
    suffix = Path(filename).suffix.lower()
    if suffix not in EXTENSIONS:
        raise HTTPException(400, "支持 HTML、CSV、TSV、XLSX 或 JSON 测试记录")
    safe_name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._ -]+", "_", Path(filename).name).strip(" .")
    if len(safe_name.encode("utf-8")) > 220:
        raise HTTPException(400, "文件名过长，请缩短文件名")
    STAGING.mkdir(parents=True, exist_ok=True)
    temporary = STAGING / (secrets.token_hex(16) + suffix)
    digest, size = hashlib.sha256(), 0
    try:
        with temporary.open("wb") as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > LIMIT:
                    raise HTTPException(413, "文件最大 20MB，请拆分后上传")
                digest.update(chunk)
                handle.write(chunk)
        if not size:
            raise HTTPException(400, "上传文件为空")
        return await run_in_threadpool(import_staged, temporary, safe_name, digest.hexdigest(), size, provider, model, category)
    finally:
        temporary.unlink(missing_ok=True)


@router.get("/import/records")
def imported_records():
    with SessionLocal() as db:
        return [dict(id=a.id, name=a.rel_path, size_bytes=a.size_bytes, sha256=a.sha256, created_at=str(a.created_at))
                for a in db.scalars(select(ImportedAsset).where(ImportedAsset.storage_path != "").order_by(ImportedAsset.id.desc()).limit(100))]


def imported_rows(run):
    if not run.evidence_dir:
        return []
    path = Path(run.evidence_dir).resolve() / "records.json"
    if not path.is_relative_to(SUITE_RUNS.resolve()) or not path.is_file() or path.stat().st_size > LIMIT:
        return []
    return json.loads(path.read_text(encoding="utf-8"))

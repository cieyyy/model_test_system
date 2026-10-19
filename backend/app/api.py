"""阶段 2 API：登记闭环（Provider / Model / Credential / TestProject / 导入）。"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import security
from .db import (
    Credential,
    DATA_DIR,
    ImportedAsset,
    MatrixReview,
    ModelEntity,
    Provider,
    Run,
    SessionLocal,
    Setting,
    TestProject,
)
from .jobs import manager as job_manager
from . import runner as runner_mod
from .schemas import (
    CredentialCreate,
    ExecuteRequest,
    ImportExecuteRequest,
    ImportScanRequest,
    ItemFilesRequest,
    MatrixReviewRequest,
    ModelCreate,
    ModelFetchRequest,
    ModelsBulkCreate,
    ModelUpdate,
    PasswordSetRequest,
    PreviewRequest,
    ProjectCreate,
    ProjectUpdate,
    ProviderCreate,
    ProviderUpdate,
    RevealRequest,
    ScriptExecuteRequest,
)

router = APIRouter(prefix="/api")

DEFAULT_LEGACY_ROOT = os.environ.get("MT_SCRIPTS_ROOT", str(Path(__file__).resolve().parents[2] / "test-scripts"))

_PASSWORD_KEY = "platform_password_hash"

_KIND_BY_EXT = {
    ".py": "script", ".sh": "script", ".js": "script",
    ".json": "json", ".jsonl": "json",
    ".csv": "table", ".xlsx": "table", ".xls": "table", ".tsv": "table",
    ".html": "html", ".htm": "html",
    ".md": "doc", ".txt": "doc",
}
_ASSET_EXTS = set(_KIND_BY_EXT)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_or_404(db: Session, model, item_id: int, label: str):
    obj = db.get(model, item_id)
    if obj is None:
        raise HTTPException(404, f"{label}不存在")
    return obj


def _guess_category(name: str) -> str:
    n = name.lower()
    if "kimi" in n or "k3" in n:
        return "Kimi 专项"
    if "glm" in n:
        return "GLM 专项"
    if "qwen" in n:
        return "Qwen 专项"
    if "deepseek" in n or "dsv" in n:
        return "DeepSeek 专项"
    if "vidu" in n:
        return "视觉/视频"
    if "统一" in name or "total" in n or "api" in n:
        return "统一 API 测试"
    return "导入资料"


def _guess_model_names(name: str) -> list[str]:
    n = name.lower()
    out = []
    if "kimi" in n or "k3" in n:
        out.append("kimi-k3")
    if "glm" in n:
        out.append("glm-5.3")
    if "qwen" in n:
        out.append("qwen-3.8-max")
    if "deepseek" in n:
        out.append("deepseek-v4.1")
    return out




# ---------- 健康与统计 ----------

@router.get("/health")
def health():
    return {"status": "ok", "phase": 2}


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    return {
        "providers": db.scalar(select(func.count(Provider.id))),
        "models": db.scalar(select(func.count(ModelEntity.id))),
        "projects": db.scalar(select(func.count(TestProject.id))),
        "credentials": db.scalar(select(func.count(Credential.id))),
        "imported_assets": db.scalar(select(func.count(ImportedAsset.id))),
        "runs": db.scalar(select(func.count(Run.id))),
        "runs_passed": db.scalar(select(func.count(Run.id)).where(Run.status == "passed")),
        "runs_failed": db.scalar(select(func.count(Run.id)).where(Run.status.in_(["failed", "error", "cancelled"]))),
        "password_set": db.get(Setting, _PASSWORD_KEY) is not None,
    }


# ---------- 平台主密码 ----------

@router.get("/security/password-status")
def password_status(db: Session = Depends(get_db)):
    return {"set": db.get(Setting, _PASSWORD_KEY) is not None}


@router.post("/security/set-password")
def set_password(req: PasswordSetRequest, db: Session = Depends(get_db)):
    stored = security.hash_password(req.password)
    setting = db.get(Setting, _PASSWORD_KEY)
    if setting is None:
        db.add(Setting(key=_PASSWORD_KEY, value=stored))
    else:
        setting.value = stored
    db.commit()
    return {"ok": True}


@router.post("/security/verify")
def verify_password(req: RevealRequest, db: Session = Depends(get_db)):
    setting = db.get(Setting, _PASSWORD_KEY)
    if setting is None:
        raise HTTPException(400, "平台主密码尚未设置，请先在「设置」中设置")
    if not security.verify_password(req.password, setting.value):
        raise HTTPException(401, "密码错误")
    return {"ok": True}


# ---------- Provider ----------

def _provider_dict(p: Provider) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "base_url": p.base_url,
        "protocol": p.protocol,
        "description": p.description,
        "enabled": p.enabled,
        "model_count": len(p.models),
        "credential_count": len(p.credentials),
        "key_hints": [c.key_hint for c in p.credentials],
        "created_at": str(p.created_at),
    }


@router.get("/providers")
def list_providers(db: Session = Depends(get_db)):
    return [_provider_dict(p) for p in db.scalars(select(Provider).order_by(Provider.id)).all()]


@router.post("/providers", status_code=201)
def create_provider(req: ProviderCreate, db: Session = Depends(get_db)):
    if db.scalar(select(Provider).where(Provider.name == req.name)):
        raise HTTPException(409, "同名中转站已存在")
    p = Provider(
        name=req.name, base_url=req.base_url, protocol=req.protocol,
        description=req.description, enabled=req.enabled,
    )
    db.add(p)
    db.flush()
    if req.api_key.strip():
        db.add(Credential(
            provider_id=p.id, label="默认",
            encrypted_key=security.encrypt_secret(req.api_key.strip()),
            key_hint=security.mask_key(req.api_key.strip()),
        ))
    db.commit()
    return _provider_dict(p)


@router.get("/providers/{provider_id}")
def get_provider(provider_id: int, db: Session = Depends(get_db)):
    return _provider_dict(_get_or_404(db, Provider, provider_id, "中转站"))


@router.put("/providers/{provider_id}")
def update_provider(provider_id: int, req: ProviderUpdate, db: Session = Depends(get_db)):
    p = _get_or_404(db, Provider, provider_id, "中转站")
    for field, value in req.model_dump(exclude_none=True).items():
        setattr(p, field, value)
    db.commit()
    return _provider_dict(p)


@router.delete("/providers/{provider_id}")
def delete_provider(provider_id: int, db: Session = Depends(get_db)):
    p = _get_or_404(db, Provider, provider_id, "中转站")
    db.delete(p)
    db.commit()
    return {"ok": True}


# ---------- Credential ----------

@router.get("/providers/{provider_id}/credentials")
def list_credentials(provider_id: int, db: Session = Depends(get_db)):
    p = _get_or_404(db, Provider, provider_id, "中转站")
    return [{"id": c.id, "label": c.label, "key_hint": c.key_hint} for c in p.credentials]


@router.post("/providers/{provider_id}/credentials", status_code=201)
def add_credential(provider_id: int, req: CredentialCreate, db: Session = Depends(get_db)):
    p = _get_or_404(db, Provider, provider_id, "中转站")
    c = Credential(
        provider_id=p.id, label=req.label,
        encrypted_key=security.encrypt_secret(req.api_key.strip()),
        key_hint=security.mask_key(req.api_key.strip()),
    )
    db.add(c)
    db.commit()
    return {"id": c.id, "label": c.label, "key_hint": c.key_hint}


@router.delete("/credentials/{credential_id}")
def delete_credential(credential_id: int, db: Session = Depends(get_db)):
    c = _get_or_404(db, Credential, credential_id, "密钥")
    db.delete(c)
    db.commit()
    return {"ok": True}


@router.post("/credentials/{credential_id}/reveal")
def reveal_credential(credential_id: int, req: RevealRequest, db: Session = Depends(get_db)):
    """查看明文 Key：需要平台主密码重新认证。"""
    setting = db.get(Setting, _PASSWORD_KEY)
    if setting is None:
        raise HTTPException(400, "平台主密码尚未设置，请先在「设置」中设置")
    if not security.verify_password(req.password, setting.value):
        raise HTTPException(401, "密码错误")
    c = _get_or_404(db, Credential, credential_id, "密钥")
    return {"provider": c.provider.name, "label": c.label, "api_key": security.decrypt_secret(c.encrypted_key)}


# ---------- Model ----------

def _model_dict(m: ModelEntity) -> dict:
    return {
        "id": m.id,
        "provider_id": m.provider_id,
        "provider_name": m.provider.name if m.provider else "",
        "model_name": m.model_name,
        "display_name": m.display_name,
        "model_url": m.model_url,
        "capabilities": m.capabilities,
        "notes": m.notes,
        "created_at": str(m.created_at),
    }


@router.get("/models")
def list_models(provider_id: int | None = None, db: Session = Depends(get_db)):
    q = select(ModelEntity).order_by(ModelEntity.id)
    if provider_id:
        q = q.where(ModelEntity.provider_id == provider_id)
    return [_model_dict(m) for m in db.scalars(q).all()]


@router.post("/models", status_code=201)
def create_model(req: ModelCreate, db: Session = Depends(get_db)):
    _get_or_404(db, Provider, req.provider_id, "中转站")
    if db.scalar(select(ModelEntity).where(
        ModelEntity.provider_id == req.provider_id, ModelEntity.model_name == req.model_name
    )):
        raise HTTPException(409, "该中转站下同名模型已存在")
    import json as _json
    m = ModelEntity(
        provider_id=req.provider_id, model_name=req.model_name,
        display_name=req.display_name or req.model_name,
        model_url=req.model_url,
        capabilities_json=_json.dumps(req.capabilities, ensure_ascii=False),
        notes=req.notes,
    )
    db.add(m)
    db.commit()
    return _model_dict(m)


@router.post("/models/fetch-remote")
def fetch_remote_models(req: ModelFetchRequest, db: Session = Depends(get_db)):
    """从中转站拉取模型列表（GET /v1/models，一般免费），用于快捷登记。"""
    import httpx

    p = _get_or_404(db, Provider, req.provider_id, "中转站")
    cred = next(iter(p.credentials), None)
    if cred is None:
        raise HTTPException(400, "该中转站尚未添加 Key，无法拉取模型列表")
    url = p.base_url.rstrip("/") + "/models"
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {security.decrypt_secret(cred.encrypted_key)}"},
            timeout=15,
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"请求中转站失败：{e}")
    if resp.status_code in (401, 403):
        raise HTTPException(401, "Key 无效或无权限（401/403）")
    if resp.status_code != 200:
        raise HTTPException(502, f"中转站返回 HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(502, "响应不是有效 JSON")
    ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
    existing = set(db.scalars(
        select(ModelEntity.model_name).where(ModelEntity.provider_id == req.provider_id)
    ).all())
    return {"url": url, "models": ids, "existing": sorted(x for x in ids if x in existing)}


@router.post("/models/bulk")
def bulk_create_models(req: ModelsBulkCreate, db: Session = Depends(get_db)):
    """按拉取结果批量登记模型（同一中转站下已存在的自动跳过；不同中转站可登记同名模型）。"""
    _get_or_404(db, Provider, req.provider_id, "中转站")
    existing = set(db.scalars(
        select(ModelEntity.model_name).where(ModelEntity.provider_id == req.provider_id)
    ).all())
    created, skipped = [], []
    for name in req.model_names:
        name = name.strip()
        if not name or name in existing:
            if name:
                skipped.append(name)
            continue
        db.add(ModelEntity(provider_id=req.provider_id, model_name=name, display_name=name))
        existing.add(name)
        created.append(name)
    db.commit()
    return {"created": created, "skipped": skipped}


@router.put("/models/{model_id}")
def update_model(model_id: int, req: ModelUpdate, db: Session = Depends(get_db)):
    m = _get_or_404(db, ModelEntity, model_id, "模型")
    data = req.model_dump(exclude_none=True)
    if "capabilities" in data:
        import json as _json
        m.capabilities_json = _json.dumps(data.pop("capabilities"), ensure_ascii=False)
    for field, value in data.items():
        setattr(m, field, value)
    db.commit()
    return _model_dict(m)


@router.delete("/models/{model_id}")
def delete_model(model_id: int, db: Session = Depends(get_db)):
    m = _get_or_404(db, ModelEntity, model_id, "模型")
    db.delete(m)
    db.commit()
    return {"ok": True}


# ---------- TestProject ----------

def _project_dict(p: TestProject) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "category": p.category,
        "provider_id": p.provider_id,
        "provider_name": p.provider_ref.name if p.provider_ref else "",
        "model_id": p.model_id,
        "model_name": p.model_ref.model_name if p.model_ref else "",
        "script_path": p.script_path,
        "command": p.command,
        "script_dir": p.script_dir,
        "exec_mode": p.exec_mode,
        "enabled": p.enabled,
        "source": p.source,
        "asset_count": len(p.assets),
        "created_at": str(p.created_at),
    }


@router.get("/projects")
def list_projects(category: str | None = None, db: Session = Depends(get_db)):
    q = select(TestProject).order_by(TestProject.id)
    if category:
        q = q.where(TestProject.category == category)
    return [_project_dict(p) for p in db.scalars(q).all()]


@router.post("/projects", status_code=201)
def create_project(req: ProjectCreate, db: Session = Depends(get_db)):
    if db.scalar(select(TestProject).where(TestProject.name == req.name)):
        raise HTTPException(409, "同名测试项目已存在")
    if req.provider_id:
        _get_or_404(db, Provider, req.provider_id, "中转站")
    p = TestProject(**req.model_dump())
    db.add(p)
    db.commit()
    return _project_dict(p)


@router.put("/projects/{project_id}")
def update_project(project_id: int, req: ProjectUpdate, db: Session = Depends(get_db)):
    p = _get_or_404(db, TestProject, project_id, "测试项目")
    for field, value in req.model_dump(exclude_none=True).items():
        setattr(p, field, value)
    db.commit()
    return _project_dict(p)


@router.delete("/projects/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db)):
    p = _get_or_404(db, TestProject, project_id, "测试项目")
    db.delete(p)
    db.commit()
    return {"ok": True}


@router.get("/projects/{project_id}/assets")
def project_assets(project_id: int, db: Session = Depends(get_db)):
    p = _get_or_404(db, TestProject, project_id, "测试项目")
    return [
        {"id": a.id, "rel_path": a.rel_path, "file_kind": a.file_kind, "size_bytes": a.size_bytes,
         "sha256": a.sha256, "storage_path": a.storage_path}
        for a in p.assets
    ]


# ---------- 资产预览 / 用系统默认软件打开 ----------

_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico"}
_PREVIEW_LIMIT = 200 * 1024  # 文本预览最多 200KB


def _asset_abs_path(asset: ImportedAsset) -> Path:
    if asset.storage_path:
        root = DATA_DIR.resolve()
        p = (root / asset.storage_path).resolve()
        if p != root and root not in p.parents:
            raise HTTPException(400, "资产路径越界，已拒绝")
        if not p.is_file():
            raise HTTPException(404, "服务器归档文件不存在")
        return p
    root = Path(os.environ.get("MT_LEGACY_ROOT", DEFAULT_LEGACY_ROOT)).resolve()
    p = (root / asset.rel_path).resolve()
    if root not in p.parents and p != root:
        raise HTTPException(400, "路径越界，已拒绝")
    if not p.is_file():
        raise HTTPException(404, f"文件不存在（可能已被移动）：{p}")
    return p


def _resolve_under_root(rel: str, root_str: str = "") -> Path:
    root = Path(root_str.strip() or os.environ.get("MT_LEGACY_ROOT", DEFAULT_LEGACY_ROOT)).resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise HTTPException(400, "路径越界，已拒绝")
    if not p.is_file():
        raise HTTPException(404, f"文件不存在：{p}")
    return p


def _preview_payload(p: Path) -> dict:
    ext = p.suffix.lower().lstrip(".")
    if ext in _IMAGE_EXTS:
        import base64
        data = p.read_bytes()
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(413, "图片超过 10MB，请用「打开」查看")
        mime = "image/svg+xml" if ext == "svg" else f"image/{'jpeg' if ext == 'jpg' else ext}"
        return {"kind": "image", "name": p.name, "ext": ext,
                "data": f"data:{mime};base64,{base64.b64encode(data).decode()}"}
    size = p.stat().st_size
    with p.open("rb") as handle:
        probe = handle.read(4096)
        if b"\x00" in probe:  # 二进制
            return {"kind": "binary", "name": p.name, "size": size}
        raw = probe + handle.read(_PREVIEW_LIMIT + 1 - len(probe))
    text = raw.decode("utf-8", errors="replace")
    return {"kind": "html" if ext in ("html", "htm") else "text", "name": p.name, "ext": ext, "size": size,
            "truncated": size > _PREVIEW_LIMIT, "content": text[:_PREVIEW_LIMIT]}


def _open_with_default(p: Path) -> None:
    import subprocess
    import sys
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        elif os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as e:  # pragma: no cover
        raise HTTPException(500, f"打开失败：{e}")


@router.get("/assets/{asset_id}/preview")
def asset_preview(asset_id: int, db: Session = Depends(get_db)):
    a = db.get(ImportedAsset, asset_id)
    if not a:
        raise HTTPException(404, "资产不存在")
    return _preview_payload(_asset_abs_path(a))


@router.get("/assets/{asset_id}/download")
def asset_download(asset_id: int, db: Session = Depends(get_db)):
    """浏览器下载服务器归档原文件；不会在服务器本机调用默认应用。"""
    a = db.get(ImportedAsset, asset_id)
    if not a:
        raise HTTPException(404, "资产不存在")
    return FileResponse(_asset_abs_path(a), filename=a.rel_path or f"asset-{a.id}")


@router.post("/assets/{asset_id}/open")
def asset_open(asset_id: int, db: Session = Depends(get_db)):
    """用系统默认软件打开文件（macOS open / Windows os.startfile / Linux xdg-open）。"""
    a = db.get(ImportedAsset, asset_id)
    if not a:
        raise HTTPException(404, "资产不存在")
    p = _asset_abs_path(a)
    _open_with_default(p)
    return {"ok": True, "path": str(p), "opened_with": "系统默认应用"}


# 按相对路径预览 / 打开（用于导入扫描、尚未登记的目录内容）
@router.get("/files/preview")
def file_preview(path: str, root: str = ""):
    return _preview_payload(_resolve_under_root(path, root))


@router.post("/files/open")
def file_open(path: str, root: str = ""):
    p = _resolve_under_root(path, root)
    _open_with_default(p)
    return {"ok": True, "path": str(p), "opened_with": "系统默认应用"}


@router.get("/files/download")
def file_download(path: str, root: str = ""):
    p = _resolve_under_root(path, root)
    return FileResponse(p, filename=p.name)


@router.post("/import/item-files")
def import_item_files(req: ItemFilesRequest):
    """列出扫描项（目录/文件）内的全部文件，供「内容」弹框预览/打开。"""
    root = Path(req.root.strip() or os.environ.get("MT_LEGACY_ROOT", DEFAULT_LEGACY_ROOT))
    if not root.is_dir():
        raise HTTPException(400, f"目录不存在：{root}")
    rootr = root.resolve()
    target = (rootr / req.name).resolve()
    if target != rootr and rootr not in target.parents:
        raise HTTPException(400, "路径越界，已拒绝")
    if not target.exists():
        raise HTTPException(404, f"目录或文件不存在：{target}")
    files = [f for f in (target.rglob("*") if target.is_dir() else [target])
             if f.is_file() and not f.name.startswith(".")]
    files.sort(key=lambda f: str(f.relative_to(rootr)))
    truncated = len(files) > 2000
    return {
        "name": req.name,
        "total": len(files),
        "truncated": truncated,
        "files": [
            {
                "rel_path": str(f.relative_to(rootr)),
                "kind": _KIND_BY_EXT.get(f.suffix.lower(), "other"),
                "size_bytes": f.stat().st_size,
            }
            for f in files[:2000]
        ],
    }




# ---------- 旧资料导入（只读扫描，不移动不复制） ----------

@router.post("/import/scan")
def import_scan(req: ImportScanRequest, db: Session = Depends(get_db)):
    root = Path(req.root.strip() or os.environ.get("MT_LEGACY_ROOT", DEFAULT_LEGACY_ROOT))
    if not root.is_dir():
        raise HTTPException(400, f"目录不存在：{root}")
    existing = {p.name for p in db.scalars(select(TestProject).where(TestProject.source == "imported")).all()}
    items = []
    for entry in sorted(root.iterdir()):
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.is_dir():
            files = [f for f in entry.rglob("*") if f.is_file() and not f.name.startswith(".")]
            if not files:
                continue
            total = sum(f.stat().st_size for f in files)
            kinds: dict[str, int] = {}
            for f in files:
                k = _KIND_BY_EXT.get(f.suffix.lower(), "other")
                kinds[k] = kinds.get(k, 0) + 1
            items.append({
                "name": entry.name,
                "type": "dir",
                "file_count": len(files),
                "size_bytes": total,
                "kinds": kinds,
                "category": _guess_category(entry.name),
                "suggested_models": _guess_model_names(entry.name),
                "imported": entry.name in existing,
            })
        elif entry.is_file() and entry.suffix.lower() in _ASSET_EXTS:
            items.append({
                "name": entry.name,
                "type": "file",
                "file_count": 1,
                "size_bytes": entry.stat().st_size,
                "kinds": {_KIND_BY_EXT.get(entry.suffix.lower(), "other"): 1},
                "category": _guess_category(entry.name),
                "suggested_models": [],
                "imported": False,
            })
    return {"root": str(root), "items": items}


@router.post("/import/execute")
def import_execute(req: ImportExecuteRequest, db: Session = Depends(get_db)):
    root = Path(req.root.strip() or os.environ.get("MT_LEGACY_ROOT", DEFAULT_LEGACY_ROOT))
    if not root.is_dir():
        raise HTTPException(400, f"目录不存在：{root}")
    selected = set(req.folders)
    created, skipped, assets_added = [], [], 0
    for entry in sorted(root.iterdir()):
        if entry.name not in selected:
            continue
        if db.scalar(select(TestProject).where(TestProject.name == entry.name)):
            skipped.append(entry.name)
            continue
        category = _guess_category(entry.name)
        project = TestProject(
            name=entry.name,
            category=category,
            source="imported",
            enabled=False,
            description=f"从旧资料目录导入（{category}）：{entry.name}",
        )
        db.add(project)
        db.flush()
        files = (
            [f for f in entry.rglob("*") if f.is_file() and not f.name.startswith(".")]
            if entry.is_dir() else [entry]
        )
        for f in files:
            ext = f.suffix.lower()
            if entry.is_dir() and ext not in _ASSET_EXTS:
                continue
            db.add(ImportedAsset(
                project_id=project.id,
                rel_path=str(f.relative_to(root)),
                file_kind=_KIND_BY_EXT.get(ext, "other"),
                size_bytes=f.stat().st_size,
            ))
            assets_added += 1
        created.append(entry.name)
    db.commit()
    return {"created": created, "skipped": skipped, "assets_added": assets_added, "root": str(root)}


# ---------- 本地文件系统浏览（文件夹选择器） ----------

@router.get("/fs/browse")
def fs_browse(path: str = ""):
    """列出指定目录的子目录，供前端文件夹选择器使用（服务仅监听 127.0.0.1）。"""
    target = Path(path.strip()).expanduser() if path.strip() else Path.home()
    if not target.is_dir():
        raise HTTPException(400, f"目录不存在：{target}")
    target = target.resolve()
    dirs = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                dirs.append(entry.name)
    except PermissionError:
        pass
    return {
        "path": str(target),
        "parent": str(target.parent) if target.parent != target else "",
        "home": str(Path.home()),
        "dirs": dirs,
    }


# ---------- 阶段 3：执行与证据 ----------

def _resolve_target(db: Session, project: TestProject) -> tuple[Provider, ModelEntity] | None:
    """执行前检查：返回 (provider, model) 或 None（并写入原因由调用方处理）。"""
    if not project.provider_ref or project.provider_ref.protocol != "openai":
        return None
    model = project.model_ref
    if model is None:
        model = next(iter(project.provider_ref.models), None)
    if model is None:
        return None
    return project.provider_ref, model


@router.post("/preview")
def preview_requests(req: PreviewRequest, db: Session = Depends(get_db)):
    """预览模式：展示将要发送的请求（Header 脱敏），不发送。"""
    plans = []
    for pid in req.project_ids:
        project = _get_or_404(db, TestProject, pid, "测试项目")
        target = _resolve_target(db, project)
        if target is None:
            plans.append({"project_id": pid, "project_name": project.name, "error": "项目未关联可执行的中转站/模型（需 OpenAI 兼容协议）"})
            continue
        provider, model = target
        plans.append(runner_mod.build_request_plan(project, provider, model.model_name))
    return {"plans": plans}


@router.post("/execute")
def execute_projects(req: ExecuteRequest, db: Session = Depends(get_db)):
    """真实执行：必须显式确认（confirmed=True），产生付费请求。"""
    if not req.project_ids:
        raise HTTPException(400, "未选择测试项目")
    if not req.confirmed:
        raise HTTPException(400, "真实付费请求需要显式确认（confirmed=true）")
    for pid in req.project_ids:
        _get_or_404(db, TestProject, pid, "测试项目")
    job_id = job_manager.start(req.project_ids)
    return {"job_id": job_id}


@router.get("/projects/{project_id}/scripts")
def project_scripts(project_id: int, db: Session = Depends(get_db)):
    """列出脚本执行模式下项目文件夹内的脚本文件。"""
    p = _get_or_404(db, TestProject, project_id, "测试项目")
    if not p.script_dir:
        raise HTTPException(400, "该项目未设置脚本文件夹（请在编辑中把执行模式设为「脚本文件夹」并填写文件夹）")
    d = Path(p.script_dir).expanduser()
    if not d.is_dir():
        raise HTTPException(404, f"脚本文件夹不存在：{d}")
    files = []
    for entry in sorted(d.iterdir(), key=lambda e: e.name.lower()):
        if entry.name.startswith(".") or entry.is_dir():
            continue
        ext = entry.suffix.lower().lstrip(".")
        runnable = ext in {"py", "sh", "bash"} or os.access(entry, os.X_OK)
        st = entry.stat()
        files.append({
            "name": entry.name,
            "ext": ext,
            "size": st.st_size,
            "mtime": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "runnable": runnable,
        })
    return {"dir": str(d), "scripts": files}


@router.post("/execute-scripts")
def execute_scripts(req: ScriptExecuteRequest, db: Session = Depends(get_db)):
    """在服务器终端运行脚本文件夹内的脚本 / 自定义命令，实时输出并落证据。"""
    p = _get_or_404(db, TestProject, req.project_id, "测试项目")
    if p.exec_mode != "script" or not p.script_dir:
        raise HTTPException(400, "该项目不是「脚本文件夹」执行模式或未设置脚本文件夹")
    job_id = job_manager.start_scripts(req.project_id, req.script_names or [], req.command)
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
def job_status(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    db = SessionLocal()
    try:
        runs = []
        for rid in job["run_ids"]:
            r = db.get(Run, rid)
            if r:
                runs.append(_run_dict(r))
        return {
            "job_id": job_id,
            "status": job["status"],
            "lines": job["lines"],
            "runs": runs,
        }
    finally:
        db.close()


@router.post("/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    if not job_manager.cancel(job_id):
        raise HTTPException(404, "任务不存在")
    return {"ok": True}


def _run_dict(r: Run) -> dict:
    import json as _json
    try:
        summary = _json.loads(r.summary_json or "{}")
    except _json.JSONDecodeError:
        summary = {}
    return {
        "id": r.id,
        "project_id": r.project_id,
        "project_name": r.project_name,
        "provider_name": r.provider_name,
        "model_name": r.model_name,
        "job_id": r.job_id,
        "status": r.status,
        "http_status": r.http_status,
        "passed": r.passed,
        "duration_ms": r.duration_ms,
        "ttft_ms": r.ttft_ms,
        "error_class": r.error_class,
        "verdict": r.verdict,
        "summary": summary,
        "evidence_dir": r.evidence_dir,
        "started_at": str(r.started_at or ""),
        "finished_at": str(r.finished_at or ""),
    }


@router.get("/runs")
def list_runs(limit: int = 50, project_id: int | None = None, db: Session = Depends(get_db)):
    q = select(Run).order_by(Run.id.desc()).limit(min(limit, 200))
    if project_id:
        q = select(Run).where(Run.project_id == project_id).order_by(Run.id.desc()).limit(min(limit, 200))
    return [_run_dict(r) for r in db.scalars(q).all()]


_EVIDENCE_FILES = [
    "manifest.json", "request.json", "request_headers.json",
    "response.json", "response_headers.json", "stream.ndjson", "timing.json",
]


@router.get("/runs/{run_id}")
def run_detail(run_id: int):
    db = SessionLocal()
    try:
        r = db.get(Run, run_id)
        if r is None:
            raise HTTPException(404, "运行记录不存在")
        out = _run_dict(r)
        evidence = {}
        if r.evidence_dir:
            from pathlib import Path as _P
            d = _P(r.evidence_dir)
            if d.is_dir():
                for name in _EVIDENCE_FILES:
                    f = d / name
                    if f.is_file():
                        if name == "stream.ndjson":
                            evidence[name] = f.read_text(encoding="utf-8").splitlines()[:20]
                        else:
                            evidence[name] = f.read_text(encoding="utf-8")
                if json.loads(r.summary_json or "{}").get("imported"):
                    records = d / "records.json"
                    if records.is_file() and records.stat().st_size <= 2 * 1024 * 1024:
                        evidence["source_rows"] = json.loads(records.read_text(encoding="utf-8"))
        out["evidence"] = evidence
        return out
    finally:
        db.close()


# ---------- 模型对比（保留 /matrix API 兼容旧客户端） ----------

STANDARD_CATEGORIES = ["chat", "stream", "json", "tools"]
REVIEW_STATUSES = {"compatible", "partial", "unsupported", "unknown", "manual_review"}
_ERROR_CLASSES_INFRA = {"auth_error", "rate_limit", "server_error", "http_error"}


def _run_category(r: Run) -> str:
    import json as _json
    try:
        return _json.loads(r.summary_json or "{}").get("category") or "其他"
    except _json.JSONDecodeError:
        return "其他"


def _cell_status(runs: list[Run]) -> tuple[str, str]:
    """从同 (中转站, 模型, 分类) 的一组运行记录推断兼容性结论，返回 (状态, 依据)。"""
    imported = []
    for r in runs:
        try:
            metadata = json.loads(r.summary_json or "{}")
            summary = metadata.get("summary", {})
        except json.JSONDecodeError:
            metadata = {}
            summary = {}
        if metadata.get("imported") and summary:
            imported.append(summary)
    if imported:
        passed = sum(int(s.get("passed", 0)) for s in imported)
        failed = sum(int(s.get("failed", 0)) for s in imported)
        partial = sum(int(s.get("partial", 0)) for s in imported)
        review = sum(int(s.get("manual_review", 0)) for s in imported)
        total = passed + failed + partial + review
        if total and passed == total:
            return "compatible", f"{passed} 条源断言全部通过"
        if review == total:
            return "manual_review", f"{total} 条源断言待复核"
        if failed == total:
            return "unsupported", f"{failed} 条源断言全部失败"
        return "partial", f"通过 {passed} / 失败 {failed} / 部分 {partial} / 待复核 {review}"
    actionable = [r for r in runs if r.status in {"passed", "failed"}]
    deferred = [r for r in runs if r not in actionable]
    if not actionable and deferred:
        infra_errors = [r.error_class for r in deferred if r.error_class]
        if infra_errors and len(infra_errors) == len(deferred) and all(e in _ERROR_CLASSES_INFRA for e in infra_errors):
            return "unknown", "执行完成但只有鉴权/限流/服务端错误，无法判定兼容性"
        return "manual_review", "执行已完成，但没有可识别的业务断言"
    passed = sum(1 for r in actionable if r.passed)
    failed = len(actionable) - passed
    failed += len(deferred)
    if failed == 0:
        return "compatible", f"{passed} 次执行全部通过"
    if passed == 0:
        if all(r.error_class == "silent_failure" for r in runs):
            return "unsupported", "全部为假成功（HTTP 200 但功能未生效）"
        if all(r.error_class in _ERROR_CLASSES_INFRA for r in runs):
            return "unknown", "全部失败且均为鉴权/限流/服务端错误，无法判定兼容性"
        return "unknown", "全部失败"
    return "partial", f"{passed} 次通过 / {failed} 次失败"


@router.get("/matrix")
def matrix(db: Session = Depends(get_db)):
    """模型对比：行 = 模型×中转站，列 = 测试分类，格 = 结果结论 + 人工复核。"""
    runs = []
    for run in db.scalars(select(Run).where(Run.status.in_(["passed", "failed", "completed", "error", "cancelled", "interrupted"]))).all():
        try:
            if json.loads(run.summary_json or "{}").get("demo"):
                continue
        except json.JSONDecodeError:
            pass
        runs.append(run)
    overrides: dict[tuple[int, str, str], MatrixReview] = {
        (o.provider_id, o.model_name, o.category): o for o in db.scalars(select(MatrixReview))
    }

    # 按 (中转站, 模型, 分类) 分组
    groups: dict[tuple[int, str, str], list[Run]] = {}
    for r in runs:
        groups.setdefault((r.provider_id or 0, r.model_name, _run_category(r)), []).append(r)

    # 分类列：标准四类在前，自定义分类在后
    cats = {g[2] for g in groups}
    categories = [c for c in STANDARD_CATEGORIES if c in cats] + sorted(cats - set(STANDARD_CATEGORIES))

    # 行来源：已登记模型 + 有运行记录但未登记的组合（运行历史仍可追溯）
    providers = {p.id: p.name for p in db.scalars(select(Provider))}
    row_names: dict[tuple[str, int], str] = {}
    for m in db.scalars(select(ModelEntity)):
        row_names.setdefault((m.model_name, m.provider_id), providers.get(m.provider_id, f"#{m.provider_id}"))
    for r in runs:
        row_names.setdefault((r.model_name, r.provider_id or 0), r.provider_name or f"#{r.provider_id or 0}")

    rows = []
    for (model_name, pid) in sorted(row_names, key=lambda k: (k[0], row_names[k])):
        cells = {}
        for cat in categories:
            rs = groups.get((pid, model_name, cat), [])
            o = overrides.get((pid, model_name, cat))
            if rs:
                base, basis = _cell_status(rs)
                last = max(rs, key=lambda r: r.id)
                cells[cat] = {
                    "status": base, "basis": basis,
                    "runs": len(rs), "passed": sum(1 for r in rs if r.passed),
                    "last_run_id": last.id, "last_run_at": str(last.started_at or ""),
                    "last_verdict": last.verdict or "",
                }
            else:
                cells[cat] = {"status": "untested", "basis": "未测试", "runs": 0, "passed": 0}
            cells[cat]["effective_status"] = cells[cat]["status"]
            if o is not None:
                cells[cat]["override_status"] = o.override_status
                cells[cat]["note"] = o.note
                cells[cat]["reviewed_at"] = str(o.updated_at or "")
                if o.override_status:
                    cells[cat]["effective_status"] = o.override_status
        rows.append({
            "model_name": model_name, "provider_id": pid,
            "provider_name": row_names[(model_name, pid)], "cells": cells,
        })

    summary = {"compatible": 0, "partial": 0, "unsupported": 0, "unknown": 0, "untested": 0, "manual_review": 0}
    for row in rows:
        for cat in categories:
            s = row["cells"][cat].get("effective_status")
            if s in summary:
                summary[s] += 1

    return {"categories": categories, "rows": rows, "summary": summary}


@router.get("/matrix/detail")
def matrix_detail(provider_id: int, model_name: str, category: str, db: Session = Depends(get_db)):
    """矩阵单元格明细：该 (中转站, 模型, 分类) 的全部运行记录 + 当前复核。"""
    rs = [
        r for r in db.scalars(
            select(Run).where(Run.provider_id == provider_id, Run.model_name == model_name)
        ).all() if _run_category(r) == category
    ]
    rs.sort(key=lambda r: r.id, reverse=True)
    o = db.scalar(select(MatrixReview).where(
        MatrixReview.provider_id == provider_id,
        MatrixReview.model_name == model_name,
        MatrixReview.category == category,
    ))
    return {
        "runs": [_run_dict(r) for r in rs],
        "review": ({"override_status": o.override_status, "note": o.note, "updated_at": str(o.updated_at or "")} if o else None),
    }


@router.post("/matrix/review")
def set_matrix_review(req: MatrixReviewRequest, db: Session = Depends(get_db)):
    """保存/清除单元格的人工复核结论（覆盖自动聚合，原始运行记录不变）。"""
    if req.override_status and req.override_status not in REVIEW_STATUSES:
        raise HTTPException(400, "无效的复核状态")
    o = db.scalar(select(MatrixReview).where(
        MatrixReview.provider_id == req.provider_id,
        MatrixReview.model_name == req.model_name,
        MatrixReview.category == req.category,
    ))
    if not req.override_status and not req.note:
        if o is not None:
            db.delete(o)
            db.commit()
        return {"ok": True, "cleared": True}
    if o is None:
        o = MatrixReview(provider_id=req.provider_id, model_name=req.model_name, category=req.category)
        db.add(o)
    o.override_status = req.override_status
    o.note = req.note
    db.commit()
    return {"ok": True, "override_status": o.override_status, "note": o.note}

"""Bounded, streaming uploads of browser-selected folders."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import threading
import uuid
import zipfile

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .db import DATA_DIR
from .script_catalog import discover_scripts

router = APIRouter(prefix="/api/files")
MAX_ARCHIVE = 120 * 1024 * 1024
MAX_TOTAL = 100 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_FILES = 2000
_publish_lock = threading.Lock()


def _matches(directory: Path, manifest: list[tuple[str, int, str]]) -> bool:
    for relative, size, digest in manifest:
        path = directory / relative
        if path.is_symlink() or any((directory / parent).is_symlink() for parent in PurePosixPath(relative).parents):
            return False
        if not path.is_file() or path.stat().st_size != size:
            return False
        with path.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != digest:
                return False
    return True


def unpack_folder(archive: Path, storage: Path, archive_upload: bool = False) -> dict:
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=storage) as temporary:
        stage = Path(temporary) / "content"
        stage.mkdir()
        manifest = []
        seen = set()
        roots = set()
        total = 0
        try:
            with zipfile.ZipFile(archive) as bundle:
                if len(bundle.infolist()) > MAX_FILES * 2:
                    raise HTTPException(413, "文件夹最多支持 2000 个文件")
                for item in bundle.infolist():
                    name = item.filename.rstrip("/")
                    parts = name.split("/")
                    kind = stat.S_IFMT(item.external_attr >> 16)
                    if (not name or "\\" in name or any(part in ("", ".", "..") or ":" in part for part in parts)
                            or any(ord(char) < 32 for char in name) or len(name.encode("utf-8")) > 2048
                            or kind not in (0, stat.S_IFREG, stat.S_IFDIR)):
                        raise HTTPException(400, "文件夹中包含不支持的路径或链接")
                    if archive_upload:
                        parts = ["uploaded-scripts", *parts]
                        name = "/".join(parts)
                    relative = PurePosixPath(name).as_posix()
                    roots.add(parts[0])
                    if item.is_dir():
                        continue
                    if len(parts) < 2 or relative in seen:
                        raise HTTPException(400, "文件夹结构不完整或文件路径重复")
                    seen.add(relative)
                    total += item.file_size
                    if len(seen) > MAX_FILES or total > MAX_TOTAL or item.file_size > MAX_FILE:
                        raise HTTPException(413, "文件夹限 2000 个文件、合计 100 MB，单文件不超过 32 MB")
                    target = stage / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    written = 0
                    with bundle.open(item) as source, target.open("xb") as destination:
                        while chunk := source.read(64 * 1024):
                            written += len(chunk)
                            if written > item.file_size or written > MAX_FILE:
                                raise HTTPException(413, "文件大小超过限制")
                            digest.update(chunk)
                            destination.write(chunk)
                    target.chmod(0o750 if target.suffix.lower() in (".py", ".sh", ".bash") or (item.external_attr >> 16) & 0o111 else 0o640)
                    manifest.append((relative, written, digest.hexdigest()))
            if not manifest or len(roots) != 1:
                raise HTTPException(400, "请选择一个包含文件的文件夹")
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, ValueError) as error:
            raise HTTPException(400, "无法读取文件夹，请重新选择后上传") from error
        digest = hashlib.sha256(json.dumps(sorted(manifest), ensure_ascii=False).encode()).hexdigest()
        destination = storage / digest
        reused = False
        with _publish_lock:
            if destination.exists():
                reused = _matches(destination, manifest)
                if not reused:
                    destination = storage / (digest + "-" + uuid.uuid4().hex[:12])
            if not reused:
                stage.rename(destination)
        root = next(iter(roots))
        return {"path": str(destination / root), "name": root, "size_bytes": total, "reused": reused,
                **discover_scripts(destination / root)}


@router.post("/upload-folder")
async def upload_folder(request: Request, archive: bool = False):
    storage = DATA_DIR / "script_uploads"
    storage.mkdir(exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".upload-", suffix=".zip", dir=storage)
    archive_path = Path(filename)
    try:
        total = 0
        with os.fdopen(descriptor, "wb") as destination:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_ARCHIVE:
                    raise HTTPException(413, "上传文件夹过大，合计不能超过 100 MB")
                destination.write(chunk)
        return await run_in_threadpool(unpack_folder, archive_path, storage, archive)
    finally:
        archive_path.unlink(missing_ok=True)

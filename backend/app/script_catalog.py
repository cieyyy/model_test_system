"""Shared discovery for uploaded test scripts and the execution queue."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import shlex
import shutil
import sys

IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages"}
EXTENSIONS = {".py": "python", ".pyw": "python", ".sh": "sh", ".bash": "bash",
              ".js": "node", ".mjs": "node", ".cjs": "node"}


def interpreter(path: Path) -> str | None:
    known = EXTENSIONS.get(path.suffix.lower())
    if known and known != "sh":
        return known
    if path.suffix and not known:
        return None
    with path.open("rb") as source:
        first = source.readline(512).decode("utf-8", errors="replace").strip()
    if not first.startswith("#!"):
        return known
    try:
        words = shlex.split(first[2:])
    except ValueError:
        return known
    if not words:
        return known
    name = Path(words[0]).name
    if name == "env":
        name = next((word for word in words[1:] if not word.startswith("-") and "=" not in word), "")
    if name.startswith("python"):
        return "python"
    return {"sh": "sh", "bash": "bash", "node": "node"}.get(name, known)


def runtime(kind: str) -> str | None:
    return sys.executable if kind == "python" else shutil.which(kind)


def discover_scripts(directory: Path) -> dict:
    directory = directory.resolve()
    scripts = []
    total_files = 0
    for current, folders, files in os.walk(directory, followlinks=False):
        folders[:] = sorted(name for name in folders if not name.startswith(".") and name not in IGNORED_DIRS
                            and not (Path(current) / name).is_symlink())
        for name in sorted(files):
            path = Path(current) / name
            if name.startswith(".") or path.is_symlink() or not path.is_file():
                continue
            total_files += 1
            kind = interpreter(path)
            if not kind:
                continue
            executable = runtime(kind)
            support_module = name.lower() == "__init__.py"
            info = path.stat()
            scripts.append({"name": path.relative_to(directory).as_posix(), "ext": path.suffix.lower().lstrip(".") or "shebang",
                            "language": {"python": "Python", "sh": "Shell", "bash": "Bash", "node": "JavaScript"}[kind],
                            "interpreter": kind, "size": info.st_size,
                            "mtime": dt.datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M"),
                            "runnable": bool(executable) and not support_module,
                            "reason": "Python 包初始化文件" if support_module else ("" if executable else f"服务器未安装 {kind}")})
    scripts.sort(key=lambda item: item["name"].casefold())
    return {"scripts": scripts, "script_count": len(scripts), "runnable_count": sum(item["runnable"] for item in scripts),
            "other_count": total_files - len(scripts), "file_count": total_files}


def script_command(directory: Path, script: dict) -> tuple[str, list[str]]:
    path = directory.resolve() / script["name"]
    executable = runtime(script["interpreter"])
    if not executable or not script["runnable"]:
        raise ValueError(script["reason"] or "脚本不可运行")
    return str(path.parent), [executable, str(path)]

"""Consistent backup, called only after maintenance mode and stopping the service."""
import shutil
import sqlite3
import sys
import os
from pathlib import Path

source, destination = map(Path, sys.argv[1:])
destination.mkdir(parents=True, exist_ok=False)


def immutable_copy(src, dst):
    """Use hardlinks for append-free evidence/object files on the same volume."""
    src, dst = Path(src), Path(dst)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


for item in source.iterdir():
    if item.name in {"app.db", "app.db-wal", "app.db-shm", "app.db-journal"}:
        continue
    target = destination / item.name
    if item.is_dir():
        copy_function = immutable_copy if item.name in {"import_objects", "suite_runs"} else shutil.copy2
        shutil.copytree(item, target, copy_function=copy_function)
    else:
        shutil.copy2(item, target)
if (source / "app.db").exists():
    with sqlite3.connect(f"file:{source / 'app.db'}?mode=ro", uri=True) as src:
        with sqlite3.connect(destination / "app.db") as dst:
            src.backup(dst)
            assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
print(destination)

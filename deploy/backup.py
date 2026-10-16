"""Consistent backup, called only after maintenance mode and stopping the service."""
import shutil
import sqlite3
import sys
from pathlib import Path

source, destination = map(Path, sys.argv[1:])
destination.mkdir(parents=True, exist_ok=False)
for item in source.iterdir():
    if item.name in {"app.db", "app.db-wal", "app.db-shm", "app.db-journal"}:
        continue
    target = destination / item.name
    if item.is_dir():
        shutil.copytree(item, target)
    else:
        shutil.copy2(item, target)
if (source / "app.db").exists():
    with sqlite3.connect(f"file:{source / 'app.db'}?mode=ro", uri=True) as src:
        with sqlite3.connect(destination / "app.db") as dst:
            src.backup(dst)
            assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
print(destination)

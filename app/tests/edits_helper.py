"""Apply demo edit JSON onto a copy of the GatePass source tree."""

from __future__ import annotations

import shutil
from pathlib import Path


def apply_v2_bugs(app_root: Path, dest: Path, edits: list[dict]) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in ("app.py", "catalog.py", "seed.py", "requirements.txt", "Dockerfile"):
        src = app_root / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    for edit in edits:
        path = dest / edit["file"]
        text = path.read_text(encoding="utf-8")
        search = edit["search"]
        if text.count(search) != 1:
            raise AssertionError(
                f"{edit['file']}: search matched {text.count(search)} times"
            )
        path.write_text(text.replace(search, edit["replace"], 1), encoding="utf-8")

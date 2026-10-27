"""Apply search/replace edits to a release source tree."""

from __future__ import annotations

import ast
import py_compile
import tempfile
from pathlib import Path
from typing import Any


def validate_edit(edit: dict) -> str | None:
    if not isinstance(edit, dict):
        return "edit must be an object"
    path = str(edit.get("file") or "").strip()
    search = edit.get("search")
    replace = edit.get("replace")
    if not path or path.startswith("/") or ".." in Path(path).parts:
        return f"invalid file path: {path!r}"
    if not isinstance(search, str) or not search:
        return f"{path}: search must be a non-empty string"
    if not isinstance(replace, str):
        return f"{path}: replace must be a string"
    return None


def apply_edits(src_root: Path, edits: list[dict]) -> tuple[list[str], list[str]]:
    """Apply edits in place. Returns (changed_files, errors)."""
    errors: list[str] = []
    changed: list[str] = []
    for edit in edits:
        msg = validate_edit(edit)
        if msg:
            errors.append(msg)
            continue
        rel = str(edit["file"]).strip().lstrip("./")
        target = (src_root / rel).resolve()
        try:
            target.relative_to(src_root.resolve())
        except ValueError:
            errors.append(f"{rel}: escapes source root")
            continue
        if not target.is_file():
            errors.append(f"{rel}: file not found")
            continue
        text = target.read_text(encoding="utf-8")
        search = edit["search"]
        count = text.count(search)
        if count == 0:
            errors.append(f"{rel}: search text not found")
            continue
        if count > 1:
            errors.append(f"{rel}: search text matched {count} times (need exactly 1)")
            continue
        new_text = text.replace(search, edit["replace"], 1)
        target.write_text(new_text, encoding="utf-8")
        changed.append(rel)

    for rel in changed:
        if not rel.endswith(".py"):
            continue
        path = src_root / rel
        try:
            py_compile.compile(str(path), doraise=True)
            ast.parse(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{rel}: python compile failed: {exc}")
    return changed, errors


def copy_tree(src: Path, dest: Path) -> None:
    import shutil

    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        src,
        dest,
        ignore=shutil.ignore_patterns("._*", "__pycache__", "*.pyc", ".pytest_cache"),
    )


def materialize_from_base(
    base_src: Path,
    dest: Path,
    edits: list[dict],
) -> tuple[list[str], list[str]]:
    """Copy base source, apply edits, return (changed, errors)."""
    copy_tree(base_src, dest)
    return apply_edits(dest, edits)


def edits_to_unified_diff(src_before: Path, src_after: Path) -> str:
    import difflib

    lines: list[str] = []
    before_files = {
        p.relative_to(src_before).as_posix()
        for p in src_before.rglob("*")
        if p.is_file() and "._" not in p.name
    }
    after_files = {
        p.relative_to(src_after).as_posix()
        for p in src_after.rglob("*")
        if p.is_file() and "._" not in p.name
    }
    for rel in sorted(before_files | after_files):
        left = src_before / rel
        right = src_after / rel
        left_text = left.read_text(encoding="utf-8", errors="replace").splitlines(True) if left.is_file() else []
        right_text = right.read_text(encoding="utf-8", errors="replace").splitlines(True) if right.is_file() else []
        if left_text == right_text:
            continue
        diff = difflib.unified_diff(
            left_text,
            right_text,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
        lines.extend(diff)
    return "".join(lines)


def normalize_edits(payload: Any) -> list[dict]:
    if payload is None:
        return []
    if isinstance(payload, dict) and "edits" in payload:
        payload = payload["edits"]
    if not isinstance(payload, list):
        raise ValueError("edits must be a list")
    out: list[dict] = []
    for item in payload:
        err = validate_edit(item)
        if err:
            raise ValueError(err)
        out.append(
            {
                "file": str(item["file"]).strip().lstrip("./"),
                "search": item["search"],
                "replace": item["replace"],
            }
        )
    return out

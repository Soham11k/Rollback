"""Apply search/replace edits to a release source tree."""

from __future__ import annotations

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


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _trim_blank(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def align_replace(text: str, index: int, replace: str) -> str:
    """Indent the later lines of a multi-line replace written at column 0.

    A search copied without its leading indentation still matches after the indent;
    the first replace line inherits that indent, so the later lines need it too.
    """
    lead = text[text.rfind("\n", 0, index) + 1 : index]
    lines = replace.split("\n")
    if not lead or lead.strip() or len(lines) < 2:
        return replace
    rest = [line for line in lines[1:] if line.strip()]
    if not rest or min(len(_indent(line)) for line in rest) >= len(lead):
        return replace
    return "\n".join(lines[:1] + [lead + line if line.strip() else line for line in lines[1:]])


def reindent_match(text: str, search: str, replace: str) -> tuple[str | None, int]:
    """Find search in text when its indentation was shifted, as models often do.

    Matches either the whole block shifted by one constant prefix, or a block whose
    first line alone lost its indentation. Returns (new_text, matches); new_text is
    None unless exactly one block matches.
    """
    lines = text.splitlines(keepends=True)
    bare = [line.rstrip("\r\n").rstrip() for line in lines]
    wanted = [line.rstrip() for line in _trim_blank(search.splitlines())]
    if not wanted:
        return None, 0
    base = min(len(_indent(w)) for w in wanted if w.strip())
    rel = [w[base:] if w.strip() else "" for w in wanted]
    size = len(rel)

    found: list[tuple[int, str, str]] = []
    for start in range(len(bare) - size + 1):
        window = bare[start : start + size]
        first = window[0]
        if first.endswith(rel[0]):
            prefix = first[: len(first) - len(rel[0])]
            if not prefix.strip() and all(
                (not want and not have.strip()) or (want and have == prefix + want)
                for want, have in zip(rel, window)
            ):
                found.append((start, prefix, "shift"))
                continue
        if wanted[0].strip() == first.strip() and wanted[1:] == window[1:]:
            found.append((start, _indent(first), "first"))
    if len(found) != 1:
        return None, len(found)

    start, prefix, strategy = found[0]
    replace_lines = replace.splitlines()
    if strategy == "shift":
        block = [
            ""
            if not line.strip()
            else prefix + (line[base:] if not line[:base].strip() else line.lstrip())
            for line in replace_lines
        ]
    else:
        block = ([prefix + replace_lines[0].lstrip()] if replace_lines else []) + replace_lines[1:]
    end = start + size
    newline = "\n" if lines[end - 1].endswith("\n") else ""
    block_text = "\n".join(block) + (newline if block else "")
    return "".join(lines[:start]) + block_text + "".join(lines[end:]), 1


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
        if count > 1:
            errors.append(f"{rel}: search text matched {count} times (need exactly 1)")
            continue
        if count == 1:
            index = text.index(search)
            new_text = text[:index] + align_replace(text, index, edit["replace"]) + text[index + len(search) :]
        else:
            new_text, matches = reindent_match(text, search, edit["replace"])
            if new_text is None:
                errors.append(
                    f"{rel}: search text not found"
                    if matches == 0
                    else f"{rel}: search text matched {matches} places ignoring indentation (need exactly 1)"
                )
                continue
        target.write_text(new_text, encoding="utf-8")
        if rel not in changed:
            changed.append(rel)

    for rel in changed:
        if not rel.endswith(".py"):
            continue
        path = src_root / rel
        try:
            compile(path.read_text(encoding="utf-8"), rel, "exec")
        except SyntaxError as exc:
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


def _text_files(root: Path) -> set[str]:
    files = set()
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if not path.is_file() or path.name.startswith("._") or path.suffix in {".pyc", ".db", ".sqlite"}:
            continue
        if "__pycache__" in rel.parts or ".pytest_cache" in rel.parts:
            continue
        files.add(rel.as_posix())
    return files


def _read_lines(path: Path) -> list[str] | None:
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines(True)
    except UnicodeDecodeError:
        return None


def edits_to_unified_diff(src_before: Path, src_after: Path) -> str:
    import difflib

    lines: list[str] = []
    for rel in sorted(_text_files(src_before) | _text_files(src_after)):
        left_text = _read_lines(src_before / rel)
        right_text = _read_lines(src_after / rel)
        if left_text is None or right_text is None:
            continue
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

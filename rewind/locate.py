"""Bug report: crossed signals, traceback frames, diff suspects."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from diagnosis import diagnose_incident
from registry import Registry

_TRACE_FILE = re.compile(
    r'File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>\S+))?'
)
_TRACE_EXC = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(: (?P<msg>.*))?$")


def parse_tracebacks(logs: str) -> list[dict]:
    """Extract Python traceback frames from docker logs."""
    if not logs:
        return []
    lines = logs.splitlines()
    frames: list[dict] = []
    i = 0
    while i < len(lines):
        if "Traceback (most recent call last):" not in lines[i]:
            i += 1
            continue
        i += 1
        stack: list[dict] = []
        while i < len(lines):
            match = _TRACE_FILE.search(lines[i])
            if match:
                file_path = match.group("file")
                # Prefer basename for matching release source.
                base = Path(file_path).name
                func = match.group("func") or ""
                line_no = int(match.group("line"))
                snippet = ""
                if i + 1 < len(lines) and not lines[i + 1].lstrip().startswith("File "):
                    # Often the next line is the source snippet.
                    if not _TRACE_EXC.match(lines[i + 1].strip()):
                        snippet = lines[i + 1].rstrip()
                        i += 1
                stack.append(
                    {
                        "file": base,
                        "path": file_path,
                        "line": line_no,
                        "function": func,
                        "snippet": snippet.strip(),
                    }
                )
                i += 1
                continue
            exc = _TRACE_EXC.match(lines[i].strip())
            if exc and stack:
                frames.append(
                    {
                        "exception": exc.group("type"),
                        "message": (exc.group("msg") or "").strip(),
                        "frames": stack,
                        "top": stack[-1],
                    }
                )
                i += 1
                break
            if lines[i].strip() == "":
                i += 1
                break
            i += 1
    # Collapse duplicates by (file, line, exception).
    seen: dict[tuple, dict] = {}
    for frame in frames:
        top = frame.get("top") or {}
        key = (top.get("file"), top.get("line"), frame.get("exception"))
        if key in seen:
            seen[key]["count"] += 1
        else:
            row = dict(frame)
            row["count"] = 1
            seen[key] = row
    return list(seen.values())


def error_tail(logs: str, limit: int = 12) -> list[str]:
    if not logs:
        return []
    interesting = []
    for line in logs.splitlines():
        lower = line.lower()
        if any(tok in lower for tok in ("error", "exception", "traceback", "critical", "failed")):
            interesting.append(line.rstrip())
    return interesting[-limit:]


def _read_text(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def diff_hunks(base_src: Path, bad_src: Path) -> list[dict]:
    """Unified hunks between two source trees."""
    if not base_src.is_dir() or not bad_src.is_dir():
        return []
    base_files = {
        p.relative_to(base_src).as_posix()
        for p in base_src.rglob("*")
        if p.is_file() and "._" not in p.name and "__pycache__" not in p.parts
    }
    bad_files = {
        p.relative_to(bad_src).as_posix()
        for p in bad_src.rglob("*")
        if p.is_file() and "._" not in p.name and "__pycache__" not in p.parts
    }
    hunks: list[dict] = []
    for rel in sorted(base_files | bad_files):
        left = _read_text(base_src / rel)
        right = _read_text(bad_src / rel)
        if left == right:
            continue
        matcher = difflib.SequenceMatcher(a=left, b=right)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            hunks.append(
                {
                    "file": rel,
                    "tag": tag,
                    "base_start": i1 + 1,
                    "base_end": i2,
                    "new_start": j1 + 1,
                    "new_end": j2,
                    "base_lines": left[i1:i2],
                    "new_lines": right[j1:j2],
                    "preview": "\n".join(right[j1:j2][:12]),
                }
            )
    return hunks


def rank_suspects(
    hunks: list[dict],
    tracebacks: list[dict],
) -> list[dict]:
    suspects: list[dict] = []
    claimed_hunks: set[int] = set()

    # 1) Traceback line inside a changed hunk.
    for tb in tracebacks:
        top = tb.get("top") or {}
        file_name = top.get("file") or ""
        line_no = int(top.get("line") or 0)
        for idx, hunk in enumerate(hunks):
            hunk_file = Path(hunk["file"]).name
            if hunk_file != file_name and hunk["file"] != file_name:
                # Also allow full relative match ending with file_name
                if not hunk["file"].endswith("/" + file_name) and hunk["file"] != file_name:
                    continue
            if hunk["new_start"] <= line_no <= max(hunk["new_end"], hunk["new_start"]):
                suspects.append(
                    {
                        "rank": 1,
                        "reason": (
                            f"{tb.get('exception')}: {tb.get('message')} "
                            f"at {hunk['file']}:{line_no}"
                        ),
                        "file": hunk["file"],
                        "line": line_no,
                        "function": top.get("function"),
                        "exception": tb.get("exception"),
                        "code": hunk.get("preview") or top.get("snippet") or "",
                        "from": "traceback+diff",
                    }
                )
                claimed_hunks.add(idx)
                break
        else:
            # Traceback without matching hunk still listed.
            if file_name:
                suspects.append(
                    {
                        "rank": 2,
                        "reason": (
                            f"{tb.get('exception')}: {tb.get('message')} "
                            f"in {file_name}:{line_no}"
                        ),
                        "file": file_name,
                        "line": line_no,
                        "function": top.get("function"),
                        "exception": tb.get("exception"),
                        "code": top.get("snippet") or "",
                        "from": "traceback",
                    }
                )

    # 2) Other changed hunks in files that appeared in a traceback.
    tb_files = {
        (tb.get("top") or {}).get("file")
        for tb in tracebacks
        if (tb.get("top") or {}).get("file")
    }
    for idx, hunk in enumerate(hunks):
        if idx in claimed_hunks:
            continue
        name = Path(hunk["file"]).name
        if name in tb_files or hunk["file"] in tb_files:
            suspects.append(
                {
                    "rank": 3,
                    "reason": f"Changed code in traceback file {hunk['file']}",
                    "file": hunk["file"],
                    "line": hunk["new_start"],
                    "function": None,
                    "exception": None,
                    "code": hunk.get("preview") or "",
                    "from": "diff-near-traceback",
                }
            )
            claimed_hunks.add(idx)

    # 3) Remaining changes.
    for idx, hunk in enumerate(hunks):
        if idx in claimed_hunks:
            continue
        suspects.append(
            {
                "rank": 4,
                "reason": f"Changed since best release in {hunk['file']}",
                "file": hunk["file"],
                "line": hunk["new_start"],
                "function": None,
                "exception": None,
                "code": hunk.get("preview") or "",
                "from": "diff",
            }
        )

    suspects.sort(key=lambda s: (s.get("rank", 99), s.get("file") or "", s.get("line") or 0))
    return suspects[:20]


def build_report(
    registry: Registry,
    app_id: str,
    version: str,
    *,
    incident: dict,
    logs: str,
) -> dict[str, Any]:
    diagnosis = diagnose_incident(incident)
    tracebacks = parse_tracebacks(logs)
    tails = error_tail(logs)

    best_version = None
    app = registry.get_app(app_id) or {}
    # Prefer previous best (not the bad version).
    for release in registry.list_releases(app_id):
        if release.get("version") == version:
            continue
        if release.get("decision") == "healthy" and not release.get("quarantined"):
            if best_version is None or float(release.get("score") or 0) >= float(
                (registry.get_release(app_id, best_version) or {}).get("score") or 0
            ):
                best_version = release.get("version")
    if not best_version:
        best_version = app.get("best_version")
        if best_version == version:
            best_version = None

    hunks: list[dict] = []
    has_source = registry.has_source(app_id, version)
    if has_source and best_version and registry.has_source(app_id, best_version):
        hunks = diff_hunks(
            registry.src_dir(app_id, best_version),
            registry.src_dir(app_id, version),
        )

    suspects = rank_suspects(hunks, tracebacks) if (hunks or tracebacks) else []
    if not suspects and not has_source:
        diagnosis_note = (
            "This release has no source tree. Signals and logs are available; "
            "diff and AI fix need a source publish."
        )
    else:
        diagnosis_note = diagnosis.get("summary") or ""

    return {
        "app_id": app_id,
        "version": version,
        "best_version": best_version,
        "has_source": has_source,
        "can_ai_fix": bool(has_source and best_version and registry.has_source(app_id, best_version)),
        "diagnosis": diagnosis,
        "summary": diagnosis_note,
        "tracebacks": tracebacks,
        "error_lines": tails,
        "hunks": [
            {
                "file": h["file"],
                "tag": h["tag"],
                "new_start": h["new_start"],
                "new_end": h["new_end"],
                "preview": h.get("preview"),
            }
            for h in hunks[:40]
        ],
        "suspects": suspects,
        "incident": {
            "decision": incident.get("decision"),
            "version": incident.get("version"),
            "confidence": incident.get("confidence"),
            "started_at": incident.get("started_at"),
            "detected_at": incident.get("detected_at"),
            "rolled_back_at": incident.get("rolled_back_at"),
            "time_to_detect_s": incident.get("time_to_detect_s"),
            "time_to_rollback_s": incident.get("time_to_rollback_s"),
            "peaks": incident.get("peaks"),
            "most_affected_path": incident.get("most_affected_path"),
        },
    }

"""Ask a local Ollama model for a fix; validate edits; ship as a new version."""

from __future__ import annotations

import ast
import difflib
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

import requests

from deployer import publish_release
from edits import apply_edits, copy_tree, edits_to_unified_diff, normalize_edits
from registry import Registry
from verify import Verifier


DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
DEFAULT_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))
FILE_CHAR_LIMIT = 20000
MAX_ATTEMPTS = 3

SYSTEM_PROMPT = (
    "You fix buggy application releases. You only change what is needed to remove "
    "the regressions. Reply with JSON only."
)


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ollama_reachable(base_url: str | None = None, timeout: float = 2.0) -> dict:
    url = (base_url or DEFAULT_URL).rstrip("/")
    try:
        response = requests.get(f"{url}/api/tags", timeout=timeout)
        if response.status_code != 200:
            return {"ok": False, "url": url, "message": f"status {response.status_code}", "model": DEFAULT_MODEL}
        models = [m.get("name") for m in (response.json().get("models") or [])]
        return {
            "ok": True,
            "url": url,
            "models": models,
            "has_model": any(
                (name or "") == DEFAULT_MODEL or (name or "") == f"{DEFAULT_MODEL}:latest"
                for name in models
            ),
            "model": DEFAULT_MODEL,
        }
    except requests.RequestException as exc:
        return {"ok": False, "url": url, "message": str(exc), "model": DEFAULT_MODEL}


def _changed_files(report: dict) -> list[str]:
    return sorted({h.get("file") for h in report.get("hunks") or [] if h.get("file")})


def _changed_file_contents(src: Path, rel_paths: list[str], limit: int = 3) -> str:
    chunks = []
    for rel in rel_paths[:limit]:
        path = src / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > FILE_CHAR_LIMIT:
            text = text[:FILE_CHAR_LIMIT] + "\n... truncated ...\n"
        chunks.append(f"----- {rel} -----\n{text}")
    return "\n\n".join(chunks)


def _unified_diff(base_src: Path, bad_src: Path, rel_paths: list[str], limit: int = 12000) -> str:
    out: list[str] = []
    for rel in rel_paths:
        before = base_src / rel
        after = bad_src / rel
        left = before.read_text(encoding="utf-8", errors="replace").splitlines(True) if before.is_file() else []
        right = after.read_text(encoding="utf-8", errors="replace").splitlines(True) if after.is_file() else []
        out.extend(difflib.unified_diff(left, right, fromfile=f"best/{rel}", tofile=f"bad/{rel}"))
    text = "".join(out)
    if len(text) > limit:
        text = text[:limit] + "\n... diff truncated ...\n"
    return text


def build_prompt(report: dict, bad_src: Path, base_src: Path) -> str:
    diagnosis = report.get("diagnosis") or {}
    bugs = diagnosis.get("bugs") or []
    suspects = report.get("suspects") or []
    tracebacks = report.get("tracebacks") or []

    bug_lines = [
        f"- {bug.get('signal')}: {bug.get('value'):.0f}{bug.get('unit')} "
        f"(limit {bug.get('limit'):.0f}{bug.get('unit')}) at {bug.get('location')}."
        for bug in bugs
    ]
    tb_lines = []
    for tb in tracebacks[:5]:
        top = tb.get("top") or {}
        tb_lines.append(
            f"- {tb.get('exception')}: {tb.get('message')} "
            f"in {top.get('file')}:{top.get('line')} ({tb.get('count')} times)"
        )
    suspect_lines = [
        f"- {s.get('file')}:{s.get('line')} ({s.get('from')}): {s.get('reason')}"
        for s in suspects[:8]
    ]

    changed = _changed_files(report)
    diff = _unified_diff(base_src, bad_src, changed) if base_src != bad_src else ""
    files_blob = _changed_file_contents(bad_src, changed)

    return f"""A release was quarantined after RewindAI verified it against live traffic.
Find the regressions it introduced and return edits that remove them.

Return ONLY a JSON object with this shape:
{{
  "summary": "one paragraph: what is wrong and how your edits fix it",
  "bugs": [{{"file": "catalog.py", "line": 1, "reason": "..."}}],
  "edits": [{{"file": "catalog.py", "search": "exact lines copied from the BAD file", "replace": "fixed lines"}}]
}}

Rules for edits:
- "search" is copied character for character from the BAD file below, including indentation.
- Each "search" must appear exactly once in its file. Use 2 to 15 whole lines.
- Keep the new behaviour the release wanted when it is harmless; remove what causes the crossed signals.
- Python must stay valid.

Crossed signals. Each one is its own regression, and the edits must remove the cause of every one:
{chr(10).join(bug_lines) or '(none)'}

How to read the signals:
- cpu or latency_p95: the release added expensive work to every request, such as loops over all records,
  repeated hashing or key derivation, or large allocations. Take that work off the request path.
- error_rate: follow the tracebacks to the exception and fix its cause.
- Memory that grows on every request (a module-level list or dict that is only appended to) is a leak even
  before memory crosses its limit: bound it or remove it.
Return one entry in "bugs" for each cause you remove.

Tracebacks from the release logs:
{chr(10).join(tb_lines) or '(none)'}

Suspects (changed code ranked by evidence):
{chr(10).join(suspect_lines) or '(none)'}

What changed since the best release (unified diff):
{diff or '(no diff)'}

Full contents of the changed files in the BAD release:
{files_blob or '(no source)'}
"""


def _closest_line(text: str, search: str) -> int | None:
    """1-based line in text that best matches the first line of search."""
    first = next((line.strip() for line in search.splitlines() if line.strip()), "")
    if not first:
        return None
    best_ratio = 0.0
    best_line = None
    for index, line in enumerate(text.splitlines()):
        ratio = difflib.SequenceMatcher(None, line.strip(), first).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_line = index + 1
    return best_line if best_ratio >= 0.5 else None


def _enclosing_block(text: str, lineno: int) -> tuple[int, int]:
    """Line range of the innermost function around lineno, else a window."""
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    best = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(node, "end_lineno", None) or node.lineno
                if node.lineno <= lineno <= end and (best is None or node.lineno > best[0]):
                    best = (node.lineno, end)
    if best:
        return best
    return max(1, lineno - 15), min(len(lines), lineno + 15)


def _source_file(src: Path, name: str) -> tuple[str, Path] | None:
    """Resolve a traceback file name (often a basename) inside the release source."""
    if not name:
        return None
    direct = src / name
    if direct.is_file():
        return name, direct
    matches = [p for p in src.rglob(Path(name).name) if p.is_file()]
    if len(matches) == 1:
        return matches[0].relative_to(src).as_posix(), matches[0]
    return None


def unfixed_tracebacks(report: dict, src: Path, edits: list[dict]) -> list[dict]:
    """Traceback lines in the release source that the patched tree leaves unchanged."""
    gaps = []
    with tempfile.TemporaryDirectory(prefix="rewind-gap-") as tmp:
        patched = Path(tmp) / "src"
        copy_tree(src, patched)
        apply_edits(patched, edits)
        for tb in (report.get("tracebacks") or [])[:5]:
            top = tb.get("top") or {}
            found = _source_file(src, str(top.get("file") or ""))
            lineno = int(top.get("line") or 0)
            if not found or lineno <= 0:
                continue
            rel, path = found
            text = path.read_text(encoding="utf-8", errors="replace")
            old = text.splitlines()
            after = patched / rel
            new = after.read_text(encoding="utf-8", errors="replace").splitlines() if after.is_file() else []
            opcodes = difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
            if any(tag != "equal" and i1 <= lineno - 1 < i2 for tag, i1, i2, _, _ in opcodes):
                continue
            code = old[lineno - 1].strip() if lineno <= len(old) else ""
            gaps.append(
                {
                    "file": rel,
                    "line": lineno,
                    "text": text,
                    "message": f"{rel}:{lineno} still raises {tb.get('exception')}: {tb.get('message')} at `{code}`. No edit changes that line.",
                }
            )
    return gaps


def gap_context(gaps: list[dict]) -> str:
    blocks = []
    for gap in gaps:
        start, end = _enclosing_block(gap["text"], gap["line"])
        snippet = "\n".join(gap["text"].splitlines()[start - 1 : end])
        blocks.append(f"----- {gap['file']} lines {start}-{end} (exact text) -----\n{snippet}")
    return "\n\n".join(blocks)


def repair_context(src: Path, edits: list[dict], errors: list[str]) -> str:
    """Exact text of the code each failing edit was aiming at."""
    blocks = []
    seen: set[tuple[str, int, int]] = set()
    for edit in edits:
        rel = str(edit.get("file") or "")
        if not any(err.startswith(f"{rel}:") for err in errors):
            continue
        path = src / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if text.count(edit.get("search") or "") == 1:
            continue
        lineno = _closest_line(text, edit.get("search") or "")
        if lineno is None:
            continue
        start, end = _enclosing_block(text, lineno)
        key = (rel, start, end)
        if key in seen:
            continue
        seen.add(key)
        snippet = "\n".join(text.splitlines()[start - 1 : end])
        blocks.append(f"----- {rel} lines {start}-{end} (exact text) -----\n{snippet}")
    return "\n\n".join(blocks)


def call_ollama(
    messages: list[dict] | str,
    *,
    base_url: str | None = None,
    model: str | None = None,
) -> tuple[dict, str]:
    """Return (parsed proposal, raw content)."""
    url = (base_url or DEFAULT_URL).rstrip("/")
    model = model or DEFAULT_MODEL
    if isinstance(messages, str):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": messages},
        ]
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "bugs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["file", "reason"],
                },
            },
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "search": {"type": "string"},
                        "replace": {"type": "string"},
                    },
                    "required": ["file", "search", "replace"],
                },
            },
        },
        "required": ["summary", "edits"],
    }
    payload = {
        "model": model,
        "stream": False,
        "format": schema,
        "options": {"temperature": 0, "num_ctx": NUM_CTX},
        "messages": messages,
    }
    response = requests.post(f"{url}/api/chat", json=payload, timeout=600)
    response.raise_for_status()
    body = response.json()
    content = ((body.get("message") or {}).get("content")) or "{}"
    try:
        return json.loads(content), content
    except json.JSONDecodeError as exc:
        raise ValueError(f"model returned non-JSON: {content[:500]}") from exc


def check_edits(bad_src: Path, edits: list[dict]) -> tuple[list[str], list[str], str]:
    """Apply edits to a temp copy of the bad tree. Returns (changed, errors, diff)."""
    errors = [
        f"{e['file']}: edit does not change anything"
        for e in edits
        if e.get("search") == e.get("replace")
    ]
    with tempfile.TemporaryDirectory(prefix="rewind-aifix-") as tmp:
        tmp_path = Path(tmp) / "src"
        copy_tree(bad_src, tmp_path)
        changed, apply_errors = apply_edits(tmp_path, edits)
        errors.extend(apply_errors)
        diff = "" if errors else edits_to_unified_diff(bad_src, tmp_path)
    return changed, errors, diff


class AIFixer:
    def __init__(self, registry: Registry, verifier: Verifier | None = None) -> None:
        self.registry = registry
        self.verifier = verifier

    def start_fix(self, app_id: str, version: str | None = None) -> dict:
        app = self.registry.get_app(app_id)
        if not app:
            raise KeyError(app_id)
        report = self.registry.read_report(app_id, version) if version else None
        if not report:
            report = self.registry.read_report(app_id)
        if not report:
            raise ValueError("no bug report available yet: publish a release and let RewindAI catch it first")
        bad_version = report.get("version") or version
        if not bad_version:
            raise ValueError("report missing version")
        if not self.registry.has_source(app_id, bad_version):
            raise ValueError("AI fix needs a source-backed release (publish with --dir, --base or GitHub)")

        status = ollama_reachable()
        job_id = f"fix-{uuid.uuid4().hex[:10]}"
        job = {
            "id": job_id,
            "type": "aifix",
            "app_id": app_id,
            "version": bad_version,
            "status": "running",
            "created_at": _ts(),
            "ollama": status,
            "model": status.get("model"),
            "summary": None,
            "bugs": [],
            "edits": [],
            "attempts": [],
            "diff": None,
            "error": None,
        }
        if not status.get("ok"):
            job["status"] = "failed"
            job["error"] = (
                f"Ollama is not reachable at {status.get('url')} ({status.get('message')}). "
                "Run ./scripts/ai-setup.sh on the Mac."
            )
        elif not status.get("has_model"):
            job["status"] = "failed"
            job["error"] = (
                f"Ollama is running but {status.get('model')} is not pulled yet. "
                "Run ./scripts/ai-setup.sh to download it."
            )
        self.registry.write_job(job_id, job)
        self.registry.write_app_blob(app_id, "last_fix.json", {"job_id": job_id, **job})
        if job["status"] == "failed":
            return job

        def runner() -> None:
            try:
                self._run_job(job_id, app_id, bad_version, report)
            except Exception as exc:  # noqa: BLE001
                self.registry.update_job(
                    job_id,
                    status="failed",
                    error=str(exc),
                    finished_at=_ts(),
                )
                self._sync_last(app_id, job_id)

        threading.Thread(target=runner, daemon=True, name=f"aifix-{job_id}").start()
        return job

    def _sync_last(self, app_id: str, job_id: str) -> None:
        job = self.registry.read_job(job_id) or {}
        self.registry.write_app_blob(app_id, "last_fix.json", {"job_id": job_id, **job})

    def _run_job(self, job_id: str, app_id: str, bad_version: str, report: dict) -> None:
        bad_src = self.registry.src_dir(app_id, bad_version)
        base_version = report.get("best_version")
        if base_version and self.registry.has_source(app_id, base_version):
            base_src = self.registry.src_dir(app_id, base_version)
        else:
            base_src = bad_src

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(report, bad_src, base_src)},
        ]
        attempts: list[dict] = []
        proposal: dict = {}
        edits: list[dict] = []
        errors: list[str] = []
        for number in range(1, MAX_ATTEMPTS + 1):
            started = time.time()
            proposal, raw = call_ollama(messages)
            try:
                edits = normalize_edits(proposal.get("edits") or [])
            except ValueError as exc:
                edits, errors = [], [str(exc)]
            else:
                errors = [] if edits else ["model returned no edits"]
            changed: list[str] = []
            diff = ""
            gaps: list[dict] = []
            if edits:
                changed, errors, diff = check_edits(bad_src, edits)
            if edits and not errors:
                gaps = unfixed_tracebacks(report, bad_src, edits)
            attempts.append(
                {
                    "attempt": number,
                    "duration_s": round(time.time() - started, 1),
                    "edit_count": len(edits),
                    "errors": errors + [gap["message"] for gap in gaps],
                }
            )
            self.registry.update_job(job_id, attempts=attempts)
            self._sync_last(app_id, job_id)
            if not errors and not gaps:
                self.registry.update_job(
                    job_id,
                    status="ready",
                    summary=proposal.get("summary"),
                    bugs=proposal.get("bugs") or [],
                    edits=edits,
                    changed_files=changed,
                    diff=diff,
                    finished_at=_ts(),
                )
                self._sync_last(app_id, job_id)
                return
            if errors:
                context = repair_context(bad_src, edits, errors)
                feedback = (
                    "Your edits could not be applied to the BAD release:\n"
                    + "\n".join(f"- {err}" for err in errors)
                    + "\n\nCopy each search block character for character from the exact text below, "
                    "then return the complete JSON again with all edits.\n\n"
                    + (context or _changed_file_contents(bad_src, _changed_files(report)))
                )
            else:
                feedback = (
                    "Your edits apply, but the release would still fail:\n"
                    + "\n".join(f"- {gap['message']}" for gap in gaps)
                    + "\n\nKeep every edit you already made and add edits that fix these lines too. "
                    "Return the complete JSON again with all edits.\n\n"
                    + gap_context(gaps)
                )
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": feedback},
            ]

        self.registry.update_job(
            job_id,
            status="failed",
            error="; ".join(errors + [gap["message"] for gap in gaps]),
            summary=proposal.get("summary"),
            bugs=proposal.get("bugs") or [],
            edits=edits,
            finished_at=_ts(),
        )
        self._sync_last(app_id, job_id)

    def get_fix(self, app_id: str, job_id: str | None = None) -> dict | None:
        if job_id:
            return self.registry.read_job(job_id)
        data = self.registry.read_app_blob(app_id, "last_fix.json")
        if not data:
            return None
        jid = data.get("job_id") or data.get("id")
        if jid:
            job = self.registry.read_job(jid)
            if job:
                return job
        return data

    def discard(self, app_id: str, job_id: str | None = None) -> dict:
        job = self.get_fix(app_id, job_id)
        if not job:
            raise KeyError("no fix job")
        jid = job.get("id") or job_id
        updated = self.registry.update_job(jid, status="discarded", discarded_at=_ts())
        self._sync_last(app_id, jid)
        return updated

    def ship(
        self,
        app_id: str,
        *,
        new_version: str,
        job_id: str | None = None,
    ) -> dict:
        job = self.get_fix(app_id, job_id)
        if not job:
            raise KeyError("no fix job")
        if job.get("status") != "ready":
            raise ValueError(f"fix job is {job.get('status')}, not ready")
        edits = normalize_edits(job.get("edits") or [])
        bad_version = job.get("version")
        if not bad_version:
            raise ValueError("fix job missing version")

        release = publish_release(
            self.registry,
            app_id,
            version=new_version,
            base_version=bad_version,
            edits=edits,
        )
        self.registry.update_release(app_id, new_version, fix_job=job["id"], fixes_version=bad_version)
        self.registry.update_job(
            job["id"],
            status="shipped",
            shipped_version=new_version,
            shipped_at=_ts(),
        )
        self._sync_last(app_id, job["id"])
        if self.verifier:
            self.verifier.verify_async(app_id, new_version)
        return {
            "status": "accepted",
            "version": new_version,
            "image": release.get("image"),
            "job_id": job["id"],
            "message": f"AI fix shipped as {new_version}, verification started",
        }

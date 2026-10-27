"""Ask a local Ollama model for a fix; validate edits; ship as a new version."""

from __future__ import annotations

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


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ollama_reachable(base_url: str | None = None, timeout: float = 2.0) -> dict:
    url = (base_url or DEFAULT_URL).rstrip("/")
    try:
        response = requests.get(f"{url}/api/tags", timeout=timeout)
        if response.status_code != 200:
            return {"ok": False, "url": url, "message": f"status {response.status_code}"}
        models = [m.get("name") for m in (response.json().get("models") or [])]
        return {
            "ok": True,
            "url": url,
            "models": models,
            "has_model": any(
                (DEFAULT_MODEL in (name or "")) or ((name or "").startswith(DEFAULT_MODEL.split(":")[0]))
                for name in models
            ),
            "model": DEFAULT_MODEL,
        }
    except requests.RequestException as exc:
        return {"ok": False, "url": url, "message": str(exc), "model": DEFAULT_MODEL}


def _changed_file_contents(src: Path, rel_paths: list[str], limit: int = 8) -> str:
    chunks = []
    for rel in rel_paths[:limit]:
        path = src / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > 6000:
            text = text[:6000] + "\n... truncated ...\n"
        chunks.append(f"----- {rel} -----\n{text}")
    return "\n\n".join(chunks)


def build_prompt(report: dict, bad_src: Path, base_src: Path) -> str:
    diagnosis = report.get("diagnosis") or {}
    bugs = diagnosis.get("bugs") or []
    suspects = report.get("suspects") or []
    tracebacks = report.get("tracebacks") or []
    hunks = report.get("hunks") or []

    bug_lines = []
    for bug in bugs:
        bug_lines.append(
            f"- {bug.get('signal')}: {bug.get('value')}{bug.get('unit')} "
            f"(limit {bug.get('limit')}{bug.get('unit')}) at {bug.get('location')}. "
            f"{bug.get('fix')}"
        )
    suspect_lines = []
    for s in suspects[:8]:
        suspect_lines.append(
            f"- {s.get('file')}:{s.get('line')} ({s.get('from')}): {s.get('reason')}\n"
            f"  code:\n{s.get('code')}"
        )
    tb_lines = []
    for tb in tracebacks[:5]:
        top = tb.get("top") or {}
        tb_lines.append(
            f"- {tb.get('exception')}: {tb.get('message')} "
            f"in {top.get('file')}:{top.get('line')} count={tb.get('count')}"
        )
    hunk_lines = []
    for h in hunks[:12]:
        hunk_lines.append(
            f"### {h.get('file')} (+{h.get('new_start')})\n{h.get('preview')}"
        )

    changed = sorted({h.get("file") for h in hunks if h.get("file")})
    files_blob = _changed_file_contents(bad_src, changed)

    return f"""You are RewindAI's repair assistant. A release was quarantined after a verify window.
Return ONLY a JSON object with this schema:
{{
  "summary": "one paragraph of what is wrong and how you fix it",
  "bugs": [{{"file": "...", "line": 1, "reason": "..."}}],
  "edits": [{{"file": "relative/path.py", "search": "exact text from the BAD release", "replace": "fixed text"}}]
}}

Rules:
- search must match the BAD release file contents exactly once.
- Prefer the smallest edits that remove the regressions vs the best release.
- Do not invent new files unless required.
- Do not mention FAULT_MODE or demo scaffolding.
- Keep Python valid.

Crossed signals:
{chr(10).join(bug_lines) or '(none)'}

Tracebacks:
{chr(10).join(tb_lines) or '(none)'}

Suspects:
{chr(10).join(suspect_lines) or '(none)'}

Diff previews vs best release:
{chr(10).join(hunk_lines) or '(none)'}

Changed file contents from the BAD release:
{files_blob or '(no source)'}
"""


def call_ollama(prompt: str, *, base_url: str | None = None, model: str | None = None) -> dict:
    url = (base_url or DEFAULT_URL).rstrip("/")
    model = model or DEFAULT_MODEL
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
        "options": {"temperature": 0},
        "messages": [
            {
                "role": "system",
                "content": "You fix buggy application releases. Reply with JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    response = requests.post(f"{url}/api/chat", json=payload, timeout=300)
    response.raise_for_status()
    body = response.json()
    content = ((body.get("message") or {}).get("content")) or "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model returned non-JSON: {content[:500]}") from exc


class AIFixer:
    def __init__(self, registry: Registry, verifier: Verifier | None = None) -> None:
        self.registry = registry
        self.verifier = verifier

    def start_fix(self, app_id: str, version: str | None = None) -> dict:
        app = self.registry.get_app(app_id)
        if not app:
            raise KeyError(app_id)
        report = self.registry.read_report(app_id, version)
        if not report:
            # Fall back to last report.
            report = self.registry.read_report(app_id)
        if not report:
            raise ValueError("no bug report available — run verify on a bad release first")
        bad_version = report.get("version") or version
        if not bad_version:
            raise ValueError("report missing version")
        if not report.get("can_ai_fix") and not self.registry.has_source(app_id, bad_version):
            raise ValueError("AI fix needs a source-backed release")

        status = ollama_reachable()
        job_id = f"fix-{uuid.uuid4().hex[:10]}"
        job = {
            "id": job_id,
            "type": "aifix",
            "app_id": app_id,
            "version": bad_version,
            "status": "running" if status.get("ok") else "failed",
            "created_at": _ts(),
            "ollama": status,
            "summary": None,
            "bugs": [],
            "edits": [],
            "diff": None,
            "error": None,
        }
        if not status.get("ok"):
            job["error"] = (
                status.get("message")
                or "Ollama is not reachable. Run ./scripts/ai-setup.sh on the Mac."
            )
            self.registry.write_job(job_id, job)
            self.registry.write_app_blob(app_id, "last_fix.json", {"job_id": job_id, **job})
            return job

        self.registry.write_job(job_id, job)
        self.registry.write_app_blob(app_id, "last_fix.json", {"job_id": job_id, **job})

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
        if not base_version or not self.registry.has_source(app_id, base_version):
            # Still allow fixing from the bad tree alone.
            base_src = bad_src
        else:
            base_src = self.registry.src_dir(app_id, base_version)

        prompt = build_prompt(report, bad_src, base_src)
        proposal = call_ollama(prompt)
        edits = normalize_edits(proposal.get("edits") or [])
        if not edits:
            raise ValueError("model returned no edits")

        # Validate by applying onto a temp copy of the BAD release.
        with tempfile.TemporaryDirectory(prefix="rewind-aifix-") as tmp:
            tmp_path = Path(tmp) / "src"
            copy_tree(bad_src, tmp_path)
            changed, errors = apply_edits(tmp_path, edits)
            if errors:
                self.registry.update_job(
                    job_id,
                    status="failed",
                    error="; ".join(errors),
                    summary=proposal.get("summary"),
                    bugs=proposal.get("bugs") or [],
                    edits=edits,
                    finished_at=_ts(),
                )
                self._sync_last(app_id, job_id)
                return
            diff = edits_to_unified_diff(bad_src, tmp_path)

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
            "message": "Fix shipped — verification started",
        }

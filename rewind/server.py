"""RewindAI HTTP service: registry, publish, pick, AI fix, console."""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
import requests
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from werkzeug.utils import secure_filename

from aifix import AIFixer, ollama_reachable
from compare import compare_releases, comparison_presets
from deployer import publish_release, restore_version
from diagnosis import LIMITS, diagnose_incident
from github import GitHubPoller
from rank import _version_key, annotate_releases, next_version, pick_best
from registry import Registry
from scanner import scan_release
from verify import Verifier
from watch import Watcher

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("REWIND_DATA", "/data"))
REGISTRY = Registry(DATA_DIR)
WATCHER = Watcher(REGISTRY)
VERIFIER = Verifier(REGISTRY, WATCHER)
AIFIXER = AIFixer(REGISTRY, VERIFIER)
GITHUB = GitHubPoller(REGISTRY, VERIFIER)
_started = False
_start_lock = threading.Lock()


def _ensure_started() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        WATCHER.start()
        GITHUB.start()
        for key in VERIFIER.resume_interrupted():
            print(f"[verify] resuming {key} after a restart", flush=True)
        _started = True


@app.before_request
def _boot() -> None:
    _ensure_started()


def _app_or_404(app_id: str) -> dict:
    record = REGISTRY.get_app(app_id)
    if not record:
        raise KeyError(app_id)
    return record


def _live_health(record: dict) -> str:
    live = record.get("live_version")
    if not live:
        return "idle"
    if VERIFIER.active_version(record["id"]) == live:
        return "verifying"
    release = REGISTRY.get_release(record["id"], live) or {}
    return release.get("decision") or "unverified"


def _hero(record: dict, live_health: str, last_incident: dict | None, verifying: dict | None) -> dict:
    live = record.get("live_version")
    if verifying:
        return {
            "state": "verifying",
            "title": f"Checking {verifying['version']} against live traffic.",
            "lede": (
                f"RewindAI is watching CPU, memory, latency and errors for "
                f"{verifying['window_s']} s. A bad release rolls back on its own."
            ),
        }
    if live_health == "anomaly":
        return {
            "state": "failing",
            "title": f"{live} is failing. Users should not wait.",
            "lede": "Revert to the best published version, or stay and fix the bugs with local AI.",
        }
    if record.get("last_decision") == "rolled_back" and last_incident:
        return {
            "state": "rolled_back",
            "title": (
                f"{last_incident.get('version')} was bad. RewindAI put {live} back "
                f"in {last_incident.get('time_to_rollback_s')} s."
            ),
            "lede": (
                f"Users kept being served. See where the bugs are below, then fix them "
                f"with AI or keep {live} serving."
            ),
        }
    if live_health == "healthy":
        return {
            "state": "healthy",
            "title": f"{live} is live and healthy.",
            "lede": "Every publish is verified against real traffic before RewindAI trusts it.",
        }
    if not live:
        return {
            "state": "idle",
            "title": "No release yet.",
            "lede": "Publish a version from the CLI or connect a GitHub repo. RewindAI verifies it with zero code changes.",
        }
    return {
        "state": "unverified",
        "title": f"{live} is live but unverified.",
        "lede": "It was published with verify skipped. Publish through RewindAI to get it checked.",
    }


def _resolved_by(app_id: str, last_incident: dict | None) -> str | None:
    """Newest healthy release published after the incident with a higher version."""
    if not last_incident:
        return None
    bad = str(last_incident.get("version") or "")
    detected = str(last_incident.get("detected_at") or "")
    newer = [
        r
        for r in REGISTRY.list_releases(app_id)
        if r.get("decision") == "healthy"
        and not r.get("quarantined")
        and str(r.get("published_at") or "") >= detected
        and _version_key(str(r.get("version") or "")) > _version_key(bad)
    ]
    if not newer:
        return None
    newer.sort(key=lambda r: _version_key(str(r.get("version") or "")))
    return newer[-1].get("version")


def _suggested_version(app_id: str) -> str:
    return next_version([r.get("version") for r in REGISTRY.list_releases(app_id)])


def _busy_response(app_id: str):
    active = VERIFIER.active_version(app_id)
    if not active:
        return None
    return jsonify(
        {
            "status": "busy",
            "version": active,
            "message": f"{active} is still verifying. Wait for the verdict, then try again.",
        }
    ), 409


def _ensure_release_scans(app_id: str, releases: list[dict]) -> list[dict]:
    for row in releases:
        ver = str(row.get("version") or "")
        if ver and row.get("security_grade") is None:
            try:
                scan = REGISTRY.read_scan(app_id, ver) or scan_release(REGISTRY, app_id, ver)
                row["security_grade"] = scan.get("grade")
                row["security_score"] = scan.get("score")
                row["security_findings"] = (scan.get("counts") or {}).get("total", 0)
                row["security_counts"] = scan.get("counts")
            except Exception:  # noqa: BLE001
                pass
    return releases


def _pipeline_steps(
    record: dict,
    releases: list[dict],
    last_incident: dict | None,
    verifying: dict | None,
    active_scan: dict | None,
    comparison: dict | None,
    fix: dict | None,
    resolved_by: str | None,
) -> list[dict]:
    """Five-stage DevSecOps lifecycle bar so any viewer sees how RewindAI works and where the app is."""
    live = record.get("live_version")
    gh = record.get("github") or {}

    # 1. Publish / GitHub CD
    if releases:
        latest = releases[0]
        if gh.get("repo"):
            s1_detail = f"{gh['repo']}@{gh.get('branch', 'release')} · {len(releases)} release(s)"
        elif latest.get("fixes_version"):
            s1_detail = f"{latest['version']} (AI fix of {latest['fixes_version']}) · {len(releases)} total"
        else:
            s1_detail = f"Latest {latest['version']} · {len(releases)} published"
        s1_state = "done"
    else:
        s1_detail = "Push to GitHub or run rewind.sh publish"
        s1_state = "active"

    # 2. DevSecOps Scan & SBOM
    if active_scan:
        grade = active_scan.get("grade") or "—"
        total = (active_scan.get("counts") or {}).get("total", 0)
        pkgs = (active_scan.get("sbom_summary") or {}).get("package_count", 0)
        s2_detail = f"Grade {grade} ({active_scan.get('score', 0)}/100) · {total} finding(s) · {pkgs} SBOM pkgs"
        s2_state = "alert" if grade in {"C", "D", "F"} else "done"
    else:
        s2_detail = "Bandit SAST · pip-audit · Trivy SBOM · Dockerfile"
        s2_state = "idle"

    # 3. Runtime Verify & Auto-Rollback
    if verifying:
        s3_detail = (
            f"Verifying {verifying['version']} · {int(verifying.get('elapsed_s') or 0)}/{verifying.get('window_s')}s"
            f" · votes {verifying.get('votes', 0)}/{verifying.get('majority', 3)}"
        )
        s3_state = "active"
    elif last_incident and not resolved_by:
        bad_v = last_incident.get("version")
        rest_v = last_incident.get("restored_version") or record.get("best_version") or "BEST"
        ttr = last_incident.get("time_to_rollback_s")
        s3_detail = f"{bad_v} failed → rolled back to {rest_v} in {ttr}s"
        s3_state = "alert"
    elif live:
        best_v = record.get("best_version") or live
        s3_detail = f"Live {live} healthy · BEST {best_v}"
        s3_state = "done"
    else:
        s3_detail = "60s live traffic check + Isolation Forest"
        s3_state = "idle"

    # 4. Diagnose & Before/After Diff
    if comparison and comparison.get("available"):
        b_ver = comparison["before"]["version"]
        a_ver = comparison["after"]["version"]
        ast_total = (comparison.get("ast_changes") or {}).get("total_changed", 0)
        s4_detail = f"Diffing {b_ver} → {a_ver} · {ast_total} AST symbol(s) changed"
        s4_state = "active" if (last_incident and not resolved_by) else "done"
    elif last_incident:
        s4_detail = f"Pinpointed {last_incident.get('version')} traceback & slow routes"
        s4_state = "active"
    else:
        s4_detail = "Route p95/errors + AST & side-by-side diff"
        s4_state = "idle"

    # 5. Local AI Fix (Ollama)
    if resolved_by:
        s5_detail = f"Shipped {resolved_by} · verified healthy"
        s5_state = "done"
    elif fix and fix.get("status") == "ready":
        s5_detail = f"Patch ready for review → ship { _suggested_version(record['id']) }"
        s5_state = "active"
    elif fix and fix.get("status") == "running":
        s5_detail = "Ollama generating & validating patch…"
        s5_state = "active"
    elif last_incident:
        s5_detail = f"Ready to patch {last_incident.get('version')} with local Ollama"
        s5_state = "active"
    else:
        s5_detail = "qwen2.5-coder:7b patches runtime + SAST bugs"
        s5_state = "idle"

    return [
        {
            "step": "01",
            "title": "Publish / GitHub CD",
            "detail": s1_detail,
            "state": s1_state,
            "target": "[data-releases]",
        },
        {
            "step": "02",
            "title": "DevSecOps Scan",
            "detail": s2_detail,
            "state": s2_state,
            "target": "[data-scanner]",
        },
        {
            "step": "03",
            "title": "Verify & Roll Back",
            "detail": s3_detail,
            "state": s3_state,
            "target": "[data-replay]",
        },
        {
            "step": "04",
            "title": "Before vs After Diff",
            "detail": s4_detail,
            "state": s4_state,
            "target": "[data-inspector]",
        },
        {
            "step": "05",
            "title": "Fix with Local AI",
            "detail": s5_detail,
            "state": s5_state,
            "target": "[data-fix-panel], [data-diagnosis]",
        },
    ]


def _console_context(app_id: str) -> dict:
    record = _app_or_404(app_id)
    releases = _ensure_release_scans(
        app_id,
        annotate_releases(
            REGISTRY.list_releases(app_id),
            record.get("live_version"),
        ),
    )
    incident = REGISTRY.read_verify(app_id) or {}
    last_incident = REGISTRY.read_app_blob(app_id, "last_incident.json")
    report = REGISTRY.read_report(app_id) or {}
    diagnosis = diagnose_incident(last_incident) if last_incident else diagnose_incident(None)
    best = pick_best(REGISTRY.list_releases(app_id))
    live = record.get("live_version")
    can_revert = bool(
        best
        and live
        and best.get("version") != live
    )
    verifying = VERIFIER.progress(app_id)
    live_health = _live_health(record)
    target_scan_ver = (
        (last_incident.get("version") if last_incident and not _resolved_by(app_id, last_incident) else None)
        or live
        or (releases[0]["version"] if releases else None)
    )
    active_scan = None
    if target_scan_ver:
        active_scan = REGISTRY.read_scan(app_id, target_scan_ver)
        if active_scan is None:
            try:
                active_scan = scan_release(REGISTRY, app_id, target_scan_ver)
            except Exception:  # noqa: BLE001
                active_scan = None
    try:
        comparison = compare_releases(REGISTRY, app_id)
    except Exception:  # noqa: BLE001
        comparison = {"app_id": app_id, "available": False, "presets": []}
    resolved = _resolved_by(app_id, last_incident)
    fix = AIFIXER.get_fix(app_id)
    pipeline = _pipeline_steps(
        record,
        releases,
        last_incident,
        verifying,
        active_scan,
        comparison,
        fix,
        resolved,
    )
    return {
        "app": record,
        "releases": releases,
        "incident": incident or None,
        "last_incident": last_incident,
        "report": report or None,
        "diagnosis": diagnosis,
        "best_release": best,
        "can_revert_to_best": can_revert,
        "fix": fix,
        "ollama": ollama_reachable(),
        "probes": WATCHER.latest_probes(app_id),
        "verifying": verifying,
        "live_health": live_health,
        "hero": _hero(record, live_health, last_incident, verifying),
        "suggested_version": _suggested_version(app_id),
        "resolved_by": resolved,
        "limits": LIMITS,
        "scan": active_scan,
        "compare": comparison,
        "pipeline": pipeline,
    }


# —— pages ——


@app.get("/")
def index():
    apps = []
    for row in REGISTRY.list_apps():
        apps.append(
            {
                **row,
                "verdict": _live_health(row),
                "incident": REGISTRY.read_app_blob(row["id"], "last_incident.json"),
            }
        )
    return render_template("index.html", apps=apps, ollama=ollama_reachable())


@app.get("/apps/<app_id>")
def app_console(app_id: str):
    try:
        ctx = _console_context(app_id)
    except KeyError:
        return render_template("missing.html", app_id=app_id), 404
    return render_template("app.html", **ctx)


# —— API ——


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "rewindai"})


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.get("/api/apps")
def api_list_apps():
    return jsonify({"apps": REGISTRY.list_apps()})


@app.post("/api/apps")
def api_register():
    payload = request.get_json(silent=True) or request.form.to_dict()
    try:
        record = REGISTRY.register_app(payload)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "created", "app": record}), 201


@app.get("/api/apps/<app_id>")
def api_get_app(app_id: str):
    try:
        record = _app_or_404(app_id)
    except KeyError:
        return jsonify({"status": "not_found", "app_id": app_id}), 404
    releases = annotate_releases(
        REGISTRY.list_releases(app_id),
        record.get("live_version"),
    )
    last_incident = REGISTRY.read_app_blob(app_id, "last_incident.json")
    verifying = VERIFIER.progress(app_id)
    live_health = _live_health(record)
    return jsonify(
        {
            "app": record,
            "releases": releases,
            "incident": REGISTRY.read_verify(app_id),
            "last_incident": last_incident,
            "report": REGISTRY.read_report(app_id),
            "fix": AIFIXER.get_fix(app_id),
            "ollama": ollama_reachable(),
            "verifying": verifying,
            "live_health": live_health,
            "hero": _hero(record, live_health, last_incident, verifying),
            "suggested_version": _suggested_version(app_id),
        }
    )


@app.get("/api/apps/<app_id>/recordings")
def api_recordings(app_id: str):
    try:
        _app_or_404(app_id)
    except KeyError:
        return jsonify({"status": "not_found", "app_id": app_id}), 404
    recordings = sorted(
        REGISTRY.list_recordings(app_id),
        key=lambda r: _version_key(str(r.get("version") or "")),
    )
    return jsonify(
        {
            "recordings": recordings,
            "verifying": VERIFIER.progress(app_id),
            "limits": LIMITS,
        }
    )


@app.post("/api/apps/<app_id>/publish")
def api_publish(app_id: str):
    try:
        _app_or_404(app_id)
    except KeyError:
        return jsonify({"status": "not_found", "app_id": app_id}), 404
    busy = _busy_response(app_id)
    if busy:
        return busy

    payload = request.get_json(silent=True) or {}
    # multipart form also supported
    if not payload and request.form:
        payload = request.form.to_dict()
        if "edits" in payload and isinstance(payload["edits"], str):
            import json as _json

            try:
                payload["edits"] = _json.loads(payload["edits"])
            except _json.JSONDecodeError:
                return jsonify({"status": "error", "message": "edits must be JSON"}), 400

    version = str(payload.get("version") or "").strip()
    if not version:
        return jsonify({"status": "error", "message": "version is required"}), 400

    image = (payload.get("image") or "").strip() or None
    base_version = (payload.get("base") or payload.get("base_version") or "").strip() or None
    edits = payload.get("edits")
    source_dir = (payload.get("source_dir") or "").strip() or None
    raw_skip = payload.get("skip_verify")
    skip_verify = str(raw_skip).lower() in {"1", "true", "yes"} if raw_skip is not None else False

    archive_path = None
    tmp_archive = None
    upload = request.files.get("source")
    if upload and upload.filename:
        tmp_archive = Path(tempfile.mkdtemp(prefix="rewind-upload-")) / secure_filename(
            upload.filename
        )
        upload.save(tmp_archive)
        archive_path = tmp_archive

    try:
        release = publish_release(
            REGISTRY,
            app_id,
            version=version,
            source_dir=Path(source_dir) if source_dir else None,
            archive_path=archive_path,
            image=image,
            base_version=base_version,
            edits=edits,
            skip_verify=skip_verify,
        )
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except (ValueError, RuntimeError, TimeoutError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    finally:
        if tmp_archive and tmp_archive.parent.exists():
            import shutil

            shutil.rmtree(tmp_archive.parent, ignore_errors=True)

    if not skip_verify:
        VERIFIER.verify_async(app_id, version)
    else:
        try:
            scan_release(REGISTRY, app_id, version)
        except Exception as exc:  # noqa: BLE001
            print(f"[publish] scan {app_id}@{version} skipped: {exc}", flush=True)
    return jsonify(
        {
            "status": "accepted",
            "version": version,
            "image": release.get("image"),
            "message": "Published — verification started"
            if not skip_verify
            else "Published — verify skipped",
        }
    ), 202


@app.post("/api/apps/<app_id>/pick")
def api_pick(app_id: str):
    try:
        _app_or_404(app_id)
    except KeyError:
        return jsonify({"status": "not_found", "app_id": app_id}), 404
    busy = _busy_response(app_id)
    if busy:
        return busy
    payload = request.get_json(silent=True) or {}
    version = str(payload.get("version") or "").strip()
    force = bool(payload.get("force"))
    if not version:
        return jsonify({"status": "error", "message": "version is required"}), 400
    release = REGISTRY.get_release(app_id, version)
    if release is None:
        return jsonify(
            {
                "status": "not_found",
                "version": version,
                "message": "No published release with that version",
            }
        ), 404
    if release.get("quarantined") and not force:
        return jsonify(
            {
                "status": "quarantined",
                "version": version,
                "image": release.get("image"),
                "message": "Release is quarantined. Confirm Pick anyway (force=true).",
            }
        ), 409

    def runner() -> None:
        try:
            restore_version(REGISTRY, app_id, version, force=force or release.get("quarantined"))
        except Exception as exc:  # noqa: BLE001
            print(f"[pick] failed: {exc}", flush=True)

    # already_live short-circuit without thread
    app_rec = REGISTRY.get_app(app_id) or {}
    if app_rec.get("live_version") == version:
        return jsonify(
            {
                "status": "already_live",
                "version": version,
                "message": "That version is already serving",
            }
        ), 200

    threading.Thread(target=runner, daemon=True).start()
    return jsonify(
        {
            "status": "accepted",
            "version": version,
            "image": release.get("image"),
            "force": force or bool(release.get("quarantined")),
            "message": "Restore started",
        }
    ), 202


@app.get("/api/apps/<app_id>/series")
def api_series(app_id: str):
    try:
        _app_or_404(app_id)
    except KeyError:
        return jsonify({"ok": False, "message": "unknown app"}), 404
    try:
        minutes = max(5, min(int(request.args.get("minutes") or 15), 120))
    except (TypeError, ValueError):
        minutes = 15
    payload = WATCHER.series(app_id, minutes=minutes)
    payload["features"] = WATCHER.features(app_id)
    payload["probes"] = WATCHER.latest_probes(app_id)
    payload["incident"] = REGISTRY.read_verify(app_id)
    return jsonify(payload)


@app.get("/api/apps/<app_id>/report")
def api_report(app_id: str):
    report = REGISTRY.read_report(app_id)
    if not report:
        return jsonify({"status": "empty", "report": None})
    return jsonify({"status": "ok", "report": report})


@app.get("/api/apps/<app_id>/scan")
@app.get("/api/apps/<app_id>/scan/<version>")
def api_get_scan(app_id: str, version: str | None = None):
    record = _app_or_404(app_id)
    target = (version or request.args.get("version") or record.get("live_version") or "").strip()
    if not target:
        releases = REGISTRY.list_releases(app_id)
        if not releases:
            return jsonify({"status": "empty", "scan": None})
        target = str(releases[-1].get("version") or "")
    if not REGISTRY.get_release(app_id, target):
        return jsonify({"status": "not_found", "version": target, "message": f"Release not found: {target}"}), 404
    refresh = str(request.args.get("refresh") or "").lower() in {"1", "true", "yes"}
    scan = None if refresh else REGISTRY.read_scan(app_id, target)
    if scan is None:
        scan = scan_release(REGISTRY, app_id, target)
    return jsonify({"status": "ok", "scan": scan})


@app.post("/api/apps/<app_id>/scan/<version>")
def api_run_scan(app_id: str, version: str):
    _app_or_404(app_id)
    if not REGISTRY.get_release(app_id, version):
        return jsonify({"status": "not_found", "version": version, "message": f"Release not found: {version}"}), 404
    scan = scan_release(REGISTRY, app_id, version)
    return jsonify({"status": "ok", "scan": scan})


@app.get("/api/apps/<app_id>/sbom/<version>")
def api_get_sbom(app_id: str, version: str):
    _app_or_404(app_id)
    if not REGISTRY.get_release(app_id, version):
        return jsonify({"status": "not_found", "version": version}), 404
    sbom = REGISTRY.read_sbom(app_id, version)
    if sbom is None:
        scan_release(REGISTRY, app_id, version)
        sbom = REGISTRY.read_sbom(app_id, version)
    return jsonify({"status": "ok", "sbom": sbom})


@app.get("/api/apps/<app_id>/compare")
def api_compare(app_id: str):
    _app_or_404(app_id)
    before = (request.args.get("before") or "").strip() or None
    after = (request.args.get("after") or "").strip() or None
    try:
        payload = compare_releases(REGISTRY, app_id, before, after)
    except FileNotFoundError as exc:
        return jsonify({"status": "not_found", "message": str(exc)}), 404
    return jsonify({"status": "ok", **payload})


@app.post("/api/apps/<app_id>/fix")
def api_fix(app_id: str):
    try:
        _app_or_404(app_id)
    except KeyError:
        return jsonify({"status": "not_found", "app_id": app_id}), 404
    payload = request.get_json(silent=True) or {}
    version = (payload.get("version") or "").strip() or None
    try:
        job = AIFIXER.start_fix(app_id, version)
    except (ValueError, KeyError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    code = 202 if job.get("status") == "running" else 503
    return jsonify(job), code


@app.get("/api/apps/<app_id>/fix")
def api_get_fix(app_id: str):
    job_id = request.args.get("job_id")
    job = AIFIXER.get_fix(app_id, job_id)
    if not job:
        return jsonify({"status": "empty", "fix": None})
    return jsonify({**job, "suggested_version": _suggested_version(app_id)})


@app.post("/api/apps/<app_id>/fix/discard")
def api_discard_fix(app_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        job = AIFIXER.discard(app_id, payload.get("job_id"))
    except KeyError:
        return jsonify({"status": "error", "message": "no fix job"}), 404
    return jsonify(job)


@app.post("/api/apps/<app_id>/fix/ship")
def api_ship_fix(app_id: str):
    busy = _busy_response(app_id)
    if busy:
        return busy
    payload = request.get_json(silent=True) or {}
    new_version = str(payload.get("version") or "").strip() or _suggested_version(app_id)
    try:
        result = AIFIXER.ship(
            app_id,
            new_version=new_version,
            job_id=payload.get("job_id"),
        )
    except (KeyError, ValueError, RuntimeError, TimeoutError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify(result), 202


@app.post("/api/apps/<app_id>/github")
def api_github_connect(app_id: str):
    _app_or_404(app_id)
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        github = GITHUB.connect(
            app_id,
            str(payload.get("repo") or ""),
            str(payload.get("branch") or "release"),
            str(payload.get("path") or "."),
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except requests.RequestException as exc:
        return jsonify({"status": "error", "message": f"GitHub is unreachable: {exc}"}), 502
    return jsonify({"status": "ok", "github": github, "message": github["message"]})


@app.delete("/api/apps/<app_id>/github")
def api_github_disconnect(app_id: str):
    _app_or_404(app_id)
    GITHUB.disconnect(app_id)
    return jsonify({"status": "ok", "message": f"{app_id} no longer follows GitHub. Publish from the CLI or connect again."})


@app.get("/api/ollama")
def api_ollama():
    return jsonify(ollama_reachable())


@app.errorhandler(KeyError)
def _key_error(exc: KeyError):
    return jsonify({"status": "not_found", "message": str(exc)}), 404


def create_app() -> Flask:
    _ensure_started()
    return app


if __name__ == "__main__":
    _ensure_started()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8090")), threaded=True)

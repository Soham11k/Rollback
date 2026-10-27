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
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from werkzeug.utils import secure_filename

from aifix import AIFixer, ollama_reachable
from deployer import publish_release, restore_version
from diagnosis import diagnose_incident
from rank import annotate_releases, pick_best
from registry import Registry
from verify import Verifier
from watch import Watcher

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("REWIND_DATA", "/data"))
REGISTRY = Registry(DATA_DIR)
WATCHER = Watcher(REGISTRY)
VERIFIER = Verifier(REGISTRY, WATCHER)
AIFIXER = AIFixer(REGISTRY, VERIFIER)
_started = False
_start_lock = threading.Lock()


def _ensure_started() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        WATCHER.start()
        _started = True


@app.before_request
def _boot() -> None:
    _ensure_started()


def _app_or_404(app_id: str) -> dict:
    record = REGISTRY.get_app(app_id)
    if not record:
        raise KeyError(app_id)
    return record


def _console_context(app_id: str) -> dict:
    record = _app_or_404(app_id)
    releases = annotate_releases(
        REGISTRY.list_releases(app_id),
        record.get("live_version"),
    )
    incident = REGISTRY.read_verify(app_id) or {}
    report = REGISTRY.read_report(app_id) or {}
    diagnosis = diagnose_incident(incident) if incident else (report.get("diagnosis") or diagnose_incident(None))
    best = pick_best(REGISTRY.list_releases(app_id))
    live = record.get("live_version")
    can_revert = bool(
        best
        and live
        and best.get("version") != live
    )
    fix = AIFIXER.get_fix(app_id)
    ollama = ollama_reachable()
    return {
        "app": record,
        "releases": releases,
        "incident": incident or None,
        "report": report or None,
        "diagnosis": diagnosis,
        "best_release": best,
        "can_revert_to_best": can_revert,
        "fix": fix,
        "ollama": ollama,
        "probes": WATCHER.latest_probes(app_id),
    }


# —— pages ——


@app.get("/")
def index():
    apps = []
    for row in REGISTRY.list_apps():
        apps.append(
            {
                **row,
                "verdict": row.get("last_decision") or "idle",
                "incident": REGISTRY.read_verify(row["id"]),
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
    return jsonify(
        {
            "app": record,
            "releases": releases,
            "incident": REGISTRY.read_verify(app_id),
            "report": REGISTRY.read_report(app_id),
            "fix": AIFIXER.get_fix(app_id),
            "ollama": ollama_reachable(),
        }
    )


@app.post("/api/apps/<app_id>/publish")
def api_publish(app_id: str):
    try:
        _app_or_404(app_id)
    except KeyError:
        return jsonify({"status": "not_found", "app_id": app_id}), 404

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
    return jsonify(job)


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
    payload = request.get_json(silent=True) or {}
    new_version = str(payload.get("version") or "").strip()
    if not new_version:
        return jsonify({"status": "error", "message": "version is required"}), 400
    try:
        result = AIFIXER.ship(
            app_id,
            new_version=new_version,
            job_id=payload.get("job_id"),
        )
    except (KeyError, ValueError, RuntimeError, TimeoutError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify(result), 202


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

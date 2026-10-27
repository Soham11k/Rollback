"""Post-deploy anomaly monitor with Isolation Forest + auto-rollback (RewindAI)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import joblib
import numpy as np
from flask import Flask, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from collect import FEATURE_NAMES, PrometheusCollector

ANOMALY_SCORE = Gauge(
    "rollbackai_anomaly_score",
    "Higher means more anomalous (negated IsolationForest decision_function)",
)
STATUS_GAUGE = Gauge(
    "rollbackai_status",
    "0=init, 1=healthy, 2=anomaly, 3=rolled_back",
)
ROLLBACKS = Counter("rollbackai_rollbacks_total", "Number of rollbacks triggered")
VERSION_GAUGE = Gauge(
    "rollbackai_watched_version_info",
    "Currently watched deployment version",
    ["version"],
)

STATUS_INIT = 0
STATUS_HEALTHY = 1
STATUS_ANOMALY = 2
STATUS_ROLLED_BACK = 3


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def print_alert(version: str, confidence: str) -> None:
    box = [
        "╔══════════════════════════╗",
        "║      RewindAI Alert      ║",
        "╠══════════════════════════╣",
        f"║ Deployment: {version:<12} ║",
        "║ Status: ANOMALY          ║",
        f"║ Confidence: {confidence:<12} ║",
        "╚══════════════════════════╝",
    ]
    print("\n".join(box), flush=True)


def _state_dir(project_root: Path) -> Path:
    env = os.environ.get("STATE_DIR", "").strip()
    if env:
        return Path(env)
    if Path("/state").is_dir() and os.access("/state", os.W_OK):
        return Path("/state")
    return project_root / "state"


def quarantine_release(
    project_root: Path,
    *,
    version: str,
    reason: str,
    peaks: dict | None = None,
    endpoint: str | None = None,
) -> None:
    """Record a bad image/version so deploy.sh refuses redeploy without FORCE_DEPLOY."""
    state = _state_dir(project_root)
    image = ""
    for candidate in (state / "current_image", project_root / "state" / "current_image"):
        if candidate.is_file():
            try:
                image = candidate.read_text().strip()
                if image:
                    break
            except OSError:
                continue
    payload = {
        "version": version,
        "image": image,
        "reason": reason,
        "quarantined_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "most_affected_endpoint": endpoint,
        "peaks": peaks or {},
    }
    text = json.dumps(payload, indent=2) + "\n"
    # Always land a durable copy on local disk first (USB bind-mounts often write 0-byte files).
    durable = Path("/tmp/quarantine.json")
    try:
        durable.write_text(text)
    except OSError as exc:
        print(f"[{_ts()}] [monitor] quarantine /tmp write failed: {exc}", flush=True)

    targets = [state / "quarantine.json"]
    if str(state) != str(project_root / "state"):
        targets.append(project_root / "state" / "quarantine.json")
    targets.append(Path("/project/state/quarantine.json"))

    wrote = False
    for path in targets:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path("/tmp") / f"quarantine-{path.parent.name}.tmp"
            tmp.write_text(text)
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
            try:
                # Prefer copy over replace — USB mounts often truncate on rename.
                import shutil

                shutil.copyfile(tmp, path)
            except OSError:
                path.write_text(text)
            if path.is_file() and path.stat().st_size > 0:
                print(f"[{_ts()}] [monitor] quarantined {version} -> {path}", flush=True)
                wrote = True
                break
        except Exception as exc:  # noqa: BLE001
            print(f"[{_ts()}] [monitor] quarantine write failed ({path}): {exc}", flush=True)
    if not wrote:
        print(
            f"[{_ts()}] [monitor] quarantine kept at {durable} (state mount write failed)",
            flush=True,
        )


def load_artifact(path: Path) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        raise FileNotFoundError(
            f"Model not found at {path}. Run train.py against a healthy baseline first."
        )
    return joblib.load(path)


def score_sample(artifact: dict, vector: list[float]) -> tuple[bool, float]:
    """Return (is_anomaly, anomaly_score). Higher score => more anomalous."""
    arr = np.asarray([vector], dtype=float)
    scaled = artifact["scaler"].transform(arr)
    prediction = artifact["model"].predict(scaled)[0]  # -1 anomaly, 1 normal
    raw = float(artifact["model"].decision_function(scaled)[0])
    anomaly_score = -raw
    return prediction == -1, anomaly_score


def threshold_is_anomaly(vector: list[float]) -> bool:
    cpu, memory, latency_ms, error_pct = vector
    return cpu > 80 or memory > 80 or latency_ms > 1000 or error_pct > 5


def run_rollback(project_root: Path) -> None:
    script = project_root / "scripts" / "rollback.sh"
    print(f"[{_ts()}] [monitor] executing {script}", flush=True)
    subprocess.run(["bash", str(script)], check=True, cwd=str(project_root))


def promote(project_root: Path) -> None:
    script = project_root / "scripts" / "promote.sh"
    print(f"[{_ts()}] [monitor] executing {script}", flush=True)
    subprocess.run(["bash", str(script)], check=True, cwd=str(project_root))


def write_result(path: Path | None, payload: dict) -> None:
    if path is None:
        return
    path = Path(path)
    text = json.dumps(payload, indent=2) + "\n"
    candidates = [path]
    # Prefer /state mount when writing from the detector container (USB-safe).
    if str(path).startswith("/project/state/"):
        candidates.insert(0, Path("/state") / path.name)
    candidates.append(Path("/tmp") / path.name)

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path("/tmp") / f"{candidate.name}.tmp"
            tmp.write_text(text)
            try:
                if candidate.exists():
                    candidate.unlink()
            except OSError:
                pass
            try:
                tmp.replace(candidate)
            except OSError:
                candidate.write_text(text)
            print(f"[{_ts()}] [monitor] wrote result to {candidate}", flush=True)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    print(f"[{_ts()}] [monitor] warn: could not write result file ({last_error})", flush=True)


def _run_deploy(
    project_root: Path,
    *,
    image: str,
    version: str,
    fault_mode: str,
    force: bool,
) -> None:
    script = project_root / "scripts" / "deploy.sh"
    env = os.environ.copy()
    state = _state_dir(project_root)
    env["STATE_DIR"] = str(state)
    if force:
        env["FORCE_DEPLOY"] = "1"
    else:
        env.pop("FORCE_DEPLOY", None)
    print(
        f"[{_ts()}] [restore] deploy image={image} version={version} "
        f"fault={fault_mode} force={int(force)}",
        flush=True,
    )
    try:
        subprocess.run(
            ["bash", str(script), image, version, fault_mode],
            check=True,
            cwd=str(project_root),
            env=env,
        )
        print(f"[{_ts()}] [restore] done version={version}", flush=True)
    except subprocess.CalledProcessError as exc:
        print(f"[{_ts()}] [restore] failed: {exc}", flush=True)


def start_metrics_server(port: int, project_root: Path | None = None) -> None:
    metrics_app = Flask("rollbackai-detector-metrics")
    root = (project_root or Path(os.environ.get("PROJECT_ROOT", "/project"))).resolve()

    @metrics_app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

    @metrics_app.get("/health")
    def health():
        return {"status": "ok"}

    @metrics_app.post("/restore")
    def restore():
        from flask import jsonify, request

        payload = request.get_json(silent=True) or {}
        image = str(payload.get("image") or "").strip()
        version = str(payload.get("version") or "").strip()
        fault_mode = str(payload.get("fault_mode") or "none").strip() or "none"
        force = bool(payload.get("force"))
        if not image or not version:
            return jsonify({"status": "error", "message": "image and version required"}), 400

        inspect = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
        )
        if inspect.returncode != 0:
            return jsonify(
                {
                    "status": "error",
                    "message": f"Image not found locally: {image}",
                }
            ), 404

        thread = threading.Thread(
            target=_run_deploy,
            kwargs={
                "project_root": root,
                "image": image,
                "version": version,
                "fault_mode": fault_mode,
                "force": force,
            },
            daemon=True,
        )
        thread.start()
        return jsonify(
            {
                "status": "accepted",
                "version": version,
                "image": image,
                "force": force,
            }
        ), 202

    thread = threading.Thread(
        target=lambda: metrics_app.run(
            host="0.0.0.0", port=port, threaded=True, use_reloader=False
        ),
        daemon=True,
    )
    thread.start()
    print(f"[{_ts()}] [monitor] metrics on :{port}/metrics", flush=True)


def verify_deployment(
    artifact: dict,
    collector: PrometheusCollector,
    *,
    version: str,
    window_seconds: int,
    interval_seconds: int,
    majority_window: int,
    mode: str,
    project_root: Path,
    auto_rollback: bool,
    result_file: Path | None,
) -> int:
    """
    Returns exit code:
      0 = healthy (promoted)
      2 = anomaly (rolled back if auto_rollback)
      1 = error
    """
    STATUS_GAUGE.set(STATUS_INIT)
    VERSION_GAUGE.labels(version=version).set(1)

    started_at = time.time()
    started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at))
    peaks = {name: 0.0 for name in FEATURE_NAMES}
    votes: deque[bool] = deque(maxlen=majority_window)
    deadline = started_at + window_seconds
    print(
        f"[{_ts()}] [monitor] verifying version={version} for {window_seconds}s "
        f"(mode={mode}, majority={majority_window})",
        flush=True,
    )

    while time.time() < deadline:
        try:
            vector = collector.sample_vector()
        except Exception as exc:  # noqa: BLE001
            print(f"[{_ts()}] [monitor] sample error: {exc}", flush=True)
            time.sleep(interval_seconds)
            continue

        for name, value in zip(FEATURE_NAMES, vector):
            peaks[name] = max(peaks[name], value)

        if mode == "threshold":
            is_anomaly = threshold_is_anomaly(vector)
            anomaly_score = 1.0 if is_anomaly else 0.0
        else:
            is_anomaly, anomaly_score = score_sample(artifact, vector)

        ANOMALY_SCORE.set(anomaly_score)
        votes.append(is_anomaly)
        STATUS_GAUGE.set(STATUS_ANOMALY if is_anomaly else STATUS_HEALTHY)

        print(
            f"[{_ts()}] [monitor] cpu={vector[0]:.1f} mem={vector[1]:.1f} "
            f"lat={vector[2]:.0f}ms err={vector[3]:.2f}% "
            f"score={anomaly_score:.3f} anomaly={is_anomaly} "
            f"votes={sum(votes)}/{len(votes)}",
            flush=True,
        )

        if len(votes) == majority_window and sum(votes) > (majority_window // 2):
            confidence = "HIGH" if sum(votes) >= majority_window - 1 else "MEDIUM"
            detected_at = time.time()
            print_alert(version, confidence)
            STATUS_GAUGE.set(STATUS_ANOMALY)
            affected = None
            try:
                affected = collector.most_affected_endpoint()
            except Exception as exc:  # noqa: BLE001
                print(f"[{_ts()}] [monitor] endpoint query failed: {exc}", flush=True)
            quarantine_release(
                project_root,
                version=version,
                reason=f"anomaly ({confidence})",
                peaks=dict(peaks),
                endpoint=affected,
            )
            rolled_back_at = None
            if auto_rollback:
                try:
                    run_rollback(project_root)
                    ROLLBACKS.inc()
                    STATUS_GAUGE.set(STATUS_ROLLED_BACK)
                    rolled_back_at = time.time()
                except subprocess.CalledProcessError as exc:
                    print(f"[{_ts()}] [monitor] rollback failed: {exc}", flush=True)
                    write_result(
                        result_file,
                        {
                            "decision": "error",
                            "version": version,
                            "started_at": started_iso,
                            "detected_at": time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(detected_at)
                            ),
                            "time_to_detect_s": round(detected_at - started_at, 1),
                            "peaks": peaks,
                            "most_affected_endpoint": affected,
                            "error": str(exc),
                        },
                    )
                    return 1
            write_result(
                result_file,
                {
                    "decision": "anomaly",
                    "version": version,
                    "confidence": confidence,
                    "started_at": started_iso,
                    "detected_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(detected_at)
                    ),
                    "rolled_back_at": (
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rolled_back_at))
                        if rolled_back_at
                        else None
                    ),
                    "time_to_detect_s": round(detected_at - started_at, 1),
                    "time_to_rollback_s": (
                        round(rolled_back_at - started_at, 1) if rolled_back_at else None
                    ),
                    "peaks": peaks,
                    "most_affected_endpoint": affected,
                },
            )
            return 2

        time.sleep(interval_seconds)

    print(f"[{_ts()}] [monitor] version {version} looks HEALTHY", flush=True)
    STATUS_GAUGE.set(STATUS_HEALTHY)
    if auto_rollback:
        promote(project_root)
    write_result(
        result_file,
        {
            "decision": "healthy",
            "version": version,
            "started_at": started_iso,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "peaks": peaks,
        },
    )
    return 0


def idle_loop(artifact: dict | None, collector: PrometheusCollector, interval: int) -> None:
    """Keep exposing metrics when the detector container is not verifying a deploy."""
    STATUS_GAUGE.set(STATUS_INIT)
    print(
        f"[{_ts()}] [monitor] idle mode — waiting for verify invocations / CI",
        flush=True,
    )
    while True:
        try:
            vector = collector.sample_vector()
            if artifact is not None:
                is_anomaly, score = score_sample(artifact, vector)
            else:
                is_anomaly, score = False, 0.0
            ANOMALY_SCORE.set(score)
            STATUS_GAUGE.set(STATUS_ANOMALY if is_anomaly else STATUS_HEALTHY)
            print(
                f"[{_ts()}] [idle] features={[round(v, 2) for v in vector]} "
                f"score={score:.3f} anomaly={is_anomaly}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[{_ts()}] [idle] {exc}", flush=True)
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="RewindAI post-deploy monitor")
    parser.add_argument("--version", default=os.environ.get("WATCH_VERSION", "unknown"))
    parser.add_argument(
        "--mode",
        choices=("isolation_forest", "threshold", "idle"),
        default=os.environ.get("MODE", "isolation_forest"),
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=Path(os.environ.get("MODEL_PATH", "models/baseline.joblib")),
    )
    parser.add_argument(
        "--window",
        type=int,
        default=int(os.environ.get("VERIFY_WINDOW_SECONDS", "180")),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("SAMPLE_INTERVAL_SECONDS", "5")),
    )
    parser.add_argument(
        "--majority",
        type=int,
        default=int(os.environ.get("MAJORITY_WINDOW", "5")),
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(os.environ.get("PROJECT_ROOT", "..")),
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=int(os.environ.get("METRICS_PORT", "9091")),
    )
    parser.add_argument(
        "--result-file",
        type=Path,
        default=None,
        help="Write JSON summary (decision, timestamps, peaks)",
    )
    parser.add_argument(
        "--no-rollback",
        action="store_true",
        help="Detect only; do not call rollback/promote scripts",
    )
    args = parser.parse_args()

    start_metrics_server(args.metrics_port, project_root=args.project_root.resolve())
    collector = PrometheusCollector()

    artifact = None
    if args.mode != "threshold":
        try:
            artifact = load_artifact(args.model)
            print(f"[{_ts()}] [monitor] loaded model {args.model}", flush=True)
        except Exception as exc:  # noqa: BLE001 — empty/corrupt joblib on USB, missing file, etc.
            if args.mode == "idle":
                print(
                    f"[{_ts()}] [monitor] model unavailable ({exc}); idle without scoring",
                    flush=True,
                )
            else:
                raise SystemExit(str(exc)) from exc

    if args.mode == "idle":
        idle_loop(artifact, collector, args.interval)
        return

    code = verify_deployment(
        artifact or {},
        collector,
        version=args.version,
        window_seconds=args.window,
        interval_seconds=args.interval,
        majority_window=args.majority,
        mode=args.mode,
        project_root=args.project_root.resolve(),
        auto_rollback=not args.no_rollback,
        result_file=args.result_file,
    )
    time.sleep(5)
    sys.exit(code)


if __name__ == "__main__":
    main()

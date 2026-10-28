"""Post-publish verification: Isolation Forest or thresholds, then rank/rollback."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from diagnosis import FEATURE_NAMES, LIMITS
from deployer import collect_logs, restore_version
from locate import build_report
from model import load_model, save_model, score_sample, train_model
from rank import headroom_score, pick_best
from registry import Registry
from watch import Watcher


def _ts(epoch: float | None = None) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(time.time() if epoch is None else epoch),
    )


def threshold_is_anomaly(vector: list[float]) -> bool:
    cpu, memory, latency_ms, error_pct = vector
    return (
        cpu > LIMITS["cpu"]
        or memory > LIMITS["memory"]
        or latency_ms > LIMITS["latency_p95"]
        or error_pct > LIMITS["error_rate"]
    )


MODEL_FLOOR = 0.5


def near_limit(vector: list[float]) -> bool:
    """Some signal is at least MODEL_FLOOR of its limit. The model is trained on a short, quiet window,
    so a flag below that is ordinary drift and must not roll back a healthy release."""
    return any(value >= MODEL_FLOOR * LIMITS[name] for name, value in zip(FEATURE_NAMES, vector))


def describe_vector(vector: list[float]) -> str:
    cpu, memory, latency_ms, error_pct = vector
    return (
        f"cpu {cpu:.0f}%, memory {memory:.0f}%, "
        f"p95 {latency_ms:,.0f} ms, errors {error_pct:.1f}%"
    )


class Recording:
    """Verify samples and timeline events for one release, saved as the verify runs."""

    def __init__(
        self,
        registry: Registry,
        app_id: str,
        version: str,
        *,
        window_s: int,
        interval_s: int,
        majority: int,
        mode: str,
    ) -> None:
        self.registry = registry
        self.app_id = app_id
        self.version = version
        self.started = time.time()
        self.data: dict = {
            "app_id": app_id,
            "version": version,
            "started_at": _ts(self.started),
            "window_s": window_s,
            "interval_s": interval_s,
            "majority": majority,
            "mode": mode,
            "limits": dict(LIMITS),
            "samples": [],
            "events": [],
            "decision": None,
            "finished": False,
        }

    def offset(self) -> float:
        return round(time.time() - self.started, 1)

    def event(self, kind: str, label: str) -> None:
        self.data["events"].append({"t": self.offset(), "kind": kind, "label": label})
        self.save()

    def sample(self, vector: list[float], *, score: float, anomaly: bool, votes: int) -> None:
        row = {
            "t": self.offset(),
            "score": round(float(score), 4),
            "anomaly": bool(anomaly),
            "votes": votes,
        }
        for name, value in zip(FEATURE_NAMES, vector):
            row[name] = round(float(value), 2)
        self.data["samples"].append(row)
        self.save()

    def finish(self, decision: str) -> None:
        self.data["decision"] = decision
        self.data["finished"] = True
        self.data["finished_at"] = _ts()
        self.save()

    def save(self) -> None:
        self.registry.write_recording(self.app_id, self.version, self.data)


class Verifier:
    def __init__(
        self,
        registry: Registry,
        watcher: Watcher,
        *,
        window_seconds: int | None = None,
        interval_seconds: int | None = None,
        majority: int | None = None,
        warmup_seconds: float | None = None,
    ) -> None:
        self.registry = registry
        self.watcher = watcher
        self.window_seconds = int(
            window_seconds
            or os.environ.get("VERIFY_WINDOW_SECONDS", "60")
        )
        self.interval_seconds = int(
            interval_seconds
            or os.environ.get("SAMPLE_INTERVAL_SECONDS", "5")
        )
        self.majority = int(majority or os.environ.get("MAJORITY_WINDOW", "3"))
        self.warmup_seconds = (
            min(4.0, float(self.interval_seconds))
            if warmup_seconds is None
            else float(warmup_seconds)
        )
        self._lock = threading.RLock()
        self._active: set[str] = set()
        self._progress: dict[str, dict] = {}

    def model_path(self, app_id: str) -> Path:
        return self.registry.app_dir(app_id) / "model.joblib"

    def healthy_csv(self, app_id: str) -> Path:
        return self.registry.app_dir(app_id) / "healthy_samples.csv"

    def active_version(self, app_id: str) -> str | None:
        with self._lock:
            progress = self._progress.get(app_id)
            return progress["version"] if progress else None

    def progress(self, app_id: str) -> dict | None:
        with self._lock:
            progress = self._progress.get(app_id)
            if not progress:
                return None
            out = dict(progress)
        out["elapsed_s"] = round(time.time() - out.pop("started"), 1)
        return out

    def _set_progress(self, app_id: str, **fields) -> None:
        with self._lock:
            if app_id in self._progress:
                self._progress[app_id].update(fields)

    def _append_healthy(self, app_id: str, vectors: list[list[float]]) -> None:
        path = self.healthy_csv(app_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8") as handle:
            if write_header:
                handle.write(",".join(FEATURE_NAMES) + "\n")
            for row in vectors:
                handle.write(",".join(f"{v:.6f}" for v in row) + "\n")

    def _retrain(self, app_id: str) -> None:
        path = self.healthy_csv(app_id)
        if not path.is_file():
            return
        rows = []
        with path.open(encoding="utf-8") as handle:
            handle.readline()
            for line in handle:
                parts = line.strip().split(",")
                if len(parts) != len(FEATURE_NAMES):
                    continue
                try:
                    rows.append([float(x) for x in parts])
                except ValueError:
                    continue
        if len(rows) < 8:
            return
        artifact = train_model(np.asarray(rows, dtype=float))
        save_model(artifact, self.model_path(app_id))

    def verify_async(self, app_id: str, version: str) -> None:
        key = f"{app_id}:{version}"
        with self._lock:
            if key in self._active:
                return
            self._active.add(key)

        def runner() -> None:
            try:
                self.verify(app_id, version)
            except Exception as exc:  # noqa: BLE001
                print(f"[verify] {key} failed: {exc}", flush=True)
                try:
                    self.registry.update_release(
                        app_id, version, status="error", verify_error=str(exc)
                    )
                    self.registry.update_app(app_id, last_decision="error")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                with self._lock:
                    self._active.discard(key)

        threading.Thread(target=runner, daemon=True, name=f"verify-{key}").start()

    def resume_interrupted(self) -> list[str]:
        """Restart verifies a restart cut short: the live release verifies again, others become unverified."""
        resumed = []
        for app in self.registry.list_apps():
            app_id = app["id"]
            for release in self.registry.list_releases(app_id):
                version = str(release.get("version") or "")
                if release.get("status") != "verifying" or self.active_version(app_id) == version:
                    continue
                if version == app.get("live_version"):
                    self.verify_async(app_id, version)
                    resumed.append(f"{app_id}:{version}")
                else:
                    self.registry.update_release(app_id, version, status="unverified")
        return resumed

    def verify(self, app_id: str, version: str) -> dict:
        app = self.registry.get_app(app_id)
        if not app:
            raise KeyError(app_id)
        if not self.registry.get_release(app_id, version):
            raise KeyError(version)

        artifact = load_model(self.model_path(app_id))
        mode = "isolation_forest" if artifact else "threshold"
        recording = Recording(
            self.registry,
            app_id,
            version,
            window_s=self.window_seconds,
            interval_s=self.interval_seconds,
            majority=self.majority,
            mode=mode,
        )
        with self._lock:
            self._progress[app_id] = {
                "version": version,
                "started": recording.started,
                "window_s": self.window_seconds,
                "majority": self.majority,
                "mode": mode,
                "votes": 0,
                "samples": 0,
                "last": None,
            }
        try:
            return self._run(app, version, recording, artifact, mode)
        finally:
            with self._lock:
                if (self._progress.get(app_id) or {}).get("version") == version:
                    self._progress.pop(app_id, None)

    def _run(self, app: dict, version: str, recording: Recording, artifact, mode: str) -> dict:
        app_id = app["id"]
        started_at = recording.started
        peaks = {name: 0.0 for name in FEATURE_NAMES}
        votes: deque[bool] = deque(maxlen=self.majority)
        healthy_vectors: list[list[float]] = []
        deadline = started_at + self.window_seconds
        self.watcher.set_status(app_id, 0)
        self.registry.update_app(app_id, last_decision="verifying")
        self.registry.update_release(app_id, version, status="verifying", decision=None)
        mode_label = "Isolation Forest" if mode == "isolation_forest" else "threshold rules"
        recording.event(
            "deployed",
            f"{version} is live. Verifying for {self.window_seconds} s with {mode_label}.",
        )

        # Let the watcher collect a few samples of the new release first.
        time.sleep(self.warmup_seconds)

        first_bad_seen = False
        while time.time() < deadline:
            vector = self.watcher.feature_vector(app_id, window_seconds=30.0, since=started_at)
            for name, value in zip(FEATURE_NAMES, vector):
                peaks[name] = max(peaks[name], value)

            if mode == "threshold" or artifact is None:
                is_anomaly = threshold_is_anomaly(vector)
                score = 1.0 if is_anomaly else 0.0
            else:
                flagged, score = score_sample(artifact, vector)
                is_anomaly = threshold_is_anomaly(vector) or (bool(flagged) and near_limit(vector))
            # Recordings and progress are JSON, so keep numpy scalars out.
            is_anomaly, score = bool(is_anomaly), float(score)

            self.watcher.set_anomaly_score(app_id, score)
            votes.append(is_anomaly)
            self.watcher.set_status(app_id, 2 if is_anomaly else 1)
            if not is_anomaly:
                healthy_vectors.append(vector)
            recording.sample(vector, score=score, anomaly=is_anomaly, votes=sum(votes))
            self._set_progress(
                app_id,
                votes=sum(votes),
                samples=len(recording.data["samples"]),
                last={
                    **{name: round(v, 2) for name, v in zip(FEATURE_NAMES, vector)},
                    "score": round(float(score), 4),
                    "anomaly": bool(is_anomaly),
                },
            )
            if is_anomaly and not first_bad_seen:
                first_bad_seen = True
                recording.event("first_bad_vote", f"First anomalous sample: {describe_vector(vector)}.")

            print(
                f"[verify] {app_id}@{version} cpu={vector[0]:.1f} mem={vector[1]:.1f} "
                f"lat={vector[2]:.0f} err={vector[3]:.2f} score={score:.3f} "
                f"anomaly={is_anomaly} votes={sum(votes)}/{len(votes)}",
                flush=True,
            )

            if len(votes) == self.majority and sum(votes) > (self.majority // 2):
                confidence = "HIGH" if sum(votes) >= self.majority - 1 else "MEDIUM"
                recording.event(
                    "majority",
                    f"{sum(votes)} of {self.majority} votes anomalous: {version} is bad ({confidence}).",
                )
                return self._on_anomaly(
                    app,
                    version,
                    recording=recording,
                    peaks=peaks,
                    detected_at=time.time(),
                    confidence=confidence,
                    mode=mode,
                )

            time.sleep(self.interval_seconds)

        score = headroom_score(peaks)
        result = {
            "decision": "healthy",
            "version": version,
            "app_id": app_id,
            "started_at": _ts(started_at),
            "finished_at": _ts(),
            "peaks": peaks,
            "score": score,
            "mode": mode,
            "most_affected_path": self.watcher.most_affected_path(app_id, since=started_at),
            "path_stats": self.watcher.path_stats(app_id, since=started_at),
        }
        self.registry.write_verify(app_id, version, result)
        self.registry.update_release(
            app_id,
            version,
            status="healthy",
            decision="healthy",
            score=score,
            quarantined=False,
            peaks=peaks,
        )
        if healthy_vectors:
            self._append_healthy(app_id, healthy_vectors)
            self._retrain(app_id)

        best = pick_best(self.registry.list_releases(app_id))
        self.registry.update_app(
            app_id,
            best_version=best.get("version") if best else version,
            last_decision="healthy",
            live_version=version,
        )
        self.watcher.set_status(app_id, 1)
        recording.event(
            "healthy",
            f"{version} passed the {self.window_seconds} s window with headroom score {score}.",
        )
        recording.finish("healthy")
        print(f"[verify] {app_id}@{version} HEALTHY score={score}", flush=True)
        return result

    def _on_anomaly(
        self,
        app: dict,
        version: str,
        *,
        recording: Recording,
        peaks: dict,
        detected_at: float,
        confidence: str,
        mode: str,
    ) -> dict:
        app_id = app["id"]
        started_at = recording.started
        affected = self.watcher.most_affected_path(app_id, since=started_at)
        path_stats = self.watcher.path_stats(app_id, since=started_at)

        # Capture logs before the swap removes the container.
        release = self.registry.get_release(app_id, version) or {}
        logs = collect_logs(app, since=release.get("deployed_at"))
        self.registry.write_logs(app_id, version, logs)
        line_count = sum(1 for line in logs.splitlines() if line.strip())
        recording.event("logs_captured", f"Saved {line_count} log lines from {version} before the swap.")

        score = headroom_score(peaks)
        result = {
            "decision": "anomaly",
            "version": version,
            "app_id": app_id,
            "confidence": confidence,
            "started_at": _ts(started_at),
            "detected_at": _ts(detected_at),
            "time_to_detect_s": round(detected_at - started_at, 1),
            "peaks": peaks,
            "score": score,
            "mode": mode,
            "most_affected_path": affected,
            "path_stats": path_stats,
            "rolled_back_at": None,
            "time_to_rollback_s": None,
            "restored_version": None,
        }

        self.registry.update_release(
            app_id,
            version,
            status="quarantined",
            decision="anomaly",
            quarantined=True,
            score=score,
            peaks=peaks,
            quarantine_reason=f"anomaly ({confidence})",
        )

        # Build the bug report while the bad release source is still on disk.
        report = build_report(self.registry, app_id, version, incident=result, logs=logs)
        self.registry.write_report(app_id, version, report)
        crossed = len((report.get("diagnosis") or {}).get("bugs") or [])
        suspects = report.get("suspects") or []
        label = f"Bug report: {crossed} crossed signal(s)"
        if suspects:
            top = suspects[0]
            label += f", top suspect {top.get('file')}:{top.get('line')}"
        recording.event("report_built", label + ".")
        result["report"] = {
            "summary": report.get("summary"),
            "suspect_count": len(suspects),
        }

        if app.get("on_anomaly", "rollback") == "rollback":
            others = [r for r in self.registry.list_releases(app_id) if r.get("version") != version]
            best = pick_best(others)
            if best:
                recording.event(
                    "rollback_started",
                    f"Rolling back to {best['version']} (BEST, score {best.get('score')}).",
                )
                try:
                    restore_version(self.registry, app_id, best["version"], force=True)
                    rolled_back_at = time.time()
                    result["rolled_back_at"] = _ts(rolled_back_at)
                    result["time_to_rollback_s"] = round(rolled_back_at - started_at, 1)
                    result["restored_version"] = best["version"]
                    self.registry.update_app(
                        app_id,
                        live_version=best["version"],
                        best_version=best["version"],
                        last_decision="rolled_back",
                    )
                    self.watcher.set_status(app_id, 3)
                    recording.event(
                        "restored",
                        f"{best['version']} is serving again, {result['time_to_rollback_s']} s after deploy.",
                    )
                except Exception as exc:  # noqa: BLE001
                    result["rollback_error"] = str(exc)
                    self.registry.update_app(app_id, last_decision="anomaly")
                    recording.event("rollback_failed", f"Rollback failed: {exc}")
            else:
                self.registry.update_app(app_id, last_decision="anomaly")
                recording.event(
                    "no_target",
                    f"No healthy release to roll back to, so {version} keeps serving.",
                )
        else:
            self.registry.update_app(app_id, last_decision="anomaly")
            recording.event(
                "waiting",
                f"on_anomaly is ask: {version} keeps serving until you revert or fix it.",
            )

        self.registry.write_verify(app_id, version, result)
        self.registry.write_app_blob(app_id, "last_incident.json", result)
        report["incident"] = {
            k: result.get(k)
            for k in (
                "decision",
                "version",
                "confidence",
                "started_at",
                "detected_at",
                "rolled_back_at",
                "time_to_detect_s",
                "time_to_rollback_s",
                "peaks",
                "most_affected_path",
                "path_stats",
                "restored_version",
            )
        }
        self.registry.write_report(app_id, version, report)
        recording.finish("anomaly")
        print(
            f"[verify] {app_id}@{version} ANOMALY ({confidence}) "
            f"ttd={result['time_to_detect_s']}s",
            flush=True,
        )
        return result

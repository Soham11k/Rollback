"""Post-publish verification: Isolation Forest or thresholds, then rank/rollback."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

import numpy as np

from diagnosis import FEATURE_NAMES, LIMITS, diagnose_incident
from deployer import collect_logs, restore_version
from locate import build_report
from model import load_model, save_model, score_sample, train_model
from rank import headroom_score, pick_best
from registry import Registry
from watch import Watcher


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def threshold_is_anomaly(vector: list[float]) -> bool:
    cpu, memory, latency_ms, error_pct = vector
    return (
        cpu > LIMITS["cpu"]
        or memory > LIMITS["memory"]
        or latency_ms > LIMITS["latency_p95"]
        or error_pct > LIMITS["error_rate"]
    )


class Verifier:
    def __init__(
        self,
        registry: Registry,
        watcher: Watcher,
        *,
        window_seconds: int | None = None,
        interval_seconds: int | None = None,
        majority: int | None = None,
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
        self._lock = threading.RLock()
        self._active: set[str] = set()

    def model_path(self, app_id: str) -> Path:
        return self.registry.app_dir(app_id) / "model.joblib"

    def healthy_csv(self, app_id: str) -> Path:
        return self.registry.app_dir(app_id) / "healthy_samples.csv"

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
            header = handle.readline()
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
            finally:
                with self._lock:
                    self._active.discard(key)

        threading.Thread(target=runner, daemon=True, name=f"verify-{key}").start()

    def verify(self, app_id: str, version: str) -> dict:
        app = self.registry.get_app(app_id)
        if not app:
            raise KeyError(app_id)
        release = self.registry.get_release(app_id, version)
        if not release:
            raise KeyError(version)

        started_at = time.time()
        started_iso = _ts()
        peaks = {name: 0.0 for name in FEATURE_NAMES}
        votes: deque[bool] = deque(maxlen=self.majority)
        healthy_vectors: list[list[float]] = []
        artifact = load_model(self.model_path(app_id))
        mode = "isolation_forest" if artifact else "threshold"
        deadline = started_at + self.window_seconds
        self.watcher.set_status(app_id, 0)
        self.registry.update_app(app_id, last_decision="verifying")
        self.registry.update_release(app_id, version, status="verifying", decision=None)

        # Let watcher collect a few samples first.
        time.sleep(min(4.0, float(self.interval_seconds)))

        detected_at = None
        while time.time() < deadline:
            vector = self.watcher.feature_vector(app_id, window_seconds=30.0)
            for name, value in zip(FEATURE_NAMES, vector):
                peaks[name] = max(peaks[name], value)

            if mode == "threshold" or artifact is None:
                is_anomaly = threshold_is_anomaly(vector)
                score = 1.0 if is_anomaly else 0.0
            else:
                is_anomaly, score = score_sample(artifact, vector)

            self.watcher.set_anomaly_score(app_id, score)
            votes.append(is_anomaly)
            self.watcher.set_status(app_id, 2 if is_anomaly else 1)
            if not is_anomaly:
                healthy_vectors.append(vector)

            print(
                f"[verify] {app_id}@{version} cpu={vector[0]:.1f} mem={vector[1]:.1f} "
                f"lat={vector[2]:.0f} err={vector[3]:.2f} score={score:.3f} "
                f"anomaly={is_anomaly} votes={sum(votes)}/{len(votes)}",
                flush=True,
            )

            if len(votes) == self.majority and sum(votes) > (self.majority // 2):
                detected_at = time.time()
                confidence = "HIGH" if sum(votes) >= self.majority - 1 else "MEDIUM"
                return self._on_anomaly(
                    app,
                    version,
                    peaks=peaks,
                    started_iso=started_iso,
                    started_at=started_at,
                    detected_at=detected_at,
                    confidence=confidence,
                    mode=mode,
                )

            time.sleep(self.interval_seconds)

        # Healthy
        score = headroom_score(peaks)
        result = {
            "decision": "healthy",
            "version": version,
            "app_id": app_id,
            "started_at": started_iso,
            "finished_at": _ts(),
            "peaks": peaks,
            "score": score,
            "mode": mode,
            "most_affected_path": self.watcher.most_affected_path(app_id),
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

        # Update best.
        releases = self.registry.list_releases(app_id)
        best = pick_best(releases)
        best_version = best.get("version") if best else version
        self.registry.update_app(
            app_id,
            best_version=best_version,
            last_decision="healthy",
            live_version=version,
        )
        self.watcher.set_status(app_id, 1)
        print(f"[verify] {app_id}@{version} HEALTHY score={score}", flush=True)
        return result

    def _on_anomaly(
        self,
        app: dict,
        version: str,
        *,
        peaks: dict,
        started_iso: str,
        started_at: float,
        detected_at: float,
        confidence: str,
        mode: str,
    ) -> dict:
        app_id = app["id"]
        affected = self.watcher.most_affected_path(app_id)
        # Capture logs before swap.
        deploy_iso = None
        release = self.registry.get_release(app_id, version) or {}
        deploy_iso = release.get("deployed_at")
        logs = collect_logs(app, since=deploy_iso)
        self.registry.write_logs(app_id, version, logs)

        score = headroom_score(peaks)
        result = {
            "decision": "anomaly",
            "version": version,
            "app_id": app_id,
            "confidence": confidence,
            "started_at": started_iso,
            "detected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(detected_at)),
            "time_to_detect_s": round(detected_at - started_at, 1),
            "peaks": peaks,
            "score": score,
            "mode": mode,
            "most_affected_path": affected,
            "rolled_back_at": None,
            "time_to_rollback_s": None,
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

        # Build bug report while the bad release source is still available.
        report = build_report(
            self.registry,
            app_id,
            version,
            incident=result,
            logs=logs,
        )
        self.registry.write_report(app_id, version, report)
        result["report"] = {
            "summary": report.get("summary"),
            "suspect_count": len(report.get("suspects") or []),
        }

        rolled_back_at = None
        if app.get("on_anomaly", "rollback") == "rollback":
            releases = self.registry.list_releases(app_id)
            # Exclude the bad version from best pick.
            others = [r for r in releases if r.get("version") != version]
            best = pick_best(others)
            if best and best.get("version") != version:
                try:
                    restore_version(
                        self.registry,
                        app_id,
                        best["version"],
                        force=True,
                    )
                    rolled_back_at = time.time()
                    result["rolled_back_at"] = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(rolled_back_at)
                    )
                    result["time_to_rollback_s"] = round(rolled_back_at - started_at, 1)
                    result["restored_version"] = best["version"]
                    self.registry.update_app(
                        app_id,
                        live_version=best["version"],
                        best_version=best["version"],
                        last_decision="anomaly",
                    )
                    self.watcher.set_status(app_id, 3)
                except Exception as exc:  # noqa: BLE001
                    result["rollback_error"] = str(exc)
                    self.registry.update_app(app_id, last_decision="anomaly")
            else:
                self.registry.update_app(app_id, last_decision="anomaly")
        else:
            self.registry.update_app(app_id, last_decision="anomaly")

        self.registry.write_verify(app_id, version, result)
        # Refresh report with rollback fields.
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
                "restored_version",
            )
        }
        self.registry.write_report(app_id, version, report)
        print(
            f"[verify] {app_id}@{version} ANOMALY ({confidence}) "
            f"ttd={result['time_to_detect_s']}s",
            flush=True,
        )
        return result

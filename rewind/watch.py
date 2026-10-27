"""Zero-instrumentation watcher: docker stats + HTTP probes."""

from __future__ import annotations

import json
import statistics
import subprocess
import threading
import time
from collections import defaultdict, deque
from typing import Any

import requests
from prometheus_client import Counter, Gauge, Histogram

from diagnosis import FEATURE_NAMES, LIMITS
from registry import Registry

CPU_GAUGE = Gauge(
    "rewind_container_cpu_percent",
    "Container CPU percent from docker stats",
    ["app"],
)
MEM_GAUGE = Gauge(
    "rewind_container_memory_percent",
    "Container memory percent from docker stats",
    ["app"],
)
PROBE_LATENCY = Histogram(
    "rewind_probe_seconds",
    "HTTP probe latency seconds",
    ["app", "path"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
PROBE_TOTAL = Counter(
    "rewind_probe_total",
    "HTTP probe results",
    ["app", "path", "status"],
)
ANOMALY_GAUGE = Gauge(
    "rewind_anomaly_score",
    "Latest anomaly score",
    ["app"],
)
LIVE_INFO = Gauge(
    "rewind_live_info",
    "Live version marker (always 1)",
    ["app", "version"],
)
STATUS_GAUGE = Gauge(
    "rewind_status",
    "0 init, 1 healthy, 2 anomaly, 3 rolled_back",
    ["app"],
)


def _docker_stats(name: str) -> tuple[float, float]:
    result = subprocess.run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            name,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return 0.0, 0.0
    try:
        row = json.loads(result.stdout.strip().splitlines()[0])
    except json.JSONDecodeError:
        return 0.0, 0.0
    cpu_raw = str(row.get("CPUPerc") or "0").replace("%", "").strip()
    mem_raw = str(row.get("MemPerc") or "0").replace("%", "").strip()
    try:
        cpu = float(cpu_raw)
    except ValueError:
        cpu = 0.0
    try:
        mem = float(mem_raw)
    except ValueError:
        mem = 0.0
    return max(0.0, cpu), max(0.0, mem)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


class Watcher:
    def __init__(self, registry: Registry, interval: float = 2.0) -> None:
        self.registry = registry
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # samples[app_id] -> deque of {t, cpu, memory, probes: {path: {ms, ok}}}
        self._samples: dict[str, deque] = defaultdict(lambda: deque(maxlen=600))
        self._lock = threading.RLock()
        self._scores: dict[str, float] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="rewind-watch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_anomaly_score(self, app_id: str, score: float) -> None:
        self._scores[app_id] = score
        ANOMALY_GAUGE.labels(app=app_id).set(score)

    def set_status(self, app_id: str, code: int) -> None:
        STATUS_GAUGE.labels(app=app_id).set(code)

    def _probe(self, app: dict, path: str) -> dict:
        alias = app.get("network_alias") or app["id"]
        port = int(app.get("container_port") or 8080)
        url = f"http://{alias}:{port}{path}"
        started = time.perf_counter()
        try:
            response = requests.get(url, timeout=5)
            ms = (time.perf_counter() - started) * 1000.0
            ok = response.status_code < 500
            status = str(response.status_code)
        except requests.RequestException:
            ms = (time.perf_counter() - started) * 1000.0
            ok = False
            status = "error"
        PROBE_LATENCY.labels(app=app["id"], path=path).observe(ms / 1000.0)
        PROBE_TOTAL.labels(app=app["id"], path=path, status=status).inc()
        return {"path": path, "ms": ms, "ok": ok, "status": status}

    def _tick_app(self, app: dict) -> None:
        app_id = app["id"]
        name = app.get("container_name") or f"rewind-{app_id}"
        cpu, mem = _docker_stats(name)
        CPU_GAUGE.labels(app=app_id).set(cpu)
        MEM_GAUGE.labels(app=app_id).set(mem)
        live = app.get("live_version") or "none"
        LIVE_INFO.labels(app=app_id, version=str(live)).set(1)

        probes = []
        for path in app.get("probe_paths") or ["/"]:
            probes.append(self._probe(app, path))
        # Health separately (not mixed into error rate).
        health = self._probe(app, app.get("health_path") or "/health")

        sample = {
            "t": time.time(),
            "cpu": cpu,
            "memory": mem,
            "probes": probes,
            "health": health,
        }
        with self._lock:
            self._samples[app_id].append(sample)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for app in self.registry.list_apps():
                    if not app.get("live_version"):
                        continue
                    try:
                        self._tick_app(app)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[watch] {app.get('id')}: {exc}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[watch] loop: {exc}", flush=True)
            self._stop.wait(self.interval)

    def _window(
        self,
        app_id: str,
        window_seconds: float,
        since: float | None = None,
    ) -> list[dict]:
        now = time.time()
        with self._lock:
            return [
                s
                for s in self._samples.get(app_id, [])
                if now - s["t"] <= window_seconds and (since is None or s["t"] >= since)
            ]

    def features(
        self,
        app_id: str,
        window_seconds: float = 30.0,
        since: float | None = None,
    ) -> dict[str, float]:
        samples = self._window(app_id, window_seconds, since)
        if not samples:
            return {name: 0.0 for name in FEATURE_NAMES}

        cpu = statistics.mean(s["cpu"] for s in samples)
        memory = statistics.mean(s["memory"] for s in samples)
        latencies: list[float] = []
        errors = 0
        total = 0
        for sample in samples:
            for probe in sample.get("probes") or []:
                total += 1
                latencies.append(float(probe.get("ms") or 0.0))
                if not probe.get("ok"):
                    errors += 1
        latency_p95 = _percentile(latencies, 95)
        error_rate = (100.0 * errors / total) if total else 0.0
        return {
            "cpu": float(cpu),
            "memory": float(memory),
            "latency_p95": float(latency_p95),
            "error_rate": float(error_rate),
        }

    def feature_vector(
        self,
        app_id: str,
        window_seconds: float = 30.0,
        since: float | None = None,
    ) -> list[float]:
        feats = self.features(app_id, window_seconds, since)
        return [feats[name] for name in FEATURE_NAMES]

    def path_stats(
        self,
        app_id: str,
        window_seconds: float = 30.0,
        since: float | None = None,
    ) -> dict[str, dict[str, float]]:
        by_path: dict[str, list[dict]] = defaultdict(list)
        for sample in self._window(app_id, window_seconds, since):
            for probe in sample.get("probes") or []:
                by_path[probe["path"]].append(probe)
        stats = {}
        for path, rows in by_path.items():
            errors = sum(1 for r in rows if not r.get("ok"))
            stats[path] = {
                "p95_ms": round(_percentile([float(r.get("ms") or 0) for r in rows], 95), 1),
                "error_pct": round(100.0 * errors / len(rows), 1),
                "count": len(rows),
            }
        return stats

    def most_affected_path(
        self,
        app_id: str,
        window_seconds: float = 30.0,
        since: float | None = None,
    ) -> str | None:
        best = None
        best_score = -1.0
        for path, row in self.path_stats(app_id, window_seconds, since).items():
            # Prefer error rate, then latency.
            score = row["error_pct"] * 10 + row["p95_ms"]
            if score > best_score:
                best_score = score
                best = path
        return best

    def series(self, app_id: str, minutes: int = 15) -> dict[str, Any]:
        now = time.time()
        cutoff = now - minutes * 60
        with self._lock:
            samples = [s for s in self._samples.get(app_id, []) if s["t"] >= cutoff]
        cpu = [{"t": s["t"], "v": s["cpu"]} for s in samples]
        memory = [{"t": s["t"], "v": s["memory"]} for s in samples]
        p95 = []
        err = []
        for s in samples:
            probes = s.get("probes") or []
            if not probes:
                p95.append({"t": s["t"], "v": 0.0})
                err.append({"t": s["t"], "v": 0.0})
                continue
            latencies = [float(p.get("ms") or 0) for p in probes]
            errors = sum(1 for p in probes if not p.get("ok"))
            p95.append({"t": s["t"], "v": _percentile(latencies, 95)})
            err.append({"t": s["t"], "v": 100.0 * errors / len(probes)})
        return {
            "ok": True,
            "cpu": cpu,
            "memory": memory,
            "p95_ms": p95,
            "error_pct": err,
            "window": {"start": cutoff, "end": now},
        }

    def latest_probes(self, app_id: str) -> dict[str, Any]:
        with self._lock:
            samples = list(self._samples.get(app_id, []))
        if not samples:
            return {"health": None, "probes": []}
        last = samples[-1]
        return {"health": last.get("health"), "probes": last.get("probes") or []}

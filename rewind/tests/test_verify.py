"""Verify loop with a fake watcher (no Docker): rollback on anomaly, BEST on healthy."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import verify
from registry import Registry
from verify import Verifier

FRT = "/api/checkout?event_id=EVT-02&tier=FRT"


class FakeWatcher:
    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.statuses: list[int] = []

    def feature_vector(self, app_id, window_seconds=30.0, since=None):
        return list(self.vector)

    def set_anomaly_score(self, app_id, score):
        pass

    def set_status(self, app_id, code):
        self.statuses.append(code)

    def most_affected_path(self, app_id, window_seconds=30.0, since=None):
        return FRT

    def path_stats(self, app_id, window_seconds=30.0, since=None):
        return {
            "/api/events": {"p95_ms": 30.0, "error_pct": 0.0, "count": 3},
            FRT: {"p95_ms": 1500.0, "error_pct": 100.0, "count": 3},
        }

    def latest_probes(self, app_id):
        return {"health": None, "probes": []}


@pytest.fixture()
def registry(tmp_path: Path) -> Registry:
    reg = Registry(tmp_path)
    reg.register_app({"id": "demo", "name": "Demo", "host_port": 8099, "probe_paths": ["/api/events", FRT]})
    reg.write_release(
        "demo",
        {"version": "1.0.0", "image": "rewind-demo:1.0.0", "decision": "healthy", "status": "healthy", "quarantined": False, "score": 92.0},
    )
    reg.write_release(
        "demo",
        {"version": "2.0.0", "image": "rewind-demo:2.0.0", "decision": None, "status": "live", "quarantined": False, "score": None},
    )
    reg.update_app("demo", live_version="2.0.0", best_version="1.0.0")
    return reg


def test_anomaly_rolls_back_to_best(registry: Registry, monkeypatch):
    restored = []
    monkeypatch.setattr(verify, "collect_logs", lambda app, since=None: "KeyError: 'FRT'\n")
    monkeypatch.setattr(
        verify,
        "restore_version",
        lambda reg, app_id, version, force=False: restored.append(version),
    )
    watcher = FakeWatcher([97.0, 30.0, 1420.0, 33.3])
    # interval below 1 s rounds to 0, so the three votes land without sleeping.
    verifier = Verifier(registry, watcher, window_seconds=30, interval_seconds=0.5, majority=3, warmup_seconds=0)

    result = verifier.verify("demo", "2.0.0")

    assert result["decision"] == "anomaly"
    assert result["restored_version"] == "1.0.0"
    assert restored == ["1.0.0"]
    app = registry.get_app("demo")
    assert app["live_version"] == "1.0.0"
    assert app["last_decision"] == "rolled_back"
    assert registry.get_release("demo", "2.0.0")["quarantined"] is True

    incident = registry.read_app_blob("demo", "last_incident.json")
    assert incident["version"] == "2.0.0"
    assert incident["path_stats"][FRT]["error_pct"] == 100.0

    recording = registry.read_recording("demo", "2.0.0")
    kinds = [e["kind"] for e in recording["events"]]
    assert kinds[0] == "deployed"
    assert kinds.index("first_bad_vote") < kinds.index("majority") < kinds.index("restored")
    assert kinds[-1] == "restored"
    assert [s["votes"] for s in recording["samples"]] == [1, 2, 3]
    assert recording["decision"] == "anomaly" and recording["finished"] is True
    assert verifier.active_version("demo") is None


def test_isolation_forest_mode_records_plain_json(registry: Registry, monkeypatch):
    import numpy as np

    from model import save_model, train_model

    monkeypatch.setattr(verify, "collect_logs", lambda app, since=None: "")
    monkeypatch.setattr(verify, "restore_version", lambda reg, app_id, version, force=False: None)
    verifier = Verifier(registry, FakeWatcher([97.0, 30.0, 1420.0, 33.3]), window_seconds=30, interval_seconds=0.5, majority=3, warmup_seconds=0)
    healthy = np.array([[3.0 + i % 3, 18.0 + i % 2, 4.0 + i % 4, 0.0] for i in range(12)])
    save_model(train_model(healthy), verifier.model_path("demo"))

    result = verifier.verify("demo", "2.0.0")

    assert (result["mode"], result["decision"]) == ("isolation_forest", "anomaly")
    recording = registry.read_recording("demo", "2.0.0")
    assert recording["mode"] == "isolation_forest"
    assert [s["votes"] for s in recording["samples"]] == [1, 2, 3]


def test_healthy_window_updates_best(registry: Registry):
    watcher = FakeWatcher([5.0, 10.0, 40.0, 0.0])
    verifier = Verifier(registry, watcher, window_seconds=2, interval_seconds=1, majority=3, warmup_seconds=0)

    result = verifier.verify("demo", "2.0.0")

    assert result["decision"] == "healthy"
    assert result["path_stats"]["/api/events"]["count"] == 3
    release = registry.get_release("demo", "2.0.0")
    assert release["decision"] == "healthy"
    assert release["score"] > 92.0
    app = registry.get_app("demo")
    assert app["last_decision"] == "healthy"
    assert app["best_version"] == "2.0.0"
    recording = registry.read_recording("demo", "2.0.0")
    assert recording["events"][-1]["kind"] == "healthy"
    assert recording["samples"] and not any(s["anomaly"] for s in recording["samples"])


def test_busy_guard_while_verifying(registry: Registry, monkeypatch):
    import server

    verifier = Verifier(registry, FakeWatcher([5.0, 10.0, 40.0, 0.0]), window_seconds=3, interval_seconds=1, majority=3, warmup_seconds=0)
    monkeypatch.setattr(server, "REGISTRY", registry)
    monkeypatch.setattr(server, "VERIFIER", verifier)
    monkeypatch.setattr(server, "ollama_reachable", lambda: {"ok": False, "model": "qwen2.5-coder:7b", "has_model": False})

    verifier.verify_async("demo", "2.0.0")
    deadline = time.time() + 2
    while verifier.active_version("demo") is None and time.time() < deadline:
        time.sleep(0.05)

    client = server.app.test_client()
    data = client.get("/api/apps/demo").get_json()
    assert data["verifying"]["version"] == "2.0.0"
    assert data["live_health"] == "verifying"
    assert data["hero"]["state"] == "verifying"
    assert client.post("/api/apps/demo/pick", json={"version": "1.0.0"}).status_code == 409

    deadline = time.time() + 10
    while verifier.active_version("demo") and time.time() < deadline:
        time.sleep(0.1)
    assert registry.get_release("demo", "2.0.0")["decision"] == "healthy"

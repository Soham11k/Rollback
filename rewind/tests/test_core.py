"""Unit tests for RewindAI core (no Docker required)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from diagnosis import diagnose_incident
from edits import apply_edits, normalize_edits, validate_edit
from locate import parse_tracebacks, rank_suspects, diff_hunks
from rank import headroom_score, pick_best
from registry import Registry


@pytest.fixture()
def registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path)


def test_register_and_list(registry: Registry):
    app = registry.register_app(
        {
            "id": "gatepass",
            "name": "GatePass",
            "host_port": 8080,
            "probe_paths": ["/api/events", "/api/checkout?event_id=EVT-01&tier=GA"],
        }
    )
    assert app["id"] == "gatepass"
    assert registry.get_app("gatepass")["name"] == "GatePass"
    with pytest.raises(ValueError):
        registry.register_app({"id": "gatepass"})


def test_diagnose_omits_under_limit():
    out = diagnose_incident(
        {
            "decision": "anomaly",
            "version": "2.0.0",
            "most_affected_path": "/api/checkout?event_id=EVT-02&tier=FRT",
            "peaks": {
                "cpu": 91.0,
                "memory": 70.0,
                "latency_p95": 2500.0,
                "error_rate": 18.0,
            },
        }
    )
    signals = [b["signal"] for b in out["bugs"]]
    assert "cpu" in signals
    assert "memory" not in signals
    assert out["bugs"][1]["location"].startswith("/api/checkout")


def test_diagnose_model_only_no_invented_path():
    out = diagnose_incident(
        {
            "decision": "anomaly",
            "most_affected_path": "/api/checkout",
            "peaks": {"cpu": 10, "memory": 10, "latency_p95": 10, "error_rate": 0},
        }
    )
    assert out["model_only"] is True
    assert out["bugs"][0]["location"] == "app process"
    assert "Do not invent a path" in out["bugs"][0]["fix"]


def test_headroom_and_best():
    assert headroom_score({"cpu": 0, "memory": 0, "latency_p95": 0, "error_rate": 0}) == 100.0
    releases = [
        {"version": "1.0.0", "decision": "healthy", "quarantined": False, "score": 90.0},
        {"version": "2.0.0", "decision": "anomaly", "quarantined": True, "score": 10.0},
        {"version": "3.0.0", "decision": "healthy", "quarantined": False, "score": 88.0},
    ]
    best = pick_best(releases)
    # within 5 points → newer wins
    assert best["version"] == "3.0.0"


def test_edits_apply_once(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    f = src / "hello.py"
    f.write_text("x = 1\ny = 2\n", encoding="utf-8")
    edits = normalize_edits([{"file": "hello.py", "search": "x = 1\n", "replace": "x = 3\n"}])
    changed, errors = apply_edits(src, edits)
    assert errors == []
    assert changed == ["hello.py"]
    assert "x = 3" in f.read_text(encoding="utf-8")
    assert validate_edit({"file": "../x", "search": "a", "replace": "b"})


def test_edits_indent_a_block_written_at_column_zero(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    f = src / "catalog.py"
    f.write_text("def checkout(code):\n    unit = int(10 * SURGE[code])\n    return unit\n", encoding="utf-8")
    edits = normalize_edits(
        [
            {
                "file": "catalog.py",
                "search": "unit = int(10 * SURGE[code])",
                "replace": "if code in SURGE:\n    unit = int(10 * SURGE[code])\nelse:\n    unit = 10",
            },
            {"file": "catalog.py", "search": "    return unit\n", "replace": "    total = unit\n    return total\n"},
        ]
    )
    changed, errors = apply_edits(src, edits)
    assert errors == []
    assert f.read_text(encoding="utf-8") == (
        "def checkout(code):\n"
        "    if code in SURGE:\n"
        "        unit = int(10 * SURGE[code])\n"
        "    else:\n"
        "        unit = 10\n"
        "    total = unit\n"
        "    return total\n"
    )


def test_parse_traceback_and_suspects(tmp_path: Path):
    logs = '''
Traceback (most recent call last):
  File "/app/catalog.py", line 188, in checkout
    multiplier = surge[code]
KeyError: 'FRT'
'''
    frames = parse_tracebacks(logs)
    assert frames
    assert frames[0]["exception"] == "KeyError"
    assert frames[0]["top"]["file"] == "catalog.py"

    base = tmp_path / "base"
    bad = tmp_path / "bad"
    base.mkdir()
    bad.mkdir()
    (base / "catalog.py").write_text("def checkout():\n    return 1\n", encoding="utf-8")
    (bad / "catalog.py").write_text(
        "def checkout():\n    surge = {}\n    return surge['FRT']\n",
        encoding="utf-8",
    )
    hunks = diff_hunks(base, bad)
    suspects = rank_suspects(hunks, frames)
    assert suspects
    assert suspects[0]["file"] == "catalog.py"


def _point_server_at(registry: Registry, monkeypatch) -> None:
    import server

    monkeypatch.setattr(server, "REGISTRY", registry)
    monkeypatch.setattr(server.WATCHER, "registry", registry)
    monkeypatch.setattr(server.VERIFIER, "registry", registry)
    monkeypatch.setattr(server.AIFIXER, "registry", registry)
    monkeypatch.setattr(server.GITHUB, "registry", registry)
    monkeypatch.setattr(
        server,
        "ollama_reachable",
        lambda: {"ok": False, "model": "qwen2.5-coder:7b", "has_model": False},
    )


def test_pick_api_rules(registry: Registry, monkeypatch):
    import server

    _point_server_at(registry, monkeypatch)
    registry.register_app({"id": "demo", "name": "Demo", "host_port": 8099})
    registry.write_release(
        "demo",
        {
            "version": "1.0.0",
            "image": "rewind-demo:1.0.0",
            "quarantined": False,
            "decision": "healthy",
            "score": 90,
            "status": "healthy",
        },
    )
    registry.write_release(
        "demo",
        {
            "version": "2.0.0",
            "image": "rewind-demo:2.0.0",
            "quarantined": True,
            "decision": "anomaly",
            "score": 5,
            "status": "quarantined",
        },
    )
    registry.update_app("demo", live_version="1.0.0", best_version="1.0.0")

    client = server.app.test_client()
    missing = client.post("/api/apps/demo/pick", json={"version": "9.9.9"})
    assert missing.status_code == 404
    blocked = client.post("/api/apps/demo/pick", json={"version": "2.0.0", "force": False})
    assert blocked.status_code == 409
    live = client.post("/api/apps/demo/pick", json={"version": "1.0.0"})
    assert live.status_code == 200
    assert live.get_json()["status"] == "already_live"


def test_pages_render(registry: Registry, monkeypatch):
    import server

    _point_server_at(registry, monkeypatch)
    registry.register_app({"id": "demo", "name": "Demo", "host_port": 8099})
    client = server.app.test_client()
    home = client.get("/")
    assert home.status_code == 200
    assert b"RewindAI" in home.data
    assert b"Demo" in home.data
    page = client.get("/apps/demo")
    assert page.status_code == 200
    assert b"Where the bugs are" in page.data

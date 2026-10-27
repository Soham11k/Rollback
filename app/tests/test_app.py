"""Tests for GatePass checkout and RewindAI redirect."""

import json
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("APP_VERSION", "test-1.0")
os.environ.setdefault("FAULT_MODE", "none")

from app import app  # noqa: E402
from edits_helper import apply_v2_bugs  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "test-1.0")
    monkeypatch.setenv("FAULT_MODE", "none")
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


def test_health_is_shallow(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "healthy"
    assert data["version"] == "test-1.0"
    assert "event" not in data


def test_home_lists_events(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "GatePass" in body
    assert "EVT-01" in body
    assert "RewindAI" in body


def test_known_event_api(client):
    response = client.get("/api/events/EVT-01")
    assert response.status_code == 200
    data = response.get_json()
    assert data["id"] == "EVT-01"
    assert any(tier["code"] == "GA" for tier in data["tiers"])


def test_checkout_confirms(client):
    response = client.get("/api/checkout?event_id=EVT-01&tier=GA&qty=1")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "confirmed"
    assert data["booking_ref"].startswith("GP-")


def test_missing_event_page(client):
    response = client.get("/events/NOPE")
    assert response.status_code == 404


def test_console_redirects_to_rewind(client, monkeypatch):
    monkeypatch.setenv("REWIND_PUBLIC_URL", "http://127.0.0.1:8090")
    for path in ("/console", "/desk"):
        response = client.get(path)
        assert response.status_code in (301, 302)
        assert response.headers["Location"] == "http://127.0.0.1:8090/apps/gatepass"


def test_metrics_exposes_prometheus(client):
    client.get("/health")
    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "app_http_requests_total" in body


def test_health_stays_up_while_checkout_faults(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "test-2.0")
    monkeypatch.setenv("FAULT_MODE", "errors")
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        statuses = {
            test_client.get("/api/checkout?event_id=EVT-01&tier=GA").status_code
            for _ in range(30)
        }
        assert 500 in statuses
        health = test_client.get("/health")
        assert health.status_code == 200


def test_v2_bugs_apply_and_frt_raises(tmp_path):
    root = Path(__file__).resolve().parents[1]
    bugs = json.loads((root.parent / "demo/gatepass/v2-bugs.json").read_text())
    dest = tmp_path / "src"
    apply_v2_bugs(root, dest, bugs)
    catalog = (dest / "catalog.py").read_text(encoding="utf-8")
    assert "_resign_holds()" in catalog
    assert "_SURGE[code]" in catalog
    assert "_RECENT_RECEIPTS.append" in catalog
    assert "Bug" not in catalog

    # Import patched catalog and expect KeyError on FRT.
    import importlib.util
    import sys

    sys.path.insert(0, str(dest))
    # Ensure seed db exists next to patched modules by copying data.
    import shutil

    shutil.copytree(root / "data", dest / "data", dirs_exist_ok=True)
    spec = importlib.util.spec_from_file_location("catalog_v2", dest / "catalog.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    with pytest.raises(KeyError):
        mod.checkout("EVT-02", "FRT", 1)
    started = time.perf_counter()
    ok = mod.checkout("EVT-01", "GA", 1)
    assert ok and ok["status"] == "confirmed"
    # The hold re-signing loop is the CPU and latency regression.
    assert time.perf_counter() - started > 0.3

"""Tests for Before-vs-After release comparison engine and HTTP endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest

import server
from compare import compare_releases
from registry import Registry

V1_CODE = """import hashlib

def checkout(code: str) -> dict:
    return {"status": "confirmed", "tier": code, "unit": 100}
"""

V2_CODE = """import hashlib

_HOLD_SECRET = b"gatepass-hold-v2"
_SURGE = {"GA": 1.0, "VIP": 1.15}
_RECENT_RECEIPTS: list[bytes] = []

def _resign_holds() -> int:
    hashlib.pbkdf2_hmac("sha256", b"seat", _HOLD_SECRET, 3600)
    return 1

def checkout(code: str) -> dict:
    _resign_holds()
    unit = int(100 * _SURGE[code])
    _RECENT_RECEIPTS.append(b"x" * 1024)
    return {"status": "confirmed", "tier": code, "unit": unit}
"""

V3_CODE = """import hashlib

_HOLD_SECRET = b"gatepass-hold-v2"
_SURGE = {"GA": 1.0, "VIP": 1.15}
_RECENT_RECEIPTS: list[bytes] = []

def _resign_holds() -> int:
    hashlib.pbkdf2_hmac("sha256", b"seat", _HOLD_SECRET, 3600)
    return 1

def checkout(code: str) -> dict:
    unit = int(100 * _SURGE.get(code, 1.0))
    return {"status": "confirmed", "tier": code, "unit": unit}
"""

DOCKERFILE = """FROM python:3.11-slim
WORKDIR /app
COPY . .
HEALTHCHECK --interval=5s CMD python -c "print(1)"
CMD ["python", "app.py"]
"""


@pytest.fixture()
def registry(tmp_path: Path) -> Registry:
    reg = Registry(tmp_path)
    reg.register_app(
        {
            "id": "gatepass",
            "name": "GatePass",
            "host_port": 8080,
            "probe_paths": ["/api/events", "/api/checkout?tier=GA", "/api/checkout?tier=FRT"],
        }
    )

    def add_rel(ver: str, code: str, decision: str, score: float, quarantined: bool, peaks: dict, paths: dict, fixes: str | None = None):
        reg.write_release(
            "gatepass",
            {
                "version": ver,
                "image": f"rewind-gatepass:{ver}",
                "kind": "source",
                "decision": decision,
                "status": decision,
                "score": score,
                "quarantined": quarantined,
                "fixes_version": fixes,
            },
        )
        src = reg.src_dir("gatepass", ver)
        src.mkdir(parents=True, exist_ok=True)
        (src / "catalog.py").write_text(code, encoding="utf-8")
        (src / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
        (src / "requirements.txt").write_text("flask==3.0.3\n", encoding="utf-8")
        reg.write_verify(
            "gatepass",
            ver,
            {
                "version": ver,
                "decision": decision,
                "peaks": peaks,
                "path_stats": paths,
            },
        )

    add_rel(
        "1.0.0",
        V1_CODE,
        "healthy",
        97.0,
        False,
        {"cpu": 2.0, "memory": 18.0, "latency_p95": 8.0, "error_rate": 0.0},
        {
            "/api/events": {"p95_ms": 4.0, "error_pct": 0.0},
            "/api/checkout?tier=GA": {"p95_ms": 8.0, "error_pct": 0.0},
            "/api/checkout?tier=FRT": {"p95_ms": 7.5, "error_pct": 0.0},
        },
    )
    add_rel(
        "2.0.0",
        V2_CODE,
        "anomaly",
        24.0,
        True,
        {"cpu": 99.5, "memory": 32.0, "latency_p95": 2450.0, "error_rate": 33.3},
        {
            "/api/events": {"p95_ms": 12.0, "error_pct": 0.0},
            "/api/checkout?tier=GA": {"p95_ms": 2450.0, "error_pct": 0.0},
            "/api/checkout?tier=FRT": {"p95_ms": 20.0, "error_pct": 100.0},
        },
    )
    add_rel(
        "3.0.0",
        V3_CODE,
        "healthy",
        96.5,
        False,
        {"cpu": 2.4, "memory": 19.0, "latency_p95": 9.0, "error_rate": 0.0},
        {
            "/api/events": {"p95_ms": 4.5, "error_pct": 0.0},
            "/api/checkout?tier=GA": {"p95_ms": 9.0, "error_pct": 0.0},
            "/api/checkout?tier=FRT": {"p95_ms": 8.5, "error_pct": 0.0},
        },
        fixes="2.0.0",
    )
    reg.update_app("gatepass", live_version="3.0.0", best_version="3.0.0")
    return reg


def test_compare_v1_to_v2_shows_regressions_and_introduced_findings(registry: Registry):
    diff = compare_releases(registry, "gatepass", "1.0.0", "2.0.0")

    assert diff["available"] is True
    assert diff["score_delta"] == -73.0
    by_sig = {s["signal"]: s for s in diff["signals"]}
    assert by_sig["cpu"]["status"] == "crossed"
    assert by_sig["cpu"]["delta"] == 97.5
    assert by_sig["latency_p95"]["status"] == "crossed"
    assert by_sig["error_rate"]["status"] == "crossed"

    by_route = {r["path"]: r for r in diff["routes"]}
    assert by_route["/api/checkout?tier=GA"]["status"] == "crossed"
    assert by_route["/api/checkout?tier=FRT"]["after_error_pct"] == 100.0

    added_names = {fn["name"] for fn in diff["ast_changes"]["added"]}
    modified_names = {fn["name"] for fn in diff["ast_changes"]["modified"]}
    assert "_resign_holds" in added_names
    assert "checkout" in modified_names

    intro_rules = {f["rule_id"] for f in diff["security"]["introduced"]}
    assert {"B105", "B400"}.issubset(intro_rules)
    assert len(diff["files"]) == 1
    assert diff["files"][0]["file"] == "catalog.py"
    assert any(r["type"] in {"insert", "replace"} for r in diff["files"][0]["side_by_side"])


def test_compare_v2_to_v3_shows_recovery_and_fixed_findings(registry: Registry):
    # Default pair when v3 fixes v2 should automatically be 2.0.0 -> 3.0.0
    diff = compare_releases(registry, "gatepass")

    assert (diff["before"]["version"], diff["after"]["version"]) == ("2.0.0", "3.0.0")
    assert diff["score_delta"] == 72.5
    by_sig = {s["signal"]: s for s in diff["signals"]}
    assert by_sig["cpu"]["status"] == "recovered"
    assert by_sig["latency_p95"]["status"] == "recovered"
    assert by_sig["error_rate"]["status"] == "recovered"

    fixed_rules = {f["rule_id"] for f in diff["security"]["fixed"]}
    assert {"B105", "B400"}.issubset(fixed_rules)
    assert diff["security"]["after_grade"] == "A"


def test_compare_and_scan_http_endpoints(registry: Registry, monkeypatch):
    monkeypatch.setattr(server, "REGISTRY", registry)
    monkeypatch.setattr(server.VERIFIER, "registry", registry)
    monkeypatch.setattr(server.AIFIXER, "registry", registry)
    monkeypatch.setattr(server.GITHUB, "registry", registry)
    client = server.app.test_client()

    res = client.get("/api/apps/gatepass/compare?before=1.0.0&after=2.0.0")
    assert res.status_code == 200
    body = res.get_json()
    assert body["before"]["version"] == "1.0.0"
    assert body["after"]["version"] == "2.0.0"
    assert len(body["presets"]) >= 3

    scan_res = client.get("/api/apps/gatepass/scan/2.0.0")
    assert scan_res.status_code == 200
    assert scan_res.get_json()["scan"]["version"] == "2.0.0"

    sbom_res = client.get("/api/apps/gatepass/sbom/1.0.0")
    assert sbom_res.status_code == 200
    assert sbom_res.get_json()["sbom"]["bomFormat"] == "CycloneDX"

    page = client.get("/apps/gatepass")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Before vs After — What Changed" in html
    assert "DevSecOps Release Scanner" in html

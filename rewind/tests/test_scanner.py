"""Tests for DevSecOps release scanner: Bandit SAST, pip-audit CVEs, Dockerfile hardening, SBOM, and security grading."""

from __future__ import annotations

from pathlib import Path

import pytest

from registry import Registry
from scanner import compute_grade, scan_release

V1_CATALOG = """import hashlib
import time

def checkout(code: str) -> dict:
    digest = hashlib.sha256(f"{code}-{int(time.time())}".encode()).hexdigest()[:8]
    return {"status": "confirmed", "ref": digest}
"""

V2_CATALOG = """import hashlib
import time

_HOLD_SECRET = b"gatepass-hold-v2"
_RECENT_RECEIPTS: list[bytes] = []

def _resign_holds() -> int:
    hashlib.pbkdf2_hmac("sha256", b"seat", _HOLD_SECRET, 3600)
    return 1

def checkout(code: str) -> dict:
    _resign_holds()
    digest = hashlib.sha1(f"{code}-{int(time.time())}".encode()).hexdigest()[:8]
    _RECENT_RECEIPTS.append(b"x" * 1024)
    return {"status": "confirmed", "ref": digest}
"""

DOCKERFILE_UNHARDENED = """FROM python:latest
WORKDIR /app
ENV API_SECRET=supersecret
COPY . .
CMD ["python", "app.py"]
"""

DOCKERFILE_HARDENED = """FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN useradd -r appuser
USER appuser
HEALTHCHECK --interval=5s CMD python -c "print(1)"
CMD ["python", "app.py"]
"""


@pytest.fixture()
def registry(tmp_path: Path) -> Registry:
    reg = Registry(tmp_path)
    reg.register_app({"id": "demo", "name": "Demo", "host_port": 8099})
    return reg


def _write_release_tree(reg: Registry, version: str, catalog_code: str, dockerfile: str, reqs: str) -> None:
    reg.write_release("demo", {"version": version, "image": f"rewind-demo:{version}", "kind": "source"})
    src = reg.src_dir("demo", version)
    src.mkdir(parents=True, exist_ok=True)
    (src / "catalog.py").write_text(catalog_code, encoding="utf-8")
    (src / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    (src / "requirements.txt").write_text(reqs, encoding="utf-8")


def test_clean_release_gets_grade_a_and_sbom(registry: Registry):
    _write_release_tree(
        registry,
        "1.0.0",
        V1_CATALOG,
        DOCKERFILE_HARDENED,
        "flask==3.0.3\ngunicorn==22.0.0\n",
    )
    scan = scan_release(registry, "demo", "1.0.0")

    assert scan["grade"] == "A"
    assert scan["score"] == 100
    assert scan["counts"]["total"] == 0
    assert scan["sbom_summary"]["package_count"] == 2
    sbom = registry.read_sbom("demo", "1.0.0")
    assert sbom is not None
    assert sbom["bomFormat"] == "CycloneDX"
    purls = {c["purl"] for c in sbom["components"]}
    assert "pkg:pypi/flask@3.0.3" in purls
    rel = registry.get_release("demo", "1.0.0")
    assert rel["security_grade"] == "A"


def test_buggy_release_flags_sast_dockerfile_and_dependency_cves(registry: Registry):
    _write_release_tree(
        registry,
        "2.0.0",
        V2_CATALOG,
        DOCKERFILE_UNHARDENED,
        "flask==3.0.3\npyyaml==5.3.1\nrequests\n",
    )
    scan = scan_release(registry, "demo", "2.0.0")

    rule_ids = {f["rule_id"] for f in scan["findings"]}
    # Bandit / AST SAST: hardcoded secret (B105), weak SHA-1 (B324), unbounded list growth (B400)
    assert "B105" in rule_ids
    assert "B324" in rule_ids
    assert "B400" in rule_ids
    # pip-audit: CVE-2020-14343 on pyyaml 5.3.1 + unpinned requests
    assert "CVE-2020-14343" in rule_ids
    assert "DEP-UNPINNED" in rule_ids
    # Dockerfile: root user (DKR-001), :latest tag (DKR-002), missing HEALTHCHECK (DKR-003), secret ENV (DKR-004)
    assert {"DKR-001", "DKR-002", "DKR-003", "DKR-004"}.issubset(rule_ids)
    assert scan["grade"] in {"D", "F"}
    assert scan["counts"]["critical"] >= 1
    assert scan["counts"]["high"] >= 2


def test_dead_helper_secret_is_not_flagged_after_ai_fix(registry: Registry):
    # When AI fix removes _resign_holds() from checkout() and removes _RECENT_RECEIPTS.append(),
    # the unused _HOLD_SECRET and _RECENT_RECEIPTS should no longer be flagged as active findings.
    fixed_catalog = V2_CATALOG.replace("    _resign_holds()\n", "").replace(
        '    _RECENT_RECEIPTS.append(b"x" * 1024)\n', ""
    )
    _write_release_tree(
        registry,
        "3.0.0",
        fixed_catalog,
        DOCKERFILE_HARDENED,
        "flask==3.0.3\n",
    )
    scan = scan_release(registry, "demo", "3.0.0")
    rule_ids = {f["rule_id"] for f in scan["findings"]}
    assert "B105" not in rule_ids
    assert "B400" not in rule_ids
    assert scan["grade"] == "A"


def test_compute_grade_boundaries():
    grade_a, score_a, _ = compute_grade([])
    assert (grade_a, score_a) == ("A", 100)
    grade_c, _, _ = compute_grade([{"severity": "high"}, {"severity": "medium"}])
    assert grade_c == "C"
    grade_f, _, _ = compute_grade([{"severity": "critical"}, {"severity": "high"}, {"severity": "high"}])
    assert grade_f == "F"

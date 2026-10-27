"""AI fix with Ollama mocked: one repair retry, fail fast without Ollama, ship on the bad release."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import aifix
from registry import Registry

BEST = "def checkout(code):\n    return 1.0\n"
BAD = "def checkout(code):\n    surge = {'GA': 1.0}\n    return surge[code]\n"


class FakeResponse:
    def __init__(self, proposal: dict) -> None:
        self.proposal = proposal

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"message": {"content": json.dumps(self.proposal)}}


@pytest.fixture()
def registry(tmp_path: Path) -> Registry:
    reg = Registry(tmp_path)
    reg.register_app({"id": "demo", "name": "Demo", "host_port": 8099})
    for version, text in (("1.0.0", BEST), ("2.0.0", BAD)):
        reg.write_release(
            "demo",
            {"version": version, "image": f"rewind-demo:{version}", "kind": "source", "quarantined": version == "2.0.0"},
        )
        src = reg.src_dir("demo", version)
        src.mkdir(parents=True)
        (src / "catalog.py").write_text(text, encoding="utf-8")
    reg.write_report(
        "demo",
        "2.0.0",
        {
            "version": "2.0.0",
            "best_version": "1.0.0",
            "hunks": [{"file": "catalog.py"}],
            "diagnosis": {"bugs": [{"signal": "error_rate", "value": 33.0, "unit": "%", "limit": 5.0, "location": "/api/checkout"}]},
            "suspects": [{"file": "catalog.py", "line": 3, "from": "traceback", "reason": "KeyError 'FRT'"}],
            "tracebacks": [],
        },
    )
    return reg


def _ollama_on(monkeypatch) -> None:
    monkeypatch.setattr(
        aifix,
        "ollama_reachable",
        lambda: {"ok": True, "has_model": True, "model": "qwen2.5-coder:7b", "url": "http://ollama"},
    )


def _wait(fixer: aifix.AIFixer, app_id: str) -> dict:
    deadline = time.time() + 10
    while time.time() < deadline:
        job = fixer.get_fix(app_id)
        if job and job.get("status") != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("fix job did not finish")


def test_fix_retries_once_with_exact_lines(registry: Registry, monkeypatch):
    replies = [
        {"summary": "first try", "edits": [{"file": "catalog.py", "search": "return surge[code ]", "replace": "return 1.0"}]},
        {
            "summary": "Unknown tiers raise KeyError.",
            "bugs": [{"file": "catalog.py", "line": 3, "reason": "KeyError for FRT"}],
            "edits": [{"file": "catalog.py", "search": "    return surge[code]\n", "replace": "    return surge.get(code, 1.0)\n"}],
        },
    ]
    requests_sent = []

    def fake_post(url, **kwargs):
        requests_sent.append(kwargs["json"])
        return FakeResponse(replies[len(requests_sent) - 1])

    _ollama_on(monkeypatch)
    monkeypatch.setattr(aifix.requests, "post", fake_post)
    fixer = aifix.AIFixer(registry)

    assert fixer.start_fix("demo")["status"] == "running"
    job = _wait(fixer, "demo")

    assert job["status"] == "ready", job.get("error")
    assert [len(a["errors"]) > 0 for a in job["attempts"]] == [True, False]
    assert "surge.get(code, 1.0)" in job["diff"]
    first = requests_sent[0]
    assert first["options"]["num_ctx"] == aifix.NUM_CTX
    prompt = first["messages"][1]["content"]
    assert "best/catalog.py" in prompt
    assert "surge = {'GA': 1.0}" in prompt
    repair = requests_sent[1]["messages"][-1]["content"]
    assert "could not be applied" in repair
    assert "return surge[code]" in repair


def test_fix_retries_when_the_traceback_line_is_untouched(registry: Registry, monkeypatch):
    report = registry.read_report("demo", "2.0.0")
    report["tracebacks"] = [{"exception": "KeyError", "message": "'FRT'", "top": {"file": "catalog.py", "line": 3}, "count": 4}]
    registry.write_report("demo", "2.0.0", report)
    replies = [
        {
            "summary": "Only the table changes.",
            "edits": [{"file": "catalog.py", "search": "    surge = {'GA': 1.0}\n    return surge[code]\n", "replace": "    surge = {'GA': 1.0, 'VIP': 2.0}\n    return surge[code]\n"}],
        },
        {
            "summary": "Unknown tiers fall back to 1.0.",
            "edits": [{"file": "catalog.py", "search": "    return surge[code]\n", "replace": "    return surge.get(code, 1.0)\n"}],
        },
    ]
    requests_sent = []

    def fake_post(url, **kwargs):
        requests_sent.append(kwargs["json"])
        return FakeResponse(replies[len(requests_sent) - 1])

    _ollama_on(monkeypatch)
    monkeypatch.setattr(aifix.requests, "post", fake_post)
    fixer = aifix.AIFixer(registry)
    fixer.start_fix("demo")
    job = _wait(fixer, "demo")

    assert job["status"] == "ready", job.get("error")
    assert "catalog.py:3 still raises KeyError" in job["attempts"][0]["errors"][0]
    assert job["attempts"][1]["errors"] == []
    repair = requests_sent[1]["messages"][-1]["content"]
    assert "Keep every edit you already made" in repair
    assert "return surge[code]" in repair


def test_fix_fails_fast_without_ollama(registry: Registry, monkeypatch):
    monkeypatch.setattr(
        aifix,
        "ollama_reachable",
        lambda: {"ok": False, "url": "http://127.0.0.1:9", "message": "refused", "model": "qwen2.5-coder:7b"},
    )
    job = aifix.AIFixer(registry).start_fix("demo")
    assert job["status"] == "failed"
    assert "ai-setup.sh" in job["error"]


def test_ship_publishes_edits_on_the_bad_release(registry: Registry, monkeypatch):
    edits = [{"file": "catalog.py", "search": "    return surge[code]\n", "replace": "    return surge.get(code, 1.0)\n"}]
    registry.write_job("fix-1", {"status": "ready", "app_id": "demo", "version": "2.0.0", "edits": edits})
    registry.write_app_blob("demo", "last_fix.json", {"job_id": "fix-1"})
    published = {}

    def fake_publish(reg, app_id, **kwargs):
        published.update(kwargs)
        reg.write_release(app_id, {"version": kwargs["version"], "image": f"rewind-demo:{kwargs['version']}"})
        return reg.get_release(app_id, kwargs["version"])

    monkeypatch.setattr(aifix, "publish_release", fake_publish)
    result = aifix.AIFixer(registry).ship("demo", new_version="3.0.0")

    assert result["version"] == "3.0.0"
    assert published["base_version"] == "2.0.0"
    assert published["edits"][0]["file"] == "catalog.py"
    assert registry.get_release("demo", "3.0.0")["fixes_version"] == "2.0.0"
    assert registry.read_job("fix-1")["status"] == "shipped"

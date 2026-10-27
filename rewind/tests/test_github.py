"""GitHub poller with HTTP mocked: refs, safe extract, baseline, folder changes, versions."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
import requests

import github
from registry import Registry

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _pkt(line: str) -> bytes:
    data = line.encode()
    return f"{len(data) + 4:04x}".encode() + data


def refs_payload(heads: dict[str, str]) -> bytes:
    out = _pkt("# service=git-upload-pack\n") + b"0000"
    for index, (name, sha) in enumerate(heads.items()):
        caps = "\0multi_ack thin-pack side-band" if index == 0 else ""
        out += _pkt(f"{sha} {name}{caps}\n")
    return out + b"0000"


def tarball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"Rollback-abc/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        escape = tarfile.TarInfo("Rollback-abc/../escape.txt")
        escape.size = 1
        tar.addfile(escape, io.BytesIO(b"x"))
        link = tarfile.TarInfo("Rollback-abc/app/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, content: bytes = b"", status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeGitHub:
    def __init__(self) -> None:
        self.heads: dict[str, str] = {}
        self.trees: dict[str, dict[str, str]] = {}

    def get(self, url: str, **kwargs) -> FakeResponse:
        if "/info/refs" in url:
            return FakeResponse(refs_payload({f"refs/heads/{b}": s for b, s in self.heads.items()}))
        if "codeload.github.com" in url:
            return FakeResponse(tarball(self.trees[url.rsplit("/", 1)[-1]]))
        return FakeResponse(status_code=404)


class FakeVerifier:
    def __init__(self) -> None:
        self.busy: str | None = None
        self.started: list[str] = []

    def active_version(self, app_id: str) -> str | None:
        return self.busy

    def verify_async(self, app_id: str, version: str) -> None:
        self.started.append(version)


@pytest.fixture()
def registry(tmp_path: Path) -> Registry:
    reg = Registry(tmp_path)
    reg.register_app({"id": "demo", "name": "Demo", "host_port": 8099})
    return reg


@pytest.fixture()
def world(registry: Registry, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    fake = FakeGitHub()
    verifier = FakeVerifier()
    published: list[tuple] = []

    def fake_publish(reg, app_id, *, version, source_dir, extra):
        published.append((version, sorted(p.name for p in Path(source_dir).iterdir()), extra["commit"]["sha"]))
        reg.write_release(app_id, {"version": version, "image": f"rewind-demo:{version}", **extra})
        return reg.get_release(app_id, version)

    poller = github.GitHubPoller(registry, verifier, session=fake, publish=fake_publish)
    return fake, verifier, published, poller


def test_parse_refs_reads_heads():
    refs = github.parse_refs(refs_payload({"refs/heads/main": SHA_A, "refs/heads/release": SHA_B}))
    assert refs == {"refs/heads/main": SHA_A, "refs/heads/release": SHA_B}


def test_extract_keeps_only_plain_files_inside(tmp_path: Path):
    out = github.extract_tarball(tarball({"app/app.py": "print(1)\n"}), tmp_path / "out")
    assert (out / "app" / "app.py").read_text() == "print(1)\n"
    assert not (out / "app" / "link").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_version_for_uses_version_file(tmp_path: Path):
    (tmp_path / "VERSION").write_text("1.1.0\n")
    assert github.version_for(tmp_path, SHA_B, set()) == "1.1.0"
    assert github.version_for(tmp_path, SHA_B, {"1.1.0"}) == "1.1.0-bbbbbbb"
    (tmp_path / "VERSION").unlink()
    assert github.version_for(tmp_path, SHA_B, set()) == "git-bbbbbbb"


def test_normalize_inputs():
    assert github.normalize_repo("https://github.com/Soham11k/Rollback.git") == "Soham11k/Rollback"
    assert github.normalize_path("./app/") == "app"
    assert github.normalize_path("") == "."
    with pytest.raises(ValueError):
        github.normalize_repo("not a repo")
    with pytest.raises(ValueError):
        github.normalize_path("../etc")


def test_baseline_then_deploy_only_when_folder_changes(registry: Registry, world):
    fake, verifier, published, poller = world
    registry.write_release("demo", {"version": "1.0.0", "image": "rewind-demo:1.0.0", "decision": "healthy"})
    fake.heads["release"] = SHA_A
    fake.trees[SHA_A] = {"app/VERSION": "1.0.0\n", "app/app.py": "print(1)\n", "README.md": "a"}

    state = poller.connect("demo", "https://github.com/Soham11k/Rollback.git", "release", "./app/")
    assert (state["repo"], state["path"], state["state"], state["last_sha"]) == ("Soham11k/Rollback", "app", "watching", SHA_A)

    poller.check("demo")
    assert published == []

    fake.heads["release"] = SHA_B
    fake.trees[SHA_B] = {**fake.trees[SHA_A], "README.md": "b"}
    unchanged = poller.check("demo")
    assert published == []
    assert unchanged["last_sha"] == SHA_B and "unchanged" in unchanged["message"]

    fake.heads["release"] = SHA_C
    fake.trees[SHA_C] = {**fake.trees[SHA_B], "app/VERSION": "1.1.0\n", "app/app.py": "print(2)\n"}
    verifier.busy = "1.0.0"
    assert poller.check("demo")["state"] == "waiting"
    assert published == []

    verifier.busy = None
    done = poller.check("demo")
    assert published == [("1.1.0", ["VERSION", "app.py"], SHA_C)]
    assert verifier.started == ["1.1.0"]
    assert (done["state"], done["last_version"], done["last_sha"]) == ("verifying", "1.1.0", SHA_C)
    assert registry.get_release("demo", "1.1.0")["commit"]["url"].endswith(SHA_C)


def test_missing_branch_waits_then_first_commit_deploys(registry: Registry, world):
    fake, verifier, published, poller = world
    fake.heads["main"] = SHA_A

    state = poller.connect("demo", "Soham11k/Rollback", "release", "app")
    assert state["state"] == "waiting" and state["last_sha"] is None

    fake.heads["release"] = SHA_A
    fake.trees[SHA_A] = {"app/VERSION": "1.0.0\n", "app/app.py": "print(1)\n"}
    poller.check("demo")
    assert [p[0] for p in published] == ["1.0.0"]
    assert verifier.started == ["1.0.0"]


def test_github_api_validates_and_disconnects(registry: Registry, monkeypatch):
    import server

    monkeypatch.setattr(server, "REGISTRY", registry)
    monkeypatch.setattr(server.GITHUB, "registry", registry)
    client = server.app.test_client()

    bad = client.post("/api/apps/demo/github", json={"repo": "not a repo"})
    assert bad.status_code == 400
    assert "owner/name" in bad.get_json()["message"]

    registry.update_app("demo", github={"repo": "o/n", "branch": "release", "path": "."})
    gone = client.delete("/api/apps/demo/github")
    assert gone.status_code == 200
    assert registry.get_app("demo")["github"] is None

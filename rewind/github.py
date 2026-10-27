"""Deploy from GitHub: watch a branch and publish each commit that changes the app folder.

Branch heads come from git smart HTTP (the same call `git ls-remote` makes) and code from
codeload tarballs, so polling stays clear of the REST API rate limit. Set GITHUB_TOKEN for
private repos.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import requests

from deployer import publish_release
from registry import Registry

POLL_SECONDS = float(os.environ.get("GITHUB_POLL_SECONDS", "30"))
MAX_TARBALL_BYTES = 200 * 1024 * 1024
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SKIP_NAMES = {"__pycache__", ".pytest_cache", ".DS_Store", ".git"}


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN", "").strip()


def normalize_repo(raw: str) -> str:
    repo = (raw or "").strip()
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:", "github.com/"):
        if repo.startswith(prefix):
            repo = repo[len(prefix):]
    repo = repo.strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not _REPO_RE.match(repo):
        raise ValueError("repo must look like owner/name")
    return repo


def normalize_path(raw: str | None) -> str:
    path = (raw or ".").strip().strip("/")
    while path.startswith("./"):
        path = path[2:]
    if not path or path == ".":
        return "."
    if ".." in Path(path).parts:
        raise ValueError("folder must stay inside the repo")
    return path


def parse_pkt_lines(payload: bytes) -> list[bytes]:
    """Data lines of a git pkt-line stream; flush packets are dropped."""
    lines = []
    i = 0
    while i + 4 <= len(payload):
        try:
            size = int(payload[i : i + 4], 16)
        except ValueError:
            break
        if size < 4:
            i += 4
            continue
        lines.append(payload[i + 4 : i + size])
        i += size
    return lines


def parse_refs(payload: bytes) -> dict[str, str]:
    refs = {}
    for line in parse_pkt_lines(payload):
        text = line.decode("utf-8", "replace").rstrip("\n").split("\0", 1)[0]
        if text.startswith("#"):
            continue
        sha, _, name = text.partition(" ")
        if len(sha) == 40 and name:
            refs[name] = sha
    return refs


def branch_sha(repo: str, branch: str, session=requests) -> str | None:
    """Head commit of a branch, or None when the branch does not exist."""
    token = _token()
    response = session.get(
        f"https://github.com/{repo}.git/info/refs?service=git-upload-pack",
        headers={"User-Agent": "git/2.43 rewindai"},
        auth=("x-access-token", token) if token else None,
        timeout=20,
    )
    if response.status_code in (401, 403, 404):
        raise ValueError(f"GitHub repo {repo} is not reachable (HTTP {response.status_code}). Set GITHUB_TOKEN for private repos.")
    response.raise_for_status()
    return parse_refs(response.content).get(f"refs/heads/{branch}")


def download_tarball(repo: str, sha: str, session=requests) -> bytes:
    token = _token()
    if token:
        url = f"https://api.github.com/repos/{repo}/tarball/{sha}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    else:
        url = f"https://codeload.github.com/{repo}/tar.gz/{sha}"
        headers = {}
    response = session.get(url, headers=headers, timeout=120)
    response.raise_for_status()
    if len(response.content) > MAX_TARBALL_BYTES:
        raise ValueError("repo tarball is larger than 200 MB")
    return response.content


def extract_tarball(data: bytes, dest: Path) -> Path:
    """Extract a GitHub tarball without its top folder. Only plain files and folders land."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if len(parts) < 2 or ".." in parts or Path(member.name).is_absolute():
                continue
            target = dest.joinpath(*parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                continue
            with open(target, "wb") as out:
                shutil.copyfileobj(source, out)
            if member.mode & 0o111:
                target.chmod(0o755)
    return dest


def _skip(rel: Path) -> bool:
    return any(part in _SKIP_NAMES or part.startswith("._") for part in rel.parts) or rel.suffix == ".pyc"


def tree_hash(folder: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if not path.is_file() or _skip(rel):
            continue
        digest.update(rel.as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def version_for(folder: Path, sha: str, existing: set[str]) -> str:
    """VERSION file in the folder, else git-<sha7>; a reused label gets -<sha7> appended."""
    label = ""
    version_file = folder / "VERSION"
    if version_file.is_file():
        lines = version_file.read_text(errors="replace").strip().splitlines()
        label = lines[0].strip() if lines else ""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,50}$", label):
        label = f"git-{sha[:7]}"
    if label in existing:
        label = f"{label}-{sha[:7]}"
    return label


class GitHubPoller:
    def __init__(
        self,
        registry: Registry,
        verifier,
        *,
        interval: float | None = None,
        session=requests,
        publish: Callable[..., dict] = publish_release,
    ) -> None:
        self.registry = registry
        self.verifier = verifier
        self.interval = POLL_SECONDS if interval is None else interval
        self.session = session
        self.publish = publish
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="rewind-github")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.poll_once()

    def poll_once(self) -> None:
        for app in self.registry.list_apps():
            if (app.get("github") or {}).get("repo"):
                try:
                    self.check(app["id"])
                except Exception as exc:  # noqa: BLE001
                    print(f"[github] {app['id']}: {exc}", flush=True)

    def _update(self, app_id: str, **fields) -> dict:
        app = self.registry.get_app(app_id) or {}
        github = dict(app.get("github") or {})
        github.update(fields, checked_at=_ts())
        self.registry.update_app(app_id, github=github)
        return github

    def connect(self, app_id: str, repo: str, branch: str = "release", path: str = ".") -> dict:
        app = self.registry.get_app(app_id)
        if not app:
            raise KeyError(app_id)
        repo = normalize_repo(repo)
        branch = (branch or "release").strip() or "release"
        path = normalize_path(path)
        sha = branch_sha(repo, branch, self.session)
        github = {"repo": repo, "branch": branch, "path": path, "last_sha": None, "tree_hash": None}
        where = f"{repo}@{branch}" + ("" if path == "." else f" ({path}/)")
        if sha is None:
            github.update(state="waiting", message=f"Branch {branch} does not exist yet. CI creates it when main passes; its first commit deploys.")
        elif not self.registry.list_releases(app_id):
            github.update(state="pending", message=f"Connected to {where}. Deploying {sha[:7]} on the next poll.")
        else:
            with tempfile.TemporaryDirectory(prefix="rewind-gh-") as tmp:
                root = extract_tarball(download_tarball(repo, sha, self.session), Path(tmp))
                folder = root if path == "." else root / path
                if not folder.is_dir():
                    raise ValueError(f"Folder {path}/ is not in {repo}@{branch}")
                github.update(last_sha=sha, tree_hash=tree_hash(folder))
            github.update(state="watching", message=f"Watching {where} from {sha[:7]}. The next commit that changes the folder deploys.")
        github["checked_at"] = _ts()
        self.registry.update_app(app_id, github=github)
        return github

    def disconnect(self, app_id: str) -> None:
        self.registry.update_app(app_id, github=None)

    def check(self, app_id: str) -> dict | None:
        with self._lock:
            return self._check(app_id)

    def _check(self, app_id: str) -> dict | None:
        app = self.registry.get_app(app_id) or {}
        github = app.get("github") or {}
        if not github.get("repo"):
            return None
        repo, branch, path = github["repo"], github.get("branch") or "release", github.get("path") or "."
        try:
            sha = branch_sha(repo, branch, self.session)
        except (requests.RequestException, ValueError) as exc:
            return self._update(app_id, state="error", message=f"GitHub check failed: {exc}")
        if sha is None:
            return self._update(app_id, state="waiting", message=f"Branch {branch} does not exist yet.")
        if sha == github.get("last_sha"):
            return self._update(app_id)
        if self.verifier.active_version(app_id):
            return self._update(app_id, state="waiting", message=f"New commit {sha[:7]}; waiting for the current verify to finish.")

        with tempfile.TemporaryDirectory(prefix="rewind-gh-") as tmp:
            try:
                root = extract_tarball(download_tarball(repo, sha, self.session), Path(tmp))
            except (requests.RequestException, tarfile.TarError, ValueError) as exc:
                return self._update(app_id, state="error", message=f"Download of {sha[:7]} failed: {exc}")
            folder = root if path == "." else root / path
            if not folder.is_dir():
                return self._update(app_id, last_sha=sha, state="error", message=f"Folder {path}/ is not in commit {sha[:7]}.")
            digest = tree_hash(folder)
            if digest == github.get("tree_hash"):
                return self._update(app_id, last_sha=sha, state="watching", message=f"Commit {sha[:7]} leaves {path}/ unchanged; nothing to deploy.")
            existing = {str(r.get("version")) for r in self.registry.list_releases(app_id)}
            version = version_for(folder, sha, existing)
            self._update(app_id, state="deploying", message=f"Building {version} from commit {sha[:7]}.")
            commit = {"sha": sha, "repo": repo, "url": f"https://github.com/{repo}/commit/{sha}"}
            try:
                self.publish(self.registry, app_id, version=version, source_dir=folder, extra={"commit": commit})
            except Exception as exc:  # noqa: BLE001
                return self._update(app_id, last_sha=sha, tree_hash=digest, state="error", message=f"Publish of {version} failed: {str(exc)[:400]}")
        self.verifier.verify_async(app_id, version)
        return self._update(
            app_id,
            last_sha=sha,
            tree_hash=digest,
            last_version=version,
            state="verifying",
            message=f"Deployed {version} from commit {sha[:7]}; verifying.",
        )

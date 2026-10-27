"""Build and run app containers for RewindAI."""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import requests

from edits import copy_tree, materialize_from_base, normalize_edits
from registry import Registry


NETWORK = os.environ.get("REWIND_NETWORK", "rollbackai")


def _run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
    )


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def image_name(app_id: str, version: str) -> str:
    return f"rewind-{app_id}:{version}"


def container_name(app: dict) -> str:
    return app.get("container_name") or f"rewind-{app['id']}"


def extract_source_archive(archive_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:*") as archive:
        # Safer extract: strip leading components that escape.
        for member in archive.getmembers():
            name = member.name.lstrip("./")
            if not name or name.startswith("..") or "/../" in f"/{name}/":
                continue
            member.name = name
            archive.extract(member, path=dest)
    # If the archive wrapped a single top-level folder, flatten common cases.
    children = [p for p in dest.iterdir() if not p.name.startswith("._")]
    if len(children) == 1 and children[0].is_dir() and not (dest / "Dockerfile").exists():
        top = children[0]
        for item in list(top.iterdir()):
            target = dest / item.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.move(str(item), str(target))
        shutil.rmtree(top)


def stage_clean_context(src: Path) -> Path:
    """Copy source to a local temp dir without AppleDouble files."""
    stage = Path(tempfile.mkdtemp(prefix="rewind-build-"))
    copy_tree(src, stage)
    for junk in stage.rglob("._*"):
        try:
            junk.unlink()
        except OSError:
            pass
    return stage


def build_image(app_id: str, version: str, src: Path) -> str:
    tag = image_name(app_id, version)
    if not (src / "Dockerfile").is_file():
        raise FileNotFoundError(f"No Dockerfile in {src}")
    stage = stage_clean_context(src)
    try:
        result = _run(["docker", "build", "-t", tag, str(stage)], check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"docker build failed:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
            )
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return tag


def wait_healthy(app: dict, timeout: int = 60) -> None:
    alias = app.get("network_alias") or app["id"]
    port = int(app.get("container_port") or 8080)
    health = app.get("health_path") or "/health"
    # From the rewind container, hit the app by network alias.
    url = f"http://{alias}:{port}{health}"
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=2)
            if response.status_code < 500:
                return
            last_err = f"status {response.status_code}"
        except requests.RequestException as exc:
            last_err = str(exc)
        time.sleep(1)
    raise TimeoutError(f"app did not become healthy at {url}: {last_err}")


def stop_container(name: str) -> None:
    _run(["docker", "rm", "-f", name], check=False)


def run_container(app: dict, image: str) -> None:
    name = container_name(app)
    stop_container(name)
    # Ensure network exists.
    _run(["docker", "network", "inspect", NETWORK], check=False)
    if _run(["docker", "network", "inspect", NETWORK], check=False).returncode != 0:
        _run(["docker", "network", "create", NETWORK], check=False)

    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--network",
        NETWORK,
        "--network-alias",
        app.get("network_alias") or app["id"],
        "--cpus",
        str(app.get("cpus") or 1.0),
        "--memory",
        str(app.get("memory") or "256m"),
        "-p",
        f"{int(app['host_port'])}:{int(app['container_port'])}",
        "-e",
        f"APP_VERSION={app.get('_pending_version') or 'unknown'}",
        "--restart",
        "unless-stopped",
        image,
    ]
    result = _run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"docker run failed:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
        )


def collect_logs(app: dict, since: str | None = None) -> str:
    name = container_name(app)
    cmd = ["docker", "logs", "--tail", "500"]
    if since:
        cmd.extend(["--since", since])
    cmd.append(name)
    result = _run(cmd, check=False)
    return (result.stdout or "") + (result.stderr or "")


def publish_release(
    registry: Registry,
    app_id: str,
    *,
    version: str,
    source_dir: Path | None = None,
    archive_path: Path | None = None,
    image: str | None = None,
    base_version: str | None = None,
    edits: list[dict] | None = None,
    skip_verify: bool = False,
) -> dict:
    """Materialize source (if any), build/tag image, deploy, return release record."""
    app = registry.get_app(app_id)
    if not app:
        raise KeyError(f"unknown app: {app_id}")
    version = registry.validate_version(version)
    if registry.get_release(app_id, version):
        raise ValueError(f"version already published: {version}")

    release_dir = registry.release_dir(app_id, version)
    release_dir.mkdir(parents=True, exist_ok=True)
    src_dest = registry.src_dir(app_id, version)
    kind = "image"
    built_image = image
    changed: list[str] = []

    if base_version:
        base = registry.get_release(app_id, base_version)
        if not base:
            raise ValueError(f"base version not found: {base_version}")
        if not registry.has_source(app_id, base_version):
            raise ValueError(f"base version has no source: {base_version}")
        edits = normalize_edits(edits or [])
        changed, errors = materialize_from_base(
            registry.src_dir(app_id, base_version),
            src_dest,
            edits,
        )
        if errors:
            shutil.rmtree(release_dir, ignore_errors=True)
            raise ValueError("; ".join(errors))
        built_image = build_image(app_id, version, src_dest)
        kind = "edits"
    elif source_dir:
        copy_tree(Path(source_dir), src_dest)
        built_image = build_image(app_id, version, src_dest)
        kind = "source"
    elif archive_path:
        extract_source_archive(Path(archive_path), src_dest)
        built_image = build_image(app_id, version, src_dest)
        kind = "source"
    elif image:
        inspect = _run(["docker", "image", "inspect", image], check=False)
        if inspect.returncode != 0:
            raise FileNotFoundError(f"Image not found locally: {image}")
        # Retag so RewindAI owns a stable name.
        tag = image_name(app_id, version)
        _run(["docker", "tag", image, tag], check=False)
        built_image = tag
        kind = "image"
    else:
        raise ValueError("provide source, archive, image, or base+edits")

    release = {
        "version": version,
        "app_id": app_id,
        "kind": kind,
        "image": built_image,
        "base_version": base_version,
        "changed_files": changed,
        "edits": edits or [],
        "published_at": _ts(),
        "status": "deploying",
        "quarantined": False,
        "score": None,
        "decision": None,
    }
    registry.write_release(app_id, release)

    app_copy = dict(app)
    app_copy["_pending_version"] = version
    run_container(app_copy, built_image)
    wait_healthy(app)
    registry.update_app(
        app_id,
        live_version=version,
        last_decision="verifying",
    )
    registry.update_release(app_id, version, status="live", deployed_at=_ts())
    release = registry.get_release(app_id, version) or release
    release["_skip_verify"] = skip_verify
    return release


def restore_version(
    registry: Registry,
    app_id: str,
    version: str,
    *,
    force: bool = False,
) -> dict:
    app = registry.get_app(app_id)
    if not app:
        raise KeyError(app_id)
    release = registry.get_release(app_id, version)
    if not release:
        raise FileNotFoundError(version)
    if release.get("quarantined") and not force:
        raise PermissionError("quarantined")
    if app.get("live_version") == version:
        return {"status": "already_live", "version": version, "image": release["image"]}

    app_copy = dict(app)
    app_copy["_pending_version"] = version
    run_container(app_copy, release["image"])
    wait_healthy(app)
    registry.update_app(app_id, live_version=version, last_decision="restored")
    registry.update_release(app_id, version, status="live", restored_at=_ts())
    return {
        "status": "accepted",
        "version": version,
        "image": release["image"],
        "force": force,
    }

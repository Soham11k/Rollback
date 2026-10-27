"""Persistent app and release registry on the rewind-data volume."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

_APP_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Registry:
    def __init__(self, data_dir: str | Path | None = None) -> None:
        root = data_dir or os.environ.get("REWIND_DATA", "/data")
        self.root = Path(root)
        self.apps_dir = self.root / "apps"
        self.jobs_dir = self.root / "jobs"
        self.apps_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def app_dir(self, app_id: str) -> Path:
        return self.apps_dir / app_id

    def release_dir(self, app_id: str, version: str) -> Path:
        return self.app_dir(app_id) / "releases" / version

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, indent=2) + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def _read_json(self, path: Path) -> dict | None:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def validate_app_id(self, app_id: str) -> str:
        key = (app_id or "").strip().lower()
        if not _APP_ID_RE.match(key):
            raise ValueError(
                "app id must be 2–32 chars, start with a letter, "
                "and use only lowercase letters, digits, and hyphens"
            )
        return key

    def validate_version(self, version: str) -> str:
        key = (version or "").strip()
        if not _VERSION_RE.match(key):
            raise ValueError("invalid version label")
        return key

    def list_apps(self) -> list[dict]:
        with self._lock:
            rows = []
            for path in sorted(self.apps_dir.glob("*/app.json")):
                data = self._read_json(path)
                if data:
                    rows.append(data)
            return rows

    def get_app(self, app_id: str) -> dict | None:
        return self._read_json(self.app_dir(app_id) / "app.json")

    def register_app(self, payload: dict) -> dict:
        with self._lock:
            app_id = self.validate_app_id(str(payload.get("id") or ""))
            if self.get_app(app_id):
                raise ValueError(f"app already registered: {app_id}")
            name = str(payload.get("name") or app_id).strip() or app_id
            container_port = int(payload.get("container_port") or 8080)
            host_port = int(payload.get("host_port") or 8080)
            health_path = str(payload.get("health_path") or "/health").strip() or "/health"
            if not health_path.startswith("/"):
                health_path = "/" + health_path
            probes = payload.get("probe_paths") or ["/"]
            if isinstance(probes, str):
                probes = [p.strip() for p in probes.split(",") if p.strip()]
            probe_paths = []
            for item in probes:
                path = str(item).strip() or "/"
                if not path.startswith("/"):
                    path = "/" + path
                probe_paths.append(path)
            if not probe_paths:
                probe_paths = ["/"]
            cpus = float(payload.get("cpus") or 1.0)
            memory = str(payload.get("memory") or "256m")
            on_anomaly = str(payload.get("on_anomaly") or "rollback").strip().lower()
            if on_anomaly not in {"rollback", "ask"}:
                raise ValueError("on_anomaly must be rollback or ask")

            record = {
                "id": app_id,
                "name": name,
                "container_port": container_port,
                "host_port": host_port,
                "health_path": health_path,
                "probe_paths": probe_paths,
                "cpus": cpus,
                "memory": memory,
                "on_anomaly": on_anomaly,
                "container_name": f"rewind-{app_id}",
                "network_alias": app_id,
                "created_at": _ts(),
                "live_version": None,
                "best_version": None,
                "last_decision": "idle",
            }
            self.app_dir(app_id).mkdir(parents=True, exist_ok=True)
            (self.app_dir(app_id) / "releases").mkdir(exist_ok=True)
            self._write_json(self.app_dir(app_id) / "app.json", record)
            return record

    def update_app(self, app_id: str, **fields: Any) -> dict:
        with self._lock:
            app = self.get_app(app_id)
            if not app:
                raise KeyError(app_id)
            app.update(fields)
            self._write_json(self.app_dir(app_id) / "app.json", app)
            return app

    def list_releases(self, app_id: str) -> list[dict]:
        root = self.app_dir(app_id) / "releases"
        if not root.is_dir():
            return []
        rows = []
        for path in sorted(root.glob("*/release.json")):
            data = self._read_json(path)
            if data:
                rows.append(data)
        return rows

    def get_release(self, app_id: str, version: str) -> dict | None:
        return self._read_json(self.release_dir(app_id, version) / "release.json")

    def write_release(self, app_id: str, release: dict) -> dict:
        with self._lock:
            version = self.validate_version(str(release.get("version") or ""))
            release["version"] = version
            release["app_id"] = app_id
            path = self.release_dir(app_id, version) / "release.json"
            self._write_json(path, release)
            return release

    def update_release(self, app_id: str, version: str, **fields: Any) -> dict:
        with self._lock:
            release = self.get_release(app_id, version)
            if not release:
                raise KeyError(f"{app_id}/{version}")
            release.update(fields)
            self._write_json(self.release_dir(app_id, version) / "release.json", release)
            return release

    def write_verify(self, app_id: str, version: str, payload: dict) -> None:
        self._write_json(self.release_dir(app_id, version) / "verify.json", payload)
        # Also keep a pointer on the app for the console.
        self._write_json(self.app_dir(app_id) / "last_verify.json", payload)

    def read_verify(self, app_id: str, version: str | None = None) -> dict | None:
        if version:
            return self._read_json(self.release_dir(app_id, version) / "verify.json")
        return self._read_json(self.app_dir(app_id) / "last_verify.json")

    def write_report(self, app_id: str, version: str, payload: dict) -> None:
        self._write_json(self.release_dir(app_id, version) / "report.json", payload)
        self._write_json(self.app_dir(app_id) / "last_report.json", payload)

    def read_report(self, app_id: str, version: str | None = None) -> dict | None:
        if version:
            return self._read_json(self.release_dir(app_id, version) / "report.json")
        return self._read_json(self.app_dir(app_id) / "last_report.json")

    def write_logs(self, app_id: str, version: str, text: str) -> None:
        path = self.release_dir(app_id, version) / "logs.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text or "", encoding="utf-8")

    def read_logs(self, app_id: str, version: str) -> str:
        path = self.release_dir(app_id, version) / "logs.txt"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def src_dir(self, app_id: str, version: str) -> Path:
        return self.release_dir(app_id, version) / "src"

    def has_source(self, app_id: str, version: str) -> bool:
        src = self.src_dir(app_id, version)
        return src.is_dir() and any(src.iterdir())

    def write_job(self, job_id: str, payload: dict) -> dict:
        payload = dict(payload)
        payload["id"] = job_id
        self._write_json(self.jobs_dir / f"{job_id}.json", payload)
        return payload

    def read_job(self, job_id: str) -> dict | None:
        return self._read_json(self.jobs_dir / f"{job_id}.json")

    def update_job(self, job_id: str, **fields: Any) -> dict:
        with self._lock:
            job = self.read_job(job_id) or {"id": job_id}
            job.update(fields)
            self._write_json(self.jobs_dir / f"{job_id}.json", job)
            return job

    def write_app_blob(self, app_id: str, name: str, payload: dict) -> None:
        self._write_json(self.app_dir(app_id) / name, payload)

    def read_app_blob(self, app_id: str, name: str) -> dict | None:
        return self._read_json(self.app_dir(app_id) / name)

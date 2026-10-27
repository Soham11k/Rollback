"""Tiny notes app with no metrics code — RewindAI watches it externally."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

NOTES: list[dict] = [
    {"id": 1, "title": "Ship v1", "body": "Healthy baseline note."},
    {"id": 2, "title": "Remember", "body": "No Prometheus client in this process."},
]


class Handler(BaseHTTPRequestHandler):
    server_version = "Notes/1.0"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._send(
                200,
                {
                    "status": "healthy",
                    "version": os.environ.get("APP_VERSION", "1.0.0"),
                },
            )
            return
        if path == "/notes":
            self._send(200, {"notes": NOTES, "version": os.environ.get("APP_VERSION", "1.0.0")})
            return
        if path.startswith("/notes/"):
            try:
                note_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self._send(404, {"status": "not_found"})
                return
            note = next((n for n in NOTES if n["id"] == note_id), None)
            if note is None:
                self._send(404, {"status": "not_found"})
                return
            # Optional fault via query ?boom=1 for demo bad releases.
            qs = parse_qs(parsed.query)
            if qs.get("boom") == ["1"]:
                raise RuntimeError("intentional notes boom")
            self._send(200, note)
            return
        self._send(404, {"status": "not_found", "path": path})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys_stderr = __import__("sys").stderr
        sys_stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"notes listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

"""GatePass flash-sale ticket checkout with optional fault injection."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path

import psutil
from flask import Flask, Response, jsonify, redirect, render_template, request

from catalog import checkout, get_event, list_events
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

app = Flask(__name__)


@app.context_processor
def _release_context():
    return {"version": get_version(), "fault_mode": get_fault_mode()}


REQUEST_COUNT = Counter(
    "app_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)
REQUEST_LATENCY = Histogram(
    "app_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["endpoint"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
ERROR_COUNT = Counter(
    "app_http_errors_total",
    "Total HTTP 5xx responses",
    ["endpoint"],
)
CPU_GAUGE = Gauge("app_process_cpu_percent", "Process CPU usage percent")
MEMORY_GAUGE = Gauge("app_process_memory_percent", "Process memory usage percent")
VERSION_INFO = Gauge(
    "app_info",
    "Application version and fault mode",
    ["version", "fault_mode"],
)

# Pre-create product error counters at zero so rate() sees a baseline.
for _endpoint in (
    "index",
    "event_page",
    "checkout_page",
    "api_events",
    "api_event",
    "api_checkout",
):
    ERROR_COUNT.labels(endpoint=_endpoint)

_UNMETERED = {"console", "desk", "static"}

_cpu_workers: list[threading.Thread] = []
_memory_balls: deque = deque()
_stop_faults = threading.Event()
_faults_started = False
_info_set = False
FEATURE_EVENTS = ("EVT-01", "EVT-02", "EVT-03")


def get_version() -> str:
    return os.environ.get("APP_VERSION", "1.0.0")


def get_fault_mode() -> str:
    return os.environ.get("FAULT_MODE", "none").lower()


def _burn_cpu() -> None:
    while not _stop_faults.is_set():
        _ = sum(i * i for i in range(50_000))


def _start_cpu_fault() -> None:
    if _cpu_workers:
        return
    for _ in range(2):
        worker = threading.Thread(target=_burn_cpu, daemon=True)
        worker.start()
        _cpu_workers.append(worker)


def _start_memory_fault() -> None:
    if _memory_balls:
        return
    chunk = bytearray(10 * 1024 * 1024)
    for _ in range(8):
        _memory_balls.append(bytearray(chunk))


def _apply_fault_side_effects() -> None:
    global _faults_started, _info_set
    fault_mode = get_fault_mode()
    version = get_version()

    if not _info_set:
        VERSION_INFO.labels(version=version, fault_mode=fault_mode).set(1)
        _info_set = True

    if _faults_started:
        return

    if fault_mode in {"cpu", "all"}:
        _start_cpu_fault()
    if fault_mode in {"memory", "all"}:
        _start_memory_fault()
    _faults_started = True


def _maybe_inject_request_fault() -> tuple[bool, float]:
    """Return (should_error, extra_latency_seconds)."""
    fault_mode = get_fault_mode()
    extra_latency = 0.0
    should_error = False

    if fault_mode in {"latency", "all"}:
        extra_latency = 2.5
    if fault_mode in {"errors", "all"}:
        should_error = (time.time_ns() % 6) == 0

    return should_error, extra_latency


def _read_cgroup_cpu_percent() -> float | None:
    try:
        path = Path("/sys/fs/cgroup/cpu.stat")
        if not path.exists():
            return None
        stats = dict(
            line.split(None, 1)
            for line in path.read_text().splitlines()
            if " " in line
        )
        usage_usec = float(stats.get("usage_usec", "0"))
        now = time.time()
        prev = getattr(_read_cgroup_cpu_percent, "_prev", None)
        _read_cgroup_cpu_percent._prev = (now, usage_usec)  # type: ignore[attr-defined]
        if not prev:
            return None
        prev_t, prev_u = prev
        dt = now - prev_t
        if dt <= 0:
            return None
        return max(0.0, min(100.0, ((usage_usec - prev_u) / (dt * 1_000_000.0)) * 100.0))
    except Exception:  # noqa: BLE001
        return None


def _read_cgroup_memory_percent() -> float | None:
    try:
        current = Path("/sys/fs/cgroup/memory.current")
        maximum = Path("/sys/fs/cgroup/memory.max")
        if not current.exists() or not maximum.exists():
            return None
        used = float(current.read_text().strip())
        raw_max = maximum.read_text().strip()
        if raw_max == "max":
            return None
        limit = float(raw_max)
        if limit <= 0:
            return None
        return max(0.0, min(100.0, (used / limit) * 100.0))
    except Exception:  # noqa: BLE001
        return None


def _update_resource_gauges() -> None:
    process = psutil.Process()
    cgroup_cpu = _read_cgroup_cpu_percent()
    if cgroup_cpu is None:
        cpu = process.cpu_percent(interval=None)
        if cpu <= 0:
            cpu = psutil.cpu_percent(interval=None)
    else:
        cpu = cgroup_cpu
    CPU_GAUGE.set(cpu)

    cgroup_mem = _read_cgroup_memory_percent()
    MEMORY_GAUGE.set(
        cgroup_mem if cgroup_mem is not None else process.memory_percent()
    )


@app.before_request
def _start_timer() -> None:
    request._start_time = time.perf_counter()  # type: ignore[attr-defined]
    _apply_fault_side_effects()


@app.after_request
def _record_metrics(response: Response) -> Response:
    endpoint = request.endpoint or "unknown"
    # Console polls and static assets must not poison detector p95 / error rate.
    if endpoint in _UNMETERED:
        return response
    status = str(response.status_code)
    REQUEST_COUNT.labels(
        method=request.method, endpoint=endpoint, status=status
    ).inc()

    start = getattr(request, "_start_time", None)
    if start is not None:
        REQUEST_LATENCY.labels(endpoint=endpoint).observe(
            time.perf_counter() - start
        )

    if response.status_code >= 500:
        ERROR_COUNT.labels(endpoint=endpoint).inc()

    _update_resource_gauges()
    return response


def _fault_response(error_message: str, *, as_json: bool):
    """Apply request faults. Return a response, or None when the request may proceed."""
    should_error, extra_latency = _maybe_inject_request_fault()
    if extra_latency:
        time.sleep(extra_latency)
    if not should_error:
        return None
    if as_json:
        return jsonify(
            {
                "status": "error",
                "version": get_version(),
                "fault_mode": get_fault_mode(),
                "message": error_message,
            }
        ), 500
    return (
        render_template(
            "error.html",
            message=error_message,
        ),
        500,
    )


def _public_payload(record: dict) -> dict:
    payload = dict(record)
    payload["version"] = get_version()
    payload["fault_mode"] = get_fault_mode()
    return payload


@app.get("/")
def index():
    fault = _fault_response("Event list failed (injected)", as_json=False)
    if fault is not None:
        return fault
    events = list_events()
    return render_template("index.html", events=events, featured=FEATURE_EVENTS)


@app.get("/events/<event_id>")
def event_page(event_id: str):
    fault = _fault_response("Event detail failed (injected)", as_json=False)
    if fault is not None:
        return fault
    event = get_event(event_id)
    if event is None:
        return render_template("missing.html", event_id=event_id.upper()), 404
    return render_template("event.html", event=event)


@app.post("/events/<event_id>/checkout")
def checkout_page(event_id: str):
    fault = _fault_response("Checkout failed (injected)", as_json=False)
    if fault is not None:
        return fault
    tier = (request.form.get("tier") or request.args.get("tier") or "").strip()
    qty = request.form.get("qty") or request.args.get("qty") or "1"
    result = checkout(event_id, tier, qty)
    if result is None:
        return render_template("missing.html", event_id=event_id.upper()), 404
    status = 200 if result.get("status") == "confirmed" else 409
    return render_template("checkout.html", result=result), status


def rewind_public_url() -> str:
    return os.environ.get("REWIND_PUBLIC_URL", "http://127.0.0.1:8090").rstrip("/")


@app.get("/desk")
@app.get("/console")
def console_redirect():
    """Developer console now lives on the RewindAI service."""
    return redirect(f"{rewind_public_url()}/apps/gatepass", code=302)


@app.get("/api/events")
def api_events():
    fault = _fault_response("Event list failed (injected)", as_json=True)
    if fault is not None:
        return fault
    return jsonify({"events": list_events(), "version": get_version()})


@app.get("/api/events/<event_id>")
def api_event(event_id: str):
    fault = _fault_response("Event detail failed (injected)", as_json=True)
    if fault is not None:
        return fault
    event = get_event(event_id)
    if event is None:
        return jsonify(
            {
                "status": "not_found",
                "event_id": event_id.upper(),
                "version": get_version(),
                "message": "No event with that id",
            }
        ), 404
    return jsonify(_public_payload(event))


@app.post("/api/checkout")
@app.get("/api/checkout")
def api_checkout():
    fault = _fault_response("Checkout failed (injected)", as_json=True)
    if fault is not None:
        return fault
    payload = request.get_json(silent=True) or {}
    event_id = (
        payload.get("event_id")
        or request.args.get("event_id")
        or request.form.get("event_id")
        or ""
    )
    tier = (
        payload.get("tier")
        or request.args.get("tier")
        or request.form.get("tier")
        or ""
    )
    qty = payload.get("qty") or request.args.get("qty") or request.form.get("qty") or 1
    result = checkout(str(event_id), str(tier), qty)
    if result is None:
        return jsonify(
            {
                "status": "not_found",
                "event_id": str(event_id).upper(),
                "version": get_version(),
                "message": "Unknown event or seat tier",
            }
        ), 404
    code = 200 if result.get("status") == "confirmed" else 409
    return jsonify(_public_payload(result)), code


@app.get("/health")
def health():
    # Deliberately shallow — never touches catalog or checkout.
    return jsonify(
        {
            "status": "healthy",
            "version": get_version(),
            "fault_mode": get_fault_mode(),
        }
    )


@app.get("/metrics")
def metrics():
    _apply_fault_side_effects()
    _update_resource_gauges()
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


def create_app() -> Flask:
    _apply_fault_side_effects()
    psutil.Process().cpu_percent(interval=None)
    return app


create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))

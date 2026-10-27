"""Steady flash-sale traffic against GatePass events and checkout."""

from __future__ import annotations

import os
import random
import time

import requests

BASE_URL = os.environ.get("BASE_URL", "http://app:8080").rstrip("/")
TARGET_URL = os.environ.get("TARGET_URL", "").strip()
RPS = float(os.environ.get("LOADGEN_RPS", "2"))
TIMEOUT = float(os.environ.get("LOADGEN_TIMEOUT", "10"))

EVENTS = (
    ("EVT-01", "GA"),
    ("EVT-01", "VIP"),
    ("EVT-02", "STD"),
    ("EVT-02", "FRT"),
    ("EVT-03", "PRE"),
    ("EVT-04", "GA"),
    ("EVT-05", "EXP"),
    ("EVT-06", "GA"),
)


def _next_request() -> tuple[str, str]:
    """Return (method, url). Mix event reads with checkout so training sees a sale window."""
    if TARGET_URL:
        # Back-compat for older compose that still sets a single GET template.
        url = TARGET_URL
        if "{event}" in url or "{card}" in url:
            event_id, tier = random.choice(EVENTS)
            url = (
                url.replace("{event}", event_id)
                .replace("{card}", event_id)
                .replace("{tier}", tier)
            )
        return "GET", url

    roll = random.random()
    event_id, tier = random.choice(EVENTS)
    if roll < 0.45:
        return "GET", f"{BASE_URL}/api/events/{event_id}"
    if roll < 0.75:
        return "GET", f"{BASE_URL}/api/checkout?event_id={event_id}&tier={tier}&qty=1"
    return "GET", f"{BASE_URL}/api/events"


def main() -> None:
    interval = 1.0 / max(RPS, 0.1)
    print(
        f"Loadgen targeting GatePass at {BASE_URL or TARGET_URL} ~{RPS} req/s",
        flush=True,
    )

    while True:
        method, url = _next_request()
        started = time.perf_counter()
        try:
            response = requests.request(method, url, timeout=TIMEOUT)
            print(
                f"{time.strftime('%H:%M:%S')} {method} {url} "
                f"status={response.status_code} "
                f"latency_ms={(time.perf_counter() - started) * 1000:.0f}",
                flush=True,
            )
        except requests.RequestException as exc:
            print(f"{time.strftime('%H:%M:%S')} {method} {url} error={exc}", flush=True)

        elapsed = time.perf_counter() - started
        sleep_for = interval - elapsed
        if sleep_for > 0:
            time.sleep(sleep_for)


if __name__ == "__main__":
    main()

"""Collect feature vectors from Prometheus for RewindAI."""

from __future__ import annotations

import os
import time
from typing import Any

import requests

FEATURE_NAMES = ("cpu", "memory", "latency_p95", "error_rate")

DEFAULT_QUERIES = {
    "cpu": "app_process_cpu_percent",
    "memory": "app_process_memory_percent",
    "latency_p95": (
        "histogram_quantile(0.95, "
        "sum(rate(app_http_request_duration_seconds_bucket[1m])) by (le))"
    ),
    "error_rate": (
        "sum(rate(app_http_errors_total[1m])) / "
        "clamp_min(sum(rate(app_http_requests_total[1m])), 1e-9)"
    ),
}

ENDPOINT_ERROR_QUERY = (
    "topk(5, sum by (endpoint) (rate(app_http_errors_total[2m])))"
)


class PrometheusCollector:
    def __init__(
        self,
        prometheus_url: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.prometheus_url = (
            prometheus_url or os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
        ).rstrip("/")
        self.timeout = timeout

    def query_instant(self, expr: str) -> float:
        response = requests.get(
            f"{self.prometheus_url}/api/v1/query",
            params={"query": expr},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed: {payload}")

        results = payload.get("data", {}).get("result", [])
        if not results:
            return 0.0

        value = results[0]["value"][1]
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        if number != number:  # NaN
            return 0.0
        return number

    def sample(self) -> dict[str, float]:
        features = {
            name: self.query_instant(expr) for name, expr in DEFAULT_QUERIES.items()
        }
        # Convert error rate to percent for more intuitive scaling (0–100).
        features["error_rate"] = features["error_rate"] * 100.0
        # Latency to milliseconds.
        features["latency_p95"] = features["latency_p95"] * 1000.0
        return features

    def sample_vector(self) -> list[float]:
        sample = self.sample()
        return [sample[name] for name in FEATURE_NAMES]

    def most_affected_endpoint(self) -> str | None:
        """Return the endpoint with the highest recent error rate, else highest p95."""
        skip = {"health", "metrics", "static", "console", "console_series", "desk"}
        best_name = None
        best_value = 0.0
        try:
            response = requests.get(
                f"{self.prometheus_url}/api/v1/query",
                params={"query": ENDPOINT_ERROR_QUERY},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            if payload.get("status") == "success":
                for row in payload.get("data", {}).get("result", []) or []:
                    metric = row.get("metric") or {}
                    endpoint = metric.get("endpoint") or "unknown"
                    if endpoint in skip:
                        continue
                    try:
                        value = float(row["value"][1])
                    except (KeyError, TypeError, ValueError, IndexError):
                        continue
                    if value != value or value <= 0:
                        continue
                    if value > best_value:
                        best_value = value
                        best_name = endpoint
        except Exception:  # noqa: BLE001
            best_name = None

        if best_name:
            return best_name

        # Fall back to the slowest product endpoint when errors never registered.
        p95_query = (
            "histogram_quantile(0.95, "
            "sum by (endpoint, le) (rate(app_http_request_duration_seconds_bucket[2m])))"
        )
        try:
            response = requests.get(
                f"{self.prometheus_url}/api/v1/query",
                params={"query": p95_query},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success":
                return None
            for row in payload.get("data", {}).get("result", []) or []:
                endpoint = (row.get("metric") or {}).get("endpoint") or "unknown"
                if endpoint in skip:
                    continue
                try:
                    value = float(row["value"][1])
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
                if value != value or value <= 0:
                    continue
                if value > best_value:
                    best_value = value
                    best_name = endpoint
        except Exception:  # noqa: BLE001
            return None
        return best_name

    def collect_window(
        self,
        duration_seconds: int,
        interval_seconds: int = 5,
    ) -> list[list[float]]:
        samples: list[list[float]] = []
        deadline = time.time() + duration_seconds
        while time.time() < deadline:
            try:
                vector = self.sample_vector()
                samples.append(vector)
                print(
                    f"[collect] cpu={vector[0]:.1f}% mem={vector[1]:.1f}% "
                    f"lat_p95={vector[2]:.0f}ms err={vector[3]:.2f}%",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[collect] warning: {exc}", flush=True)
            time.sleep(interval_seconds)
        return samples


def main() -> None:
    duration = int(os.environ.get("COLLECT_DURATION_SECONDS", "60"))
    interval = int(os.environ.get("SAMPLE_INTERVAL_SECONDS", "5"))
    collector = PrometheusCollector()
    rows = collector.collect_window(duration, interval)
    print(f"[collect] gathered {len(rows)} samples")


if __name__ == "__main__":
    main()

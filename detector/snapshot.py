"""One-shot metric snapshot / wait-for-normal helper for the distribution-morning scenario."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import joblib
import requests

from collect import FEATURE_NAMES, PrometheusCollector
from monitor import score_sample, threshold_is_anomaly


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def load_artifact(path: Path) -> dict | None:
    if not path.exists():
        return None
    return joblib.load(path)


def failed_requests(prometheus_url: str) -> float:
    url = prometheus_url.rstrip("/") + "/api/v1/query"
    try:
        response = requests.get(
            url,
            params={"query": "sum(increase(app_http_errors_total[10m]))"},
            timeout=5,
        )
        response.raise_for_status()
        results = response.json().get("data", {}).get("result", [])
        if not results:
            return 0.0
        return float(results[0]["value"][1])
    except Exception:  # noqa: BLE001
        return 0.0


def print_snapshot(vector: list[float], verdict: str, score: float) -> None:
    print(
        f"[{_ts()}] cpu={vector[0]:.1f}% mem={vector[1]:.1f}% "
        f"lat_p95={vector[2]:.0f}ms err={vector[3]:.2f}% "
        f"score={score:.3f} verdict={verdict}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="RollbackAI metric snapshot")
    parser.add_argument(
        "--model",
        type=Path,
        default=Path(os.environ.get("MODEL_PATH", "models/baseline.joblib")),
    )
    parser.add_argument(
        "--mode",
        choices=("isolation_forest", "threshold"),
        default="isolation_forest",
    )
    parser.add_argument(
        "--wait-normal",
        type=int,
        default=0,
        help="Poll until N consecutive NORMAL samples (0 = one-shot)",
    )
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--failed-requests",
        action="store_true",
        help="Print approximate failed request count over 10m and exit",
    )
    args = parser.parse_args()

    prometheus_url = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
    if args.failed_requests:
        count = failed_requests(prometheus_url)
        print(f"{count:.0f}")
        return

    collector = PrometheusCollector(prometheus_url)
    artifact = load_artifact(args.model)

    needed = max(1, args.wait_normal)
    deadline = time.time() + args.timeout
    streak = 0
    last_vector: list[float] = [0.0, 0.0, 0.0, 0.0]
    last_score = 0.0
    last_verdict = "UNKNOWN"

    while True:
        try:
            vector = collector.sample_vector()
            last_vector = vector
            if args.mode == "threshold":
                is_anom = threshold_is_anomaly(vector)
                score = 1.0 if is_anom else 0.0
            elif artifact is None:
                is_anom = False
                score = 0.0
            else:
                is_anom, score = score_sample(artifact, vector)
            last_score = score
            last_verdict = "ANOMALY" if is_anom else "NORMAL"
            print_snapshot(vector, last_verdict, score)
            if not is_anom:
                streak += 1
            else:
                streak = 0
            if streak >= needed:
                sys.exit(0)
        except Exception as exc:  # noqa: BLE001
            print(f"[{_ts()}] snapshot error: {exc}", flush=True)
            streak = 0

        if args.wait_normal <= 0:
            sys.exit(0 if last_verdict == "NORMAL" else 2)
        if time.time() >= deadline:
            print(
                f"[{_ts()}] timeout waiting for NORMAL "
                f"(last={last_verdict} features={dict(zip(FEATURE_NAMES, last_vector))})",
                flush=True,
            )
            sys.exit(1)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

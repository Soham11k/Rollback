"""Train Isolation Forest baseline from healthy Prometheus metrics."""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from collect import FEATURE_NAMES, PrometheusCollector


def save_csv(path: Path, rows: list[list[float]]) -> None:
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FEATURE_NAMES)
        writer.writerows(rows)


def load_csv(path: Path) -> np.ndarray:
    with path.open(newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        if tuple(header) != FEATURE_NAMES:
            raise ValueError(f"Unexpected CSV header: {header}")
        rows = [[float(cell) for cell in row] for row in reader if row]
    return np.asarray(rows, dtype=float)


def augment_baseline(
    data: np.ndarray,
    copies: int = 4,
    random_state: int = 42,
) -> np.ndarray:
    """Expand a short healthy window with realistic noise so normal v1 jitter is not flagged."""
    rng = np.random.default_rng(random_state)
    # Feature scales: cpu %, memory %, latency ms, error %
    scales = np.array([3.0, 2.0, 20.0, 0.5])
    extras = []
    for _ in range(copies):
        noise = rng.normal(0.0, 1.0, size=data.shape) * scales
        extras.append(np.clip(data + noise, a_min=0.0, a_max=None))
    return np.vstack([data, *extras])


def train_model(
    data: np.ndarray,
    contamination: float = 0.05,
    random_state: int = 42,
) -> dict:
    augmented = augment_baseline(data, copies=4, random_state=random_state)
    scaler = StandardScaler()
    scaled = scaler.fit_transform(augmented)
    model = IsolationForest(
        n_estimators=200,
        contamination=contamination,
        random_state=random_state,
        n_jobs=-1,
    )
    model.fit(scaled)
    return {
        "model": model,
        "scaler": scaler,
        "feature_names": FEATURE_NAMES,
        "contamination": contamination,
        "sklearn_version": getattr(__import__("sklearn"), "__version__", "unknown"),
        "n_train_samples": int(len(augmented)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train RollbackAI Isolation Forest")
    parser.add_argument(
        "--duration",
        type=int,
        default=int(os.environ.get("TRAIN_DURATION_SECONDS", "120")),
        help="Seconds of healthy metrics to collect (default 120 for quick demo)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("SAMPLE_INTERVAL_SECONDS", "5")),
    )
    parser.add_argument(
        "--from-csv",
        type=Path,
        default=None,
        help="Skip live collection and train from an existing CSV",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.environ.get("MODEL_PATH", "models/baseline.joblib")),
    )
    parser.add_argument(
        "--data-out",
        type=Path,
        default=Path("data/healthy_baseline.csv"),
    )
    parser.add_argument("--contamination", type=float, default=0.05)
    args = parser.parse_args()

    if args.from_csv:
        data = load_csv(args.from_csv)
        print(f"[train] loaded {len(data)} rows from {args.from_csv}")
    else:
        print(
            f"[train] collecting healthy baseline for {args.duration}s "
            f"(interval={args.interval}s)"
        )
        collector = PrometheusCollector()
        rows = collector.collect_window(args.duration, args.interval)
        min_samples = max(5, min(10, args.duration // max(args.interval, 1) - 1))
        if len(rows) < min_samples:
            raise SystemExit(
                f"[train] need at least {min_samples} samples, got {len(rows)}. "
                "Is the app + loadgen + Prometheus running?"
            )
        try:
            save_csv(args.data_out, rows)
            print(f"[train] saved raw data to {args.data_out}")
        except OSError as exc:
            # USB bind-mounts on Docker Desktop can intermittently fail opens.
            fallback = Path("/tmp/healthy_baseline.csv")
            save_csv(fallback, rows)
            print(f"[train] warn: could not write {args.data_out} ({exc}); saved {fallback}")
        data = np.asarray(rows, dtype=float)

    artifact = train_model(data, contamination=args.contamination)
    output = Path(args.output)
    if not output.is_absolute():
        output = Path.cwd() / output
    output.parent.mkdir(parents=True, exist_ok=True)

    # USB bind-mounts on Docker Desktop sometimes fail in-place overwrites.
    tmp_path = Path("/tmp") / f"{output.name}.tmp"
    joblib.dump(artifact, tmp_path)
    try:
        if output.exists():
            output.unlink()
        tmp_path.replace(output)
        print(f"[train] wrote model to {output}")
    except OSError as exc:
        fallback = Path("/tmp") / output.name
        joblib.dump(artifact, fallback)
        print(
            f"[train] warn: could not write {output} ({exc}); model saved at {fallback}. "
            "Copy it to detector/models/baseline.joblib on the host."
        )
        # Still try a direct dump as a last attempt for non-USB environments.
        try:
            joblib.dump(artifact, output)
            print(f"[train] wrote model to {output} on retry")
        except OSError:
            pass

    print(
        "[train] feature means:",
        {name: float(data[:, i].mean()) for i, name in enumerate(FEATURE_NAMES)},
    )


if __name__ == "__main__":
    main()

"""Compare Isolation Forest vs static thresholds on recorded windows."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import joblib
import numpy as np
from sklearn.metrics import confusion_matrix, precision_recall_fscore_support

from collect import FEATURE_NAMES
from monitor import score_sample, threshold_is_anomaly
from train import train_model


def load_labeled_csv(path: Path, label: int) -> tuple[np.ndarray, np.ndarray]:
    with path.open(newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        if tuple(header) != FEATURE_NAMES:
            raise ValueError(f"Unexpected header in {path}: {header}")
        rows = [[float(c) for c in row] for row in reader if row]
    data = np.asarray(rows, dtype=float)
    labels = np.full(len(data), label, dtype=int)
    return data, labels


def generate_synthetic(path_healthy: Path, path_faulty: Path, n: int = 80) -> None:
    """Create demo CSVs so evaluate works without a live stack.

    Healthy data includes occasional single-metric spikes (still jointly normal)
    that trip naive thresholds and create false positives — the case Isolation
    Forest is meant to handle better.
    """
    rng = np.random.default_rng(42)
    healthy = np.column_stack(
        [
            rng.normal(42, 5, n).clip(20, 60),
            rng.normal(50, 4, n).clip(35, 65),
            rng.normal(180, 30, n).clip(80, 300),
            rng.normal(0.5, 0.3, n).clip(0, 2),
        ]
    )
    # Inject brief single-metric spikes that look "bad" in isolation.
    spike_idx = rng.choice(n, size=max(8, n // 8), replace=False)
    for i in spike_idx:
        kind = int(rng.integers(0, 3))
        if kind == 0:
            healthy[i, 0] = float(rng.uniform(82, 95))  # CPU only
        elif kind == 1:
            healthy[i, 1] = float(rng.uniform(82, 92))  # memory only
        else:
            healthy[i, 2] = float(rng.uniform(1100, 1600))  # latency only

    faulty = np.column_stack(
        [
            rng.normal(92, 4, n).clip(80, 100),
            rng.normal(88, 3, n).clip(75, 99),
            rng.normal(2800, 400, n).clip(1500, 5000),
            rng.normal(14, 3, n).clip(8, 30),
        ]
    )
    path_healthy.parent.mkdir(parents=True, exist_ok=True)
    for path, data in ((path_healthy, healthy), (path_faulty, faulty)):
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(FEATURE_NAMES)
            writer.writerows(data.tolist())
    print(f"[evaluate] wrote synthetic data to {path_healthy} and {path_faulty}")


def detection_latency(
    scores_or_flags: list[bool],
    interval_seconds: float,
    majority: int,
) -> float | None:
    """Seconds until majority vote would fire."""
    window = []
    for i, flag in enumerate(scores_or_flags):
        window.append(flag)
        if len(window) > majority:
            window.pop(0)
        if len(window) == majority and sum(window) > majority // 2:
            return (i + 1) * interval_seconds
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate RollbackAI detectors")
    parser.add_argument("--healthy", type=Path, default=Path("data/healthy_baseline.csv"))
    parser.add_argument("--faulty", type=Path, default=Path("data/faulty_window.csv"))
    parser.add_argument("--model", type=Path, default=Path("models/baseline.joblib"))
    parser.add_argument("--synthesize", action="store_true")
    parser.add_argument("--majority", type=int, default=5)
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()

    if args.synthesize or not args.healthy.exists() or not args.faulty.exists():
        generate_synthetic(args.healthy, args.faulty)

    healthy_x, healthy_y = load_labeled_csv(args.healthy, 0)
    faulty_x, faulty_y = load_labeled_csv(args.faulty, 1)
    x = np.vstack([healthy_x, faulty_x])
    y_true = np.concatenate([healthy_y, faulty_y])

    # Train on healthy only (unsupervised baseline), matching production.
    artifact = train_model(healthy_x)
    args.model.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, args.model)

    if_preds = []
    thr_preds = []
    for row in x:
        is_anom, _ = score_sample(artifact, row.tolist())
        if_preds.append(1 if is_anom else 0)
        thr_preds.append(1 if threshold_is_anomaly(row.tolist()) else 0)

    print("\n=== Isolation Forest ===")
    _report(y_true, np.asarray(if_preds))
    if_latency = detection_latency(
        [bool(p) for p in if_preds[len(healthy_x) :]],
        args.interval,
        args.majority,
    )
    print(f"Detection latency on faulty window: {if_latency}s")

    print("\n=== Static Thresholds (CPU>80 | Mem>80 | Lat>1000ms | Err>5%) ===")
    _report(y_true, np.asarray(thr_preds))
    thr_latency = detection_latency(
        [bool(p) for p in thr_preds[len(healthy_x) :]],
        args.interval,
        args.majority,
    )
    print(f"Detection latency on faulty window: {thr_latency}s")

    print(
        "\nNote: Isolation Forest learns joint normal behavior; thresholds "
        "fire on any single metric breach and can false-alarm under load spikes."
    )


def _report(y_true: np.ndarray, y_pred: np.ndarray) -> None:
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_true, y_pred, average="binary", zero_division=0
    )
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    print(f"precision={precision:.3f} recall={recall:.3f} f1={f1:.3f}")
    print(f"confusion: TP={tp} FP={fp} TN={tn} FN={fn}")


if __name__ == "__main__":
    main()

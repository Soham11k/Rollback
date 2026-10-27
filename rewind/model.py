"""Isolation Forest helpers (same recipe as detector/train.py)."""

from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from diagnosis import FEATURE_NAMES


def augment_baseline(
    data: np.ndarray,
    copies: int = 4,
    random_state: int = 42,
) -> np.ndarray:
    rng = np.random.default_rng(random_state)
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
        "n_train_samples": int(len(augmented)),
    }


def save_model(artifact: dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    joblib.dump(artifact, tmp)
    tmp.replace(path)


def load_model(path: Path) -> dict | None:
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        return joblib.load(path)
    except Exception:  # noqa: BLE001
        return None


def score_sample(artifact: dict, vector: list[float]) -> tuple[bool, float]:
    arr = np.asarray([vector], dtype=float)
    scaled = artifact["scaler"].transform(arr)
    prediction = artifact["model"].predict(scaled)[0]
    raw = float(artifact["model"].decision_function(scaled)[0])
    return bool(prediction == -1), -raw

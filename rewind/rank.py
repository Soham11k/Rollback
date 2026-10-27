"""Headroom scoring and best-version selection."""

from __future__ import annotations

from typing import Any

from diagnosis import FEATURE_NAMES, LIMITS


def headroom_score(peaks: dict[str, Any] | None) -> float:
    peaks = peaks or {}
    parts = []
    for signal in FEATURE_NAMES:
        limit = LIMITS[signal]
        try:
            value = float(peaks.get(signal) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value != value:
            value = 0.0
        parts.append(max(0.0, 1.0 - (value / limit)))
    if not parts:
        return 0.0
    return round(100.0 * (sum(parts) / len(parts)), 1)


def _version_key(version: str) -> tuple:
    """Rough sort key: prefer numeric semver-ish tuples, else string."""
    parts = []
    for chunk in str(version).replace("-", ".").split("."):
        if chunk.isdigit():
            parts.append((0, int(chunk)))
        else:
            parts.append((1, chunk))
    return tuple(parts)


def pick_best(releases: list[dict]) -> dict | None:
    """Highest headroom among verified healthy non-quarantined releases.

    When two scores are within 5 points, the newer version wins.
    """
    candidates = [
        r
        for r in releases
        if r.get("decision") == "healthy"
        and not r.get("quarantined")
        and r.get("score") is not None
    ]
    if not candidates:
        # Fall back to any non-quarantined release that was ever live/healthy.
        candidates = [
            r
            for r in releases
            if not r.get("quarantined") and r.get("score") is not None
        ]
    if not candidates:
        return None

    def sort_key(row: dict) -> tuple:
        score = float(row.get("score") or 0.0)
        return (score, _version_key(str(row.get("version") or "")))

    ranked = sorted(candidates, key=sort_key, reverse=True)
    best = ranked[0]
    # Prefer newer when within 5 points of the top score.
    top = float(best.get("score") or 0.0)
    close = [r for r in ranked if top - float(r.get("score") or 0.0) <= 5.0]
    close.sort(key=lambda r: _version_key(str(r.get("version") or "")), reverse=True)
    return close[0] if close else best


def annotate_releases(releases: list[dict], live_version: str | None) -> list[dict]:
    best = pick_best(releases)
    best_version = best.get("version") if best else None
    rows = []
    for release in releases:
        row = dict(release)
        row["live"] = row.get("version") == live_version
        row["best"] = bool(best_version) and row.get("version") == best_version
        rows.append(row)
    rows.sort(key=lambda r: _version_key(str(r.get("version") or "")), reverse=True)
    return rows

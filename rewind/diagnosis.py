"""Locate which watched signals crossed RewindAI limits and where."""

from __future__ import annotations

from typing import Any

LIMITS = {
    "cpu": 80.0,
    "memory": 80.0,
    "latency_p95": 1000.0,
    "error_rate": 5.0,
}

UNITS = {
    "cpu": "%",
    "memory": "%",
    "latency_p95": "ms",
    "error_rate": "%",
}

FEATURE_NAMES = ("cpu", "memory", "latency_p95", "error_rate")

_PROCESS_LOCATION = "app process"
_PATH_FALLBACK = "the request path"


def _as_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:
        return 0.0
    return number


def _path_location(path: str | None) -> str:
    name = (path or "").strip()
    return name or _PATH_FALLBACK


def _worst_path(path_stats: Any, key: str) -> str | None:
    """Path with the highest value of key in path_stats, if any is above zero."""
    if not isinstance(path_stats, dict):
        return None
    best_path = None
    best_value = 0.0
    for path, row in path_stats.items():
        if not isinstance(row, dict):
            continue
        value = _as_float(row.get(key))
        if value > best_value:
            best_value = value
            best_path = path
    return best_path


def _fix_for(signal: str, location: str) -> str:
    if signal == "cpu":
        return (
            "Look for a busy loop or hot path on the worker process that keeps "
            "the CPU pegged above the verify limit."
        )
    if signal == "memory":
        return (
            "Look for allocations that are not released on the worker process "
            "so memory stays above the verify limit."
        )
    if signal == "latency_p95":
        return (
            f"Look for blocking or slow work on {location} that pushes "
            "latency past 1000 ms while the rest of the process still answers."
        )
    if signal == "error_rate":
        return (
            f"Look for the 500 path on {location}. /health can still answer 200 "
            "while clients hit failures on that route."
        )
    return "Inspect the crossed signal against the last verify peaks."


def diagnose_incident(incident: dict | None) -> dict:
    """Build a bug list from peaks and most_affected_path."""
    if not incident:
        return {
            "bugs": [],
            "has_bugs": False,
            "summary": "No incident yet. No crossed signals.",
            "version": None,
            "decision": None,
            "most_affected_path": None,
            "model_only": False,
        }

    peaks = incident.get("peaks") or {}
    decision = incident.get("decision")
    version = incident.get("version")
    affected = (
        incident.get("most_affected_path")
        or incident.get("most_affected_endpoint")
    )
    route = _path_location(affected if isinstance(affected, str) else None)
    path_stats = incident.get("path_stats")
    signal_routes = {
        "latency_p95": _worst_path(path_stats, "p95_ms") or route,
        "error_rate": _worst_path(path_stats, "error_pct") or route,
    }

    bugs: list[dict] = []
    for signal in FEATURE_NAMES:
        value = _as_float(peaks.get(signal))
        limit = LIMITS[signal]
        if value <= limit:
            continue
        location = signal_routes.get(signal, _PROCESS_LOCATION)
        bugs.append(
            {
                "signal": signal,
                "value": value,
                "limit": limit,
                "unit": UNITS[signal],
                "location": location,
                "fix": _fix_for(signal, location),
            }
        )

    model_only = False
    if not bugs and decision == "anomaly":
        model_only = True
        ranked = [
            (signal, _as_float(peaks.get(signal)), LIMITS[signal], UNITS[signal])
            for signal in FEATURE_NAMES
        ]
        ranked.sort(key=lambda row: row[1], reverse=True)
        top_signal, top_value, top_limit, top_unit = ranked[0]
        bugs.append(
            {
                "signal": top_signal,
                "value": top_value,
                "limit": top_limit,
                "unit": top_unit,
                "location": _PROCESS_LOCATION,
                "fix": (
                    "The model voted anomaly even though no signal crossed its "
                    f"limit. Highest peak was {top_signal} at "
                    f"{top_value:.0f}{top_unit} (limit {top_limit:.0f}{top_unit}). "
                    "Do not invent a path from this alone."
                ),
            }
        )

    if bugs and not model_only:
        summary = f"{len(bugs)} signal(s) crossed the verify limit."
    elif model_only:
        summary = "Model anomaly without a crossed threshold."
    elif decision == "healthy":
        summary = "Healthy verify. No crossed signals."
    else:
        summary = "No crossed signals."

    return {
        "bugs": bugs,
        "has_bugs": bool(bugs),
        "summary": summary,
        "version": version,
        "decision": decision,
        "most_affected_path": affected if isinstance(affected, str) else None,
        "model_only": model_only,
    }

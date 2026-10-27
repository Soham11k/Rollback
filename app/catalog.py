"""Read the GatePass flash-sale catalog."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "catalog.db"

_MONTHS = (
    "",
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)


def db_path() -> Path:
    return Path(os.environ.get("CATALOG_PATH", DEFAULT_DB))


def _connect() -> sqlite3.Connection:
    path = db_path()
    if not path.exists():
        raise FileNotFoundError(f"Catalog not found at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _date_parts(starts_at: str) -> dict:
    """Split '2026-11-08 19:00' into day / month / time for ticket stubs."""
    day, month, clock = "??", "???", "??:??"
    try:
        date_part, clock = starts_at.strip().split(None, 1)
        y, m, d = date_part.split("-")
        day = d
        month = _MONTHS[int(m)]
        clock = clock[:5]
    except (ValueError, IndexError, TypeError):
        pass
    return {"day": day, "month": month, "time": clock}


def list_events() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT e.id, e.title, e.venue, e.starts_at, e.blurb,
                   COALESCE(SUM(t.remaining), 0) AS seats_left,
                   COUNT(t.code) AS tier_count,
                   MIN(t.price_inr) AS from_price
            FROM events e
            LEFT JOIN tiers t ON t.event_id = e.id
            GROUP BY e.id
            ORDER BY e.starts_at
            """
        ).fetchall()
        tiers_by_event: dict[str, list[dict]] = {}
        for tier in conn.execute(
            """
            SELECT event_id, code, label, price_inr, remaining
            FROM tiers
            ORDER BY event_id, price_inr, code
            """
        ).fetchall():
            tiers_by_event.setdefault(tier["event_id"], []).append(
                {
                    "code": tier["code"],
                    "label": tier["label"],
                    "price_inr": int(tier["price_inr"]),
                    "remaining": int(tier["remaining"]),
                    "available": int(tier["remaining"]) > 0,
                }
            )

    events = []
    for row in rows:
        parts = _date_parts(row["starts_at"])
        tiers = tiers_by_event.get(row["id"], [])
        total = max(int(row["seats_left"]), 1)
        for i, tier in enumerate(tiers):
            tier["share_pct"] = round(100.0 * tier["remaining"] / total, 1)
            tier["pattern"] = f"t{i % 3}"
        events.append(
            {
                "id": row["id"],
                "title": row["title"],
                "venue": row["venue"],
                "starts_at": row["starts_at"],
                "blurb": row["blurb"],
                "seats_left": int(row["seats_left"]),
                "tier_count": int(row["tier_count"]),
                "from_price": int(row["from_price"] or 0),
                "tiers": tiers,
                "day": parts["day"],
                "month": parts["month"],
                "time": parts["time"],
            }
        )
    return events


def get_event(event_id: str) -> dict | None:
    key = event_id.strip().upper()
    if not key:
        return None
    with _connect() as conn:
        event = conn.execute(
            """
            SELECT id, title, venue, starts_at, blurb
            FROM events WHERE id = ?
            """,
            (key,),
        ).fetchone()
        if event is None:
            return None
        tiers = conn.execute(
            """
            SELECT code, label, price_inr, remaining
            FROM tiers WHERE event_id = ?
            ORDER BY price_inr, code
            """,
            (key,),
        ).fetchall()
    parts = _date_parts(event["starts_at"])
    tier_list = []
    for row in tiers:
        remaining = int(row["remaining"])
        # One square per 10 seats (ceil), for the seat-block map.
        blocks = max(1, (remaining + 9) // 10) if remaining > 0 else 0
        tier_list.append(
            {
                "code": row["code"],
                "label": row["label"],
                "price_inr": int(row["price_inr"]),
                "remaining": remaining,
                "available": remaining > 0,
                "blocks": blocks,
                "block_class": "tier-vip" if row["code"] in {"VIP", "TBL", "PRM", "FRT"} else "",
            }
        )
    return {
        "id": event["id"],
        "title": event["title"],
        "venue": event["venue"],
        "starts_at": event["starts_at"],
        "blurb": event["blurb"],
        "tiers": tier_list,
        "seats_left": sum(t["remaining"] for t in tier_list),
        "day": parts["day"],
        "month": parts["month"],
        "time": parts["time"],
    }


def checkout(event_id: str, tier_code: str, qty: int = 1) -> dict | None:
    """Stub confirmation — does not mutate inventory (safe across rollbacks)."""
    event = get_event(event_id)
    if event is None:
        return None
    code = tier_code.strip().upper()
    tier = next((t for t in event["tiers"] if t["code"] == code), None)
    if tier is None:
        return None
    quantity = max(1, min(int(qty), 6))
    if tier["remaining"] < quantity:
        return {
            "status": "sold_out",
            "event_id": event["id"],
            "tier": tier["code"],
            "requested": quantity,
            "remaining": tier["remaining"],
            "message": "Not enough seats left in this tier",
        }
    stamp = int(time.time())
    digest = hashlib.sha1(
        f"{event['id']}-{tier['code']}-{quantity}-{stamp}".encode()
    ).hexdigest()[:8].upper()
    return {
        "status": "confirmed",
        "booking_ref": f"GP-{digest}",
        "event_id": event["id"],
        "event_title": event["title"],
        "venue": event["venue"],
        "starts_at": event["starts_at"],
        "tier": tier["code"],
        "tier_label": tier["label"],
        "qty": quantity,
        "unit_price_inr": tier["price_inr"],
        "total_inr": tier["price_inr"] * quantity,
        "note": "Stub checkout — no payment captured; safe to roll back the app release.",
        "barcode_bars": _barcode_bars(digest),
    }


def _barcode_bars(digest: str) -> list[str]:
    """Map hex chars to bar width classes for the printed ticket."""
    bars: list[str] = []
    for i, ch in enumerate(digest):
        try:
            n = int(ch, 16)
        except ValueError:
            n = 1
        if n % 3 == 0:
            bars.append("w3")
        elif n % 2 == 0:
            bars.append("w2")
        else:
            bars.append("")
        if (i + 1) % 3 == 0:
            bars.append("gap")
    bars.extend(["w3", "", "w2"])
    return bars

"""Build the GatePass flash-sale catalog (course dataset)."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "catalog.db"

EVENTS = (
    ("EVT-01", "Monsoon Beats Live", "Arena North", "2026-11-08 19:00", "Flash sale — standing + reserved"),
    ("EVT-02", "Campus Comedy Night", "Auditorium B", "2026-11-12 20:00", "Sold in waves every hour"),
    ("EVT-03", "Indie Film Fest Opening", "Plaza Screen 1", "2026-11-15 18:30", "Limited premiere seats"),
    ("EVT-04", "Tech Summit Keynote", "Hall C", "2026-11-20 10:00", "General admission rush"),
    ("EVT-05", "City Marathon Expo", "Riverside Park", "2026-11-22 07:00", "Bib pickup + expo pass"),
    ("EVT-06", "Acoustic After Hours", "Rooftop Stage", "2026-11-28 21:00", "Intimate capacity sale"),
)

# (event_id, tier_code, label, price_inr, remaining)
TIERS = (
    ("EVT-01", "GA", "General Admission", 799, 420),
    ("EVT-01", "VIP", "VIP Balcony", 2499, 60),
    ("EVT-02", "STD", "Standard", 499, 280),
    ("EVT-02", "FRT", "Front Row", 999, 40),
    ("EVT-03", "PRE", "Premiere", 699, 180),
    ("EVT-03", "STU", "Student", 349, 90),
    ("EVT-04", "GA", "General Admission", 0, 500),
    ("EVT-04", "PRM", "Premium Seat", 1299, 75),
    ("EVT-05", "EXP", "Expo Pass", 299, 800),
    ("EVT-05", "VIP", "VIP Lounge", 1499, 50),
    ("EVT-06", "GA", "General Admission", 899, 120),
    ("EVT-06", "TBL", "Table of 4", 3499, 18),
)


def build(path: Path = DB_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="gatepass-", suffix=".db")
    os.close(fd)
    tmp = Path(name)
    try:
        _fill(tmp)
        shutil.copyfile(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def _fill(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE events (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                venue TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                blurb TEXT NOT NULL
            );
            CREATE TABLE tiers (
                event_id TEXT NOT NULL,
                code TEXT NOT NULL,
                label TEXT NOT NULL,
                price_inr INTEGER NOT NULL,
                remaining INTEGER NOT NULL,
                PRIMARY KEY (event_id, code),
                FOREIGN KEY (event_id) REFERENCES events(id)
            );
            CREATE INDEX idx_tiers_event ON tiers(event_id);
            """
        )
        conn.executemany(
            "INSERT INTO events (id, title, venue, starts_at, blurb) VALUES (?, ?, ?, ?, ?)",
            EVENTS,
        )
        conn.executemany(
            """
            INSERT INTO tiers (event_id, code, label, price_inr, remaining)
            VALUES (?, ?, ?, ?, ?)
            """,
            TIERS,
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    written = build()
    print(f"wrote {written}")
    for event_id, title, *_ in EVENTS[:3]:
        print(event_id, title)

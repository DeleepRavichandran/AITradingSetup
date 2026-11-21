"""Point-in-time store for timestamped news, earnings and macro events.

The platform ships with NO news content. This is the ingestion and query layer for a feed
the operator is licensed to use. Each record carries `published_at` — the first moment the
item was publicly knowable — and queries are always "as of" a timestamp, so research code
cannot read a headline before it existed.

STATUS: storage, CSV ingestion and as-of queries are implemented and tested. No vendor
connector is implemented, and strategies cannot yet subscribe to events inside the backtest
loop (agents and the API can query them).
"""
from __future__ import annotations

import csv
import hashlib
import sqlite3
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, published_at TEXT NOT NULL, event_time TEXT, category TEXT NOT NULL,
  symbols TEXT NOT NULL DEFAULT '', headline TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL, licence TEXT NOT NULL, ingested_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_events_pub ON events(published_at);
"""
CATEGORIES = {"news", "earnings", "macro", "corporate_action", "regulatory"}


class EventStore:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def add(self, *, published_at, category: str, headline: str, source: str, licence: str, symbols=(), body: str = "",
            event_time=None) -> str:
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of {sorted(CATEGORIES)}")
        if not source.strip() or not licence.strip():
            raise ValueError("source and licence are mandatory for every event")
        pub = pd.Timestamp(published_at).isoformat()
        eid = hashlib.sha256(f"{pub}|{source}|{headline}".encode()).hexdigest()[:20]
        self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", (
            eid, pub, pd.Timestamp(event_time).isoformat() if event_time else None, category,
            ",".join(sorted(symbols)), headline, body, source, licence, pd.Timestamp.now().isoformat(timespec="seconds")))
        self.db.commit()
        return eid

    def ingest_csv(self, path: Path, *, source: str, licence: str) -> int:
        """CSV columns: published_at, category, headline[, symbols (;-separated), body, event_time]."""
        n = 0
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                self.add(published_at=row["published_at"], category=row["category"], headline=row["headline"],
                         symbols=[s for s in (row.get("symbols") or "").split(";") if s], body=row.get("body") or "",
                         event_time=row.get("event_time") or None, source=source, licence=licence)
                n += 1
        return n

    def as_of(self, ts, *, symbol: str | None = None, category: str | None = None, limit: int = 100) -> list[dict]:
        """Events published at or before `ts`, newest first. Nothing published later is visible."""
        q, args = "SELECT * FROM events WHERE published_at <= ?", [pd.Timestamp(ts).isoformat()]
        if category:
            q += " AND category = ?"; args.append(category)
        rows = [dict(r) for r in self.db.execute(q + " ORDER BY published_at DESC", args)]
        if symbol:
            rows = [r for r in rows if symbol in r["symbols"].split(",")]
        return rows[:limit]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

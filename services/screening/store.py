"""Durable storage for the screening loop (one SQLite file, WAL mode).

    events        every 1-minute bar received, keyed (symbol, ts). A second copy of the same event is
                  ignored; a copy with DIFFERENT values is kept in `event_conflicts` and not applied
                  (first value wins). Bars that arrive after their minute was processed are stored
                  with late=1 and not applied either.
    cycles        one row per processed minute: latency, freshness, eligible/evaluated counts, gaps.
    signals       actionable decisions (BUY / EXIT ...), keyed (config_id, bar_ts), with the allocator's outcome
                  (accepted / rejected / deferred), its reason and the ranking score.
    paper_orders  the FORWARD PAPER LEDGER of the screening loop, keyed (config_id, decided_at, side).
                  Separate from the backtest ledgers (historical research) and the paper-session
                  journals; nothing here is ever sent to a broker.
    configs       the registered configurations (for listing and filtering).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from functools import wraps
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (symbol TEXT NOT NULL, ts TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
  volume REAL, source TEXT, received_at REAL, late INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (symbol, ts));
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS event_conflicts (symbol TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
  source TEXT, received_at REAL);
CREATE TABLE IF NOT EXISTS cycles (minute TEXT PRIMARY KEY, processed_at REAL, latency_ms REAL, lag_s REAL, mode TEXT,
  status TEXT, symbols_with_data INTEGER, symbols_expected INTEGER, closed TEXT, eligible INTEGER, evaluated INTEGER,
  skipped_pending INTEGER, stale_skips INTEGER, counts TEXT, fills INTEGER);
CREATE TABLE IF NOT EXISTS signals (config_id TEXT NOT NULL, bar_ts TEXT NOT NULL, horizon TEXT, symbol TEXT, decision TEXT,
  price REAL, reason TEXT, PRIMARY KEY (config_id, bar_ts));
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(bar_ts);
CREATE TABLE IF NOT EXISTS paper_orders (order_key TEXT PRIMARY KEY, config_id TEXT, symbol TEXT, side TEXT, horizon TEXT,
  decided_at TEXT, status TEXT, fill_ts TEXT, qty INTEGER, price REAL, product TEXT, charges_paise INTEGER, realized_paise INTEGER);
CREATE TABLE IF NOT EXISTS journal (fill_key TEXT PRIMARY KEY, ts TEXT, config_id TEXT, symbol TEXT, side TEXT,
  minute_of_session INTEGER, stock_move REAL, trend REAL, vol_session REAL, market_move REAL, breadth REAL, regime TEXT, news_60m INTEGER);
CREATE INDEX IF NOT EXISTS ix_journal_ts ON journal(ts);
CREATE TABLE IF NOT EXISTS paper_fills (fill_key TEXT PRIMARY KEY, order_key TEXT, config_id TEXT, symbol TEXT, side TEXT, ts TEXT,
  qty INTEGER, price REAL, charges_paise INTEGER, realized_paise INTEGER);
CREATE INDEX IF NOT EXISTS ix_fills_order ON paper_fills(order_key);
CREATE TABLE IF NOT EXISTS refusals (minute TEXT, reason TEXT, n INTEGER, PRIMARY KEY (minute, reason));
CREATE TABLE IF NOT EXISTS portfolio_events (ts TEXT, event TEXT, detail TEXT, PRIMARY KEY (ts, event));
CREATE TABLE IF NOT EXISTS configs (id TEXT PRIMARY KEY, idx INTEGER, symbol TEXT, timeframe TEXT, horizon TEXT, family TEXT,
  params TEXT, stop_pct REAL, status TEXT NOT NULL DEFAULT 'active');
CREATE INDEX IF NOT EXISTS ix_configs_h ON configs(horizon, family);
CREATE INDEX IF NOT EXISTS ix_orders_cfg ON paper_orders(config_id, side, realized_paise, charges_paise);
"""


def _locked(fn):
    """One connection is shared by the scheduler thread, API requests and replay jobs: serialise access."""
    @wraps(fn)
    def w(self, *a, **k):
        with self._lock:
            return fn(self, *a, **k)
    return w


def _iso(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")


class ScreeningStore:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        for table, col, typ in (("paper_orders", "requested_qty", "INTEGER"), ("paper_orders", "reason", "TEXT"),
                                ("signals", "alloc_status", "TEXT"), ("signals", "alloc_reason", "TEXT"), ("signals", "alloc_score", "REAL"),
                                ("cycles", "equity_inr", "REAL"), ("cycles", "cash_inr", "REAL"), ("cycles", "halted", "INTEGER"),
                                ("cycles", "decided_at", "TEXT"), ("cycles", "arrived_at", "REAL"), ("cycles", "earliest_fill", "TEXT")):
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:                                  # runs created before these columns existed
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        # Strategy Universe reads per-configuration signal counts; this index answers them without a table scan
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_signals_cfg ON signals(decision, config_id, alloc_status)")
        # dashboard reads by fill time (today's trades, charts); without it every page refresh scanned all orders
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_orders_fill ON paper_orders(fill_ts)")
        self._readers = threading.local()

    def reader(self) -> sqlite3.Connection:
        """A read-only connection for this thread (WAL: readers never wait for the minute loop's writes, and never make it
        wait). Dashboard queries use this instead of the writer connection and its lock."""
        c = getattr(self._readers, "c", None)
        if c is None:
            c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            c.row_factory = sqlite3.Row
            self._readers.c = c
        return c

    # ------------------------------------------------------------------ events
    @_locked
    def add_events(self, bars: list[dict], *, processed_upto: str | None, source: str) -> dict:
        """bars: dicts with symbol, ts, open, high, low, close, volume. Returns counts by outcome."""
        out = {"accepted": 0, "duplicate": 0, "conflict": 0, "late": 0}
        now = time.time()
        cur = self.db.cursor()
        cur.execute("BEGIN")
        try:
            for b in bars:
                ts = _iso(b["ts"])
                late = 1 if processed_upto is not None and ts <= processed_upto else 0
                row = (b["symbol"], ts, float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"]), float(b["volume"]))
                cur.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", (*row, source, now, late))
                if cur.rowcount == 1:
                    out["late" if late else "accepted"] += 1
                    continue
                old = cur.execute("SELECT open, high, low, close, volume FROM events WHERE symbol=? AND ts=?", row[:2]).fetchone()
                if tuple(old) == row[2:]:
                    out["duplicate"] += 1
                else:
                    cur.execute("INSERT INTO event_conflicts VALUES (?,?,?,?,?,?,?,?,?)", (*row, source, now))
                    out["conflict"] += 1
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return out

    @_locked
    def pending_minutes(self, after: str | None, upto: str) -> list[str]:
        q = "SELECT DISTINCT ts FROM events WHERE late = 0 AND ts <= ?" + (" AND ts > ?" if after else "") + " ORDER BY ts"
        return [r[0] for r in self.db.execute(q, (upto, after) if after else (upto,))]

    @_locked
    def decided_at(self, minute: str) -> str | None:
        """When this minute was first decided (a restart replays it with the same time, so it decides the same way)."""
        r = self.db.execute("SELECT decided_at FROM cycles WHERE minute = ? AND status = 'processed'", (minute,)).fetchone()
        return r[0] if r else None

    def arrived_at(self, ts: str) -> float | None:
        return self.db.execute("SELECT MAX(received_at) FROM events WHERE ts = ? AND late = 0", (ts,)).fetchone()[0]

    def bars_at(self, ts: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT symbol, open, high, low, close, volume FROM events WHERE ts = ? AND late = 0 ORDER BY symbol", (ts,)).fetchall()

    # ------------------------------------------------------------------ results
    @_locked
    def record(self, cycle: dict, signals: list[tuple], orders: list[tuple], fills: list[tuple], done: list[tuple] = (),
               events: list[tuple] = (), refusals: list[tuple] = (), journal: list[tuple] = ()) -> None:
        """orders: (order_key, config_id, symbol, side, horizon, decided_at, status, requested_qty, reason)
        fills: (fill_key, order_key, config_id, symbol, side, ts, qty, price, charges_paise, realized_paise)
        done: (status, last_fill_ts, filled_qty, avg_price, product, charges_paise, realized_paise, reason, order_key)"""
        cycle = {"equity_inr": None, "cash_inr": None, "halted": None, "decided_at": None, "arrived_at": None, "earliest_fill": None, **cycle}
        cur = self.db.cursor()
        cur.execute("BEGIN")
        try:
            cur.execute("INSERT OR IGNORE INTO cycles (minute, processed_at, latency_ms, lag_s, mode, status, symbols_with_data, symbols_expected, "
                        "closed, eligible, evaluated, skipped_pending, stale_skips, counts, fills, equity_inr, cash_inr, halted, decided_at, arrived_at, "
                        "earliest_fill) VALUES "
                        "(:minute,:processed_at,:latency_ms,:lag_s,:mode,:status,:symbols_with_data,:symbols_expected,:closed,:eligible,"
                        ":evaluated,:skipped_pending,:stale_skips,:counts,:fills,:equity_inr,:cash_inr,:halted,:decided_at,:arrived_at,:earliest_fill)", cycle)
            cur.executemany("INSERT OR IGNORE INTO signals (config_id, bar_ts, horizon, symbol, decision, price, reason, alloc_status, alloc_reason, "
                            "alloc_score) VALUES (?,?,?,?,?,?,?,?,?,?)", signals)
            cur.executemany("INSERT OR IGNORE INTO paper_orders (order_key, config_id, symbol, side, horizon, decided_at, status, requested_qty, reason) "
                            "VALUES (?,?,?,?,?,?,?,?,?)", orders)
            cur.executemany("INSERT OR IGNORE INTO paper_fills VALUES (?,?,?,?,?,?,?,?,?,?)", fills)
            cur.executemany("UPDATE paper_orders SET status=?, fill_ts=?, qty=?, price=?, product=?, charges_paise=?, realized_paise=?, "
                            "reason=? WHERE order_key=? AND status='pending'", done)
            cur.executemany("INSERT OR IGNORE INTO portfolio_events VALUES (?,?,?)", events)
            cur.executemany("INSERT OR IGNORE INTO refusals VALUES (?,?,?)", refusals)
            cur.executemany("INSERT OR IGNORE INTO journal VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", journal)
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    @_locked
    def register_configs(self, rows: list[tuple]) -> None:
        if self.db.execute("SELECT COUNT(*) FROM configs").fetchone()[0] == len(rows):
            return
        cur = self.db.cursor()
        cur.execute("BEGIN")
        cur.execute("DELETE FROM configs")
        cur.executemany("INSERT INTO configs (id, idx, symbol, timeframe, horizon, family, params, stop_pct) VALUES (?,?,?,?,?,?,?,?)", rows)
        cur.execute("COMMIT")

    @_locked
    def counts(self) -> dict:
        q = lambda s: self.db.execute(s).fetchone()[0]
        return {"events": q("SELECT COUNT(*) FROM events"), "late_events": q("SELECT COUNT(*) FROM events WHERE late=1"),
                "event_conflicts": q("SELECT COUNT(*) FROM event_conflicts"), "cycles": q("SELECT COUNT(*) FROM cycles"),
                "signals": q("SELECT COUNT(*) FROM signals"), "paper_orders": q("SELECT COUNT(*) FROM paper_orders WHERE status != 'rejected'"),
                "paper_refused": q("SELECT COALESCE(SUM(n), 0) FROM refusals"),
                "paper_filled": q("SELECT COUNT(*) FROM paper_orders WHERE status='filled'"), "paper_fills": q("SELECT COUNT(*) FROM paper_fills"),
                "portfolio_events": q("SELECT COUNT(*) FROM portfolio_events")}

    @_locked
    def page(self, table: str, *, where: dict | None = None, order: str, limit: int = 50, offset: int = 0,
             extra: list[tuple[str, list]] | None = None) -> dict:
        allowed = {"signals": {"horizon", "symbol", "decision", "config_id", "alloc_status"}, "paper_orders": {"horizon", "symbol", "side", "status", "config_id"},
                   "paper_fills": {"symbol", "side", "config_id", "order_key"}, "portfolio_events": {"event"}, "refusals": {"reason"},
                   "cycles": {"status"}, "configs": {"horizon", "family", "timeframe", "symbol", "status"}}
        if table not in allowed:
            raise ValueError(table)
        clauses, args = [], []
        for k, v in (where or {}).items():
            if v is None or v == "":
                continue
            if k not in allowed[table]:
                raise ValueError(f"cannot filter {table} by {k}")
            clauses.append(f"{k} = ?")
            args.append(v)
        for clause, a in extra or []:                      # fixed, code-defined clauses (never user text) with bound arguments
            clauses.append(clause)
            args.extend(a)
        w = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = self.db.execute(f"SELECT COUNT(*) FROM {table}{w}", args).fetchone()[0]
        rows = [dict(r) for r in self.db.execute(f"SELECT * FROM {table}{w} ORDER BY {order} LIMIT ? OFFSET ?", [*args, limit, offset])]
        for r in rows:
            for k in ("counts", "params", "closed"):
                if k in r and isinstance(r[k], str):
                    try:
                        r[k] = json.loads(r[k])
                    except ValueError:
                        pass
        return {"total": total, "limit": limit, "offset": offset, "rows": rows}

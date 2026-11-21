"""One classification of market-data provenance, used by every API response and dashboard page.

kind               meaning
synthetic_replay   stored SYNTHETIC bars replayed as if live (says nothing about real markets)
replay             stored real bars replayed as if live (historical, not live)
end_of_day         each day's completed daily candle, fetched after the close
live_unverified    live completed 1-minute candles polled from a broker whose adapter has NOT been verified
                   against the real service (mock-tested only)
live_verified      the same, after the operator's verification run passed (see `verification_status`)
none               no feed attached

`delay_seconds` is the structural delay of the source (a polled 1-minute candle is complete only when its
minute has closed, and is fetched up to one cycle later). `freshness_seconds` is measured: now minus the
close of the last bar actually processed. During market hours a feed with no bar for more than
`STALE_AFTER` seconds is reported stale. Nothing here is guessed: unknown values are None.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

STALE_AFTER = 180
VERIFICATION_FILE = "angel_verification.json"
LABELS = {
    "synthetic_replay": "Replay of SYNTHETIC data",
    "replay": "Replay of stored historical data",
    "end_of_day": "End-of-day candles",
    "live_unverified": "Live 1-minute candles (adapter not verified)",
    "live_verified": "Live 1-minute candles (verified)",
    "none": "No market data attached",
}


def verification_status(experiments_dir: Path | None) -> dict:
    """The operator's last real-service verification run (written by `nseql angel-verify`), if any."""
    f = Path(experiments_dir) / VERIFICATION_FILE if experiments_dir else None
    if not f or not f.exists():
        return {"verified": False, "detail": "never verified against the real service (mock-tested only)"}
    try:
        d = json.loads(f.read_text())
    except ValueError:
        return {"verified": False, "detail": "verification report unreadable"}
    ok = bool(d.get("passed"))
    bad = [c.get("name", "?") for c in d.get("checks", []) if c.get("status") != "passed"]
    return {"verified": ok, "at": d.get("at"), "detail": "verification run passed" if ok else
            (d.get("detail") or f"verification run not passed: {', '.join(bad)}")[:200]}


def classify(feed, *, synthetic: bool | None = None, last_bar: str | None = None, now: datetime | None = None,
             market_open: bool | None = None, experiments_dir: Path | None = None) -> dict:
    kind_attr = getattr(feed, "kind", None) if feed is not None else None
    if feed is None:
        kind, delay = "none", None
    elif kind_attr == "replay":
        kind, delay = ("synthetic_replay" if synthetic else "replay"), None
    elif kind_attr in ("kite_daily", "angel_daily", "end_of_day"):
        kind, delay = "end_of_day", 15 * 60          # candle treated as final from 15:45 IST, 15 min after the close
    elif kind_attr == "live_poll":
        ver = verification_status(experiments_dir)
        kind, delay = ("live_verified" if ver["verified"] else "live_unverified"), 60
    else:
        kind, delay = "none", None
    fresh = stale = None
    if last_bar and kind in ("live_unverified", "live_verified") and now is not None:
        fresh = round((pd.Timestamp(now) - pd.Timestamp(last_bar)).total_seconds(), 1)
        stale = bool(market_open) and fresh > STALE_AFTER
    out = {"kind": kind, "label": LABELS[kind], "live": kind.startswith("live"), "verified": kind == "live_verified",
           "synthetic": kind == "synthetic_replay", "delay_seconds": delay, "freshness_seconds": fresh, "stale": stale,
           "market_open": market_open, "source": getattr(feed, "label", None)}
    if kind == "live_unverified":
        out["detail"] = verification_status(experiments_dir)["detail"]
    if stale:
        out["label"] += f" - STALE: no new bar for {int(fresh)} s"
    return out


def classify_dataset(ds) -> dict:
    """For pages that read a stored dataset (backtests, research)."""
    syn = bool(getattr(ds, "synthetic", False))
    return {"kind": "synthetic_replay" if syn else "replay", "label": "SYNTHETIC dataset" if syn else "Stored historical dataset",
            "live": False, "verified": False, "synthetic": syn, "dataset": getattr(ds, "id", None)}

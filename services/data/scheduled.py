"""Scheduled events that are known in advance: company results, RBI policy, the Union Budget,
US Fed (FOMC) decisions and NSE monthly F&O expiry.

These are fair to use in a backtest because they are announced BEFORE they happen, as long as a
rule only uses an event once it was public. Every event therefore carries two dates:

    event_date   when it happens
    known_from   when it became public (board-meeting intimation for results, the published
                 meeting calendar for RBI/FOMC, the Budget date announcement ...)

An event is usable for decisions from the trading day AFTER `known_from`, never earlier. The
platform ships no event data: the operator supplies a CSV (copied from exchange filings and the
official RBI / Federal Reserve calendars). Monthly F&O expiry is the exception: it follows a
published rule and is generated (see `monthly_expiries`).

Events are stored in the point-in-time EventStore (category earnings / macro) with event_time set.
"""
from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from nseql.data.events import EventStore

KINDS = {"results": "earnings", "rbi_policy": "macro", "union_budget": "macro", "fomc": "macro", "fno_expiry": "macro"}
SYMBOL_KINDS = ("results",)
MACRO_KINDS = ("rbi_policy", "union_budget", "fomc")
# NSE moved monthly index expiry from the last Thursday to the last Tuesday (date in the market-structure
# timeline, marked unverified there). Before that: last Thursday.
EXPIRY_WEEKDAY_CHANGE = date(2025, 9, 1)


def add_event(store: EventStore, *, kind: str, event_date: date, known_from: date, label: str, source: str, licence: str,
              symbol: str | None = None) -> str:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    if known_from > event_date:
        raise ValueError(f"{label}: known_from {known_from} is after the event {event_date}")
    if kind in SYMBOL_KINDS and not symbol:
        raise ValueError(f"{label}: a {kind} event needs a symbol")
    # usable from the trading day after known_from: stamp it after that day's close
    return store.add(published_at=pd.Timestamp(known_from) + pd.Timedelta(hours=23, minutes=59), category=KINDS[kind],
                     headline=label, source=source, licence=licence, symbols=[symbol.upper()] if symbol else (),
                     body=json.dumps({"kind": kind}), event_time=pd.Timestamp(event_date))


def ingest_schedule_csv(store: EventStore, path: str | Path, *, source: str, licence: str) -> int:
    """Columns: event_date, kind, known_from, label[, symbol]. Dates as YYYY-MM-DD. known_from is required:
    an event without the date it became public cannot be used without risking hindsight."""
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for line, row in enumerate(csv.DictReader(f), 2):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            if not row.get("known_from"):
                raise ValueError(f"{path}:{line}: known_from is required (when was this event first public?)")
            add_event(store, kind=row.get("kind", "").lower(), event_date=date.fromisoformat(row["event_date"]),
                      known_from=date.fromisoformat(row["known_from"]), label=row.get("label") or row.get("kind", ""),
                      symbol=row.get("symbol") or None, source=source, licence=licence)
            n += 1
    return n


def monthly_expiries(start: date, end: date, is_session=None) -> list[date]:
    """Monthly index F&O expiry: last Thursday of the month (last Tuesday from 2025-09-01), moved to the
    previous trading day if that is a holiday. Rule-based; the switch date is unverified."""
    is_session = is_session or (lambda d: d.weekday() < 5)
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        d = nxt - timedelta(days=1)
        wd = 1 if d >= EXPIRY_WEEKDAY_CHANGE else 3
        while d.weekday() != wd:
            d -= timedelta(days=1)
        while not is_session(d):
            d -= timedelta(days=1)
        if start <= d <= end:
            out.append(d)
        y, m = nxt.year, nxt.month
    return out


def add_expiries(store: EventStore, start: date, end: date, is_session=None) -> int:
    n = 0
    for d in monthly_expiries(start, end, is_session):
        # contracts are listed about three months ahead, so the expiry date is public well before
        add_event(store, kind="fno_expiry", event_date=d, known_from=d - timedelta(days=60), label=f"monthly F&O expiry {d:%b %Y}",
                  source="NSE expiry rule (generated)", licence="rule-derived; no third-party data")
        n += 1
    return n


def scheduled(store: EventStore) -> list[dict]:
    rows = store.db.execute("SELECT * FROM events WHERE event_time IS NOT NULL ORDER BY event_time").fetchall()
    out = []
    for r in rows:
        r = dict(r)
        try:
            r["kind"] = json.loads(r["body"] or "{}").get("kind")
        except ValueError:
            r["kind"] = None
        out.append(r)
    return [r for r in out if r["kind"] in KINDS]


def blackout(store: EventStore | None, dates, symbols: list[str], *, before: int = 2) -> dict:
    """Point-in-time "be flat over the event" masks (1 = holding allowed, 0 = flat).

    A decision at the close of row t is held from the open of t+1 to the open of t+2, so a position
    decided on any of the `before` trading days before the event day is exposed to the event; those rows
    are set to 0, but only on rows where the event was already public. Returns per-symbol results mask
    [T, N], market masks [T] for macro events and expiry, and counts."""
    ts = pd.DatetimeIndex(pd.to_datetime(list(dates))).normalize()
    T, N = len(ts), len(symbols)
    res = np.ones((T, N), dtype=np.int8)
    macro = np.ones(T, dtype=np.int8)
    expiry = np.ones(T, dtype=np.int8)
    counts = {"results": 0, "macro": 0, "fno_expiry": 0}
    if store is None:
        return {"ctx_no_results": res, "ctx_no_macro_event": macro, "ctx_no_expiry": expiry, "counts": counts}
    col = {s: j for j, s in enumerate(symbols)}
    for e in scheduled(store):
        ev = pd.Timestamp(e["event_time"]).normalize()
        known = pd.Timestamp(e["published_at"])
        k = int(ts.searchsorted(ev))                                 # first row on/after the event day
        if k >= T or k == 0:
            continue
        first_known = int(ts.searchsorted(known.normalize(), side="right"))   # first row after the day it became public
        rows = np.arange(max(0, k - before), k)
        rows = rows[rows >= first_known]
        if not len(rows):
            continue
        if e["kind"] == "results":
            for s in (e["symbols"] or "").split(","):
                if s in col:
                    res[rows, col[s]] = 0
                    counts["results"] += 1
        elif e["kind"] == "fno_expiry":
            expiry[rows] = 0
            counts["fno_expiry"] += 1
        else:
            macro[rows] = 0
            counts["macro"] += 1
    return {"ctx_no_results": res, "ctx_no_macro_event": macro, "ctx_no_expiry": expiry, "counts": counts}

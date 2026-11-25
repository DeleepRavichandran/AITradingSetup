"""Chart Analyst: trend, levels, indicators and patterns from completed candles only."""
import sqlite3

import numpy as np
import pandas as pd

from nseql.screening import chart_analysis as CA


def _db(rows):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE events (symbol TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT, received_at REAL, late INTEGER)")
    db.executemany("INSERT INTO events VALUES ('AAA',?,?,?,?,?,?,'t',0,0)", rows)
    return db


def _day(day, path, vol=1000):
    t0 = pd.Timestamp(f"{day}T09:16:00")
    out, prev = [], path[0]
    for k, c in enumerate(path):
        o = prev
        out.append(((t0 + pd.Timedelta(minutes=k)).strftime("%Y-%m-%dT%H:%M:%S"), o, max(o, c) + 0.05, min(o, c) - 0.05, c, vol))
        prev = c
    return out


def test_uptrend_levels_rsi_and_breakout():
    prev = _day("2026-11-20", list(np.linspace(97, 98, 375))) + _day("2026-11-23", list(np.linspace(98, 100, 375))) + \
        _day("2026-11-24", list(np.linspace(100, 104, 375)))
    today = _day("2026-11-25", list(np.linspace(104, 110, 300)))
    db = _db(prev + today)
    a = CA.analyse(db, "AAA", "2026-11-25T14:20:00", series=True)
    assert a["trend"]["5m"] == "up" and a["trend"]["15m"] == "up"
    labels = {l["label"] for l in a["levels"]}
    assert {"previous day high", "previous day low", "VWAP", "opening range high (09:15-09:30)"} <= labels
    assert 0 <= a["indicators"]["rsi14"] <= 100 and a["indicators"]["rsi14"] > 60
    assert any(p.startswith("range breakout") for p in a["patterns"])
    assert a["support"]["price"] < a["price"] and (a["resistance"] is None or a["resistance"]["price"] > a["price"])
    assert len(a["series"]["t"]) == len(a["series"]["ema20"]) == len(a["series"]["rsi14"]) and a["read"][0].startswith("Trend:")
    lab = CA.entry_labels(a)
    assert lab["ch_trend"] == "5m and 15m both up" and lab["ch_pattern"] == "range breakout"


def test_double_bottom_is_confirmed_only_above_the_middle_peak():
    path = list(np.linspace(110, 100, 40)) + list(np.linspace(100, 105, 30)) + list(np.linspace(105, 100.1, 30)) + list(np.linspace(100.1, 106, 40))
    db = _db(_day("2026-11-25", path))
    a = CA.analyse(db, "AAA", "2026-11-25T11:40:00")
    assert any(p.startswith("double bottom confirmed") for p in a["patterns"])
    early = CA.analyse(db, "AAA", "2026-11-25T11:00:00")                  # before the close above the peak
    assert not any(p.startswith("double bottom") for p in early["patterns"])


def test_future_candles_are_never_used():
    db = _db(_day("2026-11-25", list(np.linspace(100, 105, 200))))
    a = CA.analyse(db, "AAA", "2026-11-25T11:00:00")
    db.executemany("INSERT INTO events VALUES ('AAA',?,?,?,?,?,?,'t',0,0)", _day("2026-11-25", [1.0] * 375)[105:])   # a later crash
    assert CA.analyse(db, "AAA", "2026-11-25T11:00:00") == a

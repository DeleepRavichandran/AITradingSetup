"""Price action and candlesticks at the moment of a buy (operator, 2026-11-25: "check price action, study more about candles").

From the account's own stored 1-minute bars, strictly BEFORE the fill minute (point in time), this rebuilds completed
5-minute candles for the session and describes what a chart reader would see:

  candle      the last completed 5-minute candle: colour, body and wick sizes as a share of its range
  pattern     a classic single/two-candle pattern on the last two completed candles:
                bullish engulfing / bearish engulfing  body fully covers the previous opposite-coloured body
                hammer          long lower wick (>= 2x body), small upper wick, body in the top third: buyers rejected lower prices
                shooting star   the mirror of a hammer: sellers rejected higher prices
                doji            body <= 10% of the range: indecision
                inside bar      high and low inside the previous candle: contraction before a move
                marubozu        body >= 90% of the range: one side in full control
  structure   the last three 5-minute candles: higher highs and higher lows (up), lower highs and lower lows (down), or range
  location    where the price sits in today's range (0 = day low, 1 = day high), versus VWAP and the previous day's high/low
  volume      the last 5-minute candle's volume versus the session's average 5-minute volume

Thresholds are the textbook definitions, fixed here and not fitted. What matters is the evidence: the daily review splits
the day's winners and losers by these labels, and a label becomes a candidate entry filter only after it separates them
on days it was not chosen on (the experiment protocol).
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

TF = 5


def candles(m1: pd.DataFrame, tf: int = TF) -> pd.DataFrame:
    """1-minute bars (ts = minute close) -> completed tf-minute candles aligned to 09:15."""
    if m1.empty:
        return m1
    d = m1.copy()
    d["ts"] = pd.to_datetime(d.ts)
    start = d.ts.dt.normalize() + pd.Timedelta(hours=9, minutes=15)
    k = ((d.ts - start).dt.total_seconds() // 60 - 1) // tf                      # bar 09:16..09:20 -> bucket 0
    g = d.groupby([d.ts.dt.normalize(), k])
    c = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(), "close": g.close.last(), "volume": g.volume.sum(),
                      "n": g.size(), "end": g.ts.max()}).reset_index(drop=True)
    return c[c.n >= tf].reset_index(drop=True)                                   # only complete candles


def _shape(r) -> dict:
    rng = max(r.high - r.low, 1e-9)
    body = abs(r.close - r.open)
    return {"green": bool(r.close > r.open), "body": body / rng, "upper": (r.high - max(r.open, r.close)) / rng,
            "lower": (min(r.open, r.close) - r.low) / rng, "range": rng, "body_abs": body}


def pattern(c: pd.DataFrame) -> str:
    if len(c) < 1:
        return "unknown"
    a = _shape(c.iloc[-1])
    if len(c) >= 2:
        p, pr = _shape(c.iloc[-2]), c.iloc[-2]
        cur = c.iloc[-1]
        if a["green"] and not p["green"] and cur.close >= pr.open and cur.open <= pr.close and a["body_abs"] > p["body_abs"]:
            return "bullish engulfing"
        if not a["green"] and p["green"] and cur.open >= pr.close and cur.close <= pr.open and a["body_abs"] > p["body_abs"]:
            return "bearish engulfing"
        if cur.high < pr.high and cur.low > pr.low:
            return "inside bar"
    if a["body"] <= 0.10:
        return "doji"
    if a["lower"] >= 2 * a["body"] and a["upper"] <= 0.15 and a["lower"] >= 0.5:
        return "hammer"
    if a["upper"] >= 2 * a["body"] and a["lower"] <= 0.15 and a["upper"] >= 0.5:
        return "shooting star"
    if a["body"] >= 0.90:
        return "green marubozu" if a["green"] else "red marubozu"
    return "green candle" if a["green"] else "red candle"


def structure(c: pd.DataFrame) -> str:
    if len(c) < 3:
        return "unknown"
    h, lo = c.high.to_numpy()[-3:], c.low.to_numpy()[-3:]
    if h[2] > h[1] > h[0] and lo[2] > lo[1] > lo[0]:
        return "higher highs, higher lows (up)"
    if h[2] < h[1] < h[0] and lo[2] < lo[1] < lo[0]:
        return "lower highs, lower lows (down)"
    return "range"


def at_entry(db: sqlite3.Connection, symbol: str, fill_ts: str) -> dict:
    """Price action known just before `fill_ts` (only bars stamped before it)."""
    ts = pd.Timestamp(fill_ts)
    day = ts.normalize()
    m1 = pd.read_sql_query("SELECT ts, open, high, low, close, volume FROM events WHERE symbol=? AND late=0 AND ts >= ? AND ts < ? ORDER BY ts",
                           db, params=(symbol, day.strftime("%Y-%m-%d"), ts.strftime("%Y-%m-%dT%H:%M:%S")))
    out = {"pattern": "unknown", "structure": "unknown", "day_position": None, "vs_vwap": None, "vs_prev_high": None, "volume_ratio": None}
    if m1.empty:
        return out
    c = candles(m1)
    if len(c):
        out["pattern"], out["structure"] = pattern(c), structure(c)
        a = _shape(c.iloc[-1])
        out["candle"] = {k: round(float(v), 3) for k, v in a.items() if k in ("body", "upper", "lower")} | {"green": a["green"]}
        if len(c) >= 2:
            avg = float(c.volume.iloc[:-1].mean())
            out["volume_ratio"] = round(float(c.volume.iloc[-1]) / avg, 2) if avg > 0 else None
    last = float(m1.close.iloc[-1])
    hi, lo = float(m1.high.max()), float(m1.low.min())
    out["day_position"] = round((last - lo) / (hi - lo), 3) if hi > lo else None
    tp = (m1.high + m1.low + m1.close) / 3
    vol = m1.volume.clip(lower=0)
    if vol.sum() > 0:
        out["vs_vwap"] = round(last / float((tp * vol).sum() / vol.sum()) - 1, 5)
    prev = db.execute("SELECT MAX(high) FROM events WHERE symbol=? AND late=0 AND ts >= ? AND ts < ?",
                      (symbol, (day - pd.Timedelta(days=5)).strftime("%Y-%m-%d"), day.strftime("%Y-%m-%d"))).fetchone()[0]
    if prev:
        last_day = db.execute("SELECT substr(MAX(ts),1,10) FROM events WHERE symbol=? AND late=0 AND ts < ?", (symbol, day.strftime("%Y-%m-%d"))).fetchone()[0]
        ph = db.execute("SELECT MAX(high) FROM events WHERE symbol=? AND late=0 AND ts >= ? AND ts < ?",
                        (symbol, last_day, (pd.Timestamp(last_day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))).fetchone()[0]
        out["vs_prev_high"] = round(last / float(ph) - 1, 5) if ph else None
    try:                                                   # the full chart read at that moment (chart_analysis.py)
        from nseql.screening.chart_analysis import analyse
        a = analyse(db, symbol, ts)
        out["chart"] = {k: a.get(k) for k in ("trend", "support", "resistance", "indicators", "patterns", "read")}
    except Exception:       # noqa: BLE001 - recording context must never stop trading
        out["chart"] = None
    return out


def labels(pa: dict) -> dict:
    """Readable buckets for the daily review (loss study factors)."""
    dp, vw, vr = pa.get("day_position"), pa.get("vs_vwap"), pa.get("volume_ratio")
    from nseql.screening.chart_analysis import entry_labels
    ch = entry_labels(pa.get("chart") or {})
    return {**ch, "pa_pattern": pa.get("pattern") or "unknown", "pa_structure": pa.get("structure") or "unknown",
            "pa_location": "unknown" if dp is None else "near the day's low (bottom 25%)" if dp <= 0.25 else
                           "near the day's high (top 25%)" if dp >= 0.75 else "middle of the day's range",
            "pa_vwap": "unknown" if vw is None else "above VWAP" if vw > 0 else "below VWAP",
            "pa_volume": "unknown" if vr is None else "heavy volume (>= 1.5x)" if vr >= 1.5 else "light volume (< 0.7x)" if vr < 0.7 else "normal volume"}


def bars(db: sqlite3.Connection, symbol: str, day: str, tf: int = 1) -> list[dict]:
    """A day's candles for the API / the assistant (1-minute bars, or completed tf-minute candles)."""
    m1 = pd.read_sql_query("SELECT ts, open, high, low, close, volume FROM events WHERE symbol=? AND late=0 AND ts >= ? AND ts < ? ORDER BY ts",
                           db, params=(symbol, day, (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")))
    if tf > 1:
        c = candles(m1, tf)
        return [{"end": str(r.end)[11:16], "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume,
                 "pattern_so_far": pattern(c.iloc[:k + 1])} for k, r in enumerate(c.itertuples())]
    return [{"t": str(r.ts)[11:16], "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume} for r in m1.itertuples()]


assert np is not None

"""Chart Analyst: reads a stock's chart the way a trader does, point in time (operator, 2026-11-25: "functional and technical
on charts").

From the account's stored 1-minute bars strictly BEFORE `as_of` it builds completed 5- and 15-minute candles (and daily
candles from earlier sessions) and reports:

  trend        per timeframe: up (close > EMA20 > EMA50), down (close < EMA20 < EMA50) or mixed; daily needs 20 sessions
  levels       support / resistance: previous day's high, low and close; today's opening range (first 15 minutes); VWAP;
               5-minute swing highs and lows (a high above two candles each side); round numbers. The nearest level above
               and below the price with the distance, i.e. how much room a buy has before resistance
  indicators   RSI(14), MACD(12,26,9) cross, Bollinger(20,2) %B and squeeze, volume versus average, on the 5-minute chart
  patterns     range breakout / breakdown (close beyond the last 12 candles), double bottom / double top (two swing lows /
               highs within 0.3%, confirmed by a close beyond the peak / trough between them), bull flag (an impulse of
               >= 1% then a shallow pull-back on lighter volume), Bollinger squeeze (band width at a 30-candle low)
  read         plain sentences a trader would say about the chart

Textbook definitions with fixed parameters; nothing is fitted. Whether any of it predicts this account's results is
measured by the daily review (winners vs losers by chart reading) before it can become an entry rule.
"""
from __future__ import annotations

import math
import sqlite3

import numpy as np
import pandas as pd

from nseql.screening.price_action import candles

SWING = 2


def ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) == 0:
        return out
    a = 2 / (n + 1)
    v = x[0]
    for i, c in enumerate(x):
        v = c if i == 0 else a * c + (1 - a) * v
        out[i] = v if i >= n - 1 else np.nan
    return out


def rsi(x: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) <= n:
        return out
    d = np.diff(x)
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:n].mean(), dn[:n].mean()
    for i in range(n, len(x)):
        if i > n:
            au = (au * (n - 1) + up[i - 1]) / n
            ad = (ad * (n - 1) + dn[i - 1]) / n
        out[i] = 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)
    return out


def trend(close: np.ndarray) -> str:
    if len(close) < 50:
        return "unknown (needs 50 candles)"
    e20, e50 = ema(close, 20)[-1], ema(close, 50)[-1]
    c = close[-1]
    return "up" if c > e20 > e50 else "down" if c < e20 < e50 else "mixed"


def swings(c: pd.DataFrame) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    h, lo = c.high.to_numpy(), c.low.to_numpy()
    highs, lows = [], []
    for i in range(SWING, len(c) - SWING):
        if h[i] == max(h[i - SWING:i + SWING + 1]) and not (highs and i - highs[-1][0] <= SWING):   # equal neighbours: one swing
            highs.append((i, float(h[i])))
        if lo[i] == min(lo[i - SWING:i + SWING + 1]) and not (lows and i - lows[-1][0] <= SWING):
            lows.append((i, float(lo[i])))
    return highs, lows


def _load(db: sqlite3.Connection, symbol: str, as_of: pd.Timestamp, days: int = 40) -> pd.DataFrame:
    """Completed bars before `as_of`; on a live feed only bars that had ARRIVED by then (market_structure.observable)."""
    from nseql.screening.market_structure import arrival_mode
    q = "SELECT ts, open, high, low, close, volume FROM events WHERE symbol=? AND late=0 AND ts >= ? AND ts < ?"
    params = [symbol, (as_of.normalize() - pd.Timedelta(days=days)).strftime("%Y-%m-%d"), as_of.strftime("%Y-%m-%dT%H:%M:%S")]
    if arrival_mode(db):
        q += " AND (received_at IS NULL OR received_at <= ?)"
        params.append(as_of.tz_localize("Asia/Kolkata").timestamp())
    return pd.read_sql_query(q + " ORDER BY ts", db, params=params)


def analyse(db: sqlite3.Connection, symbol: str, as_of, series: bool = False) -> dict:
    as_of = pd.Timestamp(as_of)
    m1 = _load(db, symbol, as_of)
    out: dict = {"symbol": symbol, "as_of": as_of.isoformat(timespec="minutes"), "trend": {}, "levels": [], "indicators": {}, "patterns": [], "read": []}
    if m1.empty:
        out["read"] = ["No candles stored for this stock yet."]
        return out
    m1["ts"] = pd.to_datetime(m1.ts)
    day = as_of.normalize()
    today = m1[m1.ts >= day]
    c5_all, c15_all = candles(m1, 5), candles(m1, 15)
    c5 = c5_all.tail(120).reset_index(drop=True)
    price = float(m1.close.iloc[-1])
    out["price"] = price
    # daily candles from earlier sessions
    dly = m1[m1.ts < day].groupby(m1.ts.dt.normalize()).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                                            close=("close", "last"), volume=("volume", "sum"))
    out["trend"] = {"5m": trend(c5_all.close.to_numpy()), "15m": trend(c15_all.close.to_numpy()),
                    "daily": trend(dly.close.to_numpy()) if len(dly) >= 50 else
                    ("up" if len(dly) >= 20 and dly.close.iloc[-1] > ema(dly.close.to_numpy(), 20)[-1] else
                     "down" if len(dly) >= 20 else f"unknown ({len(dly)} of 20 sessions stored)")}
    # levels
    lv = []
    if len(dly):
        p = dly.iloc[-1]
        lv += [("previous day high", float(p.high)), ("previous day low", float(p.low)), ("previous day close", float(p.close))]
    if len(today):
        orng = today[today.ts <= day + pd.Timedelta(hours=9, minutes=30)]
        if len(orng):
            lv += [("opening range high (09:15-09:30)", float(orng.high.max())), ("opening range low (09:15-09:30)", float(orng.low.min()))]
        tp = (today.high + today.low + today.close) / 3
        if today.volume.sum() > 0:
            lv.append(("VWAP", float((tp * today.volume).sum() / today.volume.sum())))
        lv += [("day high", float(today.high.max())), ("day low", float(today.low.min()))]
    sh, sl = swings(c5)
    lv += [("5-minute swing high", v) for _, v in sh[-3:]] + [("5-minute swing low", v) for _, v in sl[-3:]]
    step = 100 if price >= 5000 else 50 if price >= 1000 else 10 if price >= 200 else 5
    lv += [("round number", math.floor(price / step) * step), ("round number", math.ceil(price / step) * step)]
    lv = sorted({(round(v, 2), k) for k, v in lv if v and v > 0}, key=lambda x: x[0])
    out["levels"] = [{"price": v, "label": k, "distance": round(v / price - 1, 5)} for v, k in lv]
    above = [x for x in out["levels"] if x["price"] > price * 1.0002]
    below = [x for x in out["levels"] if x["price"] < price * 0.9998]
    out["resistance"] = min(above, key=lambda x: x["price"]) if above else None
    out["support"] = max(below, key=lambda x: x["price"]) if below else None
    # indicators (5-minute)
    cl = c5.close.to_numpy()
    if len(cl) >= 15:
        r = rsi(cl)
        out["indicators"]["rsi14"] = None if np.isnan(r[-1]) else round(float(r[-1]), 1)
    if len(cl) >= 35:
        macd = ema(cl, 12) - ema(cl, 26)
        sig = ema(np.nan_to_num(macd, nan=0.0), 9)
        cross = "bullish cross" if macd[-1] > sig[-1] and macd[-2] <= sig[-2] else "bearish cross" if macd[-1] < sig[-1] and macd[-2] >= sig[-2] else \
            ("above signal" if macd[-1] > sig[-1] else "below signal")
        out["indicators"]["macd"] = cross
    if len(cl) >= 20:
        s = pd.Series(cl)
        mid, sd = s.rolling(20).mean(), s.rolling(20).std()
        up, dn = mid + 2 * sd, mid - 2 * sd
        out["indicators"]["bollinger_pctb"] = round(float((cl[-1] - dn.iloc[-1]) / (up.iloc[-1] - dn.iloc[-1])), 2) if up.iloc[-1] > dn.iloc[-1] else None
        width = ((up - dn) / mid).dropna()
        squeeze = len(width) >= 30 and width.iloc[-1] <= width.iloc[-30:].min() * 1.0001
    else:
        squeeze = False
    if len(c5) >= 2:
        avg = float(c5.volume.iloc[:-1].tail(20).mean())
        out["indicators"]["volume_ratio"] = round(float(c5.volume.iloc[-1]) / avg, 2) if avg > 0 else None
    # patterns (5-minute)
    if len(c5) >= 13:
        prev_hi, prev_lo = float(c5.high.iloc[-13:-1].max()), float(c5.low.iloc[-13:-1].min())
        if cl[-1] > prev_hi:
            out["patterns"].append(f"range breakout: closed above the last hour's high {prev_hi:,.2f}")
        elif cl[-1] < prev_lo:
            out["patterns"].append(f"range breakdown: closed below the last hour's low {prev_lo:,.2f}")
    if len(sl) >= 2:
        (i1, a), (i2, b) = sl[-2], sl[-1]
        between = c5.high.iloc[i1:i2 + 1].max()
        if i2 - i1 >= 4 and abs(a / b - 1) <= 0.003 and cl[-1] > between:
            out["patterns"].append(f"double bottom confirmed (two lows near {min(a, b):,.2f}, closed above {between:,.2f})")
    if len(sh) >= 2:
        (i1, a), (i2, b) = sh[-2], sh[-1]
        between = c5.low.iloc[i1:i2 + 1].min()
        if i2 - i1 >= 4 and abs(a / b - 1) <= 0.003 and cl[-1] < between:
            out["patterns"].append(f"double top confirmed (two highs near {max(a, b):,.2f}, closed below {between:,.2f})")
    if len(c5) >= 9:
        imp, pb = c5.iloc[-9:-4], c5.iloc[-4:]
        rise = imp.close.iloc[-1] / imp.open.iloc[0] - 1
        if rise >= 0.01 and (imp.close > imp.open).sum() >= 3:
            retr = (imp.close.iloc[-1] - pb.low.min()) / (imp.close.iloc[-1] - imp.open.iloc[0])
            if 0 < retr <= 0.5 and pb.volume.mean() < imp.volume.mean():
                out["patterns"].append(f"bull flag: +{rise:.1%} impulse, then a {retr:.0%} pull-back on lighter volume")
    if squeeze:                                           # after the price patterns: a squeeze is a setup, not a signal
        out["patterns"].append("Bollinger squeeze (bands at their narrowest in 30 candles: a big move often follows)")
    # the read
    t = out["trend"]
    rd = [f"Trend: 5-minute {t['5m']}, 15-minute {t['15m']}, daily {t['daily']}."]
    ind = out["indicators"]
    if ind.get("rsi14") is not None:
        r = ind["rsi14"]
        rd.append(f"RSI {r:.0f}" + (" (overbought: late to buy)" if r >= 70 else " (oversold)" if r <= 30 else "") + ".")
    if out["resistance"]:
        rd.append(f"Next resistance: {out['resistance']['label']} at {out['resistance']['price']:,.2f} ({out['resistance']['distance']:+.2%}).")
    if out["support"]:
        rd.append(f"Next support: {out['support']['label']} at {out['support']['price']:,.2f} ({out['support']['distance']:+.2%}).")
    if ind.get("volume_ratio"):
        rd.append(f"Last 5-minute volume {ind['volume_ratio']:.1f}x average.")
    rd += [p[0].upper() + p[1:] + "." for p in out["patterns"]]
    out["read"] = rd
    if series:
        e20, e50 = ema(cl, 20), ema(cl, 50)
        tv = today.copy()
        vw = ((tv.high + tv.low + tv.close) / 3 * tv.volume).cumsum() / tv.volume.cumsum().replace(0, np.nan)
        vmap = dict(zip(tv.ts, vw))
        out["series"] = {"t": [str(e)[:16] for e in c5.end], "open": c5.open.round(2).tolist(), "high": c5.high.round(2).tolist(),
                         "low": c5.low.round(2).tolist(), "close": c5.close.round(2).tolist(), "volume": c5.volume.tolist(),
                         "ema20": [None if np.isnan(v) else round(float(v), 2) for v in e20],
                         "ema50": [None if np.isnan(v) else round(float(v), 2) for v in e50],
                         "vwap": [None if pd.isna(vmap.get(e)) else round(float(vmap[e]), 2) for e in c5.end],
                         "rsi14": [None if np.isnan(v) else round(float(v), 1) for v in rsi(cl)]}
    return out


def entry_labels(a: dict) -> dict:
    """Buckets for the daily review: is the trend aligned, how much room to resistance, RSI zone."""
    t = a.get("trend", {})
    aligned = "5m and 15m both up" if t.get("5m") == "up" and t.get("15m") == "up" else \
        "5m and 15m both down" if t.get("5m") == "down" and t.get("15m") == "down" else "trends disagree or unknown"
    res = a.get("resistance")
    room = "unknown" if not res else "resistance within 0.5% (little room)" if res["distance"] <= 0.005 else "room to resistance > 0.5%"
    r = (a.get("indicators") or {}).get("rsi14")
    zone = "unknown" if r is None else "RSI >= 70 (overbought)" if r >= 70 else "RSI <= 30 (oversold)" if r <= 30 else "RSI 30-70"
    return {"ch_trend": aligned, "ch_room": room, "ch_rsi": zone,
            "ch_pattern": (a.get("patterns") or ["no chart pattern"])[0].split(":")[0].split(" (")[0]}

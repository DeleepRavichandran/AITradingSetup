"""Heatmap intelligence (docs/HEATMAPS.md): sixteen heatmaps computed from data this lab actually has, each with its source,
timestamp, freshness and limitations, and the quantitative features behind them exposed to the brain.

Read-only views; no trading module imports this (test-enforced). Features influence nothing until the feature study
(feature_study.py) shows incremental predictive value on data they were not chosen on, and the operator approves.

Availability is stated, never faked:
  available    computed from stored ledger data (1-minute candles, decisions, fills, journal, research store)
  limited      computed, with a named limitation (e.g. tile size is traded value because market capitalisation is not stored)
  unavailable  the broker feed has no such data: order-book depth and its history, options open interest, tick-level
               order flow (volume delta). These are listed with what would be needed, and nothing is drawn.
Displayed resting orders, if a depth feed is ever added, will be labelled as not guaranteed executable liquidity.

Playback: every map built from candles accepts `as_of` and uses only rows at or before it.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

VERSION = "hm-1.0"
# NSE industry classification for the default watchlist (static map; verify against NSE before relying on it).
SECTORS = {"RELIANCE": "Oil & Gas", "HDFCBANK": "Banks", "ICICIBANK": "Banks", "SBIN": "Banks", "KOTAKBANK": "Banks", "AXISBANK": "Banks",
           "BAJFINANCE": "Financial services", "INFY": "IT", "TCS": "IT", "BHARTIARTL": "Telecom", "ITC": "FMCG", "HINDUNILVR": "FMCG",
           "LT": "Construction", "MARUTI": "Automobile", "TMPV": "Automobile", "SUNPHARMA": "Healthcare", "TITAN": "Consumer durables",
           "ASIANPAINT": "Consumer durables", "ULTRACEMCO": "Cement", "NTPC": "Power"}
INDEX = {"NIFTY50", "NIFTYBANK", "INDIAVIX"}
TIMEFRAMES = {"15m": 15, "60m": 60, "1d": "1d", "5d": 5, "20d": 20}
CATALOG = [
    ("market_cap", "Market map", "market", "limited"), ("sector_rotation", "Sector rotation", "market", "available"),
    ("relative_strength", "Relative strength", "market", "available"), ("orderbook_liquidity", "Order-book liquidity (history)", "liquidity", "unavailable"),
    ("inferred_liquidity", "ICT/SMC inferred liquidity zones", "liquidity", "limited"), ("volume", "Relative volume by time of day", "liquidity", "available"),
    ("order_flow", "Order flow (volume delta)", "liquidity", "unavailable"), ("volatility", "Volatility", "risk", "available"),
    ("options_oi", "Options open interest", "derivatives", "unavailable"), ("correlation", "Correlation", "risk", "available"),
    ("opportunity", "Opportunity quality", "decisions", "available"), ("portfolio_risk", "Portfolio risk", "risk", "available"),
    ("news", "News and sentiment", "context", "limited"), ("regime", "Market regime", "context", "available"),
    ("performance", "Trading performance", "evidence", "available"), ("learning", "Methodology effectiveness", "evidence", "available"),
    ("data_quality", "Market-data quality", "evidence", "available"),
]
UNAVAILABLE_WHY = {
    "orderbook_liquidity": "The Angel One feed used here delivers 1-minute candles, not market depth, and no depth snapshots are stored. A "
                           "historical liquidity heatmap needs recorded depth (top 5 or 20 levels) or order-book events. Resting orders would "
                           "also not be guaranteed executable liquidity.",
    "order_flow": "Volume delta and footprint need tick-by-tick trades with aggressor side. Candles carry total volume only; "
                  "splitting it into buy and sell volume would be invented.",
    "options_oi": "No options chain or open-interest data is connected (needs a licensed F&O data source).",
}


def sector(symbol: str) -> str:
    return SECTORS.get(symbol, "Unclassified")


def _ro(account: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(account) / 'screening.sqlite'}?mode=ro", uri=True)


def _hm(id_: str, rows, cols, values, unit: str, scale: str, source: str, as_of, availability="available", limitations=None, **extra) -> dict:
    meta = next(c for c in CATALOG if c[0] == id_)
    clean = [[None if v is None or (isinstance(v, float) and not np.isfinite(v)) else round(float(v), 6) for v in r] for r in values]
    return {"id": id_, "title": meta[1], "group": meta[2], "availability": availability, "rows": list(rows), "cols": list(cols),
            "values": clean, "unit": unit, "scale": scale, "source": source, "as_of": None if as_of is None else str(as_of),
            "limitations": limitations or [], "version": VERSION, **extra}


def unavailable(id_: str) -> dict:
    meta = next(c for c in CATALOG if c[0] == id_)
    return {"id": id_, "title": meta[1], "group": meta[2], "availability": "unavailable", "rows": [], "cols": [], "values": [],
            "why": UNAVAILABLE_WHY[id_], "limitations": [UNAVAILABLE_WHY[id_]], "version": VERSION}


# ================================================================================================ data
def minute_closes(account: Path, as_of=None, days: int = 30) -> pd.DataFrame:
    """1-minute bars observable at `as_of`: closed by then and, on a live feed, arrived by then (market_structure.observable)."""
    from nseql.screening.market_structure import observable
    con = _ro(account)
    try:
        hi = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp(con.execute("SELECT MAX(ts) FROM events WHERE late=0").fetchone()[0] or "2000-01-01")
        lo = (hi.normalize() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
        cond, params = observable(con, hi)
        b = pd.read_sql_query(f"SELECT symbol, ts, open, high, low, close, volume FROM events WHERE ts >= ? AND {cond}", con, params=(lo, *params))
    finally:
        con.close()
    b["ts"] = pd.to_datetime(b.ts)
    return b


def _ret(close: pd.DataFrame, tf) -> pd.Series:
    """Return per symbol over `tf` ending at the last row (same session for intraday windows)."""
    last = close.ffill().iloc[-1]
    t = close.index[-1]
    if tf == "1d":
        prev = close[close.index < t.normalize()]
        return (last / prev.ffill().iloc[-1] - 1) if len(prev) else pd.Series(np.nan, index=close.columns)
    if isinstance(tf, int) and tf in (5, 20):
        d = close.groupby(close.index.normalize()).last()
        return (last / d.iloc[-1 - tf] - 1) if len(d) > tf else pd.Series(np.nan, index=close.columns)
    start = t - pd.Timedelta(minutes=tf)
    base = close[(close.index <= start) & (close.index >= t.normalize())]
    return (last / base.ffill().iloc[-1] - 1) if len(base) else pd.Series(np.nan, index=close.columns)


def _pivot(b: pd.DataFrame, col: str) -> pd.DataFrame:
    return b.pivot_table(index="ts", columns="symbol", values=col).sort_index()


# ================================================================================================ heatmaps
def market_map(b: pd.DataFrame, tf="1d", sectors: list[str] | None = None) -> dict:
    cl = _pivot(b, "close")
    stocks = [s for s in cl.columns if s not in INDEX and (not sectors or sector(s) in sectors)]
    if not stocks:
        return _hm("market_cap", [], [], [], "%", "diverging", "ledger 1-minute candles", None)
    r = _ret(cl[stocks], TIMEFRAMES.get(tf, tf))
    t = cl.index[-1]
    today = b[(b.ts >= t.normalize()) & b.symbol.isin(stocks)]
    tv = (today.close * today.volume).groupby(today.symbol).sum()
    tiles = [{"symbol": s, "sector": sector(s), "size": float(tv.get(s, 0.0)), "value": None if pd.isna(r.get(s)) else float(r[s]),
              "price": float(cl[s].ffill().iloc[-1])} for s in stocks]
    tiles.sort(key=lambda x: (x["sector"], -x["size"]))
    return _hm("market_cap", [x["symbol"] for x in tiles], [tf], [[x["value"]] for x in tiles], "% change", "diverging",
               "ledger 1-minute candles", t, "limited",
               ["Tile size is today's traded value (price x volume) because market capitalisation is not stored.",
                "Sector labels come from a static NSE classification map for the default watchlist."], tiles=tiles)


def sector_rotation(b: pd.DataFrame, sectors: list[str] | None = None) -> dict:
    cl = _pivot(b, "close")
    stocks = [s for s in cl.columns if s not in INDEX]
    rets = {tf: _ret(cl[stocks], TIMEFRAMES[tf]) for tf in TIMEFRAMES}
    secs = sorted({sector(s) for s in stocks if not sectors or sector(s) in sectors})
    avg = {tf: float(rets[tf].mean()) for tf in TIMEFRAMES}
    vals = [[float(rets[tf][[s for s in stocks if sector(s) == sec]].mean()) - avg[tf] for tf in TIMEFRAMES] for sec in secs]
    return _hm("sector_rotation", secs, list(TIMEFRAMES), vals, "% vs watchlist average", "diverging", "ledger 1-minute candles (equal weight)",
               cl.index[-1] if len(cl) else None, limitations=["Equal-weighted sectors of the watchlist, not NSE sector indices.",
                                                               "Sectors with one stock are that stock."],
               members={sec: [s for s in stocks if sector(s) == sec] for sec in secs})


def relative_strength(b: pd.DataFrame, sectors: list[str] | None = None) -> dict:
    cl = _pivot(b, "close")
    bench = "NIFTY50" if "NIFTY50" in cl.columns and cl["NIFTY50"].notna().sum() > 10 else None
    stocks = [s for s in cl.columns if s not in INDEX and (not sectors or sector(s) in sectors)]
    vals = []
    for tf in TIMEFRAMES:
        r = _ret(cl[[*stocks, *([bench] if bench else [])]], TIMEFRAMES[tf])
        ref = r[bench] if bench else r[stocks].mean()
        vals.append(r[stocks] - ref)
    M = pd.concat(vals, axis=1)
    order = M.iloc[:, 2].sort_values(ascending=False).index if len(M) else []
    return _hm("relative_strength", list(order), list(TIMEFRAMES), M.loc[order].to_numpy().tolist() if len(M) else [], "% vs " + ("NIFTY 50" if bench else "watchlist average"),
               "diverging", "ledger 1-minute candles", cl.index[-1] if len(cl) else None,
               limitations=[] if bench else ["NIFTY 50 bars are not stored with the live candles here: relative to the watchlist average."])


def inferred_liquidity(account: Path, as_of=None, symbols: list[str] | None = None) -> dict:
    """Chart-inferred potential liquidity (market_structure levels): count of active levels per distance band from price."""
    from nseql.screening import market_structure as M
    con = _ro(account)
    try:
        syms = symbols or [r[0] for r in con.execute("SELECT DISTINCT symbol FROM events WHERE late=0") if r[0] not in INDEX]
        t = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp(con.execute("SELECT MAX(ts) FROM events WHERE late=0").fetchone()[0] or "2000-01-01")
        bands = [-0.03, -0.02, -0.01, -0.005, 0, 0.005, 0.01, 0.02, 0.03]
        cols = ["-3..-2%", "-2..-1%", "-1..-0.5%", "-0.5..0%", "0..+0.5%", "+0.5..1%", "+1..2%", "+2..3%"]
        rows, vals, detail = [], [], {}
        for s in sorted(syms):
            a = M.annotate(con, s, t)
            lv = [x for x in a["levels"] if x["active"]]
            m1 = M.load_m1(con, s, t, days=1)
            if m1.empty or not lv:
                continue
            px = float(m1.close.iloc[-1])
            d = np.array([x["price"] / px - 1 for x in lv])
            vals.append(np.histogram(d, bins=bands)[0].astype(float).tolist())
            rows.append(s)
            detail[s] = [{"kind": x["kind"], "price": x["price"], "distance": round(x["price"] / px - 1, 4), "side": x["side"]} for x in lv]
    finally:
        con.close()
    return _hm("inferred_liquidity", rows, cols, vals, "inferred levels", "sequential", "market_structure.py levels from candles", t, "limited",
               ["INFERRED from candle geometry (swings, equal highs/lows, previous-day/week, opening range, value area).",
                "Potential liquidity, not observed resting orders; nothing here is evidence of stop clusters or institutional orders."],
               levels=detail)


def volume(b: pd.DataFrame) -> dict:
    """Relative volume: each 30-minute bucket today versus the same bucket's average over earlier sessions."""
    if b.empty:
        return _hm("volume", [], [], [], "x", "sequential", "ledger", None)
    t = b.ts.max()
    d = b.assign(day=b.ts.dt.normalize(), bucket=((b.ts.dt.hour * 60 + b.ts.dt.minute - 556) // 30).clip(0, 12))
    g = d.groupby(["symbol", "day", "bucket"]).volume.sum().reset_index()
    today = g[g.day == t.normalize()]
    past = g[g.day < t.normalize()].groupby(["symbol", "bucket"]).volume.mean()
    labels = [f"{(555 + 30 * k) // 60:02d}:{(555 + 30 * k) % 60:02d}" for k in range(13)]
    syms = sorted(s for s in today.symbol.unique() if s not in INDEX)
    vals = [[(float(today[(today.symbol == s) & (today.bucket == k)].volume.sum()) / past.get((s, k), np.nan)) if (s, k) in past.index else None
             for k in range(13)] for s in syms]
    return _hm("volume", syms, labels, vals, "x average for the time of day", "sequential", "ledger 1-minute volume", t,
               limitations=["Total traded volume per candle; buy/sell split (order flow) is not available."])


def volatility(b: pd.DataFrame) -> dict:
    """Daily range (high-low over close) per stock per session, last 10 sessions."""
    d = b.assign(day=b.ts.dt.normalize()).groupby(["symbol", "day"]).agg(h=("high", "max"), l=("low", "min"), c=("close", "last")).reset_index()
    d["range"] = (d.h - d.l) / d.c
    days = sorted(d.day.unique())[-10:]
    syms = sorted(s for s in d.symbol.unique() if s not in INDEX)
    vals = [[float(d[(d.symbol == s) & (d.day == x)].range.iloc[0]) if len(d[(d.symbol == s) & (d.day == x)]) else None for x in days] for s in syms]
    return _hm("volatility", syms, [str(x)[:10] for x in days], vals, "daily range %", "sequential", "ledger 1-minute candles",
               b.ts.max() if len(b) else None)


def correlation(b: pd.DataFrame, holdings: list[str] | None = None) -> dict:
    cl = _pivot(b, "close")
    cl = cl[[s for s in cl.columns if s not in INDEX]]
    r = cl.resample("5min").last().pct_change(fill_method=None).dropna(how="all")
    r = r[r.index >= r.index.max().normalize() - pd.Timedelta(days=7)] if len(r) else r
    c = r.corr(min_periods=30)
    syms = list(c.columns)
    return _hm("correlation", syms, syms, c.to_numpy().tolist(), "correlation of 5-minute returns (last 5 sessions)", "diverging",
               "ledger 1-minute candles", cl.index.max() if len(cl) else None, holdings=holdings or [],
               limitations=["Correlation needs at least 30 overlapping 5-minute returns; blank cells have fewer.",
                            "Only the watchlist is stored: cross-asset (USD/INR, crude, gold) needs the market-context feed on the laptop."])


def opportunity(account: Path, as_of=None, live_from: str | None = None) -> dict:
    """Best expected edge after costs recorded by the allocator, per stock per 30-minute bucket. In playback only decisions
    already MADE by `as_of` (the minute's recorded decision time), not every signal of that date."""
    con = _ro(account)
    try:
        T = pd.Timestamp(as_of) if as_of is not None else None
        day = T.strftime("%Y-%m-%d") if T is not None else con.execute("SELECT substr(MAX(minute),1,10) FROM cycles WHERE status='processed'").fetchone()[0]
        if not day:
            return _hm("opportunity", [], [], [], "bps", "diverging", "allocator scores", None)
        q = ("SELECT s.symbol, s.bar_ts, s.alloc_score FROM signals s JOIN cycles c ON c.minute = s.bar_ts WHERE s.decision='BUY' "
             "AND s.bar_ts >= ? AND s.bar_ts < ? AND s.alloc_score IS NOT NULL")
        args = [max(day, live_from or ""), (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")]
        if T is not None:
            q += " AND s.bar_ts <= ? AND (c.decided_at IS NULL OR c.decided_at <= ?)"
            args += [T.strftime("%Y-%m-%dT%H:%M:%S"), T.isoformat()]
        s = pd.read_sql_query(q, con, params=args)
    finally:
        con.close()
    if s.empty:
        return _hm("opportunity", [], [], [], "bps", "diverging", "allocator scores", as_of or day)
    t = pd.to_datetime(s.bar_ts)
    s["bucket"] = ((t.dt.hour * 60 + t.dt.minute - 556) // 30).clip(0, 12)
    g = s.groupby(["symbol", "bucket"]).alloc_score.max()
    labels = [f"{(555 + 30 * k) // 60:02d}:{(555 + 30 * k) % 60:02d}" for k in range(13)]
    syms = sorted(s.symbol.unique())
    return _hm("opportunity", syms, labels, [[g.get((x, k)) for k in range(13)] for x in syms], "best expected edge (bps, after costs)",
               "diverging", "allocator score recorded at decision time", as_of or day,
               limitations=["Expected edge is the allocator's shrunk estimate from forward evidence, not a probability."])


def positions_at(account: Path, as_of) -> tuple[list[dict], float | None]:
    """Paper positions as they stood at `as_of`, rebuilt from fills up to then and marked at the last observable close."""
    from nseql.screening.market_structure import observable
    T = pd.Timestamp(as_of)
    con = _ro(account)
    try:
        f = pd.read_sql_query("SELECT config_id, symbol, side, qty, price FROM paper_fills WHERE ts <= ? ORDER BY ts", con,
                              params=(T.strftime("%Y-%m-%dT%H:%M:%S"),))
        cond, params = observable(con, T)
        ev = pd.read_sql_query(f"SELECT symbol, ts, close FROM events WHERE ts >= ? AND {cond}", con,
                               params=((T.normalize() - pd.Timedelta(days=10)).strftime("%Y-%m-%d"), *params))
        last = ev.sort_values("ts").groupby("symbol").close.last().to_dict() if len(ev) else {}
        eq = con.execute("SELECT equity_inr FROM cycles WHERE minute <= ? AND equity_inr IS NOT NULL ORDER BY minute DESC LIMIT 1",
                         (T.strftime("%Y-%m-%dT%H:%M:%S"),)).fetchone()
    finally:
        con.close()
    book: dict = {}
    for r in f.itertuples():
        b = book.setdefault(r.config_id, {"symbol": r.symbol, "qty": 0, "cost": 0.0})
        if r.side == "BUY":
            b["qty"] += r.qty
            b["cost"] += r.qty * r.price
        else:
            b["cost"] -= (b["cost"] / b["qty"]) * r.qty if b["qty"] else 0.0
            b["qty"] -= r.qty
    out = []
    for cid, b in book.items():
        if b["qty"] > 0:
            px = float(last.get(b["symbol"], b["cost"] / b["qty"]))
            out.append({"symbol": b["symbol"], "value_inr": b["qty"] * px, "unrealised_inr": b["qty"] * px - b["cost"], "last_price": px,
                        "stop_price": None})
    return out, (float(eq[0]) if eq else None)


def portfolio_risk(positions: list[dict], equity: float | None) -> dict:
    cols = ["weight", "stop distance", "unrealised", "same sector weight"]
    if not positions or not equity:
        return _hm("portfolio_risk", [], cols, [], "%", "sequential", "paper positions", None)
    agg: dict[str, dict] = {}
    for p in positions:
        a = agg.setdefault(p["symbol"], {"value": 0.0, "unreal": 0.0, "last": p["last_price"], "stop": p["stop_price"]})
        a["value"] += p["value_inr"]
        a["unreal"] += p["unrealised_inr"]
    secw = {}
    for s, a in agg.items():
        secw[sector(s)] = secw.get(sector(s), 0.0) + a["value"] / equity
    rows = sorted(agg, key=lambda s: -agg[s]["value"])
    vals = [[agg[s]["value"] / equity, (agg[s]["last"] / agg[s]["stop"] - 1) if agg[s]["stop"] else None, agg[s]["unreal"] / max(agg[s]["value"], 1e-9),
             secw[sector(s)]] for s in rows]
    return _hm("portfolio_risk", rows, cols, vals, "share", "sequential", "paper positions marked at the last close", None,
               limitations=[] if all(p.get("stop_price") for p in positions) else
               ["Playback: positions rebuilt from fills up to the chosen time; stop levels are not stored per minute, so the stop distance is blank."])


def news(items: list[dict], symbols: list[str], fa: dict | None = None, as_of=None) -> dict:
    """Headline counts per stock per day from the news watcher (by the time this lab first saw each headline); tone per stock
    from the Fundamental Agent when available. In playback only headlines first seen by `as_of`; tone is not shown (the
    agent's live dossier is current, not as of then)."""
    if as_of is not None:
        T = pd.Timestamp(as_of).isoformat()
        items = [i for i in items if str(i.get("first_seen") or "") and str(i.get("first_seen")) <= T]
        fa = None
    if not items:
        return _hm("news", [], [], [], "headlines", "sequential", "news watcher", None, "limited", ["No headlines recorded yet."])
    df = pd.DataFrame([{"symbol": s, "day": str(i.get("first_seen") or i.get("published") or "")[:10]} for i in items
                       for s in (i.get("symbols") or ([i["symbol"]] if i.get("symbol") else []))])
    days = sorted(df.day.unique())[-7:] if len(df) else []
    rows = [s for s in symbols if s not in INDEX]
    vals = [[float(((df.symbol == s) & (df.day == d)).sum()) for d in days] for s in rows] if len(df) else []
    tone = {s: (fa or {}).get("stocks", {}).get(s, {}).get("news", {}).get("tone") for s in rows}
    return _hm("news", rows, days, vals, "headlines", "sequential", "news watcher (public RSS) and Fundamental Agent tone", days[-1] if days else None,
               "limited", ["Headline counts by first-seen day; matching uses company-name aliases and can miss or misattribute.",
                           "Tone is a lexicon score (observation only); it has not shown predictive value."], tone=tone)


def regime(b: pd.DataFrame) -> dict:
    """Per session: breadth (share of stocks up), median absolute 1-minute return, dispersion, trend-day share."""
    d = b[~b.symbol.isin(INDEX)].assign(day=b.ts.dt.normalize())
    if d.empty:
        return _hm("regime", [], [], [], "", "sequential", "ledger", None)
    day = d.groupby(["symbol", "day"]).agg(o=("open", "first"), c=("close", "last"), h=("high", "max"), l=("low", "min")).reset_index()
    day["ret"] = day.c / day.o - 1
    day["trendiness"] = (day.c - day.o).abs() / (day.h - day.l).replace(0, np.nan)
    cl = _pivot(d, "close")
    vol = cl.pct_change(fill_method=None).abs().median(axis=1).groupby(cl.index.normalize()).mean()
    days = sorted(day.day.unique())[-15:]
    rows = [str(x)[:10] for x in days]
    vals = [[float((day[day.day == x].ret > 0).mean()), float(vol.get(x, np.nan)) * 100, float(day[day.day == x].ret.std()),
             float(day[day.day == x].trendiness.mean())] for x in days]
    return _hm("regime", rows, ["breadth (share up)", "median 1-min move %", "dispersion", "trendiness"], vals, "", "sequential",
               "ledger 1-minute candles", b.ts.max(), limitations=["India VIX is not stored with the live bars."])


def performance(account: Path, as_of=None) -> dict:
    from nseql.screening import integrity as I
    rt = I.round_trips(account)
    if as_of is not None and len(rt):
        T = pd.Timestamp(as_of).strftime("%Y-%m-%dT%H:%M:%S")
        rt = rt[rt.exit_ts.astype(str).str.replace(" ", "T") <= T]
    if rt.empty:
        return _hm("performance", [], [], [], "INR", "diverging", "paper round trips", None)
    con = _ro(account)
    try:
        fam = dict(con.execute("SELECT id, family || ' ' || timeframe FROM configs").fetchall())
    finally:
        con.close()
    rt = rt.assign(group=rt.config_id.map(fam).fillna("unknown"), hour=pd.to_datetime(rt.entry_ts).dt.hour)
    g = rt.groupby(["group", "hour"]).net.sum()
    groups = list(rt.groupby("group").net.sum().sort_values().index)
    hours = sorted(rt.hour.unique())
    return _hm("performance", groups, [f"{h:02d}:00" for h in hours], [[g.get((x, h)) for h in hours] for x in groups], "net INR after costs",
               "diverging", "paper round trips (all charges included)", rt.exit_ts.max(),
               counts=[[int(((rt.group == x) & (rt.hour == h)).sum()) for h in hours] for x in groups])


def learning(exp_dir: Path, as_of=None) -> dict:
    from nseql.screening import concept_study as CS
    lat = CS.latest(exp_dir, as_of)
    res = [r for r in lat.get("results", []) if r["horizon"] == CS.HORIZONS[0]]
    if not res:
        return _hm("learning", [], [], [], "", "diverging", "concept study", as_of,
                   limitations=["No concept study had completed by then." if as_of is not None else "No concept study yet (runs with the daily review)."])
    concepts = sorted({r["concept"] for r in res})
    cols = ["mean after costs", "vs simpler rule", "vs random timing", "holdout"]
    by = {r["concept"]: r for r in res}
    vals = [[by[c]["mean_net"], (by[c]["mean_net"] or 0) - (by[c]["baseline_mean"] or 0) if by[c]["baseline_mean"] is not None else None,
             (by[c]["mean_net"] or 0) - (by[c]["placebo_mean"] or 0) if by[c]["placebo_mean"] is not None else None, by[c]["holdout_mean"]] for c in concepts]
    return _hm("learning", concepts, cols, vals, "return per trade (after costs) / difference", "diverging", f"concept study run {lat['run']}",
               lat["run"], verdicts={c: by[c]["verdict"] for c in concepts}, n={c: by[c]["n"] for c in concepts})


def data_quality(account: Path, as_of=None) -> dict:
    from nseql.screening.market_structure import observable
    con = _ro(account)
    try:
        T = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp("2100-01-01")
        cond, params = observable(con, T)
        tq = T.tz_localize("Asia/Kolkata").timestamp()
        cov = pd.read_sql_query(f"SELECT symbol, substr(ts,1,10) AS day, COUNT(*) AS n FROM events WHERE {cond} GROUP BY 1, 2", con, params=params)
        late = dict(((r[0], r[1]), r[2]) for r in con.execute("SELECT symbol, substr(ts,1,10), COUNT(*) FROM events WHERE late=1 AND ts <= ? "
                                                               "AND (received_at IS NULL OR received_at <= ?) GROUP BY 1, 2",
                                                               (T.strftime("%Y-%m-%dT%H:%M:%S"), tq)))
        try:
            quar = dict(((r[0], r[1]), r[2]) for r in con.execute("SELECT symbol, substr(ts,1,10), COUNT(*) FROM event_quarantine WHERE ts <= ? "
                                                                   "AND (received_at IS NULL OR received_at <= ?) GROUP BY 1, 2",
                                                                   (T.strftime("%Y-%m-%dT%H:%M:%S"), tq)))
        except sqlite3.Error:
            quar = {}
    finally:
        con.close()
    if cov.empty:
        return _hm("data_quality", [], [], [], "", "sequential", "ledger", None)
    days = sorted(cov.day.unique())[-10:]
    syms = sorted(cov.symbol.unique())
    c = cov.set_index(["symbol", "day"]).n
    vals = [[float(c.get((s, d), 0)) / 375 for d in days] for s in syms]
    return _hm("data_quality", syms, days, vals, "share of the 375 session minutes with a bar", "sequential", "ledger arrival records", days[-1],
               late={f"{s}|{d}": late.get((s, d), 0) for s in syms for d in days if late.get((s, d))},
               quarantined={f"{s}|{d}": quar.get((s, d), 0) for s in syms for d in days if quar.get((s, d))},
               limitations=["A session has 375 one-minute bars (09:16-15:30); half-days and holidays score lower by design."])


def build(id_: str, account: Path | None, exp_dir: Path, as_of=None, tf: str = "1d", sectors: list[str] | None = None,
          positions: list[dict] | None = None, equity: float | None = None, news_items: list[dict] | None = None, fa: dict | None = None,
          live_from: str | None = None) -> dict:
    if id_ in UNAVAILABLE_WHY:
        return unavailable(id_)
    if id_ == "learning":
        return learning(exp_dir, as_of)
    if account is None:
        return {"id": id_, "availability": "unavailable", "why": "no live paper account yet", "rows": [], "cols": [], "values": []}
    if id_ in ("market_cap", "sector_rotation", "relative_strength", "volume", "volatility", "correlation", "regime"):
        b = minute_closes(account, as_of)
        if b.empty:
            return _hm(id_, [], [], [], "", "sequential", "ledger", as_of)
        held = [p["symbol"] for p in (positions_at(account, as_of)[0] if as_of is not None else positions or [])]
        return {"market_cap": lambda: market_map(b, tf, sectors), "sector_rotation": lambda: sector_rotation(b, sectors),
                "relative_strength": lambda: relative_strength(b, sectors), "volume": lambda: volume(b), "volatility": lambda: volatility(b),
                "correlation": lambda: correlation(b, held), "regime": lambda: regime(b)}[id_]()
    if id_ == "inferred_liquidity":
        syms = [s for s in SECTORS if not sectors or sector(s) in sectors] if sectors else None
        return inferred_liquidity(account, as_of, syms)
    if id_ == "opportunity":
        return opportunity(account, as_of, live_from)
    if id_ == "portfolio_risk":
        if as_of is not None:                                    # playback: never today's positions
            positions, equity = positions_at(account, as_of)
        return portfolio_risk(positions or [], equity)
    if id_ == "news":
        return news(news_items or [], list(SECTORS), fa, as_of)
    if id_ == "performance":
        return performance(account, as_of)
    if id_ == "data_quality":
        return data_quality(account, as_of)
    raise KeyError(id_)


PLAYBACK = {
    "portfolio_risk": "rebuilt from fills up to the chosen time; stop distance unavailable (stops are not stored per minute)",
    "news": "headlines first seen by the chosen time; tone unavailable (the agent's dossier is current)",
    "learning": "only research runs completed by the chosen time",
    "opportunity": "only decisions already made by the chosen minute",
    "orderbook_liquidity": "unavailable", "order_flow": "unavailable", "options_oi": "unavailable",
}


def catalog() -> list[dict]:
    return [{"id": i, "title": t, "group": g, "availability": a, "playback": PLAYBACK.get(i, "bars observable (closed and, on a live feed, arrived) by the chosen time"),
             **({"why": UNAVAILABLE_WHY[i]} if i in UNAVAILABLE_WHY else {})} for i, t, g, a in CATALOG]

"""Fundamental Analysis Agent: what is true about a stock and the market BEYOND the chart, at a point in time.

Five lenses, each a score in [-1, 1] (or a risk level in [0, 1]) with the evidence behind it and the time that evidence
became public. Nothing here reads information published after `as_of`.

  1. Company reports   latest quarterly result vs the same quarter a year earlier: revenue and profit growth, operating
                       margin change, debt to equity (services/data/financials.py; the operator's CSV, filing-timed)
  2. News tone         headlines about the stock first seen by the lab in the last 3 days, scored with a transparent
                       finance word list (positive vs negative words); attention = headlines vs the stock's usual count
  3. Economic events   results due, RBI / Budget / FOMC decisions and F&O expiry in the next sessions (event risk, not
                       direction), from the scheduled-event calendar, used only once the event was public
  4. Crowd emotion     market mood from India VIX (level vs its last year), breadth, panic selling (big fall on heavy
                       volume), euphoric buying, stocks crowding at 52-week highs, herding (stocks moving together)
  5. Behaviour         documented behavioural patterns for the stock: anchoring near a 52-week high or low, an
                       overreaction day, a gap that faded, drift after a results reaction, short-term momentum

"Fear" and "overconfidence" are not observable directly; these are proxies with fixed, documented thresholds. They are
NOT fitted. The agent is OBSERVATION ONLY: it never places, sizes or blocks a trade. Each lens must first show, in the
placebo test (`evaluate`), that it separates winning from losing paper trades better than the same scores shifted in
time; only then can it be proposed as a filter, through the experiment protocol, with the operator's decision.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

AGENT_VERSION = "fa-1.0"
CLOSE_H, CLOSE_M = 15, 30
POS = ("beat", "beats", "surge", "surges", "jump", "jumps", "soar", "record", "upgrade", "upgrades", "outperform", "buy rating",
       "profit rises", "profit up", "growth", "strong", "wins", "bags", "order win", "expands", "raises guidance", "dividend",
       "approval", "rally", "rallies", "gains", "bullish", "beat estimates", "higher", "robust", "boost")
NEG = ("miss", "misses", "plunge", "plunges", "slump", "slumps", "fall", "falls", "drop", "drops", "downgrade", "downgrades",
       "underperform", "sell rating", "loss", "losses", "profit falls", "profit down", "weak", "probe", "raid", "fraud", "penalty",
       "fine", "default", "cut guidance", "lowers guidance", "resigns", "strike", "ban", "bearish", "lower", "decline", "declines",
       "concern", "warning", "crash", "tumble", "tumbles")
THRESH = {"vix_fear_pct": 0.8, "vix_calm_pct": 0.2, "panic_share": 0.2, "euphoria_share": 0.2, "near_high_share": 0.4,
          "weak_breadth": 0.25, "strong_breadth": 0.75, "near_high": 0.02, "overreaction_sigma": 2.5, "heavy_volume": 2.0,
          "gap": 0.01, "drift_sessions": 20, "news_days": 3}


def _clip(x: float) -> float:
    return float(max(-1.0, min(1.0, x)))


def _words(title: str, words) -> int:
    t = f" {title.lower()} "
    return sum(1 for w in words if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", t))


@dataclass
class Sources:
    """Everything the agent may read. Each is optional; a missing source makes its lens 'unavailable', never invented."""
    financials: object | None = None                       # nseql.data.financials.Financials
    headlines: sqlite3.Connection | None = None            # the news watcher's database (headlines table)
    events: object | None = None                           # nseql.data.events.EventStore with scheduled events
    daily: Callable[[str], pd.DataFrame] | None = None     # symbol -> daily bars ts/open/high/low/close/volume
    vix: pd.Series | None = None                           # India VIX daily closes indexed by date (known at that day's close)
    notes: list[str] = field(default_factory=list)


def _complete_daily(df: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Daily bars whose session had closed by `as_of` (a bar for day d is complete at d 15:30)."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    d = df.copy()
    d["ts"] = pd.to_datetime(d.ts)
    closes = d.ts.dt.normalize() + pd.Timedelta(hours=CLOSE_H, minutes=CLOSE_M)
    return d[closes <= as_of].sort_values("ts").reset_index(drop=True)


# ---------------------------------------------------------------------------------------------------- 1. company reports
def lens_reports(fin, symbol: str, as_of: pd.Timestamp) -> dict:
    if fin is None:
        return {"status": "unavailable", "reason": "no financial results imported (see docs/FUNDAMENTALS.md)"}
    q = fin.as_of(symbol, as_of, quarters=8)
    if not q:
        return {"status": "unavailable", "reason": f"no result for {symbol} published before {as_of:%Y-%m-%d %H:%M}"}
    cur = q[0]
    yago = next((r for r in q[1:] if 330 <= (pd.Timestamp(cur["period_end"]) - pd.Timestamp(r["period_end"])).days <= 400), None)
    ev, parts, comp = {"quarter": cur["period_end"], "published_at": cur["published_at"], "source": cur["source"]}, [], []
    margin = cur["operating_profit_cr"] / cur["revenue_cr"] if cur["operating_profit_cr"] is not None and cur["revenue_cr"] else None
    if margin is not None:
        ev["operating_margin"] = round(margin, 4)
    if yago:
        ev["compared_with"] = yago["period_end"]
        if cur["revenue_cr"] and yago["revenue_cr"]:
            g = cur["revenue_cr"] / yago["revenue_cr"] - 1
            ev["revenue_yoy"] = round(g, 4)
            comp.append(_clip(g / 0.20))
            parts.append(f"revenue {g:+.0%} YoY")
        if cur["net_profit_cr"] is not None and yago["net_profit_cr"] is not None:
            if yago["net_profit_cr"] > 0:
                g = cur["net_profit_cr"] / yago["net_profit_cr"] - 1
                ev["profit_yoy"] = round(g, 4)
                comp.append(_clip(g / 0.30))
                parts.append(f"profit {g:+.0%} YoY")
            else:
                turn = cur["net_profit_cr"] > 0
                ev["profit_turnaround"] = turn
                comp.append(1.0 if turn else -0.5)
                parts.append("back to profit" if turn else "still loss-making")
        m0 = yago["operating_profit_cr"] / yago["revenue_cr"] if yago["operating_profit_cr"] is not None and yago["revenue_cr"] else None
        if margin is not None and m0 is not None:
            ev["margin_change_pp"] = round((margin - m0) * 100, 2)
            comp.append(_clip((margin - m0) * 100 / 5))
            parts.append(f"margin {margin:.1%} ({(margin - m0) * 100:+.1f} pp)")
    if cur["total_debt_cr"] is not None and cur["equity_cr"]:
        de = cur["total_debt_cr"] / cur["equity_cr"]
        ev["debt_to_equity"] = round(de, 3)
        comp.append(_clip(-(de - 1.0)) if de > 1.0 else 0.25)
        parts.append(f"debt/equity {de:.2f}")
    age = (as_of - pd.Timestamp(cur["published_at"])).days
    ev["age_days"] = age
    if not comp:
        return {"status": "partial", "score": None, "evidence": ev, "plain": f"Result for {cur['period_end']} has too few figures to score."}
    stale = age > 120
    return {"status": "stale" if stale else "ok", "score": round(float(np.mean(comp)), 3), "evidence": ev,
            "plain": f"Quarter to {cur['period_end']} (published {cur['published_at'][:10]}): " + ", ".join(parts)
                     + (f". Older than 120 days ({age} d): the next result is due." if stale else ".")}


# ---------------------------------------------------------------------------------------------------- 2. news tone
def lens_news(db: sqlite3.Connection | None, symbol: str, as_of: pd.Timestamp) -> dict:
    if db is None:
        return {"status": "unavailable", "reason": "news watcher database not found"}
    lo = (as_of - pd.Timedelta(days=THRESH["news_days"])).isoformat(timespec="seconds")
    hi = as_of.isoformat(timespec="seconds")
    rows = db.execute("SELECT title, first_seen, source FROM headlines WHERE first_seen >= ? AND first_seen < ? AND symbols LIKE ? "
                      "ORDER BY first_seen DESC", (lo, hi, f'%"{symbol}"%')).fetchall()
    base_lo = (as_of - pd.Timedelta(days=30)).isoformat(timespec="seconds")
    base = db.execute("SELECT COUNT(*) FROM headlines WHERE first_seen >= ? AND first_seen < ? AND symbols LIKE ?",
                      (base_lo, hi, f'%"{symbol}"%')).fetchone()[0]
    if not rows:
        return {"status": "ok", "score": 0.0, "evidence": {"headlines": 0, "headlines_30d": int(base)},
                "plain": f"No headlines about {symbol} in the last {THRESH['news_days']} days."}
    p = sum(_words(r[0], POS) for r in rows)
    n = sum(_words(r[0], NEG) for r in rows)
    tone = (p - n) / (p + n) if p + n else 0.0
    usual = base / 30 * THRESH["news_days"]
    attention = len(rows) / usual if usual > 0 else None
    return {"status": "ok", "score": round(_clip(tone), 3),
            "evidence": {"headlines": len(rows), "positive_words": p, "negative_words": n, "headlines_30d": int(base),
                         "attention_vs_usual": round(attention, 2) if attention else None,
                         "latest": [{"title": r[0], "first_seen": r[1], "source": r[2]} for r in rows[:5]]},
            "plain": f"{len(rows)} headline(s) in {THRESH['news_days']} days, tone {'positive' if tone > 0.2 else 'negative' if tone < -0.2 else 'mixed'} "
                     f"({p} positive / {n} negative words)" + (f", {attention:.1f}x the usual attention" if attention and attention >= 2 else "") + "."}


# ---------------------------------------------------------------------------------------------------- 3. economic events
def lens_events(store, symbol: str | None, as_of: pd.Timestamp, ahead_days: int = 3) -> dict:
    if store is None:
        return {"status": "unavailable", "reason": "no event calendar"}
    from nseql.data.scheduled import scheduled
    soon = []
    for e in scheduled(store):
        when, known = pd.Timestamp(e["event_time"]), pd.Timestamp(e["published_at"])
        if known >= as_of or not (as_of.normalize() <= when.normalize() <= as_of.normalize() + pd.Timedelta(days=ahead_days)):
            continue
        if e["kind"] == "results" and (symbol is None or symbol not in (e["symbols"] or "").split(",")):
            continue
        soon.append({"kind": e["kind"], "date": str(when.date()), "label": e["headline"]})
    risk = min(1.0, sum(1.0 if x["kind"] == "results" else 0.6 if x["kind"] in ("rbi_policy", "union_budget", "fomc") else 0.3 for x in soon))
    return {"status": "ok", "risk": round(risk, 2), "evidence": {"upcoming": soon, "window_days": ahead_days},
            "plain": ("Upcoming: " + "; ".join(f"{x['label']} on {x['date']}" for x in soon) + ".") if soon
                     else f"No scheduled result or macro event in the next {ahead_days} days."}


# ---------------------------------------------------------------------------------------------------- 4/5. price-based lenses
def _stock_stats(d: pd.DataFrame) -> dict | None:
    if len(d) < 25:
        return None
    c, v = d.close.to_numpy(float), d.volume.to_numpy(float)
    r = np.diff(np.log(c))
    sig = float(np.std(r[-21:-1])) if len(r) > 21 else float(np.std(r))
    vol20 = float(np.mean(v[-21:-1])) if len(v) > 21 else float(np.mean(v))
    hi = float(d.high.to_numpy(float)[-252:].max())
    lo = float(d.low.to_numpy(float)[-252:].min())
    o = float(d.open.iloc[-1])
    prev = float(c[-2])
    gap = o / prev - 1
    return {"ret1": float(r[-1]), "sigma20": sig, "vol_ratio": float(v[-1] / vol20) if vol20 > 0 else None, "close": float(c[-1]),
            "hi52": hi, "lo52": lo, "years_of_history": round(len(d) / 252, 2), "gap": gap, "prev": prev, "open": o,
            "mom20": float(c[-1] / c[-21] - 1) if len(c) > 21 else None, "sma20": float(np.mean(c[-20:])), "rets": r[-20:]}


def lens_behaviour(st: dict | None, fin, symbol: str, d: pd.DataFrame, as_of: pd.Timestamp) -> dict:
    if st is None:
        return {"status": "unavailable", "reason": "fewer than 25 completed daily bars"}
    pats, score = [], []
    to_hi, to_lo = st["close"] / st["hi52"] - 1, st["close"] / st["lo52"] - 1
    if st["years_of_history"] >= 0.5 and to_hi >= -THRESH["near_high"]:
        pats.append({"pattern": "anchoring: near the 52-week high", "detail": f"{to_hi:+.1%} from the high", "lean": "overconfidence risk"})
        score.append(-0.3)
    elif st["years_of_history"] >= 0.5 and to_lo <= THRESH["near_high"]:
        pats.append({"pattern": "anchoring: near the 52-week low", "detail": f"{to_lo:+.1%} above the low", "lean": "fear / capitulation risk"})
        score.append(0.2)
    big = st["sigma20"] > 0 and abs(st["ret1"]) > THRESH["overreaction_sigma"] * st["sigma20"]
    heavy = st["vol_ratio"] is not None and st["vol_ratio"] >= THRESH["heavy_volume"]
    if big and heavy:
        up = st["ret1"] > 0
        pats.append({"pattern": "overreaction day: " + ("euphoric buying" if up else "panic selling"),
                     "detail": f"{math.expm1(st['ret1']):+.1%} on {st['vol_ratio']:.1f}x normal volume", "lean": "possible reversal"})
        score.append(-0.4 if up else 0.4)
    if abs(st["gap"]) >= THRESH["gap"] and np.sign(st["close"] - st["open"]) == -np.sign(st["gap"]) and \
            abs(st["close"] / st["prev"] - 1) < abs(st["gap"]) / 2:
        pats.append({"pattern": "gap faded", "detail": f"opened {st['gap']:+.1%}, closed back near the previous close",
                     "lean": "the crowd's first reaction was reversed"})
    if fin is not None:
        q = fin.as_of(symbol, as_of, quarters=1)
        if q:
            pub = pd.Timestamp(q[0]["published_at"])
            after = d[d.ts.dt.normalize() >= pub.normalize()]
            before = d[d.ts.dt.normalize() < pub.normalize()]
            if 1 <= len(after) <= THRESH["drift_sessions"] and len(before):
                react = float(after.close.iloc[0] / before.close.iloc[-1] - 1)
                pats.append({"pattern": "drift after a results reaction", "detail": f"results-day reaction {react:+.1%}, session {len(after)} "
                             f"of {THRESH['drift_sessions']}", "lean": "documented post-results drift in the reaction's direction"})
                score.append(_clip(react / 0.05) * 0.5)
    if st["mom20"] is not None:
        score.append(_clip(st["mom20"] / 0.10) * 0.3)
    return {"status": "ok", "score": round(float(np.mean(score)), 3) if score else 0.0,
            "evidence": {"to_52w_high": round(to_hi, 4), "to_52w_low": round(to_lo, 4), "momentum_20d": None if st["mom20"] is None else round(st["mom20"], 4),
                         "last_day_return": round(math.expm1(st["ret1"]), 4), "volume_vs_20d": None if st["vol_ratio"] is None else round(st["vol_ratio"], 2),
                         "patterns": pats},
            "plain": "; ".join(f"{p['pattern']} ({p['detail']})" for p in pats) + "." if pats else "No behavioural pattern flagged."}


def lens_market(stats: dict[str, dict], vix: pd.Series | None, as_of: pd.Timestamp) -> dict:
    ok = {s: x for s, x in stats.items() if x}
    ev: dict = {"stocks": len(ok)}
    reasons: list[str] = []
    vix_pct = None
    if vix is not None and len(vix):
        v = vix[(pd.to_datetime(vix.index).normalize() + pd.Timedelta(hours=CLOSE_H, minutes=CLOSE_M)) <= as_of]
        if len(v) >= 120:
            last = float(v.iloc[-1])
            vix_pct = float((v.iloc[-252:-1] < last).mean())
            ev.update({"india_vix": round(last, 2), "vix_percentile_1y": round(vix_pct, 3), "vix_date": str(pd.Timestamp(v.index[-1]).date())})
    if not ok:
        return {"status": "unavailable" if vix_pct is None else "partial", "mood": "unknown", "score": None, "evidence": ev,
                "plain": "Not enough daily price history for the watchlist."}
    n = len(ok)
    up = np.mean([x["ret1"] > 0 for x in ok.values()])
    above = np.mean([x["close"] > x["sma20"] for x in ok.values()])
    panic = np.mean([x["sigma20"] > 0 and x["ret1"] < -2 * x["sigma20"] and (x["vol_ratio"] or 0) >= 1.5 for x in ok.values()])
    euph = np.mean([x["sigma20"] > 0 and x["ret1"] > 2 * x["sigma20"] and (x["vol_ratio"] or 0) >= 1.5 for x in ok.values()])
    near_hi = np.mean([x["years_of_history"] >= 0.5 and x["close"] / x["hi52"] - 1 >= -THRESH["near_high"] for x in ok.values()])
    R = np.array([x["rets"] for x in ok.values() if len(x["rets"]) == 20])
    herd = None
    if len(R) >= 3:
        C = np.corrcoef(R)
        herd = float(C[np.triu_indices(len(R), 1)].mean())
    ev.update({"up_today": round(float(up), 3), "above_20d_average": round(float(above), 3), "panic_selling_share": round(float(panic), 3),
               "euphoric_buying_share": round(float(euph), 3), "near_52w_high_share": round(float(near_hi), 3),
               "herding_correlation_20d": None if herd is None else round(herd, 3)})
    fear = (vix_pct is not None and vix_pct >= THRESH["vix_fear_pct"]) or panic >= THRESH["panic_share"] or above <= THRESH["weak_breadth"]
    greed = ((vix_pct is not None and vix_pct <= THRESH["vix_calm_pct"] and above >= THRESH["strong_breadth"])
             or near_hi >= THRESH["near_high_share"] or euph >= THRESH["euphoria_share"])
    if fear:
        reasons += [r for r, c in ((f"VIX in the top {1 - THRESH['vix_fear_pct']:.0%} of its year", vix_pct is not None and vix_pct >= THRESH["vix_fear_pct"]),
                                   (f"{panic:.0%} of stocks fell hard on heavy volume", panic >= THRESH["panic_share"]),
                                   (f"only {above:.0%} above their 20-day average", above <= THRESH["weak_breadth"])) if c]
    if greed:
        reasons += [r for r, c in (("VIX unusually low with broad strength", vix_pct is not None and vix_pct <= THRESH["vix_calm_pct"] and above >= THRESH["strong_breadth"]),
                                   (f"{near_hi:.0%} of stocks crowd near 52-week highs", near_hi >= THRESH["near_high_share"]),
                                   (f"{euph:.0%} jumped on heavy volume", euph >= THRESH["euphoria_share"])) if c]
    mood = "mixed (fear and greed signals)" if fear and greed else "fear" if fear else "greed / overconfidence" if greed else "neutral"
    score = _clip((above - 0.5) * 2 - (0 if vix_pct is None else (vix_pct - 0.5)) - panic * 2 + euph)
    if herd is not None and herd >= 0.6:
        reasons.append(f"stocks moving together (herding, correlation {herd:.2f})")
    return {"status": "ok" if vix_pct is not None else "partial", "mood": mood, "score": round(score, 3), "evidence": ev,
            "plain": f"Market mood: {mood}" + (f" ({'; '.join(reasons)})" if reasons else "") + f". {above:.0%} of {n} stocks above their 20-day average"
                     + ("" if vix_pct is not None else "; India VIX not available (import market context to add it)") + "."}


# ---------------------------------------------------------------------------------------------------- the dossier
def analyse(symbols: list[str], as_of, src: Sources) -> dict:
    """Point-in-time dossier for the watchlist and the market. Observation only."""
    as_of = pd.Timestamp(as_of)
    daily = {s: _complete_daily(src.daily(s) if src.daily else None, as_of) for s in symbols}
    stats = {s: _stock_stats(d) for s, d in daily.items()}
    market = lens_market(stats, src.vix, as_of)
    macro = lens_events(src.events, None, as_of)
    stocks = {}
    for s in symbols:
        L = {"reports": lens_reports(src.financials, s, as_of), "news": lens_news(src.headlines, s, as_of),
             "events": lens_events(src.events, s, as_of), "behaviour": lens_behaviour(stats[s], src.financials, s, daily[s], as_of)}
        st = stats[s]
        if st and st["sigma20"] > 0 and (st["vol_ratio"] or 0) >= 1.5 and abs(st["ret1"]) > 2 * st["sigma20"]:
            L["emotion"] = {"status": "ok", "score": -0.5 if st["ret1"] > 0 else 0.5,
                            "plain": ("euphoric buying today" if st["ret1"] > 0 else "panic selling today") + f" ({math.expm1(st['ret1']):+.1%}, "
                                     f"{st['vol_ratio']:.1f}x volume)"}
        else:
            L["emotion"] = {"status": "ok" if st else "unavailable", "score": 0.0, "plain": "No crowd extreme in this stock today." if st else ""}
        scored = [L[k]["score"] for k in ("reports", "news", "behaviour") if L[k].get("score") is not None and L[k]["status"] in ("ok", "stale")]
        view = float(np.mean(scored)) if scored else None
        stance = ("no view (not enough data)" if view is None else "supportive" if view >= 0.25 else "cautious" if view <= -0.25 else "neutral")
        if L["events"].get("risk", 0) >= 1.0:
            stance += "; results due: event risk"
        stocks[s] = {"stance": stance, "view": None if view is None else round(view, 3), "lenses": L,
                     "coverage": {k: v["status"] for k, v in L.items()}}
    return {"agent": AGENT_VERSION, "as_of": as_of.isoformat(timespec="minutes"), "market": {"emotion": market, "events": macro},
            "stocks": stocks, "notes": src.notes,
            "use": "observation only: not used to place, size or block trades until a lens passes the placebo test (evaluate)"}


# ---------------------------------------------------------------------------------------------------- snapshots + test
SNAP = """CREATE TABLE IF NOT EXISTS snapshots (as_of TEXT, symbol TEXT, agent TEXT, view REAL, reports REAL, news REAL, behaviour REAL,
  emotion REAL, event_risk REAL, mood TEXT, data TEXT, PRIMARY KEY (as_of, symbol));"""


def save(dossier: dict, path: Path) -> int:
    db = sqlite3.connect(str(path))
    try:
        db.executescript(SNAP)
        mood = dossier["market"]["emotion"].get("mood")
        rows = []
        for s, x in dossier["stocks"].items():
            L = x["lenses"]
            g = lambda k: L[k].get("score") if L[k].get("status") in ("ok", "stale") else None
            rows.append((dossier["as_of"], s, dossier["agent"], x["view"], g("reports"), g("news"), g("behaviour"), g("emotion"),
                         L["events"].get("risk"), mood, json.dumps(x, default=str)))
        db.executemany("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.commit()
        return len(rows)
    finally:
        db.close()


MIN_TRADES, MIN_DAYS, LENSES = 100, 20, ("view", "reports", "news", "behaviour", "emotion", "event_risk")


def evaluate(account: Path, snapshots: Path, placebo_seed: int = 20261125) -> dict:
    """Does a lens separate the account's winning from losing round trips, better than chance? (see evaluate_frames)"""
    from nseql.screening.trade_report import round_trips
    snap = sqlite3.connect(f"file:{snapshots}?mode=ro", uri=True)
    try:
        S = pd.read_sql_query("SELECT as_of, symbol, " + ", ".join(LENSES) + " FROM snapshots ORDER BY as_of", snap)
    finally:
        snap.close()
    rt = round_trips(Path(account) / "screening.sqlite", "9999-12-31", all_days=True)
    return evaluate_frames(rt, S, placebo_seed)


def evaluate_frames(rt: pd.DataFrame, S: pd.DataFrame, placebo_seed: int = 20261125) -> dict:
    """Each round trip gets the latest snapshot taken BEFORE its entry (snapshots are made after the close, so it is the
    previous session's dossier). Per lens: mean net P&L per trade when the score was above zero minus when below.
    Placebo: the same scores with the snapshot DAYS circularly shifted (keeps how often a score is positive, breaks its
    link to the trade). A lens is informative only if the real difference beats the 95th percentile of the shifts, with
    at least MIN_TRADES trades and MIN_DAYS snapshot days; otherwise 'not enough evidence' or 'no usable information'."""
    days = sorted(S.as_of.unique()) if len(S) else []
    out = {"agent": AGENT_VERSION, "trades": int(len(rt)), "snapshot_days": len(days), "min_trades": MIN_TRADES, "min_days": MIN_DAYS, "lenses": {}}
    if rt is None or rt.empty or not days:
        out["verdict"] = "not enough evidence"
        out["lenses"] = {k: {"verdict": "not enough evidence"} for k in LENSES}
        return out
    rt = rt.dropna(subset=["entry_ts"]).copy()
    keys = pd.to_datetime(rt.entry_ts).dt.strftime("%Y-%m-%dT%H:%M").to_numpy(object)
    rt["day_i"] = np.searchsorted(np.array(days, dtype=object), keys, side="left") - 1
    rt = rt[rt.day_i >= 0]
    out["trades_with_a_prior_snapshot"] = int(len(rt))
    table = {(a, s): r for a, s, *r in S.itertuples(index=False, name=None)}
    rng = np.random.default_rng(placebo_seed)
    shifts = sorted(set(int(x) for x in rng.integers(1, max(len(days), 2), size=min(200, max(len(days) - 1, 1)))))
    net = rt.net_inr.to_numpy(float)

    def diff(k: int, shift: int) -> float | None:
        vals = [table.get((days[(int(i) - shift) % len(days)], s)) for i, s in zip(rt.day_i, rt.symbol)]
        x = np.array([np.nan if v is None or v[k] is None or (isinstance(v[k], float) and math.isnan(v[k])) else v[k] for v in vals], dtype=float)
        pos, neg = net[x > 0], net[x < 0]
        return float(pos.mean() - neg.mean()) if len(pos) >= 5 and len(neg) >= 5 else None

    for k, name in enumerate(LENSES):
        real = diff(k, 0)
        plac = [d for d in (diff(k, sh) for sh in shifts) if d is not None]
        q95 = float(np.quantile(plac, 0.95)) if len(plac) >= 10 else None
        enough = len(rt) >= MIN_TRADES and len(days) >= MIN_DAYS and real is not None and q95 is not None
        out["lenses"][name] = {"difference_inr_per_trade": None if real is None else round(real, 2), "placebo_95th": None if q95 is None else round(q95, 2),
                               "placebo_shifts": len(plac),
                               "verdict": "not enough evidence" if not enough else
                                          "informative: candidate filter (test it in the experiment protocol)" if real > q95 else "no usable information"}
    out["verdict"] = "see lenses"
    return out


# ---------------------------------------------------------------------------------------------------- wiring
def default_sources(exp_dir: Path, data_dir: Path, account: Path | None = None) -> Sources:
    """The laptop's real inputs: imported results, the news watcher, the event calendar, daily history (the Angel One daily
    dataset, continued with daily bars built from the live account's 1-minute bars) and India VIX if market context exists."""
    from nseql.data.events import EventStore
    from nseql.data.financials import Financials
    exp_dir, data_dir = Path(exp_dir), Path(data_dir)
    notes = []
    fp = exp_dir / "fundamental" / "financials.sqlite"
    fin = Financials(fp) if fp.exists() else None
    if fin is None or not fin.count():
        notes.append("no company results imported yet: python -m nseql.cli financials-import <file.csv> --source \"...\"")
    nw = exp_dir / "news" / "watch.sqlite"
    heads = sqlite3.connect(f"file:{nw}?mode=ro", uri=True, check_same_thread=False) if nw.exists() else None
    ev = EventStore(exp_dir / "events.sqlite") if (exp_dir / "events.sqlite").exists() else None
    hist = None
    try:
        from nseql.data.store import Dataset
        hist = Dataset(data_dir / "angel_daily") if (data_dir / "angel_daily").exists() else None
    except Exception:       # noqa: BLE001
        hist = None
    minute = sqlite3.connect(f"file:{Path(account) / 'screening.sqlite'}?mode=ro", uri=True, check_same_thread=False) \
        if account is not None and (Path(account) / "screening.sqlite").exists() else None

    def daily(sym: str) -> pd.DataFrame:
        parts = []
        if hist is not None:
            try:
                parts.append(hist.bars(sym, "1d")[["ts", "open", "high", "low", "close", "volume"]])
            except KeyError:
                pass
        if minute is not None:
            m = pd.read_sql_query("SELECT ts, open, high, low, close, volume FROM events WHERE symbol=? AND late=0 ORDER BY ts", minute, params=(sym,))
            if len(m):
                m["ts"] = pd.to_datetime(m.ts)
                g = m.groupby(m.ts.dt.normalize())
                parts.append(pd.DataFrame({"ts": list(g.groups), "open": g.open.first().values, "high": g.high.max().values,
                                           "low": g.low.min().values, "close": g.close.last().values, "volume": g.volume.sum().values}))
        if not parts:
            return pd.DataFrame()
        d = pd.concat(parts)
        d["ts"] = pd.to_datetime(d.ts).dt.normalize()
        return d.drop_duplicates("ts", keep="last").sort_values("ts")          # minute-built days replace a daily file's same day

    vix = None
    for ds_id in ("context_v1", "context"):
        try:
            from nseql.data.store import Dataset
            if (data_dir / ds_id).exists():
                b = Dataset(data_dir / ds_id).bars("INDIAVIX", "1d")
                vix = pd.Series(b.close.to_numpy(float), index=pd.to_datetime(b.ts).dt.normalize())
                break
        except Exception:       # noqa: BLE001
            continue
    if vix is None:
        notes.append("India VIX not available: import market context (python -m nseql.cli import-context ...) to add the fear gauge")
    if hist is None and minute is None:
        notes.append("no daily price history found: behaviour and crowd lenses are unavailable")
    return Sources(fin, heads, ev, daily, vix, notes)

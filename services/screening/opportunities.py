"""Opportunity ledger (docs/VISION.md, phase 1): what the bot took, what it refused and what it missed, and what happened
next, measured AFTER the session for research only.

Separation from trading. The outcomes below are hindsight. They are written to their own research database
(experiments/research/opportunities.sqlite), which no trading component reads; a test asserts that the engine, scheduler,
executor, allocator and challenger never reference it. Decisions therefore stay exactly as they were made.

1. Decisions: every BUY signal of the session at or after the live start (warm-up and catch-up excluded), grouped per
   stock and minute and by what the bot did (accepted / deferred / rejected, with the recorded reason category).
2. Outcome of each stock-minute, with one yardstick for all of them: buy at the next bar's open, sell 30 minutes later or
   at 15:15, minus a fixed round-trip cost of COST (charges + spread, a conservative estimate for liquid large caps).
3. Gate review per reason category: how many setups it refused and how they would have done. A refusal category whose
   setups went on to lose protected the account; one whose setups went on to win cost opportunities.
4. Missed moves: stocks that rose at least MISSED_MOVE after costs within an hour (from a completed 5-minute candle
   before 14:30) while the bot made no buy in them; with whether any strategy signalled and why it was refused.

These are research inputs. A conclusion such as "the max-positions limit cost money" becomes a proposal only through the
experiment protocol and the operator's approval, never by itself.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from nseql.screening.metrics import _next_day, reason_category

COST = 0.0015
MISSED_MOVE = 0.015
SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_outcomes (account TEXT, day TEXT, symbol TEXT, minute TEXT, status TEXT, category TEXT, signals INTEGER,
  fwd30_net REAL, to_close_net REAL, PRIMARY KEY (account, day, symbol, minute, status, category));
CREATE TABLE IF NOT EXISTS missed (account TEXT, day TEXT, symbol TEXT, start TEXT, peak TEXT, move_net REAL, signalled INTEGER, refused_because TEXT,
  PRIMARY KEY (account, day, symbol));
"""


def research_db(exp_dir: Path) -> Path:
    d = Path(exp_dir) / "research"
    d.mkdir(parents=True, exist_ok=True)
    return d / "opportunities.sqlite"


def _bars(db: sqlite3.Connection, day: str) -> pd.DataFrame:
    b = pd.read_sql_query("SELECT symbol, ts, open, high, low, close FROM events WHERE late=0 AND ts >= ? AND ts < ? ORDER BY symbol, ts",
                          db, params=(day, _next_day(day)))
    b["ts"] = pd.to_datetime(b.ts)
    return b


def run(account: Path, day: str, exp_dir: Path) -> dict:
    account = Path(account)
    db = sqlite3.connect(f"file:{account / 'screening.sqlite'}?mode=ro", uri=True)
    try:
        live_from = None
        try:
            import json
            live_from = json.loads((account / "live.json").read_text()).get("live_from")
        except (OSError, ValueError):
            pass
        sig = pd.read_sql_query("SELECT symbol, bar_ts AS minute, alloc_status AS status, alloc_reason AS reason, COUNT(*) AS n FROM signals "
                                "WHERE decision='BUY' AND bar_ts >= ? AND bar_ts < ? GROUP BY 1, 2, 3, 4", db, params=(day, _next_day(day)))
        bars = _bars(db, day)
        buys = {r[0] for r in db.execute("SELECT DISTINCT symbol FROM paper_orders WHERE side='BUY' AND decided_at >= ? AND decided_at < ?",
                                         (day, _next_day(day)))}
    finally:
        db.close()
    if live_from:
        sig = sig[sig.minute >= live_from]
    sig["category"] = [("accepted" if s == "accepted" else reason_category(r)) for s, r in zip(sig.status, sig.reason)]
    sig = sig[~sig.category.isin(["warm-up history", "catch-up (late data, not traded)"])]
    g = sig.groupby(["symbol", "minute", "status", "category"], as_index=False).n.sum()
    # one yardstick for every stock-minute: buy at the next bar's open, out 30 minutes later or at 15:15
    sq = np.datetime64(pd.Timestamp(day) + pd.Timedelta(hours=15, minutes=15))
    g["fwd30_net"], g["to_close_net"] = np.nan, np.nan
    for sym, rows in g.groupby("symbol").groups.items():           # vectorised per stock (tens of thousands of setups a day)
        f = bars[bars.symbol == sym]
        if f.empty:
            continue
        ts, op, cl = f.ts.to_numpy(), f.open.to_numpy(float), f.close.to_numpy(float)
        t = pd.to_datetime(g.loc[rows, "minute"]).to_numpy()
        e = np.searchsorted(ts, t, side="right")                   # the next bar after the decision minute
        j30 = np.searchsorted(ts, t + np.timedelta64(31, "m"), side="right") - 1
        jsq = np.searchsorted(ts, sq, side="right") - 1
        ok = e < len(ts)
        e_ = np.minimum(e, len(ts) - 1)
        entry = op[e_]
        v30 = np.where(ok & (j30 >= e), cl[np.clip(j30, 0, len(ts) - 1)] / entry - 1 - COST, np.nan)
        vcl = np.where(ok & (jsq >= e), cl[max(jsq, 0)] / entry - 1 - COST, np.nan)
        g.loc[rows, "fwd30_net"], g.loc[rows, "to_close_net"] = v30, vcl
    # gate review
    review = []
    for (status, cat), x in g.groupby(["status", "category"]):
        v30 = x.fwd30_net.dropna()
        review.append({"decision": status, "reason": cat, "setups": int(len(x)), "signals": int(x.n.sum()),
                       "avg_30min_net": None if v30.empty else round(float(v30.mean()), 5),
                       "avg_to_close_net": None if x.to_close_net.dropna().empty else round(float(x.to_close_net.mean()), 5),
                       "share_positive_30min": None if v30.empty else round(float((v30 > 0).mean()), 3)})
    review.sort(key=lambda r: -r["setups"])
    # missed moves
    from nseql.screening.price_action import candles
    missed = []
    for s, f in bars.groupby("symbol"):
        c = candles(f.rename(columns={}).assign(volume=0), 5)
        if len(c) < 3:
            continue
        best = None
        for i in range(len(c)):
            if c.end.iloc[i] >= pd.Timestamp(day) + pd.Timedelta(hours=14, minutes=30):
                break
            fwd = c.iloc[i + 1:i + 13]
            if fwd.empty:
                continue
            mv = float(fwd.high.max()) / float(c.close.iloc[i]) - 1 - COST
            if best is None or mv > best[0]:
                best = (mv, c.end.iloc[i], fwd.end.iloc[int(np.argmax(fwd.high.to_numpy()))])
        if best and best[0] >= MISSED_MOVE and s not in buys:
            win = sig[(sig.symbol == s) & (sig.minute >= best[1].strftime("%Y-%m-%dT%H:%M:%S")) &
                      (sig.minute <= best[2].strftime("%Y-%m-%dT%H:%M:%S"))]
            why = win[win.status != "accepted"].category.value_counts()
            missed.append({"symbol": s, "start": best[1].strftime("%H:%M"), "peak": best[2].strftime("%H:%M"), "move_net": round(best[0], 4),
                           "signalled": bool(len(win)), "refused_because": why.index[0] if len(why) else None})
    missed.sort(key=lambda m: -m["move_net"])
    rdb = sqlite3.connect(str(research_db(exp_dir)))
    try:
        rdb.executescript(SCHEMA)
        rdb.executemany("INSERT OR REPLACE INTO signal_outcomes VALUES (?,?,?,?,?,?,?,?,?)",
                        [(account.name, day, r.symbol, r.minute, r.status, r.category, int(r.n),
                          None if pd.isna(r.fwd30_net) else float(r.fwd30_net), None if pd.isna(r.to_close_net) else float(r.to_close_net))
                         for r in g.itertuples()])
        rdb.executemany("INSERT OR REPLACE INTO missed VALUES (?,?,?,?,?,?,?,?)",
                        [(account.name, day, m["symbol"], m["start"], m["peak"], m["move_net"], int(m["signalled"]), m["refused_because"]) for m in missed])
        rdb.commit()
    finally:
        rdb.close()
    plain = []
    for r in review:
        if r["decision"] == "accepted" or r["avg_30min_net"] is None:
            continue
        verdict = "protected the account" if r["avg_30min_net"] < 0 else "cost opportunities"
        plain.append(f"'{r['reason']}' refused {r['setups']:,} setups; 30 minutes later they averaged {r['avg_30min_net']:+.2%} after costs: it {verdict}.")
    acc = next((r for r in review if r["decision"] == "accepted"), None)
    if acc and acc["avg_30min_net"] is not None:
        plain.insert(0, f"Setups the bot took averaged {acc['avg_30min_net']:+.2%} 30 minutes later on the same yardstick ({acc['setups']} setups).")
    if missed:
        plain.append("Missed moves: " + "; ".join(f"{m['symbol']} {m['move_net']:+.1%} from {m['start']}" for m in missed[:4]) + ".")
    return {"day": day, "account": account.name, "gate_review": review, "missed": missed, "plain": plain, "cost_assumed": COST,
            "note": "hindsight outcomes for research only: stored separately and never read by any trading decision"}

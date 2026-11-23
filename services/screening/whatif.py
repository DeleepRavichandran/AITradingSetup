"""What-if replay: run the bars a paper account already stored through other trading rules, side by side.

Uses only the account's own stored 1-minute bars (no new data). Both runs start from the same capital and
place paper orders only from the account's live start. A day the rules were designed from is NOT unseen
data: the comparison is a sanity check, not evidence that a rule works.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

from nseql.screening.execution import PortfolioRules
from nseql.screening.scheduler import MinuteScheduler

INDEX = {"NIFTY50", "NIFTYBANK", "INDIAVIX"}


def _bars(src: Path, upto: str) -> list[dict]:
    db = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT symbol, ts, open, high, low, close, volume FROM events WHERE late = 0 AND ts <= ? ORDER BY ts, symbol",
                          (upto,)).fetchall()
    finally:
        db.close()
    return [{"symbol": r[0], "ts": pd.Timestamp(r[1]), "open": r[2], "high": r[3], "low": r[4], "close": r[5], "volume": r[6]} for r in rows]


def _summary(sch: MinuteScheduler, day: str) -> dict:
    db = sch.store.db
    n, real, ch = db.execute("SELECT COUNT(*), COALESCE(SUM(CASE WHEN side='SELL' THEN realized_paise END), 0), COALESCE(SUM(charges_paise), 0) "
                             "FROM paper_orders WHERE status IN ('filled','partial') AND substr(fill_ts,1,10) = ?", (day,)).fetchone()
    eq = db.execute("SELECT equity_inr FROM cycles WHERE substr(minute,1,10) = ? AND equity_inr IS NOT NULL ORDER BY minute DESC LIMIT 1",
                    (day,)).fetchone()
    refused = dict(db.execute("SELECT reason, SUM(n) FROM refusals WHERE substr(minute,1,10) = ? GROUP BY reason ORDER BY 2 DESC", (day,)).fetchall())
    halted = db.execute("SELECT MIN(ts) FROM portfolio_events WHERE event='DAILY_LOSS_LIMIT' AND substr(ts,1,10) = ?", (day,)).fetchone()[0]
    return {"trades": int(n), "realised_after_charges_inr": real / 100, "charges_inr": ch / 100,
            "end_value_inr": eq[0] if eq else None, "loss_limit_hit_at": halted, "refused_buys": refused}


def what_if(account: Path, day: str, out: Path, progress=print) -> dict:
    account, out = Path(account), Path(out)
    meta = json.loads((account / "live.json").read_text())
    capital, live_from = float(meta.get("capital") or PortfolioRules.capital), meta["live_from"]
    syms = list(meta["symbols"])
    bars = _bars(account / "screening.sqlite", f"{day}T23:59:59")
    if not any(str(b["ts"])[:10] == day for b in bars):
        raise ValueError(f"account {account.name} has no stored bars for {day}")
    runs = {"rules 1.0 (as traded)": (PortfolioRules(capital=capital), syms),
            "rules 1.2 (churn limits, 10% per trade, max 8 positions, index not traded)": (PortfolioRules.live(capital), [s for s in syms if s not in INDEX])}
    res = {}
    for k, (name, (rules, ss)) in enumerate(runs.items()):
        root = out / f"run{k}"
        if root.exists():
            raise FileExistsError(f"{root} exists; use an empty --out folder")
        progress(f"{name}: replaying {len(bars):,} stored bars for {len(ss)} symbols ...")
        sch = MinuteScheduler(root, ss, portfolio=rules, live_from=live_from, mode="replay")
        sch.ingest([b for b in bars if b["symbol"] in ss], source="what-if replay of stored bars")
        sch.run_cycle(pd.Timestamp(f"{day}T15:31:00"))
        res[name] = _summary(sch, day)
    return {"account": account.name, "day": day, "capital": capital, "live_from": live_from, "runs": res,
            "caution": "Replay of a day the new rules were designed from: a sanity check, not evidence they work. "
                       "Judge the rules on later days they have not seen."}

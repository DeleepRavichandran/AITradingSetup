"""Loss study: WHY did a day's paper trades lose? (operator, 2026-11-24)

The per-trade RCA says HOW each trade lost (stop, never covered costs, ...). This study asks what was true at the
moment of each BUY, and whether losers differ from winners:

1. The account's stored 1-minute bars for the day are replayed through the rules it traded with (a replay
   reproduces the live ledger exactly; tested in the what-if check), this time with the trade journal on, so every
   entry has its point-in-time market context: the stock's move since the open, trend vs the 5-minute 50-bar average,
   session-scaled volatility, the watchlist's move and breadth, a volatility regime label and the minute of the session.
2. Every round trip of the day is joined with its entry context and its RCA (exit reason, best and worst move).
3. For each factor the trades are split into buckets; per bucket: trades, win rate, total and mean net P&L after
   costs, and a 95% bootstrap interval of the mean. A bucket is a FINDING only if it has at least MIN_N trades and its
   interval is entirely below zero AND below the day's overall mean (it loses more than the rest, beyond chance on
   this day). Everything else is reported as "no clear difference".

One day is one sample: findings are hypotheses for a challenger filter, to be tested on days not used to find them.
Read-only: the account's ledger is never modified; the replay runs in a scratch folder.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from nseql.screening.execution import PortfolioRules
from nseql.screening.rca import analyse
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.whatif import _bars

MIN_N = 15
BOOT = {"resamples": 5000, "seed": 20261124}

BUCKETS = {
    "trend": ("trend at entry (close vs 5-minute 50-bar average)", lambda v: "unknown" if pd.isna(v) else "below average (downtrend)" if v < 0 else "above average (uptrend)"),
    "stock_move": ("stock's move since its open", lambda v: "unknown" if pd.isna(v) else "down > 0.5%" if v < -0.005 else "down 0-0.5%" if v < 0 else "up 0-0.5%" if v < 0.005 else "up > 0.5%"),
    "breadth": ("watchlist breadth (share of stocks up on the day)", lambda v: "unknown" if pd.isna(v) else "weak (< 40% up)" if v < 0.4 else "mixed (40-60%)" if v <= 0.6 else "strong (> 60% up)"),
    "market_move": ("watchlist average move since the open", lambda v: "unknown" if pd.isna(v) else "market down" if v < 0 else "market up"),
    "regime": ("volatility regime", lambda v: v or "unknown"),
    "session_part": ("time of day", lambda m: "unknown" if pd.isna(m) else "first 30 min (09:16-09:45)" if m <= 30 else "morning (09:46-11:30)" if m <= 135
                     else "midday (11:31-13:30)" if m <= 255 else "afternoon (13:31-15:00)"),
    "family": ("strategy type", lambda v: v),
    "timeframe": ("chart speed", lambda v: v),
    "stop": ("stop-loss size", lambda v: f"{v:.1%}"),
}


def _ci(x: np.ndarray) -> tuple[float, float]:
    if len(x) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(BOOT["seed"])
    m = x[rng.integers(0, len(x), size=(BOOT["resamples"], len(x)))].mean(axis=1)
    return float(np.quantile(m, 0.025)), float(np.quantile(m, 0.975))


def replay_with_journal(account: Path, day: str, out: Path) -> MinuteScheduler:
    """Replay the account's stored bars up to the end of `day` through the rules it traded with (a fresh folder)."""
    meta = json.loads((account / "live.json").read_text())
    capital = float(meta.get("start_cash") or meta.get("capital") or PortfolioRules.capital)
    rules = PortfolioRules.live(capital) if meta.get("rules_version") else PortfolioRules(capital=capital)
    sch = MinuteScheduler(out, list(meta["symbols"]), portfolio=rules, live_from=meta["live_from"], mode="replay")
    sch.verbose = False
    sch.ingest(_bars(account / "screening.sqlite", f"{day}T23:59:59"), source="loss-study replay of stored bars")
    sch.run_cycle(pd.Timestamp(f"{day}T15:31:00"))
    return sch


def study(trades: pd.DataFrame) -> dict:
    """trades: one row per round trip with net_inr and the factor columns."""
    overall = float(trades.net_inr.mean())
    factors = []
    for col, (label, fn) in BUCKETS.items():
        if col not in trades:
            continue
        b = trades.assign(bucket=trades[col].map(fn))
        rows = []
        for name, g in b.groupby("bucket"):
            x = g.net_inr.to_numpy(dtype=float)
            lo, hi = _ci(x)
            finding = len(x) >= MIN_N and hi < 0 and hi < overall
            rows.append({"bucket": str(name), "trades": int(len(x)), "win_rate": round(float((x > 0).mean()), 3), "net_inr": round(float(x.sum()), 2),
                         "mean_inr": round(float(x.mean()), 2), "ci95": [round(lo, 2), round(hi, 2)], "finding": bool(finding)})
        rows.sort(key=lambda r: r["mean_inr"])
        factors.append({"factor": col, "label": label, "buckets": rows})
    findings = []
    for f in factors:
        for r in f["buckets"]:
            if r["finding"]:
                findings.append({"factor": f["label"], "bucket": r["bucket"], "trades": r["trades"], "mean_inr": r["mean_inr"], "ci95": r["ci95"],
                                 "win_rate": r["win_rate"],
                                 "plain": f"Buys when {f['label']} was '{r['bucket']}' lost {abs(r['mean_inr']):.2f} INR per trade on average "
                                          f"({r['trades']} trades, 95% range {r['ci95'][0]:.2f} to {r['ci95'][1]:.2f}; day average {overall:.2f})"})
    findings.sort(key=lambda f: f["mean_inr"])
    return {"trades": int(len(trades)), "net_inr": round(float(trades.net_inr.sum()), 2), "mean_inr": round(overall, 2),
            "win_rate": round(float((trades.net_inr > 0).mean()), 3), "factors": factors, "findings": findings}


def run(account: Path, day: str, out: Path) -> dict:
    account, out = Path(account), Path(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty")
    sch = replay_with_journal(account, day, out)
    db = sch.store.db
    with sch.store._lock:
        rc = pd.DataFrame(analyse(db, day, write=False))
        j = pd.read_sql_query("SELECT config_id, ts AS entry_key, minute_of_session, stock_move, vol_session, market_move FROM journal WHERE side='BUY'", db)
        live = sqlite3.connect(f"file:{account / 'screening.sqlite'}?mode=ro", uri=True)
        try:
            live_net = live.execute("SELECT COALESCE(SUM(realized_paise),0) FROM paper_orders WHERE side='SELL' AND status IN ('filled','partial') "
                                    "AND substr(fill_ts,1,10)=?", (day,)).fetchone()[0] / 100
        finally:
            live.close()
    if rc.empty:
        raise ValueError(f"no completed round trips on {day}")
    rc["entry_key"] = pd.to_datetime(rc.entry_ts).dt.strftime("%Y-%m-%dT%H:%M:%S")
    t = rc.merge(j.drop_duplicates(["config_id", "entry_key"]), on=["config_id", "entry_key"], how="left")      # RCA already carries trend, breadth, regime
    t["session_part"] = t["minute_of_session"]
    t["stop"] = t["stop_pct"]
    rep = study(t)
    rep.update({"account": account.name, "day": day, "replay_matches_live": abs(round(float(rc.net_inr.sum()), 2) - round(live_net, 2)) < 0.05,
                "replay_net_inr": round(float(rc.net_inr.sum()), 2), "live_net_inr": round(live_net, 2),
                "caution": "One day is one sample. Findings are hypotheses for a challenger filter, to be tested on days not used to find them."})
    (out / "loss_study.json").write_text(json.dumps(rep, indent=2, default=str))
    return rep

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


PAIRS = (("session_part", "trend"), ("session_part", "regime"), ("trend", "regime"), ("session_part", "breadth"))


def two_factor(trades: pd.DataFrame) -> list[dict]:
    """Losers vs winners on TWO factors at once, to separate overlapping causes (e.g. 'first 30 minutes' and 'volatile'
    are often the same trades). Same finding rule as the one-factor view, per cell."""
    overall = float(trades.net_inr.mean())
    out = []
    for a, b in PAIRS:
        if a not in trades or b not in trades:
            continue
        (la, fa), (lb, fb) = BUCKETS[a], BUCKETS[b]
        t = trades.assign(_a=trades[a].map(fa), _b=trades[b].map(fb))
        cells = []
        for (va, vb), g in t.groupby(["_a", "_b"]):
            x = g.net_inr.to_numpy(dtype=float)
            lo, hi = _ci(x)
            cells.append({"a": str(va), "b": str(vb), "trades": int(len(x)), "win_rate": round(float((x > 0).mean()), 3),
                          "net_inr": round(float(x.sum()), 2), "mean_inr": round(float(x.mean()), 2), "ci95": [round(lo, 2), round(hi, 2)],
                          "finding": bool(len(x) >= MIN_N and hi < 0 and hi < overall)})
        cells.sort(key=lambda c: c["mean_inr"])
        out.append({"a": a, "b": b, "label_a": la, "label_b": lb, "cells": cells,
                    "findings": [f"'{c['a']}' AND '{c['b']}': {c['trades']} trades lost {abs(c['mean_inr']):.2f} INR each "
                                 f"(95% range {c['ci95'][0]:.2f} to {c['ci95'][1]:.2f}; day average {overall:.2f})" for c in cells if c["finding"]]})
    return out


def reconcile(db: sqlite3.Connection, day: str, rc: pd.DataFrame) -> dict:
    """Every analysed round trip must be a SELL order the ledger closed on `day`, with the same quantity, exit price and
    net P&L; and every such ledger order must be analysed. Only a reconciled study may publish a headline."""
    from nseql.screening.metrics import _next_day
    led = pd.read_sql_query("SELECT order_key, config_id, fill_ts, qty, price, realized_paise, charges_paise FROM paper_orders "
                            "WHERE side='SELL' AND status IN ('filled','partial') AND fill_ts >= ? AND fill_ts < ?", db, params=(day, _next_day(day)))
    fills = db.execute("SELECT COUNT(*), COALESCE(SUM(qty),0), COALESCE(SUM(charges_paise),0) FROM paper_fills f WHERE f.side='SELL' AND f.order_key IN "
                       "(SELECT order_key FROM paper_orders WHERE side='SELL' AND status IN ('filled','partial') AND fill_ts >= ? AND fill_ts < ?)",
                       (day, _next_day(day))).fetchone()
    a = pd.DataFrame({"config_id": rc.config_id if len(rc) else [], "fill_ts": pd.to_datetime(rc.exit_ts).dt.strftime("%Y-%m-%dT%H:%M:%S") if len(rc) else [],
                      "qty_a": rc.qty if len(rc) else [], "px_a": rc.exit_px if len(rc) else [], "net_a": rc.net_inr if len(rc) else []})
    m = led.merge(a, on=["config_id", "fill_ts"], how="outer", indicator=True)
    only_ledger, only_study = int((m._merge == "left_only").sum()), int((m._merge == "right_only").sum())
    both = m[m._merge == "both"]
    bad = both[(both.qty != both.qty_a) | ((both.price - both.px_a).abs() > 1e-6) | ((both.realized_paise / 100 - both.net_a).abs() > 0.005)]
    ok = only_ledger == 0 and only_study == 0 and bad.empty
    return {"reconciled": bool(ok), "ledger_round_trips": int(len(led)), "analysed_round_trips": int(len(rc)), "matched": int(len(both)),
            "missing_from_study": only_ledger, "not_in_ledger": only_study, "mismatched": int(len(bad)),
            "ledger_net_inr": round(float(led.realized_paise.sum()) / 100, 2), "analysed_net_inr": round(float(rc.net_inr.sum()) if len(rc) else 0.0, 2),
            "ledger_sell_fills": int(fills[0]), "ledger_sell_qty": int(fills[1]),
            "keys": "order identity (configuration, exit fill time), quantity, exit price, net P&L after both legs' charges"}


def run(account: Path, day: str, out: Path) -> dict:
    """Why the account's trades of `day` lost, from the ACCOUNT'S OWN LEDGER (round trips, fills, charges) and the trade
    journal it recorded live (context at each fill). Nothing is reconstructed, so the study describes exactly the trades
    the account made under its own rules, starting state and inherited evidence. Read-only."""
    account, out = Path(account), Path(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty")
    db = sqlite3.connect(f"file:{account / 'screening.sqlite'}?mode=ro", uri=True)
    try:
        rc = pd.DataFrame(analyse(db, day, write=False))
        has_j = db.execute("SELECT 1 FROM sqlite_master WHERE name='journal'").fetchone() is not None
        j = (pd.read_sql_query("SELECT config_id, ts AS entry_key, minute_of_session, stock_move, vol_session, market_move FROM journal WHERE side='BUY'", db)
             if has_j else pd.DataFrame(columns=["config_id", "entry_key", "minute_of_session", "stock_move", "vol_session", "market_move"]))
        rec = reconcile(db, day, rc)
    finally:
        db.close()
    if rc.empty:
        raise ValueError(f"no completed round trips on {day}")
    rc["entry_key"] = pd.to_datetime(rc.entry_ts).dt.strftime("%Y-%m-%dT%H:%M:%S")
    t = rc.merge(j.drop_duplicates(["config_id", "entry_key"]), on=["config_id", "entry_key"], how="left")
    t["session_part"] = t["minute_of_session"]
    t["stop"] = t["stop_pct"]
    rep = study(t)
    rep["two_factor"] = two_factor(t)
    rep.update({"account": account.name, "day": day, "source": "the account's own ledger and live trade journal (no replay)",
                "reconciliation": rec, "journal_coverage": round(float(t.minute_of_session.notna().mean()), 4),
                "caution": "One day is one sample. Findings are hypotheses for a challenger filter, to be tested on days not used to find them."})
    out.mkdir(parents=True, exist_ok=True)
    (out / "loss_study.json").write_text(json.dumps(rep, indent=2, default=str))
    return rep

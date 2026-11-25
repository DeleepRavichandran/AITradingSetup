"""Benchmarks (docs/VISION.md, phase 0): the account's after-cost result, always next to what a simpler choice would have
done over the same sessions.

  account       the paper account's equity, after all charges and slippage
  cash          0% (doing nothing is a valid decision)
  NIFTY 50      close-to-close index return (stored index bars if present, else the daily history dataset)
  buy and hold  the watchlist, equal weight, held close-to-close through the period (no trading costs after entry)
  trend rule    each day, hold equally the watchlist stocks whose previous close was above their 20-day average; flat
                otherwise; a simple, transparent rule. Costs: one round trip of 0.1% per changed position

Daily closes come from the account's stored 1-minute bars (the last bar of each session), extended backwards with the
daily history dataset where available. Every figure is after costs where costs apply. With fewer than 20 sessions the
risk-adjusted numbers are marked as not yet meaningful.
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

INDEX = {"NIFTY50", "NIFTYBANK", "INDIAVIX"}
TREND_N, TREND_COST = 20, 0.001
MIN_SESSIONS = 20


def _closes_from_ledger(db: sqlite3.Connection) -> pd.DataFrame:
    """Last close per symbol per session from the stored 1-minute bars."""
    # SQLite returns the bare column `close` from the row holding MAX(ts) in each group (documented behaviour)
    q = "SELECT symbol, substr(ts,1,10) AS day, close, MAX(ts) FROM events WHERE late=0 GROUP BY symbol, substr(ts,1,10)"
    d = pd.read_sql_query(q, db)
    return d.pivot_table(index="day", columns="symbol", values="close", aggfunc="last") if len(d) else pd.DataFrame()


def _closes_from_dataset(data_dir: Path | None, symbols: list[str]) -> pd.DataFrame:
    if data_dir is None or not (Path(data_dir) / "angel_daily").exists():
        return pd.DataFrame()
    from nseql.data.store import Dataset
    ds = Dataset(Path(data_dir) / "angel_daily")
    cols = {}
    for s in symbols:
        try:
            b = ds.bars(s, "1d")
        except KeyError:
            continue
        cols[s] = pd.Series(b.close.to_numpy(float), index=pd.to_datetime(b.ts).dt.strftime("%Y-%m-%d"))
    return pd.DataFrame(cols)


def stats(r: pd.Series, start_value: float = 1.0) -> dict:
    r = r.fillna(0.0)
    eq = start_value * (1 + r).cumprod()
    peak = np.maximum.accumulate(np.concatenate([[start_value], eq.to_numpy()]))
    dd = float((np.concatenate([[start_value], eq.to_numpy()]) / peak - 1).min())
    n = len(r)
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    dn = float(r[r < 0].std(ddof=1)) if (r < 0).sum() > 1 else 0.0
    return {"sessions": n, "return": round(float(eq.iloc[-1] / start_value - 1), 5) if n else 0.0, "max_drawdown": round(dd, 5),
            "sharpe": round(float(r.mean() / sd * math.sqrt(252)), 2) if sd > 0 else None,
            "sortino": round(float(r.mean() / dn * math.sqrt(252)), 2) if dn > 0 else None,
            "best_day": round(float(r.max()), 5) if n else None, "worst_day": round(float(r.min()), 5) if n else None}


def compare(account: Path, data_dir: Path | None = None, sessions: list[str] | None = None) -> dict:
    account = Path(account)
    meta = json.loads((account / "live.json").read_text()) if (account / "live.json").exists() else {}
    db = sqlite3.connect(f"file:{account / 'screening.sqlite'}?mode=ro", uri=True)
    try:
        cyc = pd.read_sql_query("SELECT minute, equity_inr FROM cycles WHERE equity_inr IS NOT NULL AND status='processed' AND mode != 'warmup' "
                                "ORDER BY minute", db)
        ledger = _closes_from_ledger(db)
        costs = db.execute("SELECT COALESCE(SUM(charges_paise),0) FROM paper_orders WHERE status IN ('filled','partial')").fetchone()[0] / 100
    finally:
        db.close()
    if cyc.empty:
        return {"account": account.name, "sessions": [], "series": {}, "note": "no traded session yet"}
    cyc["day"] = cyc.minute.str[:10]
    days = sorted(cyc.day.unique()) if sessions is None else sessions
    eq = cyc.groupby("day").equity_inr.last().reindex(days)
    start = float(meta.get("start_cash") or meta.get("capital") or eq.iloc[0])
    acct_r = (eq / eq.shift(1).fillna(start) - 1).fillna(0.0)
    syms = [s for s in (meta.get("symbols") or list(ledger.columns)) if s not in INDEX]
    hist = _closes_from_dataset(data_dir, [*syms, "NIFTY50"])
    closes = pd.concat([hist, ledger]).groupby(level=0).last().sort_index() if len(hist) or len(ledger) else pd.DataFrame()
    out = {"account": account.name, "sessions": days, "start_value": start, "series": {}, "notes": []}
    out["series"]["account (after costs)"] = {**stats(acct_r, start), "daily": [round(float(x), 6) for x in acct_r], "charges_inr": round(costs, 2)}
    out["series"]["cash"] = {**stats(pd.Series(0.0, index=days)), "daily": [0.0] * len(days)}
    if len(closes):
        rets = closes.pct_change()
        if "NIFTY50" in closes and closes.NIFTY50.reindex(days).notna().all():
            nr = rets.NIFTY50.reindex(days)
            out["series"]["NIFTY 50"] = {**stats(nr), "daily": [round(float(x), 6) for x in nr.fillna(0)]}
        else:
            out["notes"].append("NIFTY 50 not available for every session (the index is not stored with the live bars); shown when it is")
        have = [s for s in syms if s in closes]
        if have:
            bh = rets[have].reindex(days).mean(axis=1)
            out["series"]["buy and hold watchlist"] = {**stats(bh), "daily": [round(float(x), 6) for x in bh.fillna(0)]}
            sma = closes[have].rolling(TREND_N, min_periods=TREND_N).mean()
            sig = (closes[have] > sma).shift(1)                                 # yesterday's close decides today's holding
            tr = []
            prev_hold = None
            for d in days:
                if d not in rets.index:
                    tr.append(0.0)
                    continue
                hold = sig.loc[d].fillna(False).astype(bool) if d in sig.index else pd.Series(False, index=have)
                r = float(rets.loc[d, have][hold].mean()) if hold.any() else 0.0
                changed = int((hold != prev_hold).sum()) if prev_hold is not None else int(hold.sum())
                r -= TREND_COST * changed / max(len(have), 1)
                tr.append(r if math.isfinite(r) else 0.0)
                prev_hold = hold
            out["series"]["trend rule (above 20-day average)"] = {**stats(pd.Series(tr, index=days)), "daily": [round(x, 6) for x in tr]}
            if sig.loc[[d for d in days if d in sig.index]].isna().all().all():
                out["notes"].append("trend rule needs 20 prior daily closes per stock; until then it holds nothing")
    if len(days) < MIN_SESSIONS:
        out["notes"].append(f"{len(days)} session(s): risk-adjusted figures (Sharpe, Sortino) are not meaningful before {MIN_SESSIONS}")
    a = out["series"]["account (after costs)"]["return"]
    beat = [k for k, v in out["series"].items() if k != "account (after costs)" and a > v["return"]]
    lost = [k for k, v in out["series"].items() if k != "account (after costs)" and a <= v["return"]]
    out["plain"] = (f"Over {len(days)} session(s) the account returned {a:+.2%} after costs"
                    + (f"; ahead of {', '.join(beat)}" if beat else "") + (f"; behind {', '.join(lost)}" if lost else "") + ".")
    return out


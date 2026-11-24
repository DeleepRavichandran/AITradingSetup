"""Day backtest: every registered strategy, on its OWN, over one stored session (operator, 2026-11-24 "practice day").

The live account shares one pot of money, so most strategies never get capital and their result is unknown. Here
each configuration is run independently with its own Rs 1,00,000 (no shared portfolio, no churn limits), through the
same realistic execution (next-bar fills, participation cap, spread, impact, entry time-to-live) and dated NSE
charges. Earlier stored days only warm up the indicators; trades are counted on `day` only. The challenger is run
on the same bars with its own rules.

Multiple testing: with thousands of strategies on one day, many will show a profit by chance. The report gives the
share that made money, a luck baseline (the share that would be positive if every strategy's result were pure noise
around zero: about half of those that traded, by symmetry), and the best and worst ten, labelled as one-day results.
The useful output is broad: which TYPES lose or hold up across their many variants, not the single best strategy.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from nseql.screening.challenger import challenger_rules, factory
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.whatif import INDEX, _bars


def _per_config(db, day: str) -> pd.DataFrame:
    """Net result after charges per configuration for round trips that EXITED on `day`, plus open-at-end counts."""
    q = ("SELECT c.id AS config_id, c.symbol, c.timeframe, c.horizon, c.family, c.params, c.stop_pct, "
         "COALESCE(SUM(CASE WHEN o.side='SELL' AND o.fill_ts >= ? AND o.fill_ts < ? THEN o.realized_paise END), 0) / 100.0 AS net_inr, "
         "SUM(CASE WHEN o.side='SELL' AND o.fill_ts >= ? AND o.fill_ts < ? THEN 1 ELSE 0 END) AS trades, "
         "COALESCE(SUM(CASE WHEN o.fill_ts >= ? AND o.fill_ts < ? THEN o.charges_paise END), 0) / 100.0 AS charges_inr "
         "FROM configs c LEFT JOIN paper_orders o ON o.config_id = c.id AND o.status IN ('filled','partial') GROUP BY c.id")
    from nseql.screening.metrics import _next_day
    lo, hi = day, _next_day(day)
    return pd.read_sql_query(q, db, params=(lo, hi, lo, hi, lo, hi))


def _group(df: pd.DataFrame, by: str) -> list[dict]:
    traded = df[df.trades > 0]
    g = df.groupby(by).agg(strategies=("config_id", "size"), net_inr=("net_inr", "sum"), trades=("trades", "sum"), charges_inr=("charges_inr", "sum"))
    pos = traded.groupby(by).net_inr.apply(lambda x: float((x > 0).mean()) if len(x) else 0.0)
    trd = traded.groupby(by).size()
    out = []
    for k, r in g.iterrows():
        n = int(trd.get(k, 0))
        out.append({"group": str(k), "strategies": int(r.strategies), "traded": n, "trades": int(r.trades), "net_inr": round(float(r.net_inr), 2),
                    "charges_inr": round(float(r.charges_inr), 2), "net_per_trade": round(float(r.net_inr) / r.trades, 2) if r.trades else None,
                    "share_positive": round(float(pos.get(k, 0.0)), 3) if n else None})
    return sorted(out, key=lambda x: x["net_inr"])


def run(account: Path, day: str, out: Path) -> dict:
    account, out = Path(account), Path(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty")
    meta = json.loads((account / "live.json").read_text())
    syms = [s for s in meta["symbols"] if s not in INDEX]
    bars = [b for b in _bars(account / "screening.sqlite", f"{day}T23:59:59") if b["symbol"] in syms]
    if not any(str(b["ts"])[:10] == day for b in bars):
        raise ValueError(f"account {account.name} has no stored bars for {day}")
    live_from = f"{day}T09:15:00"
    # 1. every configuration standalone: realistic execution, no shared portfolio (each has its own Rs 1,00,000)
    allc = MinuteScheduler(out / "all", syms, portfolio=None, live_from=live_from, mode="replay")
    allc.verbose = False
    allc.ingest(bars, source="day-backtest replay of stored bars")
    allc.run_cycle(pd.Timestamp(f"{day}T15:31:00"))
    df = _per_config(allc.store.db, day)
    # 2. the challenger on the same bars, with its own rules and the same starting money as the live account
    cash = float(meta.get("start_cash") or meta.get("capital") or 100_000)
    ch = MinuteScheduler(out / "challenger", syms, portfolio=challenger_rules(cash), live_from=live_from, mode="replay", engine_factory=factory())
    ch.verbose = False
    ch.ingest(bars, source="day-backtest replay of stored bars")
    ch.run_cycle(pd.Timestamp(f"{day}T15:31:00"))
    cdf = _per_config(ch.store.db, day)
    traded = df[df.trades > 0]
    n_traded, n_pos = len(traded), int((traded.net_inr > 0).sum())
    cols = ["symbol", "family", "timeframe", "stop_pct", "params", "trades", "net_inr", "charges_inr"]
    rep = {
        "account": account.name, "day": day, "symbols": len(syms), "strategies": int(len(df)),
        "capital_each_inr": 100_000, "execution": "realistic (next-bar fills, volume cap, spread, impact, entry TTL), dated NSE charges",
        "traded": n_traded, "trades": int(df.trades.sum()), "net_inr": round(float(df.net_inr.sum()), 2), "charges_inr": round(float(df.charges_inr.sum()), 2),
        "positive": n_pos, "share_positive": round(n_pos / n_traded, 3) if n_traded else None,
        "luck_baseline": "if results were pure noise around zero, about half of the strategies that traded would be positive",
        "by_family": _group(df, "family"), "by_timeframe": _group(df, "timeframe"), "by_stop": _group(df.assign(stop=df.stop_pct.map(lambda v: f"{v:.1%}")), "stop"),
        "by_symbol": _group(df, "symbol"),
        "best_10": traded.nlargest(10, "net_inr")[cols].round(2).to_dict("records"),
        "worst_10": traded.nsmallest(10, "net_inr")[cols].round(2).to_dict("records"),
        "challenger": {"trades": int(cdf.trades.sum()), "net_inr": round(float(cdf.net_inr.sum()), 2), "charges_inr": round(float(cdf.charges_inr.sum()), 2),
                       "start_cash_inr": cash},
        "caution": "One day, in-sample, thousands of strategies: the best of them looks good partly by luck. Judge TYPES across their "
                   "variants and over many days, never the single best strategy of one day.",
    }
    (out / "day_backtest.json").write_text(json.dumps(rep, indent=2, default=str))
    return rep

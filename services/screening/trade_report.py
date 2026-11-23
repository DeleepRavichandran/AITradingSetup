"""Root-cause report for one paper day: every round trip (buy -> sell) with its exit reason, grouped.

Read-only on the account's screening.sqlite. Reports facts from the ledger (counts, P&L after charges,
win rate, holding time) per exit reason, strategy family, timeframe, stop size, stock, entry hour and
holding-time bucket, plus plain findings. A finding is a description of where money was lost on THIS day;
one day is too few trades to prove a cause or that a fix works.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

HOLD_BUCKETS = [(0, 2, "under 2 min"), (2, 5, "2-5 min"), (5, 15, "5-15 min"), (15, 60, "15-60 min"), (60, 10**6, "over 1 hour")]


def round_trips(db_path, day: str) -> pd.DataFrame:
    """db_path: the account's screening.sqlite path, or an open sqlite3 connection (left open)."""
    own = not isinstance(db_path, sqlite3.Connection)
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) if own else db_path
    try:
        o = pd.read_sql_query("SELECT o.order_key, o.config_id, o.symbol, o.side, o.decided_at, o.fill_ts, o.qty, o.price, o.charges_paise, "
                              "o.realized_paise, c.family, c.timeframe, c.stop_pct, c.params, s.reason AS exit_reason "
                              "FROM paper_orders o JOIN configs c ON c.id = o.config_id "
                              "LEFT JOIN signals s ON s.config_id = o.config_id AND s.bar_ts = o.decided_at "
                              "WHERE o.status IN ('filled','partial') AND substr(o.fill_ts,1,10) <= ? ORDER BY o.fill_ts", db, params=(day,))
    finally:
        if own:
            db.close()
    rows = []
    last_buy: dict[str, pd.Series] = {}
    for _, r in o.iterrows():
        if r.side == "BUY":
            last_buy[r.config_id] = r
            continue
        b = last_buy.pop(r.config_id, None)
        entry_ts = pd.Timestamp(b.fill_ts) if b is not None else pd.NaT
        exit_ts = pd.Timestamp(r.fill_ts)
        ch = (r.charges_paise or 0) + ((b.charges_paise or 0) if b is not None else 0)
        net = (r.realized_paise or 0) / 100
        rows.append({"config_id": r.config_id, "symbol": r.symbol, "family": r.family, "timeframe": r.timeframe, "stop_pct": r.stop_pct, "params": r.params,
                     "exit_reason": r.exit_reason or "unknown", "entry_ts": entry_ts, "exit_ts": exit_ts,
                     "entry_px": b.price if b is not None else None, "exit_px": r.price, "qty": r.qty,
                     "hold_min": (exit_ts - entry_ts).total_seconds() / 60 if b is not None else None,
                     "net_inr": net, "charges_inr": ch / 100, "gross_inr": net + ch / 100})
    # pair across dates FIRST, then keep the round trips that EXITED on `day` (review 2026-11-24, finding 6: an overnight
    # swing exit used to lose its entry time and buy-side charges because orders were filtered by day before pairing)
    rt = pd.DataFrame(rows)
    if not rt.empty:
        rt = rt[rt.exit_ts.dt.strftime("%Y-%m-%d") == day].reset_index(drop=True)
    rt.attrs["open_buys"] = len(last_buy)
    return rt


def _group(rt: pd.DataFrame, by: str) -> list[dict]:
    g = rt.groupby(by, dropna=False).agg(trades=("net_inr", "size"), net_inr=("net_inr", "sum"), gross_inr=("gross_inr", "sum"),
                                         charges_inr=("charges_inr", "sum"), win_rate=("net_inr", lambda x: float((x > 0).mean())),
                                         avg_hold_min=("hold_min", "mean"))
    g = g.sort_values("net_inr")
    return [{"group": str(k), **{c: round(float(v), 2) for c, v in row.items()}} for k, row in g.iterrows()]


def report(account: Path, day: str) -> dict:
    rt = round_trips(Path(account) / "screening.sqlite", day)
    if rt.empty:
        raise ValueError(f"no completed round trips on {day} in {Path(account).name}")
    rt["entry_hour"] = rt.entry_ts.dt.strftime("%H:00").fillna("unknown")
    rt["hold"] = rt.hold_min.apply(lambda m: next((lab for lo, hi, lab in HOLD_BUCKETS if m is not None and lo <= m < hi), "unknown"))
    rt["stop"] = rt.stop_pct.map(lambda x: f"{x:.1%}")
    tot = {"round_trips": len(rt), "net_inr": round(rt.net_inr.sum(), 2), "gross_inr": round(rt.gross_inr.sum(), 2),
           "charges_inr": round(rt.charges_inr.sum(), 2), "win_rate": round(float((rt.net_inr > 0).mean()), 3),
           "avg_win_inr": round(float(rt.net_inr[rt.net_inr > 0].mean() or 0), 2), "avg_loss_inr": round(float(rt.net_inr[rt.net_inr <= 0].mean() or 0), 2),
           "median_hold_min": round(float(rt.hold_min.median()), 1), "buys_without_sell": int(rt.attrs.get("open_buys", 0))}
    groups = {k: _group(rt, k) for k in ("exit_reason", "family", "timeframe", "stop", "symbol", "entry_hour", "hold")}
    f = []
    stop = rt[rt.exit_reason == "stop"]
    if len(stop):
        f.append(f"Stop-loss exits: {len(stop)} of {len(rt)} trades ({len(stop) / len(rt):.0%}), net {stop.net_inr.sum():,.0f} INR.")
    quick = rt[rt.hold_min < 5]
    if len(quick):
        f.append(f"Held under 5 minutes: {len(quick)} trades ({len(quick) / len(rt):.0%}), net {quick.net_inr.sum():,.0f} INR, "
                 f"charges {quick.charges_inr.sum():,.0f} INR.")
    if tot["gross_inr"] < 0:
        f.append(f"Before charges the trades lost {tot['gross_inr']:,.0f} INR; charges added {tot['charges_inr']:,.0f} INR. "
                 "The entries themselves lost money, not only the costs.")
    else:
        f.append(f"Before charges the trades made {tot['gross_inr']:,.0f} INR; charges of {tot['charges_inr']:,.0f} INR turned it into a loss.")
    for k, label in (("family", "strategy type"), ("timeframe", "chart speed"), ("symbol", "stock")):
        w = groups[k][0]
        f.append(f"Worst {label}: {w['group']} ({int(w['trades'])} trades, net {w['net_inr']:,.0f} INR, win rate {w['win_rate']:.0%}).")
    rep_tf = rt.groupby("timeframe").size()
    if "1m" in rep_tf and rep_tf["1m"] / len(rt) > 0.5:
        f.append(f"{rep_tf['1m'] / len(rt):.0%} of trades came from 1-minute rules.")
    from nseql.screening.rca import analyse, summary
    con = sqlite3.connect(f"file:{Path(account) / 'screening.sqlite'}?mode=ro", uri=True)
    try:
        rca = summary(analyse(con, day, write=False))
    finally:
        con.close()
    worst = rt.nsmallest(10, "net_inr")[["symbol", "family", "timeframe", "stop", "exit_reason", "entry_ts", "exit_ts", "hold_min", "net_inr"]]
    return {"account": Path(account).name, "day": day, "totals": tot, "findings": f, "by": groups, "rca": rca,
            "worst_10": [{k: (str(v) if isinstance(v, pd.Timestamp) else (round(v, 2) if isinstance(v, float) else v)) for k, v in r.items()}
                         for r in worst.to_dict("records")],
            "caution": "Facts from one day of paper trading. They show where money was lost, not proven causes; judge fixes on later days."}

"""Robustness checks before trusting any result (operator, 2026-11-24): Monte Carlo trade-order shuffle,
walk-forward, parameter sensitivity.

1. Monte Carlo (sequence risk). The account's closed trades (net after costs) are reshuffled N times (seeded);
   each order gives an equity path from the starting capital and a maximum drawdown. Total P&L does not change,
   only the sequence. Flag: if the 95th-percentile drawdown is at least twice the drawdown that happened, a live
   account should expect to see it eventually.
2. Walk-forward (edge vs memory). Over a small, predeclared grid of challenger parameters, the best variant on one
   window (in-sample) is tested on the next window it has never seen (out-of-sample), window after window. The
   frozen default is also scored out-of-sample. Flag: out-of-sample results that collapse relative to in-sample, or
   are negative, mean the selection is memorising, not finding an edge.
3. Parameter sensitivity (plateau vs spike). Each challenger parameter is nudged one step either side of the frozen
   default, one at a time. A plateau keeps the same sign and at least half of the baseline result at every
   neighbour; anything else is a lonely spike. A losing baseline has no edge to test and is reported as such.

The incumbent screening trader runs thousands of fixed rules without a parameter search, so walk-forward and
sensitivity apply to the challenger; its counterpart for the backtester is the Walk-Forward workspace.
Results are labelled with their data kind; synthetic results say nothing about real markets.
"""
from __future__ import annotations

import json
import math
import tempfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from nseql.screening.challenger import CHALLENGER_CONFIG, ChallengerConfig, challenger_rules, factory
from nseql.screening.contest import KINDS, account_metrics
from nseql.screening.scheduler import MinuteScheduler

MC = {"shuffles": 1000, "seed": 20261124, "flag_multiple": 2.0}
WF_GRID = {"or_minutes": (15, 30, 45), "breadth_threshold": (0.4, 0.5, 0.6)}
SENSITIVITY = {"or_minutes": (25, 35), "breadth_threshold": (0.45, 0.55), "min_breadth_coverage": (0.7, 0.9),
               "risk_per_trade": (0.002, 0.003), "fresh_minutes": (1, 3)}
PLATEAU_KEEP = 0.5


# ---------------------------------------------------------------------------------------------------- 1. Monte Carlo
def trade_pnls(db) -> list[float]:
    """Closed round trips (net after both legs' charges) in exit order, from an account ledger."""
    rows = db.execute("SELECT realized_paise FROM paper_orders WHERE side='SELL' AND status IN ('filled','partial') ORDER BY fill_ts").fetchall()
    return [float(r[0] or 0) / 100 for r in rows]


def max_drawdown(pnls: np.ndarray, capital: float) -> float:
    eq = capital + np.concatenate([[0.0], np.cumsum(pnls)])
    peak = np.maximum.accumulate(eq)
    return float((eq / peak - 1).min())


def monte_carlo(pnls: list[float], capital: float, cfg: dict = MC) -> dict:
    p = np.asarray(pnls, dtype=float)
    if len(p) < 2:
        return {"trades": int(len(p)), "status": "not enough trades to shuffle"}
    actual = max_drawdown(p, capital)
    rng = np.random.default_rng(cfg["seed"])
    dds = np.array([max_drawdown(rng.permutation(p), capital) for _ in range(cfg["shuffles"])])
    p95 = float(np.quantile(dds, 0.05))                      # drawdowns are negative: the 5% quantile is the 95th-worst
    worse2 = float((dds <= cfg["flag_multiple"] * actual).mean()) if actual < 0 else float((dds < 0).mean())
    flag = actual < 0 and p95 <= cfg["flag_multiple"] * actual
    return {"trades": int(len(p)), "net_pnl_inr": round(float(p.sum()), 2), "actual_max_drawdown": round(actual, 6),
            "median": round(float(np.median(dds)), 6), "p95_worst": round(p95, 6), "worst": round(float(dds.min()), 6),
            "share_at_least_double": round(worse2, 4), "sequence_risk": bool(flag), "shuffles": cfg["shuffles"], "seed": cfg["seed"],
            "plain": (f"1 in 20 orderings reaches a drawdown of {abs(p95):.1%} (actual {abs(actual):.1%})"
                      + ("; that is at least double: expect it live eventually" if flag else ""))}


# ---------------------------------------------------------------------------------------------------- runs
def _run_variant(args) -> dict:
    """One challenger run over the whole stream (top-level so it can run in a worker process)."""
    params, stream, syms, capital, days = args
    cfg = replace(CHALLENGER_CONFIG, **params)
    with tempfile.TemporaryDirectory() as d:
        s = MinuteScheduler(Path(d), syms, portfolio=challenger_rules(capital), engine_factory=factory(cfg), mode="replay")
        s.verbose = False
        for t, bars in stream:
            s.ingest(bars)
            s.run_cycle(pd.Timestamp(t) + pd.Timedelta(seconds=6))
        m = account_metrics(s, capital, days)
        pn = trade_pnls(s.store.db)
    return {"params": params, "daily": [x["return"] for x in m["daily"]], "net_pnl_inr": m["net_pnl_inr"], "trades": m["trades_closed"],
            "max_drawdown": m["max_drawdown"], "pnls": pn}


def run_variants(variants: list[dict], stream, syms, capital, days, workers: int = 4) -> list[dict]:
    jobs = [(v, stream, syms, capital, days) for v in variants]
    if workers <= 1:
        return [_run_variant(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_run_variant, jobs))


def _key(p: dict) -> str:
    return json.dumps(p, sort_keys=True)


def _score(daily: list[float]) -> float:
    return float(np.sum(np.log1p(daily))) if daily else 0.0          # compounded return over a window


# ---------------------------------------------------------------------------------------------------- 2. walk-forward
def walk_forward(results: dict[str, dict], days: list[str], window: int) -> dict:
    grid = [{"or_minutes": a, "breadth_threshold": b} for a in WF_GRID["or_minutes"] for b in WF_GRID["breadth_threshold"]]
    base = _key({})
    folds = []
    for start in range(0, len(days) - 2 * window + 1, window):
        ins, oos = slice(start, start + window), slice(start + window, start + 2 * window)
        scored = sorted(grid, key=lambda g: (-_score(results[_key(g)]["daily"][ins]), _key(g)))
        best = scored[0]
        folds.append({"in_sample": [days[ins.start], days[ins.stop - 1]], "out_of_sample": [days[oos.start], days[oos.stop - 1]],
                      "chosen": best, "in_sample_return": _score(results[_key(best)]["daily"][ins]),
                      "out_of_sample_return": _score(results[_key(best)]["daily"][oos]),
                      "default_out_of_sample_return": _score(results[base]["daily"][oos])})
    if not folds:
        return {"status": f"need at least {2 * window} sessions for one fold", "folds": []}
    ins_m = float(np.mean([f["in_sample_return"] for f in folds]))
    oos_m = float(np.mean([f["out_of_sample_return"] for f in folds]))
    eff = oos_m / ins_m if ins_m > 0 else None
    memory = oos_m <= 0 or (eff is not None and eff < 0.5)
    return {"window_sessions": window, "folds": folds, "grid": WF_GRID, "mean_in_sample": ins_m, "mean_out_of_sample": oos_m,
            "efficiency": eff, "default_mean_out_of_sample": float(np.mean([f["default_out_of_sample_return"] for f in folds])),
            "verdict": "memory, not edge" if memory else "holds out of sample",
            "plain": (f"best-in-window settings made {ins_m:+.2%} per window in-sample and {oos_m:+.2%} on the next, unseen window"
                      + (" - it only works where it was optimised" if memory else ""))}


# ---------------------------------------------------------------------------------------------------- 3. sensitivity
def sensitivity(results: dict[str, dict]) -> dict:
    base = results[_key({})]
    b = _score(base["daily"])
    rows = []
    for p, vals in SENSITIVITY.items():
        for v in vals:
            r = results[_key({p: v})]
            rows.append({"parameter": p, "value": v, "default": getattr(CHALLENGER_CONFIG, p), "return": _score(r["daily"]),
                         "net_pnl_inr": r["net_pnl_inr"], "trades": r["trades"],
                         "no_effect": bool(np.allclose(r["daily"], base["daily"]) and r["trades"] == base["trades"])})
    if b <= 0:
        verdict, plain = "no edge to test", f"the frozen default lost money ({b:+.2%}): there is no edge whose stability could be checked"
    else:
        ok = all(x["return"] > 0 and x["return"] >= PLATEAU_KEEP * b for x in rows)
        verdict = "plateau" if ok else "lonely spike"
        weak = [f"{x['parameter']}={x['value']}" for x in rows if not (x["return"] > 0 and x["return"] >= PLATEAU_KEEP * b)]
        plain = "every neighbour keeps at least half the result" if ok else f"falls apart at {', '.join(weak)}"
    inert = sorted({x["parameter"] for x in rows if x["no_effect"]})
    if inert:
        plain += f"; no effect on this data (a nudge changed no decision): {', '.join(inert)}"
    return {"baseline_return": b, "neighbours": rows, "keep": PLATEAU_KEEP, "verdict": verdict, "plain": plain, "no_effect": inert}


# ---------------------------------------------------------------------------------------------------- protocol
def run(root: Path, stream: list[tuple], syms: list[str], capital: float, kind: str, window: int = 5, workers: int = 4,
        incumbent_pnls: list[float] | None = None, data: dict | None = None) -> dict:
    days = sorted({pd.Timestamp(t).strftime("%Y-%m-%d") for t, _ in stream})
    variants = [{}] + [{"or_minutes": a, "breadth_threshold": b} for a in WF_GRID["or_minutes"] for b in WF_GRID["breadth_threshold"]
                       if not (a == CHALLENGER_CONFIG.or_minutes and b == CHALLENGER_CONFIG.breadth_threshold)]
    variants += [{p: v} for p, vals in SENSITIVITY.items() for v in vals]
    out = run_variants(variants, stream, syms, capital, days, workers)
    results = {_key(r["params"]): r for r in out}
    results[_key({"or_minutes": CHALLENGER_CONFIG.or_minutes, "breadth_threshold": CHALLENGER_CONFIG.breadth_threshold})] = results[_key({})]
    rep = {"kind": kind, "kind_label": KINDS[kind], "sessions": days, "capital": capital, "symbols": len(syms), "data": data or {},
           "challenger": CHALLENGER_CONFIG.version, "variants_run": len(out),
           "monte_carlo": {"challenger": monte_carlo(results[_key({})]["pnls"], capital)},
           "walk_forward": walk_forward(results, days, window), "sensitivity": sensitivity(results)}
    if incumbent_pnls is not None:
        rep["monte_carlo"]["incumbent"] = monte_carlo(incumbent_pnls, capital)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.json").write_text(json.dumps(rep, indent=2, default=str))
    return rep


def run_fixture(root: Path, symbols_n: int = 8, days_n: int = 20, window: int = 5, seed: int = 7, capital: float = 100_000,
                workers: int = 4, incumbent: bool = True) -> dict:
    from nseql.screening.execution import PortfolioRules
    from nseql.screening.synthetic import minute_stream, symbols
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=days_n)]
    syms = symbols(symbols_n)
    stream = minute_stream(symbols_n, days, seed=seed)
    inc = None
    if incumbent:
        with tempfile.TemporaryDirectory() as d:
            s = MinuteScheduler(Path(d), syms, portfolio=PortfolioRules.live(capital), mode="replay")
            s.verbose = False
            for t, bars in stream:
                s.ingest(bars)
                s.run_cycle(pd.Timestamp(t) + pd.Timedelta(seconds=6))
            inc = trade_pnls(s.store.db)
    return run(root, stream, syms, capital, "synthetic", window, workers, inc,
               {"source": "nseql.screening.synthetic.minute_stream", "seed": seed, "symbols": symbols_n})


def ledger_monte_carlo(account: Path, capital: float | None = None) -> dict:
    """Monte Carlo on an existing paper account's closed trades (e.g. the live account)."""
    import sqlite3
    account = Path(account)
    con = sqlite3.connect(f"file:{account / 'screening.sqlite'}?mode=ro", uri=True)
    try:
        pn = trade_pnls(con)
    finally:
        con.close()
    if capital is None and (account / "live.json").exists():
        m = json.loads((account / "live.json").read_text())
        capital = float(m.get("start_cash") or m.get("capital") or 100_000)
    return {"account": account.name, **monte_carlo(pn, capital or 100_000)}


assert math.isfinite(PLATEAU_KEEP)
assert isinstance(CHALLENGER_CONFIG, ChallengerConfig)

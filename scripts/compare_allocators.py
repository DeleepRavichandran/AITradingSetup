"""Compare the ranked allocator with the first-come (registry order) baseline on the SAME synthetic streams.

Each run: warm-up days build forward evidence (both allocators see the same signals, but the evidence they
collect differs because they take different trades); metrics are measured ONLY on the evaluation days that
follow. Seeds here are not used by any test or during development (unseen data). No parameter was tuned on
these runs. SYNTHETIC data: says nothing about real markets.

    python scripts/compare_allocators.py --symbols 20 --seeds 201 202 203 204 205 --out docs/benchmarks/allocator_comparison.json
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd
from nseql.screening.execution import PortfolioRules
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import factor_stream, minute_stream, symbols

WARM = ["2026-11-02", "2026-11-03"]
EVAL = ["2026-11-04", "2026-11-05", "2026-11-06"]


def run(stream, n_sym: int, allocation: str, root: Path) -> dict:
    sch = MinuteScheduler(root, symbols(n_sym), portfolio=PortfolioRules(allocation=allocation))
    t0, eval_start = time.perf_counter(), pd.Timestamp(EVAL[0])
    lat = []
    for ts, bars in stream:
        sch.ingest(bars)
        c = sch.run_cycle(ts + pd.Timedelta(seconds=6))
        lat.append(c.get("latency_ms", 0) if isinstance(c, dict) else 0)
    db = sch.store.db
    t = eval_start.strftime("%Y-%m-%dT%H:%M:%S")
    eq = [r[0] for r in db.execute("SELECT equity_inr FROM cycles WHERE minute >= ? AND equity_inr IS NOT NULL ORDER BY minute", (t,))]
    start = db.execute("SELECT equity_inr FROM cycles WHERE minute < ? AND equity_inr IS NOT NULL ORDER BY minute DESC LIMIT 1", (t,)).fetchone()[0]
    curve = np.array([start, *eq])
    dd = float(np.max(1 - curve / np.maximum.accumulate(curve)))
    q = lambda s, *a: db.execute(s, a).fetchall()
    buys = q("SELECT bar_ts, COUNT(*) FROM signals WHERE decision='BUY' AND bar_ts >= ? GROUP BY bar_ts", t)
    status = dict(q("SELECT alloc_status, COUNT(*) FROM signals WHERE decision='BUY' AND bar_ts >= ? GROUP BY 1", t))
    sym_entries = q("SELECT symbol, COUNT(*) FROM paper_orders WHERE side='BUY' AND decided_at >= ? AND status != 'rejected' GROUP BY symbol", t)
    n_ent = sum(n for _, n in sym_entries)
    real = q("SELECT COALESCE(SUM(realized_paise), 0), COALESCE(SUM(charges_paise), 0), COUNT(*) FROM paper_orders "
             "WHERE side='SELL' AND decided_at >= ? AND status IN ('filled', 'partial')", t)[0]
    alloc = sch.engine.x.snapshot()["allocation"]
    return {"net_pnl_inr": round(float(curve[-1] - start), 2), "return_pct": round(float(curve[-1] / start - 1) * 100, 4),
            "max_drawdown_pct": round(dd * 100, 4), "realised_after_charges_inr": real[0] / 100, "charges_inr": real[1] / 100,
            "closed_exits": real[2], "entries": n_ent, "distinct_symbols": len(sym_entries),
            "top_symbol_share_of_entries": round(max((n for _, n in sym_entries), default=0) / max(n_ent, 1), 4),
            "buy_signals": sum(n for _, n in buys), "busiest_minute_buys": max((n for _, n in buys), default=0),
            "minutes_with_10plus_buys": sum(1 for _, n in buys if n >= 10), "allocation_outcomes": status,
            "round_trips_seen": alloc["round_trips"], "qualified_configs_at_end": alloc["qualified_configs"],
            "calibration": alloc["calibration"], "p99_cycle_ms": round(float(np.percentile(lat, 99)), 2),
            "wall_s": round(time.perf_counter() - t0, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--seeds", type=int, nargs="+", default=[201, 202, 203, 204, 205])
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    out = {"generated": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(timespec="seconds"), "symbols": a.symbols,
           "configs": a.symbols * 171, "warmup_days": WARM, "evaluation_days": EVAL, "seeds": a.seeds,
           "data": "SYNTHETIC. Not market data; results say nothing about real profitability.", "scenarios": {}}
    gens = {"random_walk": lambda s: minute_stream(a.symbols, WARM + EVAL, seed=s, missing=0.02),
            "common_factor_with_persistent_edges": lambda s: factor_stream(a.symbols, WARM + EVAL, seed=s)}
    for name, gen in gens.items():
        rows = []
        for seed in a.seeds:
            stream = gen(seed)
            row = {"seed": seed}
            for mode in ("first_come", "ranked"):
                with tempfile.TemporaryDirectory() as d:
                    row[mode] = run(stream, a.symbols, mode, Path(d))
                print(name, seed, mode, {k: row[mode][k] for k in ("net_pnl_inr", "max_drawdown_pct", "entries", "distinct_symbols", "busiest_minute_buys")}, flush=True)
            rows.append(row)
        diff = [r["ranked"]["net_pnl_inr"] - r["first_come"]["net_pnl_inr"] for r in rows]
        out["scenarios"][name] = {"runs": rows, "ranked_minus_first_come_pnl_inr": diff,
                                  "ranked_better_in": f"{sum(d > 0 for d in diff)} of {len(diff)} seeds",
                                  "mean_difference_inr": round(float(np.mean(diff)), 2)}
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(out, indent=2))
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "runs"} for k, v in out["scenarios"].items()}, indent=2))


if __name__ == "__main__":
    main()

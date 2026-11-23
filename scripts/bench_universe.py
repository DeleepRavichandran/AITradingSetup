"""Strategy Universe read-model latency at 1k / 10k / 50k configurations (SYNTHETIC stream).

For each size: run one synthetic session through the real scheduler (realistic execution, shared
portfolio, ranked allocation) so configurations have evaluations, signals and trades; then time the
functions behind the API (first call; after each new minute, when per-minute columns are rebuilt; cached):
  universe.summary  (/api/universe/summary, also inside /api/command-center)
  universe.page     (/api/universe/configs, 500 rows, filtered and sorted)
  universe.dossier  (/api/universe/configs/{id})
Measures Python call time in-process, not HTTP; one machine; not a load test.

    python scripts/bench_universe.py --out docs/benchmarks/universe_api_2026-11-22.json
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path

import pandas as pd
from nseql.screening import universe
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols


def timed(fn, n=5):
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000)
    return out


def run(n_configs: int) -> dict:
    n_sym = -(-n_configs // 171)
    stream = minute_stream(n_sym, ["2026-11-16"], seed=11)
    with tempfile.TemporaryDirectory() as d:
        sch = MinuteScheduler(Path(d), symbols(n_sym), n_configs)
        t0 = time.perf_counter()
        for ts, bars in stream:
            sch.ingest(bars)
            sch.run_cycle(ts + pd.Timedelta(seconds=6))
        replay_s = time.perf_counter() - t0
        universe._cache.clear()
        universe._static_cache.clear()
        t = time.perf_counter()
        s = universe.summary(sch)
        first = (time.perf_counter() - t) * 1000                      # includes the one-off static columns
        def new_minute():
            universe._cache.clear()                                   # what each newly processed minute costs
            universe.summary(sch)
        minute = timed(new_minute)
        warm = timed(lambda: universe.summary(sch))
        page = timed(lambda: universe.page(sch, horizon="intraday", sort="mean_net", limit=500))
        search = timed(lambda: universe.page(sch, q="syn00", sort="trades", limit=500))
        top = universe.page(sch, sort="trades", limit=1)["rows"][0]["id"]
        dossier = timed(lambda: universe.dossier(sch, top))
        return {"configurations": n_configs, "symbols": n_sym, "replay_one_session_s": round(replay_s, 1),
                "with_trades": s["by_stage"]["forward_evidence"] + s["by_stage"]["qualified"] + s["by_stage"]["gated"],
                "summary_first_call_ms": round(first, 1), "summary_after_new_minute_ms_median": round(statistics.median(minute), 1),
                "summary_cached_ms_median": round(statistics.median(warm), 2),
                "page_500_sorted_ms_median": round(statistics.median(page), 1), "search_ms_median": round(statistics.median(search), 1),
                "dossier_ms_median": round(statistics.median(dossier), 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000, 50000])
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    rows = []
    for n in a.sizes:
        rows.append(run(n))
        print(rows[-1], flush=True)
    out = {"generated": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(timespec="seconds"), "machine": f"{platform.machine()} {platform.processor() or ''}".strip(),
           "python": platform.python_version(), "data": "SYNTHETIC one-session stream; in-process call timing, not HTTP", "rows": rows}
    if a.out:
        a.out.write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

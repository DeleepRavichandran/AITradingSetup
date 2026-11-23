"""Watchlist size vs the 1-minute poller (MODELLED, not measured against Angel One).

Runs the real BrokerMinutePoller against a fake API on a simulated clock: each request costs
max(request spacing, round-trip latency). Reports, per watchlist size, the cycle time, whether every symbol
is polled each minute, the rotation period when not, and the worst-case data age that follows. The real
service's latency and limits must be measured with `nseql angel-verify` before relying on these numbers.

    python scripts/bench_poller.py --out docs/benchmarks/poller_capacity_2026-11-21.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from nseql.screening.feeds import BrokerMinutePoller


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spacing", type=float, default=0.4)
    ap.add_argument("--latency", type=float, default=0.15)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    clock = Clock()

    class Api:
        def candles(self, token, interval, start, end):
            clock.t += max(a.spacing, a.latency)
            e = pd.Timestamp(end).floor("1min")
            return pd.DataFrame({"ts": [e], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]})

    rows = []
    for n in (10, 20, 50, 100, 125, 150, 250, 500, 1000):
        toks = [{"symbol": f"S{i}", "token": str(i)} for i in range(n)]
        p = BrokerMinutePoller(toks, lambda: Api(), min_interval_s=a.spacing, clock=clock)
        p.fetch(pd.Timestamp("2026-11-23 10:00:05"))
        h = p.health(pd.Timestamp("2026-11-23 10:00:05"), market_open=True)
        rot = h["rotation_minutes"]
        rows.append({"symbols": n, "requests_per_cycle": h["last_cycle"]["requests"], "cycle_seconds": h["last_cycle"]["seconds"],
                     "every_symbol_every_minute": not h["degraded"], "rotation_minutes": rot,
                     "worst_case_bar_age_minutes": rot + 1})
        print(rows[-1])
    out = {"modelled": True, "note": "Simulated clock; request spacing and latency are assumptions, not Angel One measurements.",
           "request_spacing_s": a.spacing, "assumed_latency_s": a.latency, "cycle_budget_s": 50.0,
           "capacity_symbols_per_minute": rows[0] and BrokerMinutePoller([], None, min_interval_s=a.spacing).capacity, "rows": rows}
    if a.out:
        a.out.write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

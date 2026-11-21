#!/usr/bin/env python3
"""Measure how far the margin approximation is from real exchange/broker margin figures.

    python scripts/reconcile_margin.py <dataset_id> examples.json

examples.json is a list of {"name", "positions": [{"symbol", "qty"}], "marks": {symbol: price},
"exchange_margin": rupees}, where exchange_margin is a figure YOU took from your broker's margin
calculator or the exchange's SPAN output for those exact positions and prices. Nothing is fetched.
"""
import json
import sys
from pathlib import Path

from nseql.backtester.margin import reconcile
from nseql.config import get_settings
from nseql.data.store import Dataset
from nseql.instruments.rules import MarginRules


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    ds = Dataset(get_settings().data_dir / sys.argv[1])
    rows = reconcile(json.loads(Path(sys.argv[2]).read_text()), ds.instruments, MarginRules())
    for r in rows:
        dev = "n/a" if r["deviation_pct"] is None else f"{r['deviation_pct']:+.1%}"
        print(f"{r['name'][:50]:50s} model {r['model_margin']:>14,.0f}  exchange {r['exchange_margin']:>14,.0f}  deviation {dev}")
    worst = max((abs(r["deviation_pct"]) for r in rows if r["deviation_pct"] is not None), default=0)
    print(f"\nlargest absolute deviation: {worst:.1%}. Under-margining (negative deviation) overstates the leverage a backtest can use.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""SYNTHETIC 1-minute bars for load tests of the screening loop. Random walks, not market data; every
benchmark that uses them says so."""
from __future__ import annotations

import numpy as np
import pandas as pd

from nseql.screening.features import SESSION_MINUTES, SESSION_OPEN_MIN


def minute_stream(n_symbols: int, days: list[str], *, seed: int = 0, missing: float = 0.02) -> list[tuple[pd.Timestamp, list[dict]]]:
    """[(minute close time, [bar dicts])] in time order. `missing` = share of symbol-minutes with no trade."""
    rng = np.random.default_rng(seed)
    syms = [f"SYN{i:03d}" for i in range(n_symbols)]
    px = rng.uniform(80, 3000, n_symbols)
    out = []
    for day in days:
        d0 = pd.Timestamp(day)
        for k in range(1, SESSION_MINUTES + 1):
            ts = d0 + pd.Timedelta(minutes=SESSION_OPEN_MIN + k)
            r = rng.normal(0, 0.0012, n_symbols)
            o = px
            c = np.round(px * np.exp(r), 2)
            h = np.round(np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.0005, n_symbols))), 2)
            lo = np.round(np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.0005, n_symbols))), 2)
            v = rng.integers(100, 5000, n_symbols)
            keep = rng.random(n_symbols) >= missing
            out.append((ts, [{"symbol": syms[j], "ts": ts, "open": float(o[j]), "high": float(h[j]), "low": float(lo[j]),
                              "close": float(c[j]), "volume": float(v[j])} for j in np.flatnonzero(keep)]))
            px = c
    return out


def symbols(n: int) -> list[str]:
    return [f"SYN{i:03d}" for i in range(n)]

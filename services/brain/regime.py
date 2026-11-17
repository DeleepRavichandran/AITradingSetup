"""Point-in-time market regime: volatility tercile and trend of the benchmark index.

Tercile thresholds are computed from the index's own history UP TO the decision date (expanding
window), so a regime label never uses future volatility."""
from __future__ import annotations

import numpy as np

VOL = ("lowvol", "midvol", "highvol", "unknown")
TREND = ("up", "down", "unknown")


def regime_at(P, d: int) -> dict:
    rv = np.asarray(P["idx_rv20"][: d + 1], dtype=float)
    hist = rv[np.isfinite(rv)]
    cur = rv[-1] if len(rv) else np.nan
    if len(hist) >= 60 and np.isfinite(cur):
        lo, hi = np.quantile(hist, [1 / 3, 2 / 3])
        vol = "lowvol" if cur <= lo else "midvol" if cur <= hi else "highvol"
    else:
        lo = hi = None
        vol = "unknown"
    ic, sma = float(P["idx_close"][d]), float(P["idx_sma_200"][d])
    trend = "unknown" if not np.isfinite(sma) else ("up" if ic > sma else "down")
    return {"label": f"{vol}_{trend}", "vol": vol, "trend": trend, "idx_rv20": None if not np.isfinite(cur) else float(cur),
            "thresholds": None if lo is None else [float(lo), float(hi)]}

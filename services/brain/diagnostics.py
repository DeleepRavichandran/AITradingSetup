"""In-sample diagnostics for screened configurations, computed strictly from data up to the
decision date. `HistoryView` is the only door to the result matrices that a research arm gets:
it refuses any row after the decision row."""
from __future__ import annotations

import math

import numpy as np
from scipy.stats import norm

FEATURES = ("sh250", "sh120", "sh60", "vol250", "maxdd250", "stab", "turnover", "exposure", "cost_drag", "tstat")
IS_WINDOW = 250


class LookAheadError(RuntimeError):
    pass


class HistoryView:
    def __init__(self, R, G, E, upto: int, cost: float):
        self._R, self._G, self._E, self.upto, self.cost = R, G, E, int(upto), cost

    def window(self, idx, n: int, which: str = "R") -> np.ndarray:
        lo = max(self.upto - n + 1, 0)
        src = {"R": self._R, "G": self._G, "E": self._E}[which]
        return np.asarray(src[np.asarray(idx)][:, lo:self.upto + 1], dtype=np.float64)

    def rows(self, idx, start: int, stop: int, which: str = "R") -> np.ndarray:
        if stop > self.upto + 1:
            raise LookAheadError(f"row {stop - 1} is after the decision row {self.upto}")
        src = {"R": self._R, "G": self._G, "E": self._E}[which]
        return np.asarray(src[np.asarray(idx)][:, start:stop], dtype=np.float64)


def _sharpe(x: np.ndarray) -> np.ndarray:
    m, s = x.mean(axis=1), x.std(axis=1, ddof=1)
    return np.where(s > 1e-12, m / np.where(s > 1e-12, s, 1) * math.sqrt(252), 0.0)


def diagnostics(view: HistoryView, idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """-> (features [B, len(FEATURES)], one-sided p-value that the mean daily return is <= 0)."""
    R = view.window(idx, IS_WINDOW)
    G = view.window(idx, IS_WINDOW, "G")
    E = view.window(idx, IS_WINDOW, "E")
    n = R.shape[1]
    cum = np.cumsum(R, axis=1)
    dd = (cum - np.maximum.accumulate(cum, axis=1)).min(axis=1)
    k = max(n // 5, 1)
    blocks = np.stack([R[:, i * k:(i + 1) * k].mean(axis=1) for i in range(5)], axis=1) if n >= 5 else R
    s = R.std(axis=1, ddof=1)
    t = np.where(s > 1e-12, R.mean(axis=1) / np.where(s > 1e-12, s, 1) * math.sqrt(n), 0.0)
    gross = G.sum(axis=1)
    cost = (G - R).sum(axis=1)
    X = np.column_stack([
        _sharpe(R), _sharpe(R[:, -120:]), _sharpe(R[:, -60:]), s * math.sqrt(252), dd, (blocks > 0).mean(axis=1),
        (G - R).mean(axis=1) / max(view.cost, 1e-9), E.mean(axis=1), np.clip(cost / (np.abs(gross) + 1e-6), 0, 5), t])
    return np.nan_to_num(X), norm.sf(t)

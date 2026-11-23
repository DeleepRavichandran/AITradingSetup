"""Causal indicators.

Every function takes the history available *now* (oldest first, last element = the
bar that just closed) and returns the indicator value for that last bar only, or NaN
when there is not enough history. Because they are functions of a trailing window and
never see anything after the last element, they cannot look ahead.
`series_*` variants return a full causal series (value i depends only on x[:i+1]).
"""
from __future__ import annotations

import math

import numpy as np

NaN = float("nan")


def _a(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


def sma(x, n: int) -> float:
    x = _a(x)
    return float(x[-n:].mean()) if len(x) >= n and n > 0 else NaN


def ema(x, n: int) -> float:
    s = series_ema(x, n)
    return float(s[-1]) if len(s) else NaN


def series_ema(x, n: int) -> np.ndarray:
    x = _a(x)
    out = np.full(len(x), np.nan)
    if len(x) < n or n <= 0:
        return out
    k = 2.0 / (n + 1)
    v = x[:n].mean()
    out[n - 1] = v
    for i in range(n, len(x)):
        v = x[i] * k + v * (1 - k)
        out[i] = v
    return out


def series_sma(x, n: int) -> np.ndarray:
    x = _a(x)
    out = np.full(len(x), np.nan)
    if len(x) >= n > 0:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def rsi(x, n: int = 14) -> float:
    """Wilder's RSI."""
    x = _a(x)
    if len(x) < n + 1:
        return NaN
    d = np.diff(x)
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:n].mean(), dn[:n].mean()
    for i in range(n, len(d)):
        au = (au * (n - 1) + up[i]) / n
        ad = (ad * (n - 1) + dn[i]) / n
    if ad == 0:
        return 100.0 if au > 0 else 50.0
    return float(100 - 100 / (1 + au / ad))


def stdev(x, n: int) -> float:
    x = _a(x)
    return float(x[-n:].std(ddof=1)) if len(x) >= n > 1 else NaN


def zscore(x, n: int) -> float:
    x = _a(x)
    if len(x) < n or n < 2:
        return NaN
    w = x[-n:]
    s = w.std(ddof=1)
    return float((w[-1] - w.mean()) / s) if s > 0 else 0.0


def bollinger(x, n: int = 20, k: float = 2.0) -> tuple[float, float, float]:
    m, s = sma(x, n), stdev(x, n)
    if math.isnan(m) or math.isnan(s):
        return NaN, NaN, NaN
    return m - k * s, m, m + k * s


def roc(x, n: int) -> float:
    x = _a(x)
    return float(x[-1] / x[-1 - n] - 1) if len(x) > n and x[-1 - n] > 0 else NaN


def highest(x, n: int) -> float:
    x = _a(x)
    return float(x[-n:].max()) if len(x) >= n > 0 else NaN


def lowest(x, n: int) -> float:
    x = _a(x)
    return float(x[-n:].min()) if len(x) >= n > 0 else NaN


def atr(high, low, close, n: int = 14) -> float:
    h, l, c = _a(high), _a(low), _a(close)
    if len(c) < n + 1:
        return NaN
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    v = tr[:n].mean()
    for i in range(n, len(tr)):
        v = (v * (n - 1) + tr[i]) / n
    return float(v)


def realized_vol(x, n: int, periods_per_year: float = 252.0) -> float:
    x = _a(x)
    if len(x) < n + 1:
        return NaN
    r = np.diff(np.log(x[-(n + 1):]))
    return float(r.std(ddof=1) * math.sqrt(periods_per_year))


def vwap(price, volume) -> float:
    p, v = _a(price), _a(volume)
    tot = v.sum()
    return float((p * v).sum() / tot) if tot > 0 else NaN


def crossed_above(a_prev: float, a_now: float, b_prev: float, b_now: float) -> bool:
    return a_prev <= b_prev and a_now > b_now


def crossed_below(a_prev: float, a_now: float, b_prev: float, b_now: float) -> bool:
    return a_prev >= b_prev and a_now < b_now


REGISTRY = {"sma": sma, "ema": ema, "rsi": rsi, "stdev": stdev, "zscore": zscore, "roc": roc,
            "highest": highest, "lowest": lowest, "realized_vol": realized_vol}

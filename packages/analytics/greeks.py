"""Black-Scholes-Merton pricing, implied volatility and Greeks for European index options.

Used (a) to analyse the exposure of option positions from their *observed* premiums and
(b) by the synthetic fixture generator. The backtester never uses a model price as a
fill price — fills come from the option contract's own historical bars.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SQRT2PI = math.sqrt(2 * math.pi)
DEFAULT_RATE = 0.065


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT2PI


def bs_price(s: float, k: float, t: float, vol: float, r: float = DEFAULT_RATE, kind: str = "CE") -> float:
    """t in years. At t<=0 or vol<=0 returns (discounted) intrinsic value."""
    if t <= 0 or vol <= 0:
        return max(s - k, 0.0) if kind == "CE" else max(k - s, 0.0)
    sd = vol * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * vol * vol) * t) / sd
    d2 = d1 - sd
    if kind == "CE":
        return s * _cdf(d1) - k * math.exp(-r * t) * _cdf(d2)
    return k * math.exp(-r * t) * _cdf(-d2) - s * _cdf(-d1)


@dataclass(frozen=True, slots=True)
class Greeks:
    delta: float
    gamma: float
    theta: float   # per calendar day
    vega: float    # per 1 vol point (0.01)
    iv: float


def bs_greeks(s: float, k: float, t: float, vol: float, r: float = DEFAULT_RATE, kind: str = "CE") -> Greeks:
    if t <= 0 or vol <= 0:
        itm = (s > k) if kind == "CE" else (s < k)
        return Greeks((1.0 if kind == "CE" else -1.0) if itm else 0.0, 0.0, 0.0, 0.0, vol)
    sq = math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * vol * vol) * t) / (vol * sq)
    d2 = d1 - vol * sq
    gamma = _pdf(d1) / (s * vol * sq)
    vega = s * _pdf(d1) * sq / 100.0
    if kind == "CE":
        delta = _cdf(d1)
        theta = (-s * _pdf(d1) * vol / (2 * sq) - r * k * math.exp(-r * t) * _cdf(d2)) / 365.0
    else:
        delta = _cdf(d1) - 1.0
        theta = (-s * _pdf(d1) * vol / (2 * sq) + r * k * math.exp(-r * t) * _cdf(-d2)) / 365.0
    return Greeks(delta, gamma, theta, vega, vol)


def implied_vol(price: float, s: float, k: float, t: float, r: float = DEFAULT_RATE, kind: str = "CE",
                lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-6) -> float:
    """Bisection IV. Returns NaN when the premium is outside no-arbitrage bounds."""
    if t <= 0 or price <= 0:
        return float("nan")
    if price < bs_price(s, k, t, lo, r, kind) - 1e-9 or price > bs_price(s, k, t, hi, r, kind):
        return float("nan")
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_price(s, k, t, mid, r, kind) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def year_fraction(days: float) -> float:
    return max(days, 0.0) / 365.0

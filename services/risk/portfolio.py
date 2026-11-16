"""Portfolio construction and multi-strategy risk analytics."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.optimize import minimize

METHODS = ("equal", "risk_parity", "min_variance", "vol_target")


def weights(returns: pd.DataFrame, method: str = "equal", *, cap: float = 0.6, target_vol: float = 0.10) -> dict[str, float]:
    """Capital weights from a matrix of daily strategy returns (columns = strategies).

    equal         1/N
    risk_parity   inverse-volatility (equal risk contribution when correlations are equal)
    min_variance  long-only minimum variance, each weight <= cap
    vol_target    equal weights scaled so that estimated portfolio volatility equals target_vol
                  (never levered: the scale is capped at 1, the remainder stays in cash)
    """
    cols = list(returns.columns)
    n = len(cols)
    if n == 0:
        return {}
    r = returns.fillna(0.0)
    if method == "equal" or n == 1 and method != "vol_target":
        w = np.full(n, 1 / n)
    elif method == "risk_parity":
        vol = r.std().replace(0, np.nan)
        inv = (1 / vol).fillna(0.0).to_numpy()
        w = inv / inv.sum() if inv.sum() > 0 else np.full(n, 1 / n)
    elif method == "min_variance":
        cov = r.cov().to_numpy() + np.eye(n) * 1e-10
        res = minimize(lambda x: x @ cov @ x, np.full(n, 1 / n), method="SLSQP", bounds=[(0, max(cap, 1 / n))] * n,
                       constraints=[{"type": "eq", "fun": lambda x: x.sum() - 1}])
        w = res.x if res.success else np.full(n, 1 / n)
    elif method == "vol_target":
        w = np.full(n, 1 / n)
        pv = float(np.sqrt(w @ r.cov().to_numpy() @ w) * math.sqrt(252)) if n > 1 else float(r.iloc[:, 0].std() * math.sqrt(252))
        w = w * min(target_vol / pv, 1.0) if pv > 0 else w
    else:
        raise ValueError(f"unknown allocation method {method!r}; choose from {METHODS}")
    return {c: float(x) for c, x in zip(cols, w)}


def correlation_report(returns: pd.DataFrame) -> dict:
    c = returns.corr().fillna(0.0)
    n = len(c)
    off = c.to_numpy()[~np.eye(n, dtype=bool)] if n > 1 else np.array([])
    return {"labels": list(c.columns), "matrix": c.round(4).to_numpy().tolist(),
            "average": float(off.mean()) if len(off) else None, "max": float(off.max()) if len(off) else None}


def overlap_report(positions_by_book: dict[str, dict[str, float]]) -> list[dict]:
    """Symbols held by more than one strategy at the end of the simulation (value in rupees)."""
    seen: dict[str, dict[str, float]] = {}
    for book, pos in positions_by_book.items():
        for sym, val in pos.items():
            seen.setdefault(sym, {})[book] = val
    return [{"symbol": s, "books": b, "total_value": sum(b.values())} for s, b in sorted(seen.items()) if len(b) > 1]


def exposure_report(positions: list[dict], instruments: dict, marks: dict[str, float], spot: dict[str, float] | None = None) -> dict:
    """Sector exposure for cash equities and delta-equivalent exposure for derivatives.
    Option deltas are computed from observed premiums via Black-Scholes implied volatility."""
    from datetime import date as _date

    from nseql.analytics.greeks import bs_greeks, implied_vol, year_fraction
    sectors: dict[str, float] = {}
    delta_eq: dict[str, float] = {}
    greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for p in positions:
        inst = instruments[p["symbol"]]
        m = marks.get(p["symbol"], 0.0)
        if inst.is_cash:
            sectors[inst.sector or "Unknown"] = sectors.get(inst.sector or "Unknown", 0.0) + p["qty"] * m
        elif inst.is_future:
            delta_eq[inst.underlying] = delta_eq.get(inst.underlying, 0.0) + p["qty"] * m
        elif inst.is_option:
            s = (spot or {}).get(inst.underlying) or marks.get(inst.underlying)
            asof = p.get("asof")
            if not s or not asof:
                continue
            t = year_fraction((inst.expiry - _date.fromisoformat(str(asof)[:10])).days)
            iv = implied_vol(m, s, inst.strike, t, kind=inst.option_type)
            if iv != iv:
                continue
            g = bs_greeks(s, inst.strike, t, iv, kind=inst.option_type)
            delta_eq[inst.underlying] = delta_eq.get(inst.underlying, 0.0) + p["qty"] * g.delta * s
            for k in greeks:
                greeks[k] += p["qty"] * getattr(g, k)
    return {"sector_exposure": sectors, "delta_equivalent": delta_eq, "option_greeks": greeks}

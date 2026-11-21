"""Vectorised strategy evaluation on daily bars (the screening tier).

Timing convention, identical to the event-driven backtester:
  * target weights W[t] are decided from data up to the close of day t;
  * the trade executes at the open of t+1 and pays per-side costs on the change in weights;
  * the position is held from the open of t+1 to the open of t+2.
So the net return booked on day t is   R[t] = W[t-2] . oo[t]  -  c * |W[t-1] - W[t-2]|_1
where oo[t] = open[t] / open[t-1] - 1. Nothing at row t depends on data after row t.

Approximations (why the event engine remains the reference): fractional weights instead of
integer shares, a flat per-side cost instead of the dated charge schedule per fill, no volume
cap or price-band rejections, and switching between configurations costs nothing.
"""
from __future__ import annotations

from datetime import date

import numpy as np

from nseql.evaluation.space import Config
from nseql.instruments.models import Instrument, Kind
from nseql.instruments.rules import ChargeSchedule
from nseql.money import to_paise

INVEST = 0.95
MIN_SLOTS = 4


def side_cost(d: date, half_spread_bps: float = 2.0) -> float:
    """Average per-side cost of a delivery equity trade on date d, as a fraction of value."""
    s, inst = ChargeSchedule(allow_extrapolation=True), Instrument("X", Kind.EQ)
    buy = s.compute(inst, +1, 1000, to_paise(100.0), "CNC", d).total
    sell = s.compute(inst, -1, 1000, to_paise(100.0), "CNC", d, dp_applicable=False).total
    return (buy + sell) / 2 / 100 / 100_000 + half_spread_bps / 1e4


def _hysteresis(enter: np.ndarray, exit_: np.ndarray) -> np.ndarray:
    """1 from an entry signal until the next exit signal (entry wins ties), else 0."""
    ev = np.where(enter, 1, np.where(exit_, -1, 0)).astype(np.int8)
    T = ev.shape[0]
    pos = np.where(ev != 0, np.arange(T)[:, None], -1)
    np.maximum.accumulate(pos, axis=0, out=pos)
    cols = np.broadcast_to(np.arange(ev.shape[1]), ev.shape)
    last = np.where(pos >= 0, ev[np.clip(pos, 0, None), cols], -1)
    return (last > 0).astype(np.float64)


def _shift(a: np.ndarray, k: int) -> np.ndarray:
    if k == 0:
        return a
    out = np.full_like(a, np.nan, dtype=float)
    out[k:] = a[:-k]
    return out


def signals(cfg: Config, P) -> np.ndarray:
    """0/1 holdings intent per symbol per day, before sizing."""
    p, c = cfg.p, np.asarray(P["close"])
    alive = np.asarray(P["alive"]) > 0
    umask = alive if cfg.universe == "all" else alive & (np.asarray(P[f"u_{cfg.universe}"]) > 0)
    with np.errstate(invalid="ignore"):
        if cfg.family == "trend":
            s = (np.asarray(P[f"sma_{p['fast']}"]) > np.asarray(P[f"sma_{p['slow']}"])).astype(float)
        elif cfg.family == "breakout":
            s = _hysteresis(c > np.asarray(P[f"hh_{p['entry']}"]), c < np.asarray(P[f"ll_{p['exit']}"]))
        elif cfg.family == "meanrev":
            z = np.asarray(P[f"z_{p['n']}"])
            s = _hysteresis(z < p["z_in"], z > p["z_out"])
        elif cfg.family == "rsi":
            r = np.asarray(P[f"rsi_{p['n']}"])
            s = _hysteresis(r < p["buy"], r > p["sell"])
        elif cfg.family == "momentum":
            score = _shift(c, p["skip"]) / _shift(c, p["skip"] + p["lookback"]) - 1.0
            score = np.where(umask & np.isfinite(score), score, -np.inf)
            T = c.shape[0]
            order = np.argsort(-score, axis=1, kind="stable")
            top = np.zeros_like(score)
            rows = np.arange(T)[:, None]
            top[rows, order[:, : p["top_k"]]] = 1.0
            top[~np.isfinite(score)] = 0.0
            reb = (np.arange(T) % p["rebalance"]) == 0
            held = np.where(reb[:, None], top, np.nan)
            idx = np.where(reb, np.arange(T), -1)
            np.maximum.accumulate(idx, out=idx)
            s = np.where(idx[:, None] >= 0, held[np.clip(idx, 0, None)], 0.0)
            s = np.nan_to_num(s)
        else:
            raise ValueError(cfg.family)
        s = s * umask
        if cfg.regime.startswith("ctx_"):
            m = np.asarray(P[cfg.regime]) > 0                   # [T] market-wide state, or [T, N] per symbol (results)
            s = s * (m[:, None] if m.ndim == 1 else m)
        elif cfg.regime != "none":
            ic, sma = np.asarray(P["idx_close"]), np.asarray(P[f"idx_{cfg.regime.replace('sma', 'sma_')}"])
            s = s * (ic > sma)[:, None]
        if cfg.vol is not None:
            s = s * (np.asarray(P["rv20"]) < cfg.vol)
    return np.nan_to_num(s)


def weights(cfg: Config, P) -> np.ndarray:
    s = signals(cfg, P)
    n = s.sum(axis=1, keepdims=True)
    if cfg.sizing == "equal":
        w = s / np.maximum(n, MIN_SLOTS)
    else:
        rv = np.asarray(P["rv20"])
        raw = np.where((s > 0) & (rv > 0), s / np.where(rv > 0, rv, 1.0), 0.0)
        tot = raw.sum(axis=1, keepdims=True)
        w = np.where(tot > 0, raw / np.where(tot > 0, tot, 1.0), 0.0) * np.minimum(n, MIN_SLOTS) / MIN_SLOTS
    return INVEST * np.nan_to_num(w)


def evaluate(cfg: Config, P, cost: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """-> (gross returns, net returns, gross exposure) per day, float32."""
    W = weights(cfg, P)
    T = W.shape[0]
    g, r, e = np.zeros(T), np.zeros(T), np.zeros(T)
    oo = np.asarray(P["oo"])
    g[2:] = (W[:-2] * oo[2:]).sum(axis=1)
    turn = np.zeros(T)
    turn[1] = np.abs(W[0]).sum()
    turn[2:] = np.abs(W[1:-1] - W[:-2]).sum(axis=1)
    r[:] = g - cost * turn
    e[2:] = np.abs(W[:-2]).sum(axis=1)
    return g.astype(np.float32), r.astype(np.float32), e.astype(np.float16)

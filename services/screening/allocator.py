"""Ranked capital allocation for the shared screening portfolio.

The original allocator served BUY signals in registry order, which is the order of the configurations'
hash ids: effectively arbitrary. When more configurations want to buy in one minute than the portfolio
can hold, the ones that won were simply the ones with the "smallest" hash. This module ranks them instead,
using only information available at the decision minute.

Inputs (all observed at or before the decision minute; NO future returns are used)
  * Forward evidence: every round trip a configuration has completed in THIS paper ledger (net return after
    charges and simulated slippage, win or loss). Nothing from backtests, nothing from later bars.
  * Shrinkage: a configuration's mean net return and win rate are pulled toward its group (same rule family
    and timeframe, all symbols), and the group toward zero return / 50% wins, with pseudo-counts
    ``k_config`` and ``k_group``. A configuration with no trades therefore has expected return 0 (or its
    group's, once the group has evidence).
  * Confidence = n / (n + k_config). "Qualified" = at least ``min_trades`` round trips and a shrunk mean
    above zero. Qualified configurations are ranked ahead of unqualified ones.
  * Calibration: at each entry the predicted win probability is recorded; at the exit the outcome. Once
    ``calib_min`` predictions have resolved, the evidence term is scaled by a reliability factor in
    [0.5, 1]: 1 when the predictions beat the base-rate Brier score, lower when they do worse.
  * Expected return after costs: the shrunk mean (already net of charges and slippage) minus the extra
    market impact this order would cause at the symbol's recent volume (EWMA of 1-minute volume).
  * Liquidity and capacity: an order larger than participation x EWMA volume x entry TTL is cut to that
    size; below one share it is rejected ("no liquidity").
  * Correlation and redundancy: penalty for the correlation of the symbol's last 60 one-minute returns with
    the current portfolio's returns; only one new entry per symbol per minute (the best-ranked; the rest are
    deferred as redundant).
  * Diversification: penalty for the symbol's current share of the per-symbol limit, and for the share of
    open positions already in the same rule family.
  * Positions, capital and risk limits: the existing hard limits (cash, per-symbol, gross, max positions,
    daily loss halt) are then applied in rank order.

Score (basis points) = reliability x expected net return - impact - corr_bps x max(corr, 0)
                       - conc_bps x symbol share of its limit - family_bps x family share of open positions.
Ties are broken by configuration index, so a replay or a recovery makes exactly the same choices.

Negative-expectancy gate (``reject_negative``): a configuration with at least ``min_trades`` forward round
trips whose expected net return after costs (reliability x shrunk mean - impact) is below zero is refused
capital. Configurations with fewer trades still trade, so evidence keeps accumulating (exploration).

Outcomes recorded per BUY signal: ``accepted``, ``rejected`` (fails on its own: halted, too expensive, no
liquidity, a limit already full at the start of the minute) or ``deferred`` (would have passed, but a
higher-ranked signal in the same minute used the capital, the symbol room or the position slot).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

WINDOW = 60                                   # one-minute returns kept per symbol for correlation


@dataclass(frozen=True)
class AllocatorConfig:
    k_config: float = 10.0
    k_group: float = 20.0
    min_trades: int = 5
    calib_min: int = 30
    corr_bps: float = 10.0
    conc_bps: float = 10.0
    family_bps: float = 5.0
    vol_alpha: float = 0.1                    # EWMA weight of the newest 1-minute volume
    max_new_per_symbol: int = 1
    reject_negative: bool = True              # proven (>= min_trades) configurations expected to lose after costs get no capital

    def to_dict(self) -> dict:
        return asdict(self)


class Allocator:
    """Market memory (returns, volume) and the forward evidence book. Part of the engine checkpoint."""

    ARR = ("ret_buf", "last_px", "ewma_vol", "ev_n", "ev_w", "ev_sum", "pred", "calib")

    def __init__(self, n_configs: int, n_symbols: int, n_groups: int, cfg: AllocatorConfig | None = None):
        self.cfg = cfg or AllocatorConfig()
        self.n_groups = n_groups
        self.ret_buf = np.zeros((n_symbols, WINDOW))
        self.buf_pos = 0
        self.buf_n = 0
        self.last_px = np.full(n_symbols, np.nan)
        self.ewma_vol = np.zeros(n_symbols)
        self.ev_n = np.zeros(n_configs)
        self.ev_w = np.zeros(n_configs)
        self.ev_sum = np.zeros(n_configs)        # sum of net returns per round trip (fractions)
        self.pred = np.full(n_configs, np.nan)   # win probability predicted at the open entry
        self.calib = np.zeros(3)                 # resolved predictions, sum of squared errors, wins

    # ------------------------------------------------------------------ market memory
    def observe(self, has_bar: np.ndarray, close: np.ndarray, volume: np.ndarray) -> None:
        r = np.zeros(len(close))
        ok = has_bar & np.isfinite(self.last_px) & (self.last_px > 0)
        r[ok] = np.log(close[ok] / self.last_px[ok])
        self.ret_buf[:, self.buf_pos] = r
        self.buf_pos = (self.buf_pos + 1) % WINDOW
        self.buf_n = min(self.buf_n + 1, WINDOW)
        self.last_px[has_bar] = close[has_bar]
        a = self.cfg.vol_alpha
        fresh = has_bar & (self.ewma_vol == 0)
        self.ewma_vol[fresh] = volume[fresh]
        upd = has_bar & ~fresh
        self.ewma_vol[upd] = (1 - a) * self.ewma_vol[upd] + a * volume[upd]

    def correlation(self, weights: np.ndarray) -> np.ndarray:
        """Correlation of each symbol's recent returns with the portfolio (weights = held value per symbol)."""
        if self.buf_n < 10 or weights.sum() <= 0:
            return np.zeros(len(weights))
        R = self.ret_buf[:, :self.buf_n] if self.buf_n < WINDOW else self.ret_buf
        port = weights @ R / weights.sum()
        Rc = R - R.mean(axis=1, keepdims=True)
        pc = port - port.mean()
        den = np.sqrt((Rc ** 2).sum(axis=1) * (pc ** 2).sum())
        with np.errstate(invalid="ignore", divide="ignore"):
            c = np.where(den > 0, Rc @ pc / den, 0.0)
        return np.nan_to_num(c)

    # ------------------------------------------------------------------ evidence
    def estimates(self, group: np.ndarray) -> dict:
        """Shrunk mean net return, win probability and confidence for every configuration."""
        c = self.cfg
        gn = np.bincount(group, weights=self.ev_n, minlength=self.n_groups)
        gs = np.bincount(group, weights=self.ev_sum, minlength=self.n_groups)
        gw = np.bincount(group, weights=self.ev_w, minlength=self.n_groups)
        g_mu = gs / (gn + c.k_group)
        g_p = (gw + 0.5 * c.k_group) / (gn + c.k_group)
        mu = (self.ev_sum + c.k_config * g_mu[group]) / (self.ev_n + c.k_config)
        p = (self.ev_w + c.k_config * g_p[group]) / (self.ev_n + c.k_config)
        conf = self.ev_n / (self.ev_n + c.k_config)
        return {"mu": mu, "p_win": p, "confidence": conf, "qualified": (self.ev_n >= c.min_trades) & (mu > 0)}

    def reliability(self) -> float:
        n, sse, wins = self.calib
        if n < self.cfg.calib_min:
            return 1.0
        base = wins / n
        ref = base * (1 - base)                         # Brier score of always predicting the base rate
        if ref <= 0:
            return 1.0
        return float(np.clip(1 - (sse / n - ref) / ref, 0.5, 1.0))

    def record_entry(self, i: int, p_win: float) -> None:
        self.pred[i] = p_win

    def record_round_trip(self, i: int, net_return: float) -> None:
        win = 1.0 if net_return > 0 else 0.0
        self.ev_n[i] += 1
        self.ev_w[i] += win
        self.ev_sum[i] += net_return
        if np.isfinite(self.pred[i]):
            self.calib += (1, (self.pred[i] - win) ** 2, win)
        self.pred[i] = np.nan

    def calibration(self) -> dict:
        n, sse, wins = self.calib
        return {"resolved_predictions": int(n), "brier": round(sse / n, 4) if n else None,
                "base_rate_brier": round((wins / n) * (1 - wins / n), 4) if n else None, "reliability": round(self.reliability(), 3)}

    # ------------------------------------------------------------------ checkpoint
    def state(self) -> dict:
        st = {f"a_{k}": getattr(self, k) for k in self.ARR}
        st["a_scalars"] = np.array([self.buf_pos, self.buf_n])
        return st

    def load(self, st: dict) -> None:
        if "a_scalars" not in st:                        # checkpoint from before the ranked allocator: start empty
            return
        for k in self.ARR:
            setattr(self, k, np.array(st[f"a_{k}"], dtype=float))
        self.buf_pos, self.buf_n = (int(x) for x in st["a_scalars"])

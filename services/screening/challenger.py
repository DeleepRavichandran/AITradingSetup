"""Challenger trader: 30-minute opening-range breakout with a market-breadth filter (one deliberately simple rule set).

It runs on the SAME scheduler, ledger, feature engine, execution model (next-bar fills, participation cap, spread,
impact, entry time-to-live, partial fills), dated NSE charges, portfolio limits, daily loss limit and 15:15
intraday square-off as the incumbent screening trader. Only the decision rule and the position size differ.

Rules (version CHALLENGER_VERSION, frozen in CHALLENGER_CONFIG; not tuned on any evaluation period):
  * Universe: the same eligible equity watchlist as the incumbent (indices are never traded).
  * Opening range (OR): high and low of the first 30 one-minute bars of the NSE session (09:16-09:45 stamps).
    A symbol whose 30 OR bars are not all present that day is skipped for the day.
  * Entry: the first completed 5-minute close after the OR (bars stamped 09:50 onward) that is above the OR high,
    AND, at that decision minute, breadth > 50%: of the watchlist stocks with fresh data (a bar within
    `fresh_minutes`), more than half trade above their own session open. Breadth is only used when fresh-data
    coverage >= `min_breadth_coverage` (frozen at 80% of the eligible watchlist); otherwise no entry.
    One decision per symbol per day: once a BUY is decided for a symbol (accepted or refused by the portfolio),
    it does not enter again that day. No averaging down. No entries after 15:00.
  * Orders: submitted after the signal; the earliest fill is the next available 1-minute bar, through the shared
    execution model (same liquidity, slippage, charges and partial fills as the incumbent).
  * Stop: the OR low, checked every minute on the 1-minute low; the exit order fills at the next bar's open, so
    a gap through the stop is executed at the (worse) price that was actually available.
  * Size: shares = floor(risk_per_trade x equity / (signal close - OR low)), i.e. the planned loss at the stop is
    at most 0.25% of equity, then capped by the incumbent's tighter limits (per-trade cap, cash, per-symbol,
    gross exposure, max positions, daily loss limit), applied by the shared Executor.
  * Exit: stop, or the existing intraday square-off from 15:15. No overnight holdings by design; a position that
    liquidity prevents closing by the end of the session is reported as residual by the comparison.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from nseql.screening.allocator import AllocatorConfig
from nseql.screening.engine import D, DECISIONS, LAST_ENTRY_MIN, SQUARE_OFF_MIN, TIE_EPS, MinuteResult, ScreeningEngine
from nseql.screening.execution import ExecConfig, PortfolioRules
from nseql.screening.features import SESSION_MINUTES, minute_of_session
from nseql.screening.registry import FAMILIES, TF_NAMES, Registry, config_id

CHALLENGER_VERSION = "orb30-breadth-1.0"


@dataclass(frozen=True)
class ChallengerConfig:
    version: str = CHALLENGER_VERSION
    or_minutes: int = 30                 # opening range: first 30 one-minute bars
    entry_timeframe: str = "5m"          # breakout confirmed on a completed 5-minute close
    breadth_threshold: float = 0.5       # strictly more than half of fresh watchlist stocks above their session open
    min_breadth_coverage: float = 0.8    # frozen before evaluation: fresh-data share of the eligible watchlist
    fresh_minutes: int = 2               # a bar within this many minutes counts as fresh
    risk_per_trade: float = 0.0025       # planned loss at the stop <= 0.25% of equity
    last_entry_minute: int = LAST_ENTRY_MIN   # 15:00
    square_off_minute: int = SQUARE_OFF_MIN   # 15:15 (existing mechanism)

    def to_dict(self) -> dict:
        return asdict(self)


CHALLENGER_CONFIG = ChallengerConfig()


def challenger_rules(capital: float) -> PortfolioRules:
    """The incumbent's live risk ceilings (cash, per-trade, per-symbol, gross, positions, daily loss, churn limits).
    The incumbent's strategy-SELECTION gate (group_gate_trades) and its opening blackout (no_entry_first_minutes) are not
    risk ceilings and would change the frozen challenger rule mid-evaluation, so they are off."""
    from dataclasses import replace
    return replace(PortfolioRules.live(capital), group_gate_trades=0, no_entry_first_minutes=0)   # its own frozen rule decides entry times


def build_registry(symbols: list[str], cfg: ChallengerConfig = CHALLENGER_CONFIG) -> Registry:
    """One challenger configuration per symbol, in watchlist order (config index == symbol index)."""
    n = len(symbols)
    tf = TF_NAMES.index(cfg.entry_timeframe)
    params = [{"strategy": cfg.version, "or_minutes": cfg.or_minutes}] * n
    ids = np.array([config_id("orb", cfg.entry_timeframe, params[i], s, 0.0) + "" for i, s in enumerate(symbols)])
    z = np.zeros(n)
    neg = np.full(n, -1, dtype=np.int16)
    return Registry(ids, np.arange(n, dtype=np.int32), np.full(n, tf, dtype=np.int8), np.full(n, FAMILIES.index("orb"), dtype=np.int8),
                    neg, neg, z, neg, neg, z, z.copy(), np.ones(n, dtype=bool), list(params), list(symbols))


def factory(cfg: ChallengerConfig = CHALLENGER_CONFIG):
    """engine_factory for MinuteScheduler."""
    def make(symbols, exec_cfg, portfolio):
        reg = build_registry(symbols, cfg)
        return reg, ChallengerEngine(reg, exec_cfg, portfolio, cfg)
    return make


class ChallengerEngine(ScreeningEngine):
    def __init__(self, reg: Registry, exec_cfg: ExecConfig | None, portfolio: PortfolioRules | None, cfg: ChallengerConfig = CHALLENGER_CONFIG):
        # the frozen rule is not switched off by the incumbent's evidence gate (that is a selection policy, not a risk limit)
        super().__init__(reg, exec_cfg, portfolio, AllocatorConfig(reject_negative=False))
        self.cfg = cfg
        S = len(reg.symbols)
        self.or_day = np.full(S, -1, dtype=np.int64)
        self.or_hi = np.full(S, -np.inf)
        self.or_lo = np.full(S, np.inf)
        self.or_n = np.zeros(S, dtype=np.int64)
        self.decided = np.zeros(S, dtype=bool)          # a BUY was decided today (one entry decision per symbol per day)
        self.breadth_log: list[dict] = []

    # ------------------------------------------------------------------ one minute
    def step(self, ts: pd.Timestamp, symbols: list[str], o, h, lo, c, v, *, allow_orders: bool = True,
             block_reason: str = "kill switch engaged", reduce_only: bool = False) -> MinuteResult:
        ts = pd.Timestamp(ts)
        em = self.epoch_min(ts)
        if em <= self.last_minute:
            return MinuteResult(ts, "duplicate")
        m = minute_of_session(ts)
        if not 1 <= m <= SESSION_MINUTES:
            raise ValueError(f"{ts} is outside the 09:16-15:30 session")
        r, fe, cfg = self.reg, self.fe, self.cfg
        sel = np.array([fe.index[s] for s in symbols], dtype=np.int64)
        o, h, lo, c, v = (np.asarray(x, dtype=float) for x in (o, h, lo, c, v))
        res = MinuteResult(ts, "processed", symbols_with_data=len(sel))
        has_bar = np.zeros(fe.S, dtype=bool)
        has_bar[sel] = True
        open_px = np.full(fe.S, np.nan)
        open_px[sel] = o
        vol = np.zeros(fe.S)
        vol[sel] = v
        self.x.new_day(ts)
        day = ts.toordinal()
        new = self.or_day != day
        if new.any():
            self.or_day[new], self.or_hi[new], self.or_lo[new], self.or_n[new], self.decided[new] = day, -np.inf, np.inf, 0, False
        # 1. working orders execute against this bar (shared execution model)
        res.fills, res.done = self.x.fill(ts, has_bar, open_px, vol, block=block_reason if (reduce_only and not allow_orders) else None)
        held = self.pos == 1
        self.stop_px[held] = self.or_lo[r.sym[held]]          # the stop is the opening-range low, not a % of the fill
        # 2. features and opening range (completed 1-minute bars only)
        res.closed = fe.update(ts, sel, o, h, lo, c, v)
        self.x.alloc.observe(has_bar, fe.last_close, vol)
        if m <= cfg.or_minutes:
            self.or_hi[sel] = np.fmax(self.or_hi[sel], h)
            self.or_lo[sel] = np.fmin(self.or_lo[sel], lo)
            self.or_n[sel] += 1
        n = len(r)
        dec = np.full(n, -1, dtype=np.int8)
        reason = np.zeros(n, dtype=np.int8)
        # 3. exits: stop on the 1-minute low, square-off from 15:15
        open_pos = (self.pos == 1) & has_bar[r.sym] & (self.pend == 0)
        stop_hit = open_pos & (fe.last_low[r.sym] <= self.stop_px)
        sq_off = open_pos & (m >= cfg.square_off_minute)
        dec[stop_hit | sq_off] = D["EXIT"]
        reason[stop_hit | sq_off] = np.where(stop_hit[stop_hit | sq_off], 1, 2)
        # 4. entries: first completed 5-minute close above the OR high, with breadth
        cap: dict[int, int] = {}
        if cfg.entry_timeframe in res.closed and m >= cfg.or_minutes + TF_MIN[cfg.entry_timeframe] and m <= cfg.last_entry_minute:
            fresh = (fe.last_ts >= np.datetime64(ts - pd.Timedelta(minutes=cfg.fresh_minutes), "m")) & (fe.sess[:, 0] == float(day))
            n_fresh = int(fresh.sum())
            coverage = n_fresh / max(fe.S, 1)
            above = int((fresh & (fe.last_close > fe.sess[:, 1])).sum())
            breadth = above / n_fresh if n_fresh else 0.0
            ok = coverage >= cfg.min_breadth_coverage and breadth > cfg.breadth_threshold
            self.breadth_log.append({"minute": str(ts), "coverage": round(coverage, 4), "breadth": round(breadth, 4), "ok": bool(ok)})
            close5 = fe.feat[cfg.entry_timeframe]["close"]
            upd = fe.updated[cfg.entry_timeframe]
            cand = np.flatnonzero(upd[r.sym] & (self.or_n[r.sym] == cfg.or_minutes) & ~self.decided[r.sym] & (self.pos == 0) & (self.pend == 0))
            hi = self.or_hi[r.sym[cand]]
            brk = close5[r.sym[cand]] > hi + TIE_EPS * np.maximum(1.0, np.abs(hi))
            if ok:
                eq = self.x.equity() / 100 if self.x.rules else 0.0
                for i in cand[brk]:
                    s = int(r.sym[i])
                    risk_ps = float(close5[s]) - float(self.or_lo[s])
                    if risk_ps <= 0 or not math.isfinite(risk_ps):
                        continue
                    dec[i] = D["BUY"]
                    self.decided[s] = True
                    cap[int(i)] = int(cfg.risk_per_trade * eq // risk_ps)
        ev_all = np.flatnonzero(dec >= 0)
        self.evals[ev_all] += 1
        self.last_eval[ev_all] = em
        res.evaluated = int(len(ev_all))
        res.eligible = res.evaluated
        res.counts = {DECISIONS[k]: int((dec[ev_all] == k).sum()) for k in np.unique(dec[ev_all])}
        ev = self.x.check_loss(ts)
        if ev:
            res.events.append(ev)
        act = [int(i) for i in ev_all]
        names = ("rule", "stop", "square-off")
        for i in act:
            res.signals.append((i, DECISIONS[dec[i]], float(fe.last_close[r.sym[i]]), names[reason[i]]))
        self.x.qty_cap = cap
        try:
            res.orders = self.x.submit(act, {i: DECISIONS[dec[i]] for i in act}, em, allow_orders, block_reason, reduce_only)
        finally:
            self.x.qty_cap = None
        self.last_minute = em
        return res

    # ------------------------------------------------------------------ checkpoint
    def state(self) -> dict:
        st = super().state()
        st.update({"c_or_day": self.or_day, "c_or_hi": self.or_hi, "c_or_lo": self.or_lo, "c_or_n": self.or_n, "c_decided": self.decided})
        return st

    def load(self, st: dict) -> None:
        super().load(st)
        if "c_or_day" in st:
            self.or_day, self.or_hi, self.or_lo = np.array(st["c_or_day"]), np.array(st["c_or_hi"]), np.array(st["c_or_lo"])
            self.or_n, self.decided = np.array(st["c_or_n"]), np.array(st["c_decided"])


TF_MIN = {"1m": 1, "5m": 5, "15m": 15}

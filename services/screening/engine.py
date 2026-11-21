"""One-minute screening engine: features -> eligible configurations -> decisions -> forward paper ledger.

Per minute M (bars stamped at M's close):
  1. Pending simulated orders fill at the OPEN of the symbol's bar for M (decided at an earlier close).
  2. Shared features are updated for the symbols that have a bar; timeframes whose bar closed are noted.
  3. Eligible configurations are evaluated, all at once:
       * entry/exit rule: the configuration's timeframe closed this minute AND its symbol has a new bar;
       * stop check, every minute, for every open position whose symbol has a bar (1-minute low <= stop);
       * intraday square-off: every open intraday position exits from 15:15; no intraday entries after 15:00.
  4. Decisions: BUY, EXIT, HOLD, NO_TRADE (this slice trades long-only cash equity; SELL, ACCUMULATE,
     REDUCE and REBALANCE are part of the decision vocabulary for products not yet in the slice).
     BUY and EXIT become simulated orders executed from the next bar (see execution.py: the simple model
     fills whole orders at the open; the realistic model caps fills at a share of the bar's volume, adds
     spread and impact, and puts every configuration in one shared portfolio). No broker is ever called.

Idempotency: a minute at or before the last processed minute is refused (no state change), so replaying
the same market event cannot evaluate a configuration twice or create a second order. Every order and
signal also carries a deterministic key, and the ledger tables reject duplicate keys.
Costs: the dated charge schedule (MIS for intraday, CNC delivery for swing).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from nseql.instruments.rules import ChargeSchedule
from nseql.screening.execution import NOTIONAL, ExecConfig, Executor, PortfolioRules  # noqa: F401  (NOTIONAL re-exported)
from nseql.screening.features import SESSION_MINUTES, TIMEFRAMES, FeatureEngine, minute_of_session
from nseql.screening.registry import FEATURES, TF_NAMES, Registry

DECISIONS = ("NO_TRADE", "BUY", "SELL", "HOLD", "ACCUMULATE", "REDUCE", "EXIT", "REBALANCE")
D = {k: i for i, k in enumerate(DECISIONS)}
LAST_ENTRY_MIN = 345          # 15:00
SQUARE_OFF_MIN = 360          # 15:15
TIE_EPS = 1e-9                # relative tolerance for rule comparisons


@dataclass
class MinuteResult:
    minute: pd.Timestamp
    status: str                                  # processed | duplicate
    closed: list[str] = field(default_factory=list)
    symbols_with_data: int = 0
    eligible: int = 0
    evaluated: int = 0
    skipped_pending: int = 0
    stale_skips: int = 0
    counts: dict = field(default_factory=dict)
    signals: list[tuple] = field(default_factory=list)      # (config_idx, decision, price, reason)
    orders: list[dict] = field(default_factory=list)        # new orders, accepted (pending) or refused
    fills: list[dict] = field(default_factory=list)         # executions (several per order when partial)
    done: list[dict] = field(default_factory=list)          # orders that finished: filled / partial / cancelled
    events: list[dict] = field(default_factory=list)        # portfolio events, e.g. the daily loss limit


class ScreeningEngine:
    def __init__(self, reg: Registry, exec_cfg: ExecConfig | None = None, portfolio: PortfolioRules | None = None):
        self.reg = reg
        self.fe = FeatureEngine(reg.symbols)
        n = len(reg)
        self.pos = np.zeros(n, dtype=np.int8)
        self.qty = np.zeros(n, dtype=np.int64)
        self.entry_px = np.zeros(n)
        self.entry_ch = np.zeros(n, dtype=np.int64)          # paise
        self.stop_px = np.zeros(n)
        self.pend = np.zeros(n, dtype=np.int8)               # +1 buy pending, -1 exit pending
        self.pend_key = np.zeros(n, dtype=np.int64)          # minute (epoch minutes) the pending order was decided
        self.evals = np.zeros(n, dtype=np.int64)
        self.last_eval = np.full(n, -1, dtype=np.int64)
        self.last_minute = -1                                # epoch minutes of the last processed minute
        self._by_tf = {k: np.flatnonzero(reg.tf == k) for k in range(len(TF_NAMES))}
        self._charges = ChargeSchedule(allow_extrapolation=True)
        self.x = Executor(self, exec_cfg, portfolio)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def epoch_min(ts: pd.Timestamp) -> int:
        return int(pd.Timestamp(ts).value // 60_000_000_000)

    def _cond(self, F: np.ndarray, sym: np.ndarray, fa: np.ndarray, fb: np.ndarray, k: np.ndarray) -> np.ndarray:
        a = np.where(fa >= 0, F[np.maximum(fa, 0), sym], k)
        b = np.where(fb >= 0, F[np.maximum(fb, 0), sym], k)
        # "greater" must hold beyond floating-point noise: on flat prices two averages are mathematically
        # equal, and rounding differences would otherwise flip the decision (and trade on noise)
        with np.errstate(invalid="ignore"):
            return np.nan_to_num(a, nan=-np.inf) > np.nan_to_num(b, nan=np.inf) + TIE_EPS * np.maximum(1.0, np.abs(np.nan_to_num(b)))

    # ------------------------------------------------------------------ one minute
    def step(self, ts: pd.Timestamp, symbols: list[str], o, h, lo, c, v, *, allow_orders: bool = True,
             block_reason: str = "kill switch engaged") -> MinuteResult:
        ts = pd.Timestamp(ts)
        em = self.epoch_min(ts)
        if em <= self.last_minute:
            return MinuteResult(ts, "duplicate")
        m = minute_of_session(ts)
        if not 1 <= m <= SESSION_MINUTES:
            raise ValueError(f"{ts} is outside the 09:16-15:30 session")
        r, fe = self.reg, self.fe
        sel = np.array([fe.index[s] for s in symbols], dtype=np.int64)
        o, h, lo, c, v = (np.asarray(x, dtype=float) for x in (o, h, lo, c, v))
        res = MinuteResult(ts, "processed", symbols_with_data=len(sel))
        has_bar = np.zeros(fe.S, dtype=bool)
        has_bar[sel] = True
        # 1. working orders execute against this bar (open price; volume-capped in the realistic model)
        open_px = np.full(fe.S, np.nan)
        open_px[sel] = o
        vol = np.zeros(fe.S)
        vol[sel] = v
        self.x.new_day(ts)
        res.fills, res.done = self.x.fill(ts, has_bar, open_px, vol)
        # 2. features
        res.closed = fe.update(ts, sel, o, h, lo, c, v)
        # 3. eligibility
        rule = np.zeros(len(r), dtype=bool)
        for tf in res.closed:
            idx = self._by_tf[TF_NAMES.index(tf)]
            rule[idx] = fe.updated[tf][r.sym[idx]]
        stale = np.zeros(len(r), dtype=bool)
        for tf in res.closed:
            idx = self._by_tf[TF_NAMES.index(tf)]
            stale[idx] = ~fe.updated[tf][r.sym[idx]]
        open_pos = (self.pos == 1) & has_bar[r.sym]
        stop_hit = open_pos & (fe.last_low[r.sym] <= self.stop_px)
        sq_off = open_pos & r.intraday & (m >= SQUARE_OFF_MIN)
        eligible = rule | stop_hit | sq_off
        busy = eligible & (self.pend != 0)
        eligible &= self.pend == 0
        res.eligible, res.skipped_pending, res.stale_skips = int(eligible.sum()), int(busy.sum()), int(stale.sum())
        dec = np.full(len(r), -1, dtype=np.int8)
        reason = np.zeros(len(r), dtype=np.int8)                     # 0 rule, 1 stop, 2 square-off, 3 no new intraday entries
        # rule evaluation per timeframe that closed
        for tf in res.closed:
            k = TF_NAMES.index(tf)
            idx = self._by_tf[k]
            idx = idx[eligible[idx] & rule[idx]]
            if not len(idx):
                continue
            F = np.vstack([fe.feat[tf][name] for name in FEATURES])
            enter = self._cond(F, r.sym[idx], r.f_enter_a[idx], r.f_enter_b[idx], r.k_enter[idx])
            exit_ = self._cond(F, r.sym[idx], r.f_exit_a[idx], r.f_exit_b[idx], r.k_exit[idx])
            flat = self.pos[idx] == 0
            late = r.intraday[idx] & (m > LAST_ENTRY_MIN)
            d = np.where(flat, np.where(enter & ~late, D["BUY"], D["NO_TRADE"]), np.where(exit_, D["EXIT"], D["HOLD"]))
            dec[idx] = d
            reason[idx] = np.where(flat & enter & late, 3, 0)
        # stops and square-off override the rule (exit takes precedence)
        forced = eligible & (stop_hit | sq_off)
        dec[forced] = D["EXIT"]
        reason[forced] = np.where(stop_hit[forced], 1, 2)
        ev_all = np.flatnonzero(dec >= 0)
        self.evals[ev_all] += 1
        self.last_eval[ev_all] = em
        res.evaluated = int(len(ev_all))
        res.counts = {DECISIONS[k]: int((dec[ev_all] == k).sum()) for k in np.unique(dec[ev_all])}
        # 4. portfolio check, then actionable decisions -> simulated orders (filled from the next bar)
        ev = self.x.check_loss(ts)
        if ev:
            res.events.append(ev)
        act = [int(i) for i in ev_all[(dec[ev_all] == D["BUY"]) | (dec[ev_all] == D["EXIT"])]]
        names = ("rule", "stop", "square-off", "no new intraday entries")
        for i in act:
            res.signals.append((i, DECISIONS[dec[i]], float(fe.last_close[r.sym[i]]), names[reason[i]]))
        res.orders = self.x.submit(act, {i: DECISIONS[dec[i]] for i in act}, em, allow_orders, block_reason)
        self.last_minute = em
        return res

    # ------------------------------------------------------------------ checkpoint
    def state(self) -> dict:
        st = {f"fe_{k}": v for k, v in self.fe.state().items()}
        for k in ("pos", "qty", "entry_px", "entry_ch", "stop_px", "pend", "pend_key", "evals", "last_eval"):
            st[k] = getattr(self, k)
        st["last_minute"] = np.array([self.last_minute])
        st["ids"] = self.reg.ids
        st.update(self.x.state())
        return st

    def load(self, st: dict) -> None:
        if not np.array_equal(np.asarray(st["ids"]), self.reg.ids):
            raise ValueError("checkpoint was taken with a different configuration registry")
        self.fe.load({k[3:]: v for k, v in st.items() if k.startswith("fe_")})
        for k in ("pos", "qty", "entry_px", "entry_ch", "stop_px", "pend", "pend_key", "evals", "last_eval"):
            setattr(self, k, np.array(st[k]))
        self.last_minute = int(np.asarray(st["last_minute"])[0])
        if "x_scalars" in st:
            self.x.load(st)


assert set(TIMEFRAMES) == set(TF_NAMES)

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
     BUY and EXIT create a simulated order filled at the next bar's open. No broker is ever called.

Idempotency: a minute at or before the last processed minute is refused (no state change), so replaying
the same market event cannot evaluate a configuration twice or create a second order. Every order and
signal also carries a deterministic key, and the ledger tables reject duplicate keys.
Sizing: each configuration trades a fixed notional of Rs 1,00,000 in its own sub-account (no shared
capital model yet). Costs: the dated charge schedule (MIS for intraday, CNC delivery for swing).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from nseql.instruments.models import Instrument, Kind
from nseql.instruments.rules import ChargeSchedule
from nseql.money import to_paise
from nseql.screening.features import SESSION_MINUTES, TIMEFRAMES, FeatureEngine, minute_of_session
from nseql.screening.registry import FEATURES, TF_NAMES, Registry

DECISIONS = ("NO_TRADE", "BUY", "SELL", "HOLD", "ACCUMULATE", "REDUCE", "EXIT", "REBALANCE")
D = {k: i for i, k in enumerate(DECISIONS)}
NOTIONAL = 100_000.0
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
    fills: list[dict] = field(default_factory=list)


class ScreeningEngine:
    def __init__(self, reg: Registry):
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

    def _fill(self, i: int, side: int, px: float, ts: pd.Timestamp, key: int) -> dict | None:
        r = self.reg
        inst = Instrument(r.symbols[r.sym[i]], Kind.EQ)
        product = "MIS" if r.intraday[i] else "CNC"
        d: date = ts.date()
        if side > 0:
            q = int(NOTIONAL // px)
            if q <= 0:
                return {"config": i, "side": "BUY", "status": "cancelled", "reason": "price above notional", "ts": ts, "decided": key}
            ch = self._charges.compute(inst, +1, q, to_paise(px), product, d).total
            self.pos[i], self.qty[i], self.entry_px[i], self.entry_ch[i] = 1, q, px, ch
            self.stop_px[i] = px * (1 - r.stop[i])
            return {"config": i, "side": "BUY", "status": "filled", "qty": q, "price": px, "charges_paise": int(ch), "realized_paise": 0,
                    "ts": ts, "decided": key, "product": product}
        q = int(self.qty[i])
        ch = self._charges.compute(inst, -1, q, to_paise(px), product, d, dp_applicable=product == "CNC").total
        gross = (to_paise(px) - to_paise(self.entry_px[i])) * q
        realized = int(gross - self.entry_ch[i] - ch)
        self.pos[i], self.qty[i], self.entry_px[i], self.entry_ch[i], self.stop_px[i] = 0, 0, 0.0, 0, 0.0
        return {"config": i, "side": "SELL", "status": "filled", "qty": q, "price": px, "charges_paise": int(ch), "realized_paise": realized,
                "ts": ts, "decided": key, "product": product}

    # ------------------------------------------------------------------ one minute
    def step(self, ts: pd.Timestamp, symbols: list[str], o, h, lo, c, v, *, allow_orders: bool = True) -> MinuteResult:
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
        # 1. fills at this bar's open
        open_px = np.full(fe.S, np.nan)
        open_px[sel] = o
        pend = np.flatnonzero((self.pend != 0) & has_bar[r.sym])
        for i in pend:
            f = self._fill(int(i), int(self.pend[i]), float(open_px[r.sym[i]]), ts, int(self.pend_key[i]))
            self.pend[i] = 0
            if f:
                res.fills.append(f)
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
        ev = np.flatnonzero(dec >= 0)
        self.evals[ev] += 1
        self.last_eval[ev] = em
        res.evaluated = int(len(ev))
        res.counts = {DECISIONS[k]: int((dec[ev] == k).sum()) for k in np.unique(dec[ev])}
        # 4. actionable decisions -> simulated orders (filled at the next bar's open)
        act = ev[(dec[ev] == D["BUY"]) | (dec[ev] == D["EXIT"])]
        names = ("rule", "stop", "square-off", "no new intraday entries")
        for i in act:
            res.signals.append((int(i), DECISIONS[dec[i]], float(fe.last_close[r.sym[i]]), names[reason[i]]))
            if allow_orders:
                self.pend[i] = 1 if dec[i] == D["BUY"] else -1
                self.pend_key[i] = em
        self.last_minute = em
        return res

    # ------------------------------------------------------------------ checkpoint
    def state(self) -> dict:
        st = {f"fe_{k}": v for k, v in self.fe.state().items()}
        for k in ("pos", "qty", "entry_px", "entry_ch", "stop_px", "pend", "pend_key", "evals", "last_eval"):
            st[k] = getattr(self, k)
        st["last_minute"] = np.array([self.last_minute])
        st["ids"] = self.reg.ids
        return st

    def load(self, st: dict) -> None:
        if not np.array_equal(np.asarray(st["ids"]), self.reg.ids):
            raise ValueError("checkpoint was taken with a different configuration registry")
        self.fe.load({k[3:]: v for k, v in st.items() if k.startswith("fe_")})
        for k in ("pos", "qty", "entry_px", "entry_ch", "stop_px", "pend", "pend_key", "evals", "last_eval"):
            setattr(self, k, np.array(st[k]))
        self.last_minute = int(np.asarray(st["last_minute"])[0])


assert set(TIMEFRAMES) == set(TF_NAMES)

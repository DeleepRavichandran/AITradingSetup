"""A scripted strategy for execution tests: BUY/EXIT decisions at chosen minutes, everything else real.

It runs through MinuteScheduler (engine_factory), the real Executor (shared portfolio, realistic fills, dated
charges) and the real ledger. Only the decision rule is replaced, so a test can state exact prices and sizes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from nseql.screening.engine import D, DECISIONS, MinuteResult, ScreeningEngine
import inspect

from nseql.screening.execution import Executor, PortfolioRules

# signature-adaptive so the same tests can be run against the reviewed commit (before the fixes) for the before/after table
_FILL_BLOCK = "block" in inspect.signature(Executor.fill).parameters
_SUBMIT_RO = "reduce_only" in inspect.signature(Executor.submit).parameters
from nseql.screening.features import SESSION_MINUTES, minute_of_session
from nseql.screening.registry import FAMILIES, TF_NAMES, Registry, config_id


def build(cfgs: list[tuple[str, bool]], symbols: list[str]) -> Registry:
    n = len(cfgs)
    sym = np.array([symbols.index(s) for s, _ in cfgs], dtype=np.int32)
    ids = np.array([config_id("scripted", "1m", {"k": k}, s, 0.0) for k, (s, _) in enumerate(cfgs)])
    z = np.zeros(n)
    neg = np.full(n, -1, dtype=np.int16)
    return Registry(ids, sym, np.zeros(n, dtype=np.int8), np.full(n, FAMILIES.index("orb"), dtype=np.int8), neg, neg, z, neg, neg, z,
                    np.full(n, 0.5), np.array([i for _, i in cfgs], dtype=bool), [{"k": k} for k in range(n)], list(symbols))


class ScriptedEngine(ScreeningEngine):
    def __init__(self, reg, exec_cfg, portfolio, script, qty=None):
        super().__init__(reg, exec_cfg, portfolio)
        self.script, self.fixed_qty = script, qty or {}

    def step(self, ts, symbols, o, h, lo, c, v, *, allow_orders=True, block_reason="kill switch engaged", reduce_only=False):
        ts = pd.Timestamp(ts)
        em = self.epoch_min(ts)
        if em <= self.last_minute:
            return MinuteResult(ts, "duplicate")
        assert 1 <= minute_of_session(ts) <= SESSION_MINUTES
        fe = self.fe
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
        res.fills, res.done = self.x.fill(ts, has_bar, open_px, vol, **({"block": block_reason if (reduce_only and not allow_orders) else None} if _FILL_BLOCK else {}))
        res.closed = fe.update(ts, sel, o, h, lo, c, v)
        self.x.alloc.observe(has_bar, fe.last_close, vol)
        dec = self.script.get(ts.strftime("%H:%M"), {})
        act = [i for i in dec if (dec[i] == "BUY" and self.pos[i] == 0 and self.pend[i] == 0) or (dec[i] == "EXIT" and self.pos[i] == 1 and self.pend[i] == 0)]
        for i in act:
            res.signals.append((i, dec[i], float(fe.last_close[self.reg.sym[i]]), "rule"))
        ev = self.x.check_loss(ts)
        if ev:
            res.events.append(ev)
        self.x.qty_cap = {i: q for i, q in self.fixed_qty.items()}
        try:
            res.orders = self.x.submit(act, {i: dec[i] for i in act}, em, allow_orders, block_reason, *((reduce_only,) if _SUBMIT_RO else ()))
        finally:
            self.x.qty_cap = None
        self.last_minute = em
        return res



def factory(cfgs, script, qty=None):
    def make(symbols, exec_cfg, portfolio):
        reg = build(cfgs, symbols)
        return reg, ScriptedEngine(reg, exec_cfg, portfolio, script, qty)
    return make


def bar(sym, ts, px, vol=1_000_000, hi=None, lo=None, op=None):
    return {"symbol": sym, "ts": pd.Timestamp(ts), "open": op if op is not None else px, "high": hi if hi is not None else max(px, op or px),
            "low": lo if lo is not None else min(px, op or px), "close": px, "volume": vol}


def rules(capital, **kw) -> PortfolioRules:
    base = dict(capital=capital, per_trade_pct=1.0, max_symbol_pct=1.0, max_gross_pct=1.0, max_positions=50, daily_loss_pct=0.5, cost_buffer=0.0)
    base.update(kw)
    return PortfolioRules(**base)


__all__ = ["D", "DECISIONS", "TF_NAMES", "build", "factory", "bar", "rules"]

"""Run one screened configuration (from the 49,728-configuration universe) as an ordinary strategy,
so a screener or Learning Brain pick can be backtested in the event engine and paper-traded.

At every daily close it rebuilds the screener's indicator panel from the history the engine has
shown so far (nothing later), takes the target weights for today, and orders the difference. The
same code is used by the backtester and by paper sessions (replay or the end-of-day Kite feed).

Context-filtered configurations (ctx_*) are refused: they need context and event data delivered
day by day, which paper trading does not have yet.
"""
from __future__ import annotations

import numpy as np
from nseql.strategies.api import Strategy


def find_config(key: str):
    from nseql.evaluation.space import universe
    cfg = next((c for c in universe() if c.key == key), None)
    if cfg is None:
        ctx_cfg = next((c for c in universe(True) if c.key == key), None)
        if ctx_cfg is not None:
            raise ValueError(f"configuration {key} uses the market-context filter '{ctx_cfg.regime}'; paper trading of context "
                             "filters is NOT IMPLEMENTED (it needs live context and event data each day)")
        raise ValueError(f"no screened configuration with key {key!r}")
    return cfg


class ScreenedConfig(Strategy):
    """A configuration from the screening universe, identified by its 16-character key."""
    name = "screened_config"
    family = "screened"
    hypothesis = "Whatever the screened configuration's family states; it must be re-validated here before being trusted."
    default_params = {"key": "", "band": 0.0}
    requires = "EQ"
    intervals = ("1d",)

    def __init__(self, **params):
        super().__init__(**params)
        self.cfg = find_config(self.params["key"])

    def _panel(self, ctx):
        from nseql.evaluation.cache import derive, from_arrays
        syms = sorted(ctx.universe("EQ"))
        idxs = ctx.universe("INDEX")
        if not syms or not idxs:
            return None, syms
        idx = "NIFTY50" if "NIFTY50" in idxs else sorted(idxs)[0]
        ic = np.asarray(ctx.history(idx, "close"), dtype=float)
        T = len(ic)
        if T < 3:
            return None, syms
        raw = {"ts": np.arange(T), "symbols": syms, "index_symbol": idx, "idx_close": ic}
        for f in ("open", "high", "low", "close", "volume"):
            m = np.full((T, len(syms)), np.nan)
            for j, s in enumerate(syms):
                h = np.asarray(ctx.history(s, f), dtype=float)[-T:]
                if len(h):
                    m[T - len(h):, j] = h            # symbols that listed later are right-aligned
            raw[f] = m
        return from_arrays(derive(raw)), syms

    def on_bar(self, ctx):
        if not ctx.is_last_bar_of_session():
            return
        from nseql.evaluation.vector import weights
        P, syms = self._panel(ctx)
        if P is None:
            return
        w = weights(self.cfg, P)[-1]
        eq = ctx.equity()
        for j, s in enumerate(syms):
            px = ctx.last(s)
            if not px or not ctx.has_bar(s) or ctx.open_orders(s):
                continue
            target = int(w[j] * eq / px)
            if target != ctx.position(s) and abs(target - ctx.position(s)) * px > self.params["band"] * eq:
                ctx.order_target(s, target, tag=f"screened:{self.cfg.key}")

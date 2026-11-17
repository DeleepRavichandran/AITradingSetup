"""Cross-check the vectorised screener against the event-driven backtester.

`ConfigStrategy` trades a screened configuration inside the real engine: at each close it sets
every symbol to the target weight the screener computed for that day (target weights are causal,
so precomputing them over the whole panel is equivalent to recomputing each day on truncated data
— tests/brain/test_evaluation.py checks that). The engine then applies its own fills, integer
shares, volume caps, dated charges and ledger.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from nseql.backtester.engine import Allocation, BacktestEngine
from nseql.backtester.execution import ExecutionConfig
from nseql.data.portal import DataPortal
from nseql.data.store import Dataset
from nseql.evaluation import cache as cache_mod
from nseql.evaluation.space import Config
from nseql.evaluation.vector import evaluate, weights
from nseql.strategies.api import Strategy


class ConfigStrategy(Strategy):
    name = "screened_config"
    family = "screened"
    default_params: dict = {}

    def __init__(self, W: np.ndarray, symbols: list[str], dates: list, band: float = 0.0):
        super().__init__()
        self.W, self.symbols, self.band = W, symbols, band
        self.row = {d: i for i, d in enumerate(dates)}

    def on_bar(self, ctx):
        i = self.row.get(ctx.now.date())
        if i is None or not ctx.is_last_bar_of_session():
            return
        eq = ctx.equity()
        for j, s in enumerate(self.symbols):
            px = ctx.last(s)
            if not px or not ctx.has_bar(s) or ctx.open_orders(s):
                continue
            target = int(self.W[i, j] * eq / px)
            if abs(target - ctx.position(s)) * px > self.band * eq:
                ctx.order_target(s, target, tag="screened")


def compare(ds: Dataset, cfg: Config, panel: cache_mod.Panel, cost: float, capital: float = 10_000_000.0, band: float = 0.0) -> dict:
    """Daily-return agreement between the screener and the event engine for one configuration."""
    W = weights(cfg, panel)
    syms = panel.meta["symbols"]
    dates = panel.dates()
    portal = DataPortal.from_dataset(ds, "1d", [*syms, panel.meta["index"]])
    eng = BacktestEngine(portal, [Allocation(ConfigStrategy(W, syms, dates, band), "b", capital)],
                         exec_cfg=ExecutionConfig(max_participation=1.0))
    res = eng.run()
    if res.error:
        raise RuntimeError(res.error)
    ev = pd.Series(res.equity / 100.0, index=pd.to_datetime(res.ts)).groupby(lambda x: x.normalize()).last().pct_change().fillna(0.0)
    _, r, _ = evaluate(cfg, panel, cost)
    # The screener books open-to-open returns: R[t+1] covers open t -> open t+1, which is the same
    # holding period as the engine's mark-to-close return for day t (up to the overnight gap).
    idx = pd.to_datetime(panel["ts"]).normalize()
    vec = pd.Series(r[1:].astype(float), index=idx[:-1])
    j = ev.index.intersection(vec.index)[260:]
    a, b = ev[j].to_numpy(), vec[j].to_numpy()
    return {"config": cfg.describe(), "days": int(len(j)), "corr": float(np.corrcoef(a, b)[0, 1]) if a.std() > 0 and b.std() > 0 else None,
            "event_total": float(np.prod(1 + a) - 1), "screen_total": float(np.prod(1 + b) - 1),
            "event_ann_vol": float(a.std() * np.sqrt(252)), "tracking_error_ann": float((a - b).std() * np.sqrt(252)),
            "ann_return_gap": float((b.mean() - a.mean()) * 252),
            "event_costs": float(sum(f.charges.get("total", 0) for f in res.account.fills) / 100 / capital),
            "reconciled": bool(res.account.reconcile()["ok"])}

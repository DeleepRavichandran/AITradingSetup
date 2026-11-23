"""Hand-built miniature markets for precise unit tests."""
from __future__ import annotations

from datetime import date

import pandas as pd

from nseql.backtester.engine import Allocation, BacktestEngine
from nseql.backtester.execution import ExecutionConfig
from nseql.data.portal import DataPortal
from nseql.instruments.models import Instrument, Kind
from nseql.instruments.rules import BrokerageProfile, ChargeSchedule
from nseql.strategies.api import Strategy

# No spread / impact / fees unless a test asks for them, so expected numbers are exact.
FRICTIONLESS = dict(spread_bps_eq=0, spread_bps_fut=0, spread_bps_opt=0, min_spread_ticks_opt=0,
                    impact_coeff_bps=0, max_participation=1.0)


def days(n: int, start="2025-01-06") -> list[pd.Timestamp]:
    return list(pd.bdate_range(start, periods=n) + pd.Timedelta(hours=15, minutes=30))


def frame(rows, start="2025-01-06", ts=None) -> pd.DataFrame:
    """rows: list of (open, high, low, close[, volume])."""
    ts = ts or days(len(rows), start)
    return pd.DataFrame({"ts": ts, "open": [r[0] for r in rows], "high": [r[1] for r in rows],
                         "low": [r[2] for r in rows], "close": [r[3] for r in rows],
                         "volume": [r[4] if len(r) > 4 else 1_000_000 for r in rows], "oi": 0})


def flat(prices, vol=1_000_000):
    return [(p, p, p, p, vol) for p in prices]


EQ = Instrument("AAA", Kind.EQ)
IDX = Instrument("IDX", Kind.INDEX)


def fut(symbol="IDXFUT", expiry=date(2025, 1, 10), lot=50):
    return Instrument(symbol, Kind.FUT, segment="FO", lot_size=lot, underlying="IDX", underlying_kind="INDEX", expiry=expiry)


def opt(strike, kind, expiry=date(2025, 1, 10), lot=50, symbol=None):
    return Instrument(symbol or f"IDX{int(strike)}{kind}", Kind.OPT, segment="FO", lot_size=lot, underlying="IDX",
                      underlying_kind="INDEX", expiry=expiry, strike=float(strike), option_type=kind)


class Script(Strategy):
    """Runs `actions[bar_index](ctx)` so a test can place exact orders on exact bars."""
    name = "script"
    default_params = {}

    def __init__(self, actions: dict):
        super().__init__()
        self.actions = actions
        self.i = -1
        self.fills = []

    def on_bar(self, ctx):
        self.i += 1
        if self.i in self.actions:
            self.actions[self.i](ctx)

    def on_fill(self, ctx, fill):
        self.fills.append(fill)


def run(frames: dict, instruments: list[Instrument], actions: dict, *, capital=1_000_000.0, exec_kw=None,
        fees=False, actions_ca=None, interval="1d", risk=None, margin_rules=None, strategies=None):
    portal = DataPortal(frames, {i.symbol: i for i in instruments}, actions_ca or [], interval)
    charges = ChargeSchedule() if fees else ChargeSchedule(brokerage=BrokerageProfile.zero(), multiplier=0.0)
    allocs = strategies or [Allocation(Script(actions), "t", capital)]
    eng = BacktestEngine(portal, allocs, exec_cfg=ExecutionConfig(**{**FRICTIONLESS, **(exec_kw or {})}),
                         charges=charges, risk=risk, margin_rules=margin_rules)
    res = eng.run()
    assert res.error is None, res.error
    assert res.account.reconcile()["ok"], res.account.reconcile()
    return res

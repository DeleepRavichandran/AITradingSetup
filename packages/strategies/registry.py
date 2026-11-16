"""Catalogue of built-in strategies."""
from __future__ import annotations

from nseql.strategies.api import Strategy
from nseql.strategies.library import derivatives, meanrev, ml, priceaction, trend

_ALL = [trend.SmaCross, trend.MomentumRank, trend.DonchianBreakout, trend.MultiTimeframeTrend, trend.BuyAndHold,
        trend.RandomEntry, meanrev.RsiReversion, meanrev.BollingerReversion, meanrev.ZScoreReversion,
        priceaction.OpeningRangeBreakout, priceaction.VwapReversion, priceaction.GapFade,
        priceaction.PrevDayBreakout, priceaction.VolumeBreakout, derivatives.FuturesTrend, derivatives.FuturesCarry,
        derivatives.OptionStructure, derivatives.CalendarSpread, ml.MLDirection]

STRATEGIES: dict[str, type[Strategy]] = {c.name: c for c in _ALL}


def get(name: str) -> type[Strategy]:
    if name not in STRATEGIES:
        raise KeyError(f"unknown strategy '{name}'. Known: {sorted(STRATEGIES)}")
    return STRATEGIES[name]


def create(name: str, params: dict | None = None) -> Strategy:
    return get(name)(**(params or {}))

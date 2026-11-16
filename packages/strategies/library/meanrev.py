"""Mean-reversion baselines (daily bars, cash equities, long-only)."""
from __future__ import annotations

import math

from nseql import indicators as ind
from nseql.strategies.api import Strategy
from nseql.strategies.library.trend import _slot_value


class RsiReversion(Strategy):
    """Buy when RSI falls below `buy_below` while price is above its long SMA (buy dips in
    uptrends); exit when RSI recovers above `exit_above` or after `max_hold` bars."""
    name = "rsi_reversion"
    family = "mean_reversion"
    hypothesis = ("Short-term oversold readings inside a long-term uptrend revert within days; "
                  "falsified if average net trade P&L is not positive after doubling costs.")
    default_params = {"rsi_n": 3, "buy_below": 15, "exit_above": 60, "trend": 150, "max_hold": 10,
                      "max_positions": 5, "invest": 0.95}
    param_grid = {"rsi_n": [2, 3, 5], "buy_below": [10, 15, 25], "exit_above": [50, 60, 70], "trend": [100, 150, 200]}

    def on_bar(self, ctx):
        p = self.params
        age = self.state.setdefault("age", {})
        n_pos = len([q for q in ctx.positions().values() if q > 0])
        for s in ctx.universe("EQ"):
            c = ctx.history(s, "close", p["trend"] + 1)
            if len(c) < p["trend"]:
                continue
            r = ind.rsi(c[-(p["rsi_n"] * 10 + 1):], p["rsi_n"])
            if ctx.position(s) > 0:
                age[s] = age.get(s, 0) + 1
                if r > p["exit_above"] or age[s] >= p["max_hold"]:
                    ctx.order_target(s, 0, tag="rsi_exit")
                    age.pop(s, None)
            elif (not math.isnan(r) and r < p["buy_below"] and c[-1] > ind.sma(c, p["trend"])
                  and n_pos < p["max_positions"] and not ctx.open_orders(s)):
                ctx.order_target_value(s, _slot_value(ctx, p["max_positions"], p["invest"]), tag="rsi_entry")
                n_pos += 1


class BollingerReversion(Strategy):
    """Buy a close below the lower Bollinger band, exit at the middle band or a time stop."""
    name = "bollinger_reversion"
    family = "mean_reversion"
    hypothesis = ("Closes more than k standard deviations below the 20-day mean overshoot and revert to the mean; "
                  "falsified if the edge disappears in the high-volatility regime where bands widen.")
    default_params = {"n": 20, "k": 2.0, "max_hold": 15, "max_positions": 5, "invest": 0.95}
    param_grid = {"n": [10, 20, 30], "k": [1.5, 2.0, 2.5], "max_hold": [5, 10, 15]}

    def on_bar(self, ctx):
        p = self.params
        age = self.state.setdefault("age", {})
        n_pos = len([q for q in ctx.positions().values() if q > 0])
        for s in ctx.universe("EQ"):
            c = ctx.history(s, "close", p["n"] + 1)
            if len(c) < p["n"]:
                continue
            lo, mid, _ = ind.bollinger(c, p["n"], p["k"])
            if ctx.position(s) > 0:
                age[s] = age.get(s, 0) + 1
                if c[-1] >= mid or age[s] >= p["max_hold"]:
                    ctx.order_target(s, 0, tag="bb_exit")
                    age.pop(s, None)
            elif c[-1] < lo and n_pos < p["max_positions"] and not ctx.open_orders(s):
                ctx.order_target_value(s, _slot_value(ctx, p["max_positions"], p["invest"]), tag="bb_entry")
                n_pos += 1


class ZScoreReversion(Strategy):
    """Statistical mean reversion on the z-score of price versus its rolling mean, entered with a
    resting LIMIT order below the close (demonstrates passive entries)."""
    name = "zscore_reversion"
    family = "mean_reversion"
    hypothesis = ("A z-score below -z_entry reverts toward zero; a passive limit entry captures extra edge. "
                  "Falsified if limit-fill selection (only filled when price keeps falling) erases the edge.")
    default_params = {"n": 30, "z_entry": 2.0, "z_exit": 0.0, "limit_offset": 0.005, "max_hold": 20,
                      "max_positions": 5, "invest": 0.95}
    param_grid = {"n": [20, 30, 60], "z_entry": [1.5, 2.0, 2.5], "limit_offset": [0.0, 0.005, 0.01]}

    def on_bar(self, ctx):
        p = self.params
        age = self.state.setdefault("age", {})
        n_pos = len([q for q in ctx.positions().values() if q > 0])
        for s in ctx.universe("EQ"):
            c = ctx.history(s, "close", p["n"] + 1)
            if len(c) < p["n"]:
                continue
            z = ind.zscore(c, p["n"])
            if ctx.position(s) > 0:
                age[s] = age.get(s, 0) + 1
                if z >= p["z_exit"] or age[s] >= p["max_hold"]:
                    ctx.order_target(s, 0, tag="z_exit")
                    age.pop(s, None)
            elif z < -p["z_entry"] and n_pos < p["max_positions"] and not ctx.open_orders(s):
                px = c[-1] * (1 - p["limit_offset"])
                raw_px = ctx.last(s) * (1 - p["limit_offset"])
                qty = int(_slot_value(ctx, p["max_positions"], p["invest"]) / max(raw_px, 0.05))
                if qty > 0 and px > 0:
                    ctx.order(s, qty, type="LIMIT", limit=raw_px, tag="z_entry")
                    n_pos += 1

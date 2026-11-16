"""Trend and momentum baselines (daily bars, cash equities, long-only)."""
from __future__ import annotations

import math

from nseql import indicators as ind
from nseql.strategies.api import Strategy


def _slot_value(ctx, n_slots: int, invest: float) -> float:
    return ctx.equity() * invest / max(n_slots, 1)


class SmaCross(Strategy):
    """Long when the fast SMA is above the slow SMA, flat otherwise. Equal capital per symbol."""
    name = "sma_cross"
    family = "trend"
    hypothesis = ("Prices that are above their long-run average tend to keep drifting up for some weeks; "
                  "falsified if net Sharpe is not above equal-weight buy-and-hold out of sample.")
    default_params = {"fast": 20, "slow": 100, "invest": 0.95}
    param_grid = {"fast": [10, 20, 30, 50], "slow": [60, 100, 150, 200]}

    def on_bar(self, ctx):
        p = self.params
        syms = ctx.universe("EQ")
        n = max(len(syms), 1)
        for s in syms:
            c = ctx.history(s, "close", p["slow"] + 1)
            if len(c) < p["slow"]:
                continue
            want = ind.sma(c, p["fast"]) > ind.sma(c, p["slow"])
            have = ctx.position(s) > 0
            if want and not have and not ctx.open_orders(s):
                ctx.order_target_value(s, _slot_value(ctx, n, p["invest"]), tag="trend_entry")
            elif not want and have:
                ctx.order_target(s, 0, tag="trend_exit")


class MomentumRank(Strategy):
    """Cross-sectional momentum: every `rebalance` bars hold the top `top_k` symbols by trailing
    return (skipping the most recent `skip` bars)."""
    name = "momentum_rank"
    family = "momentum"
    hypothesis = ("Recent relative winners keep outperforming recent losers over the next month; "
                  "falsified if the top-k portfolio does not beat the equal-weight universe net of costs.")
    default_params = {"lookback": 120, "skip": 5, "top_k": 4, "rebalance": 21, "invest": 0.95}
    param_grid = {"lookback": [60, 120, 180, 250], "top_k": [3, 4, 5], "rebalance": [10, 21, 42]}

    def on_bar(self, ctx):
        p = self.params
        self.state["i"] = self.state.get("i", -1) + 1
        if self.state["i"] % p["rebalance"]:
            return
        scores = {}
        for s in ctx.universe("EQ"):
            c = ctx.history(s, "close", p["lookback"] + p["skip"] + 1)
            if len(c) > p["lookback"] + p["skip"]:
                scores[s] = c[-1 - p["skip"]] / c[0] - 1
        if len(scores) < p["top_k"]:
            return
        winners = set(sorted(scores, key=lambda s: (-scores[s], s))[: p["top_k"]])
        for s, q in ctx.positions().items():
            if q > 0 and s not in winners:
                ctx.order_target(s, 0, tag="rank_exit")
        for s in sorted(winners):
            ctx.order_target_value(s, _slot_value(ctx, p["top_k"], p["invest"]), tag="rank_entry")


class DonchianBreakout(Strategy):
    """Buy a close above the prior N-bar high; exit on a close below the prior M-bar low or an
    ATR-multiple stop (placed as a resting stop order so it can trigger intrabar)."""
    name = "donchian_breakout"
    family = "breakout"
    hypothesis = ("A new N-day closing high marks the start of a trend continuation; "
                  "falsified if breakout entries do not outperform random entries with the same holding period.")
    default_params = {"entry": 55, "exit": 20, "atr_n": 20, "atr_stop": 3.0, "max_positions": 5, "invest": 0.95}
    param_grid = {"entry": [20, 55, 100], "exit": [10, 20, 40], "atr_stop": [2.0, 3.0, 4.0]}

    def on_bar(self, ctx):
        p = self.params
        held = [s for s, q in ctx.positions().items() if q > 0]
        for s in ctx.universe("EQ"):
            n = max(p["entry"], p["exit"], p["atr_n"]) + 2
            c = ctx.history(s, "close", n)
            if len(c) < n:
                continue
            if ctx.position(s) > 0:
                if c[-1] < ind.lowest(c[:-1], p["exit"]):
                    ctx.cancel_all(s)
                    ctx.order_target(s, 0, tag="channel_exit")
                elif not ctx.open_orders(s):          # re-arm the protective stop (GTC)
                    self._stop(ctx, s, c)
            elif c[-1] > ind.highest(c[:-1], p["entry"]) and len(held) < p["max_positions"] and not ctx.open_orders(s):
                ctx.order_target_value(s, _slot_value(ctx, p["max_positions"], p["invest"]), tag="breakout_entry")
                held.append(s)

    def _stop(self, ctx, s, c):
        p = self.params
        a = ind.atr(ctx.history(s, "high", p["atr_n"] + 1), ctx.history(s, "low", p["atr_n"] + 1), c[-p["atr_n"] - 1:], p["atr_n"])
        if not math.isnan(a):
            ctx.order(s, -ctx.position(s), type="STOP", stop=max(ctx.entry_price(s) - p["atr_stop"] * a, 0.05),
                      tif="GTC", tag="atr_stop")


class MultiTimeframeTrend(Strategy):
    """Long only when the weekly trend (SMA of every 5th close) and the daily trend agree."""
    name = "mtf_trend"
    family = "trend"
    hypothesis = ("Daily trend signals are more reliable when the weekly trend points the same way; "
                  "falsified if the weekly filter does not reduce drawdown versus the unfiltered daily rule.")
    default_params = {"daily": 20, "weekly": 26, "invest": 0.95}
    param_grid = {"daily": [10, 20, 50], "weekly": [13, 26, 40]}

    def on_bar(self, ctx):
        p = self.params
        syms = ctx.universe("EQ")
        for s in syms:
            c = ctx.history(s, "close", p["weekly"] * 5 + 5)
            if len(c) < p["weekly"] * 5:
                continue
            weekly = c[::-1][::5][::-1]            # every 5th close ending at the latest bar
            up = c[-1] > ind.sma(c, p["daily"]) and weekly[-1] > ind.sma(weekly, p["weekly"])
            if up and ctx.position(s) == 0 and not ctx.open_orders(s):
                ctx.order_target_value(s, _slot_value(ctx, len(syms), p["invest"]), tag="mtf_entry")
            elif not up and ctx.position(s) > 0:
                ctx.order_target(s, 0, tag="mtf_exit")


class BuyAndHold(Strategy):
    """Baseline: buy the whole universe equal-weight on the first bar and hold."""
    name = "buy_and_hold"
    family = "baseline"
    hypothesis = "Baseline, not a hypothesis: the return available with no skill."
    default_params = {"invest": 0.97}

    def on_bar(self, ctx):
        done = self.state.setdefault("bought", set())
        syms = ctx.universe("EQ")
        n = max(len(syms), self.state.setdefault("n", len(syms)))
        for s in syms:
            if s not in done:
                done.add(s)
                ctx.order_target_value(s, _slot_value(ctx, n, self.params["invest"]), tag="hold")


class RandomEntry(Strategy):
    """Baseline: enter at pseudo-random times and hold a fixed number of bars. Deterministic given
    `seed`. A strategy that cannot beat this has no demonstrated entry edge."""
    name = "random_entry"
    family = "baseline"
    hypothesis = "Baseline, not a hypothesis: what random timing earns with the same costs."
    default_params = {"hold": 10, "prob": 0.05, "seed": 7, "max_positions": 5, "invest": 0.95}

    def on_bar(self, ctx):
        import numpy as np
        p = self.params
        rng = self.state.setdefault("rng", np.random.default_rng(p["seed"]))
        age = self.state.setdefault("age", {})
        for s in ctx.universe("EQ"):
            u = rng.random()
            if ctx.position(s) > 0:
                age[s] = age.get(s, 0) + 1
                if age[s] >= p["hold"]:
                    ctx.order_target(s, 0, tag="random_exit")
                    age.pop(s)
            elif u < p["prob"] and len(ctx.positions()) < p["max_positions"] and not ctx.open_orders(s):
                ctx.order_target_value(s, _slot_value(ctx, p["max_positions"], p["invest"]), tag="random_entry")

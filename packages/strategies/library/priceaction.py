"""Price-action strategies: opening range, previous-day levels, gaps, VWAP, volume breakouts."""
from __future__ import annotations

import math
from datetime import time

import numpy as np

from nseql import indicators as ind
from nseql.strategies.api import Strategy
from nseql.strategies.library.trend import _slot_value


class _Intraday(Strategy):
    intervals = ("1m", "3m", "5m", "15m")

    def _session(self, ctx, s):
        """Bars of the current session so far for symbol s -> (open, high, low, close, volume) arrays."""
        day = self.state.setdefault("day", {})
        d = ctx.now.date()
        if day.get(s, (None, 0))[0] != d:
            day[s] = (d, ctx.bars_seen(s) - 1)
        n = ctx.bars_seen(s) - day[s][1]
        return tuple(ctx.history(s, f, n, adjusted=False) for f in ("open", "high", "low", "close", "volume"))


class OpeningRangeBreakout(_Intraday):
    """Intraday (MIS). After the first `range_minutes`, buy a break above the opening-range high
    (or sell short a break below the low) with a stop at the other side of the range; one trade
    per symbol per day; the engine squares off at 15:20."""
    name = "opening_range_breakout"
    family = "price_action"
    hypothesis = ("A break of the first 15-minute range predicts the direction of the rest of the session; "
                  "falsified if net expectancy per trade is not positive after intraday costs and slippage.")
    default_params = {"range_minutes": 15, "last_entry": "13:00", "risk_per_trade": 0.005, "allow_short": True,
                      "max_leverage": 1.0}
    param_grid = {"range_minutes": [5, 15, 30], "risk_per_trade": [0.0025, 0.005, 0.01]}

    def on_bar(self, ctx):
        p = self.params
        hh, mm = map(int, p["last_entry"].split(":"))
        traded = self.state.setdefault("traded", {})
        for s in ctx.universe("EQ"):
            o, h, l, c, _ = self._session(ctx, s)
            mins = len(c) * {"1m": 1, "3m": 3, "5m": 5, "15m": 15}[ctx.interval]
            k = max(p["range_minutes"] // {"1m": 1, "3m": 3, "5m": 5, "15m": 15}[ctx.interval], 1)
            if mins <= p["range_minutes"] or traded.get(s) == ctx.now.date() or ctx.now.time() > time(hh, mm):
                continue
            hi, lo = h[:k].max(), l[:k].min()
            width = hi - lo
            if width <= 0 or ctx.position(s) != 0 or ctx.open_orders(s):
                continue
            direction = 1 if c[-1] > hi else -1 if (c[-1] < lo and p["allow_short"]) else 0
            if not direction:
                continue
            qty = int(min(ctx.equity() * p["risk_per_trade"] / width, ctx.equity() * p["max_leverage"] / c[-1]))
            if qty <= 0:
                continue
            traded[s] = ctx.now.date()
            ctx.order(s, direction * qty, product="MIS", tag="orb_entry")
            self.state.setdefault("pending_stop", {})[s] = (direction, lo if direction > 0 else hi)

    def on_fill(self, ctx, fill):
        ps = self.state.get("pending_stop", {})
        if fill.tag == "orb_entry" and fill.symbol in ps and ctx.position(fill.symbol) != 0:
            direction, level = ps.pop(fill.symbol)
            ctx.order(fill.symbol, -ctx.position(fill.symbol), type="STOP", stop=level, product="MIS", tag="orb_stop")


class VwapReversion(_Intraday):
    """Intraday (MIS). Fade deviations of more than `k` session standard deviations from VWAP;
    exit at VWAP or after `max_bars`."""
    name = "vwap_reversion"
    family = "mean_reversion"
    hypothesis = ("Intraday price stretched more than k sigma from session VWAP reverts to VWAP; "
                  "falsified if expectancy is negative after half-spread and impact on 1-minute bars.")
    default_params = {"k": 2.5, "min_bars": 30, "max_bars": 45, "invest": 0.5, "last_entry": "14:30"}
    param_grid = {"k": [2.0, 2.5, 3.0], "max_bars": [20, 45, 90]}

    def on_bar(self, ctx):
        p = self.params
        hh, mm = map(int, p["last_entry"].split(":"))
        age = self.state.setdefault("age", {})
        syms = ctx.universe("EQ")
        for s in syms:
            o, h, l, c, v = self._session(ctx, s)
            if len(c) < p["min_bars"]:
                continue
            typical = (h + l + c) / 3
            vw = ind.vwap(typical, v)
            sd = float(np.std(c - vw))
            pos = ctx.position(s)
            if pos != 0:
                age[s] = age.get(s, 0) + 1
                if (pos > 0 and c[-1] >= vw) or (pos < 0 and c[-1] <= vw) or age[s] >= p["max_bars"]:
                    ctx.order_target(s, 0, product="MIS", tag="vwap_exit")
                    age.pop(s, None)
            elif sd > 0 and not ctx.open_orders(s) and ctx.now.time() <= time(hh, mm) and not math.isnan(vw):
                z = (c[-1] - vw) / sd
                if abs(z) > p["k"]:
                    qty = int(ctx.equity() * p["invest"] / len(syms) / c[-1])
                    if qty:
                        ctx.order(s, -int(math.copysign(qty, z)), product="MIS", tag="vwap_entry")


class GapFade(_Intraday):
    """Intraday (MIS). If the session opens more than `min_gap` away from the previous close, fade
    the gap after the first bar; exit at the previous close (gap fill) or a stop beyond the open."""
    name = "gap_fade"
    family = "price_action"
    hypothesis = ("Opening gaps without news partially fill during the session; "
                  "falsified if gap-fill frequency is not materially above 50% net of costs.")
    default_params = {"min_gap": 0.004, "stop_mult": 1.0, "invest": 0.5}
    param_grid = {"min_gap": [0.003, 0.004, 0.008], "stop_mult": [0.5, 1.0, 2.0]}

    def on_bar(self, ctx):
        p = self.params
        prev = self.state.setdefault("prev_close", {})
        syms = ctx.universe("EQ")
        for s in syms:
            o, h, l, c, _ = self._session(ctx, s)
            if len(c) == 1 and s in prev and ctx.position(s) == 0:
                gap = o[0] / prev[s] - 1
                if abs(gap) >= p["min_gap"]:
                    qty = int(ctx.equity() * p["invest"] / len(syms) / c[-1])
                    side = -1 if gap > 0 else 1
                    if qty:
                        ctx.order(s, side * qty, product="MIS", tag="gap_entry")
                        self.state.setdefault("plan", {})[s] = (prev[s], o[0] * (1 + p["stop_mult"] * gap))
            if ctx.is_last_bar_of_session():
                prev[s] = c[-1]

    def on_fill(self, ctx, fill):
        plan = self.state.get("plan", {})
        if fill.tag == "gap_entry" and fill.symbol in plan and ctx.position(fill.symbol) != 0:
            target, stop = plan.pop(fill.symbol)
            q = -ctx.position(fill.symbol)
            ctx.order(fill.symbol, q, type="LIMIT", limit=target, product="MIS", oco="gap", tag="gap_target")
            ctx.order(fill.symbol, q, type="STOP", stop=stop, product="MIS", oco="gap", tag="gap_stop")


class PrevDayBreakout(Strategy):
    """Daily. Each evening place a buy STOP at the day's high (active the next session only). If
    filled, exit after `hold` sessions. The stop order triggers intrabar the next day."""
    name = "prev_day_breakout"
    family = "price_action"
    hypothesis = ("Trading through the previous day's high during an uptrend leads to follow-through; "
                  "falsified if stop-entry slippage (fills at the worse of stop and open) consumes the edge.")
    default_params = {"trend": 50, "hold": 3, "max_positions": 5, "invest": 0.95}
    param_grid = {"trend": [20, 50, 100], "hold": [1, 3, 5]}

    def on_bar(self, ctx):
        p = self.params
        age = self.state.setdefault("age", {})
        n_pos = len([q for q in ctx.positions().values() if q > 0])
        for s in ctx.universe("EQ"):
            c = ctx.history(s, "close", p["trend"] + 1)
            if len(c) < p["trend"]:
                continue
            if ctx.position(s) > 0:
                age[s] = age.get(s, 0) + 1
                if age[s] >= p["hold"]:
                    ctx.order_target(s, 0, tag="pdh_exit")
                    age.pop(s, None)
            elif c[-1] > ind.sma(c, p["trend"]) and n_pos < p["max_positions"]:
                qty = int(_slot_value(ctx, p["max_positions"], p["invest"]) / ctx.last(s, "high"))
                if qty:
                    ctx.order(s, qty, type="STOP", stop=ctx.last(s, "high") + 0.05, tif="DAY", tag="pdh_entry")


class VolumeBreakout(Strategy):
    """Daily. Buy when the close is the highest in `n` bars AND volume exceeds `vol_mult` times its
    average AND the day's range expands; exit after `hold` bars or on a close below the entry-day low."""
    name = "volume_breakout"
    family = "breakout"
    hypothesis = ("Breakouts confirmed by unusual volume and range expansion have higher follow-through; "
                  "falsified if the volume filter does not improve expectancy over the unfiltered breakout.")
    default_params = {"n": 20, "vol_mult": 1.8, "hold": 10, "max_positions": 5, "invest": 0.95}
    param_grid = {"n": [10, 20, 40], "vol_mult": [1.5, 1.8, 2.5], "hold": [5, 10, 20]}

    def on_bar(self, ctx):
        p = self.params
        info = self.state.setdefault("info", {})
        n_pos = len([q for q in ctx.positions().values() if q > 0])
        for s in ctx.universe("EQ"):
            c = ctx.history(s, "close", p["n"] + 2)
            v = ctx.history(s, "volume", p["n"] + 2)
            if len(c) < p["n"] + 2:
                continue
            if ctx.position(s) > 0:
                age, floor = info.get(s, (0, 0.0))
                info[s] = (age + 1, floor)
                if age + 1 >= p["hold"] or c[-1] < floor:
                    ctx.order_target(s, 0, tag="vb_exit")
                    info.pop(s, None)
            elif (c[-1] > c[:-1].max() and v[-1] > p["vol_mult"] * v[:-1].mean() and n_pos < p["max_positions"]
                  and not ctx.open_orders(s)):
                ctx.order_target_value(s, _slot_value(ctx, p["max_positions"], p["invest"]), tag="vb_entry")
                info[s] = (0, float(ctx.history(s, "low", 1)[-1]))
                n_pos += 1

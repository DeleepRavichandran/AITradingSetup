"""Execution simulation on OHLCV bars.

Causality: an order created while the strategy processes bar t can first be matched
against bar t+1 (plus `latency_bars`). A market order therefore fills at the NEXT bar's
open — never at the close of the bar that generated the signal.

Intrabar ambiguity: a bar gives O/H/L/C but not the path. The rules below are chosen so
that every ambiguity is resolved against the trader:
  * limit orders need the price to trade THROUGH the limit (not merely touch it) unless
    `limit_fill_on_touch` is set — a resting order at the touch is not guaranteed a fill;
  * a limit order that gaps favourably fills at the open (price improvement is real);
  * stop orders fill at the worse of the stop price and the open, plus slippage;
  * if both legs of an OCO pair (stop-loss / take-profit) are reachable in one bar, the
    stop is assumed to have been hit first (`oco_policy="worst"`).

Liquidity: a bar can supply at most `max_participation` of its volume across all orders
in that symbol; the remainder stays working (partial fill). Zero-volume bars fill nothing.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from nseql.backtester.orders import Order, OrderType
from nseql.data.portal import Bar
from nseql.instruments.models import Instrument
from nseql.money import round_to_tick


@dataclass
class ExecutionConfig:
    spread_bps_eq: float = 4.0          # full quoted spread; half is paid on each market order
    spread_bps_fut: float = 2.0
    spread_bps_opt: float = 40.0
    min_spread_ticks_opt: float = 1.0   # options never trade tighter than one tick
    impact_coeff_bps: float = 15.0      # impact = coeff * sqrt(order qty / bar volume)
    max_participation: float = 0.10
    latency_bars: int = 0               # extra bars of delay (stress testing)
    limit_fill_on_touch: bool = False
    oco_policy: str = "worst"           # "worst" | "first_by_id"
    slippage_multiplier: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)


def _slip_fraction(cfg: ExecutionConfig, inst: Instrument, qty: int, bar: Bar, ref: float) -> float:
    spread = cfg.spread_bps_opt if inst.is_option else cfg.spread_bps_fut if inst.is_future else cfg.spread_bps_eq
    half = spread / 2e4
    if inst.is_option and ref > 0:
        half = max(half, cfg.min_spread_ticks_opt * inst.tick_size / 2 / ref)
    impact = cfg.impact_coeff_bps / 1e4 * math.sqrt(qty / bar.volume) if bar.volume > 0 else 0.0
    return (half + impact) * cfg.slippage_multiplier


def fillable_qty(order: Order, inst: Instrument, bar: Bar, cfg: ExecutionConfig, volume_used: int) -> int:
    cap = int(cfg.max_participation * bar.volume) - volume_used
    q = min(order.remaining, max(cap, 0))
    return q // inst.lot_size * inst.lot_size


def match_price(order: Order, inst: Instrument, bar: Bar, cfg: ExecutionConfig, qty: int,
                prev_close: float | None) -> float | None:
    """Price (rupees, on the tick grid) at which `qty` of the order would fill on this bar, or None.
    Pure: it never changes the order, so every matching pass prices the same order the same way. A stop-limit
    order is marked triggered by the engine only AFTER the bar on which its stop was hit (`stop_hit`); on that
    bar it is priced from the trigger, on later bars it rests as a limit order."""
    side = order.side
    o, h, l = bar.open, bar.high, bar.low
    band = inst.price_band_pct
    if band and prev_close:
        upper = round_to_tick(prev_close * (1 + band), inst.tick_size, -1)
        lower = round_to_tick(prev_close * (1 - band), inst.tick_size, 1)
        # locked at circuit: no sellers at the upper limit, no buyers at the lower limit
        if side > 0 and l >= upper - 1e-9:
            return None
        if side < 0 and h <= lower + 1e-9:
            return None
    else:
        upper = lower = None
    typ = order.type
    if typ is OrderType.STOP_LIMIT and order.triggered:
        typ = OrderType.LIMIT
    px: float | None = None
    slip = lambda ref: _slip_fraction(cfg, inst, qty, bar, ref)
    if typ is OrderType.MARKET:
        px = o * (1 + side * slip(o))
    elif typ is OrderType.LIMIT:
        L = order.limit_price
        if side > 0:
            if o <= L:
                px = o
            elif l < L or (cfg.limit_fill_on_touch and l <= L):
                px = L
        else:
            if o >= L:
                px = o
            elif h > L or (cfg.limit_fill_on_touch and h >= L):
                px = L
        if px is None:
            return None
        return round_to_tick(px, inst.tick_size, 0)
    elif typ is OrderType.STOP:
        S = order.stop_price
        if side > 0 and h >= S:
            base = max(S, o)
            px = base * (1 + slip(base))
        elif side < 0 and l <= S:
            base = min(S, o)
            px = base * (1 - slip(base))
    elif typ is OrderType.STOP_LIMIT:
        S, L = order.stop_price, order.limit_price
        if side > 0 and h >= S:
            trig = max(S, o)            # triggered on THIS bar: no price before the trigger is available
            if trig <= L:
                px = trig
            elif l < L:
                px = L
        elif side < 0 and l <= S:
            trig = min(S, o)
            if trig >= L:
                px = trig
            elif h > L:
                px = L
        if px is None:
            return None
        return round_to_tick(px, inst.tick_size, 0)
    if px is None:
        return None
    px = round_to_tick(px, inst.tick_size, side)       # adverse rounding
    if upper is not None:
        px = min(max(px, lower), upper)
    return max(px, inst.tick_size)


def stop_hit(order: Order, bar: Bar) -> bool:
    """True when a stop-limit order's stop is reached on `bar`."""
    if order.type is not OrderType.STOP_LIMIT or order.stop_price is None:
        return False
    return bar.high >= order.stop_price if order.side > 0 else bar.low <= order.stop_price


def is_stop_like(order: Order) -> bool:
    return order.type in (OrderType.STOP, OrderType.STOP_LIMIT)

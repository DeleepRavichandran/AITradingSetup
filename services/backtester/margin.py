"""Margin and buying-power model (approximation of SPAN + exposure; see MarginRules).

Requirement is measured against net liquidation value (equity):

    excess = equity - sum(requirements)

* cash equity, delivery: 100% of market value (no leverage)
* cash equity, intraday (MIS): `eq_mis` of |value|
* futures: rate x notional
* options are netted per (underlying, expiry): the requirement is the smaller of
    (a) naive: long premium value + per-short max(rate*S - OTM amount, rate*S/2) * qty
    (b) defined-risk: current net option value minus the worst-case value at expiry
  so a vertical spread or iron condor is margined at its maximum loss while a naked
  short call is margined at the naive SPAN-like amount.
"""
from __future__ import annotations

from nseql.instruments.models import Instrument
from nseql.instruments.rules import MarginRules


def required(positions: dict[str, tuple[int, str]], instruments: dict[str, Instrument], marks: dict[str, int],
             rules: MarginRules) -> int:
    """positions: symbol -> (signed qty, product). marks in paise. Returns requirement in paise."""
    total = 0.0
    groups: dict[tuple, list[tuple[Instrument, int, int]]] = {}
    for sym, (qty, product) in positions.items():
        if qty == 0:
            continue
        inst = instruments[sym]
        m = marks.get(sym, 0)
        if inst.is_option:
            groups.setdefault((inst.underlying, inst.expiry), []).append((inst, qty, m))
        elif inst.is_future:
            total += rules.fut_rate(inst) * abs(qty) * m
        elif product == "MIS":
            total += rules.eq_mis * abs(qty) * m
        else:
            total += rules.eq_cnc * abs(qty) * m
    for (und, _), legs in groups.items():
        spot = marks.get(und or "", 0)
        naive = 0.0
        for inst, qty, m in legs:
            if qty > 0:
                naive += qty * m
            else:
                s = spot or int(inst.strike * 100)
                otm = max((inst.strike * 100 - s) if inst.option_type == "CE" else (s - inst.strike * 100), 0)
                r = rules.opt_short_rate(inst)
                naive += max(r * s - otm, 0.5 * r * s) * abs(qty)
        req = naive
        if any(q < 0 for _, q, _ in legs):
            value_now = sum(q * m for _, q, m in legs)
            strikes = sorted({i.strike for i, _, _ in legs})
            grid = [0.0, *strikes, strikes[-1] * 3.0]
            worst = min(sum(q * i.intrinsic(s) * 100 for i, q, _ in legs) for s in grid)
            net_calls = sum(q for i, q, _ in legs if i.option_type == "CE")
            if net_calls >= 0:                       # bounded upside loss -> defined risk
                req = min(naive, max(value_now - worst, 0.0))
        total += req
    return int(round(total))

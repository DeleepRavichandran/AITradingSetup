"""Futures and options strategies. All prices come from the contracts' own historical bars."""
from __future__ import annotations

from nseql import indicators as ind
from nseql.strategies.api import Strategy


class FuturesTrend(Strategy):
    """Long/short the near-month index future on an SMA trend of the underlying index, rolling to
    the next contract `roll_days` calendar days before expiry (roll costs are real trades)."""
    name = "futures_trend"
    family = "futures"
    requires = "FUT"
    hypothesis = ("Index trends persist enough to pay for futures roll and carry; "
                  "falsified if net P&L is not positive once roll costs and the futures basis are included.")
    default_params = {"underlying": "SYNNIFTY", "fast": 20, "slow": 80, "roll_days": 3, "lots": 1, "allow_short": True}
    param_grid = {"fast": [10, 20, 40], "slow": [60, 80, 120], "roll_days": [1, 3, 7]}

    def on_bar(self, ctx):
        p = self.params
        und = p["underlying"]
        c = ctx.history(und, "close", p["slow"] + 1)
        exps = [e for e in ctx.expiries(und, "FUT") if (e - ctx.now.date()).days > p["roll_days"]]
        if len(c) < p["slow"] or not exps:
            return
        front = ctx.chain(und, exps[0], "FUT")
        if not front:
            return
        sym = front[0].symbol
        lot = front[0].lot_size
        f, s = ind.sma(c, p["fast"]), ind.sma(c, p["slow"])
        want = p["lots"] * lot * (1 if f > s else (-1 if p["allow_short"] else 0))
        for held, q in ctx.positions().items():
            if held != sym and ctx.instrument(held).underlying == und and not ctx.open_orders(held):
                ctx.order_target(held, 0, tag="roll_out")
        if ctx.position(sym) != want and not ctx.open_orders(sym):
            ctx.order_target(sym, want, tag="trend" if ctx.position(sym) == 0 else "flip")


class FuturesCarry(Strategy):
    """Relative-value / carry research: when the next-month future trades rich to the near month by
    more than `threshold` annualised, sell the calendar spread (short far, long near); close when the
    richness normalises or `exit_days` before near expiry."""
    name = "futures_calendar_carry"
    family = "futures"
    requires = "FUT"
    hypothesis = ("An unusually steep futures term structure mean-reverts toward cost of carry; "
                  "falsified if spread P&L does not exceed two-leg transaction costs.")
    default_params = {"underlying": "SYNNIFTY", "threshold": 0.075, "exit": 0.066, "exit_days": 3, "lots": 1}
    param_grid = {"threshold": [0.07, 0.075, 0.085], "exit_days": [2, 3, 5]}

    def on_bar(self, ctx):
        p = self.params
        und = p["underlying"]
        today = ctx.now.date()
        exps = [e for e in ctx.expiries(und, "FUT") if (e - today).days > 0]
        legs = self.state.get("legs")
        if legs:
            near, far, near_exp = legs
            if ctx.position(near) == 0 and ctx.position(far) == 0:
                self.state["legs"] = None
            elif not ctx.open_orders():
                carry = self._carry(ctx, near, far)
                if (near_exp - today).days <= p["exit_days"] or (carry is not None and carry < p["exit"]):
                    ctx.multi_leg([(near, -ctx.position(near)), (far, -ctx.position(far))], tag="carry_exit")
            return
        if len(exps) < 2 or (exps[0] - today).days <= p["exit_days"] + 2:
            return
        a, b = ctx.chain(und, exps[0], "FUT"), ctx.chain(und, exps[1], "FUT")
        if not a or not b:
            return
        carry = self._carry(ctx, a[0].symbol, b[0].symbol)
        if carry is not None and carry > p["threshold"]:
            q = p["lots"] * a[0].lot_size
            ctx.multi_leg([(a[0].symbol, q), (b[0].symbol, -q)], tag="carry_entry")
            self.state["legs"] = (a[0].symbol, b[0].symbol, exps[0])

    @staticmethod
    def _carry(ctx, near, far):
        pn, pf = ctx.last(near), ctx.last(far)
        days = (ctx.instrument(far).expiry - ctx.instrument(near).expiry).days
        return (pf / pn - 1) * 365 / days if pn and pf and days > 0 else None


STRUCTURES = {
    # name: list of (option type, strike offset in strike steps from ATM, signed lots)
    "long_call": [("CE", 0, 1)],
    "long_put": [("PE", 0, 1)],
    "bull_call_spread": [("CE", 0, 1), ("CE", 3, -1)],        # debit spread
    "bear_put_spread": [("PE", 0, 1), ("PE", -3, -1)],        # debit spread
    "bull_put_spread": [("PE", -2, -1), ("PE", -5, 1)],       # credit spread
    "bear_call_spread": [("CE", 2, -1), ("CE", 5, 1)],        # credit spread
    "long_straddle": [("CE", 0, 1), ("PE", 0, 1)],
    "short_straddle": [("CE", 0, -1), ("PE", 0, -1)],
    "long_strangle": [("CE", 3, 1), ("PE", -3, 1)],
    "short_strangle": [("CE", 4, -1), ("PE", -4, -1)],
    "iron_condor": [("CE", 3, -1), ("CE", 6, 1), ("PE", -3, -1), ("PE", -6, 1)],
    "long_call_butterfly": [("CE", -3, 1), ("CE", 0, -2), ("CE", 3, 1)],
}


class OptionStructure(Strategy):
    """Systematic option structure seller/buyer.

    Opens `structure` on the expiry whose days-to-expiry is closest to `dte_entry` (within
    [dte_min, dte_max]), as one atomic multi-leg order. Closes on a profit target, a loss limit
    (both as multiples of the entry net premium) or `dte_exit` days before expiry; otherwise the
    engine settles the legs at intrinsic value on expiry.
    """
    name = "option_structure"
    family = "options"
    requires = "OPT"
    hypothesis = ("Implied volatility exceeds subsequently realised volatility often enough that a defined-risk "
                  "premium-selling structure earns positive net carry; falsified if tail losses in high-volatility "
                  "regimes exceed the premium collected over the sample.")
    default_params = {"underlying": "SYNNIFTY", "structure": "iron_condor", "width": 1.0, "dte_entry": 30,
                      "dte_min": 20, "dte_max": 40, "dte_exit": 1, "profit_target": 0.5, "loss_limit": 2.0, "lots": 1}
    param_grid = {"structure": list(STRUCTURES), "width": [0.5, 1.0, 1.5], "dte_entry": [15, 30],
                  "profit_target": [0.25, 0.5, 0.75], "loss_limit": [1.0, 2.0, 3.0]}

    def calendar(self, ctx):  # hook for subclasses
        return None

    def on_bar(self, ctx):
        p = self.params
        und = p["underlying"]
        today = ctx.now.date()
        legs = self.state.get("legs")
        if legs:
            live = {s: q for s, q in legs.items() if ctx.position(s) != 0}
            if not live:
                self.state["legs"] = None
                return
            if ctx.open_orders():
                return
            value = sum(ctx.position(s) * (ctx.last(s) or 0.0) for s in live)
            pnl = value - self.state["entry_value"]
            basis = abs(self.state["entry_value"]) or 1.0
            dte = (self.state["expiry"] - today).days
            why = ("profit_target" if pnl >= p["profit_target"] * basis else
                   "loss_limit" if pnl <= -p["loss_limit"] * basis else
                   "time_exit" if dte <= p["dte_exit"] else None)
            if why and all(ctx.has_bar(s) for s in live):
                ctx.multi_leg([(s, -ctx.position(s)) for s in sorted(live)], tag=why)
            return
        if ctx.open_orders():
            return
        spot = ctx.last(und)
        exps = [e for e in ctx.expiries(und, "OPT") if p["dte_min"] <= (e - today).days <= p["dte_max"]]
        if not exps or not spot:
            return
        exp = min(exps, key=lambda e: abs((e - today).days - p["dte_entry"]))
        chain = ctx.chain(und, exp, "OPT")
        strikes = sorted({i.strike for i in chain})
        if len(strikes) < 3:
            return
        atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        by = {(i.strike, i.option_type): i for i in chain}
        order = []
        for typ, off, lots in STRUCTURES[p["structure"]]:
            j = atm_i + int(round(off * p["width"]))
            if not 0 <= j < len(strikes) or (strikes[j], typ) not in by:
                return
            inst = by[(strikes[j], typ)]
            order.append((inst.symbol, lots * p["lots"] * inst.lot_size))
        if len({s for s, _ in order}) != len(order):
            return
        ctx.multi_leg(order, tag=p["structure"])
        self.state["pending"] = (dict(order), exp)

    def on_fill(self, ctx, fill):
        pend = self.state.get("pending")
        if pend and fill.symbol in pend[0]:
            legs, exp = pend
            if all(ctx.position(s) == q for s, q in legs.items()):
                self.state["legs"] = legs
                self.state["expiry"] = exp
                self.state["entry_value"] = sum(ctx.position(s) * ctx.entry_price(s) for s in legs)
                self.state["pending"] = None


class CalendarSpread(Strategy):
    """Sell the near-expiry ATM option and buy the same strike in the next expiry; close both
    `dte_exit` days before the near leg expires. Needs two listed expiries with data."""
    name = "option_calendar_spread"
    family = "options"
    requires = "OPT"
    hypothesis = ("Near-dated ATM options decay faster than far-dated ones, so a long calendar earns positive theta "
                  "when the underlying stays near the strike; falsified if realised moves away from the strike "
                  "dominate the theta collected.")
    default_params = {"underlying": "SYNNIFTY", "kind": "CE", "near_dte_max": 25, "near_dte_min": 10, "dte_exit": 2, "lots": 1}
    param_grid = {"kind": ["CE", "PE"], "near_dte_max": [15, 25], "dte_exit": [1, 2, 5]}

    def on_bar(self, ctx):
        p = self.params
        und = p["underlying"]
        today = ctx.now.date()
        legs = self.state.get("legs")
        if legs:
            near, far, exp = legs
            if ctx.position(near) == 0 and ctx.position(far) == 0:
                self.state["legs"] = None
            elif (exp - today).days <= p["dte_exit"] and not ctx.open_orders() and ctx.has_bar(near) and ctx.has_bar(far):
                ctx.multi_leg([(s, -ctx.position(s)) for s in (near, far) if ctx.position(s)], tag="calendar_exit")
            return
        if ctx.open_orders():
            return
        exps = ctx.expiries(und, "OPT")
        near = [e for e in exps if p["near_dte_min"] <= (e - today).days <= p["near_dte_max"]]
        spot = ctx.last(und)
        if not near or not spot:
            return
        later = [e for e in exps if e > near[0]]
        if not later:
            return
        c1 = {i.strike: i for i in ctx.chain(und, near[0], "OPT") if i.option_type == p["kind"]}
        c2 = {i.strike: i for i in ctx.chain(und, later[0], "OPT") if i.option_type == p["kind"]}
        common = sorted(set(c1) & set(c2))
        if not common:
            return
        k = min(common, key=lambda x: abs(x - spot))
        q = p["lots"] * c1[k].lot_size
        ctx.multi_leg([(c1[k].symbol, -q), (c2[k].symbol, q)], tag="calendar_entry")
        self.state["legs"] = (c1[k].symbol, c2[k].symbol, near[0])

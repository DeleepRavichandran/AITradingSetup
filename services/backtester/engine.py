"""Deterministic event-driven backtest engine.

    Market event -> (match resting orders) -> mark -> strategy -> signal -> risk check
                 -> order -> execution simulation (next bar) -> portfolio -> ledger

Per timeline step, in this fixed order:

  1. session start (first bar of a date): corporate actions effective that date
  2. match orders that were submitted on EARLIER steps against this step's bars
  3. mark positions to this step's closes; margin surveillance (a breach queues liquidation
     orders, which like all orders can first trade on the next bar)
  4. from the intraday cut-off bar onward, queue square-off orders for MIS positions
  5. each strategy's on_bar (may submit orders -> pre-trade risk -> resting queue)
  6. session end (last bar of a date): expiry settlement, futures MTM, DAY-order expiry

Because step 2 precedes step 5 and only considers orders from earlier steps, no order
can ever be filled using information from the bar that generated it. The engine has no
random state: the same inputs give byte-identical ledgers.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from nseql.backtester import margin as margin_mod
from nseql.backtester.execution import ExecutionConfig, fillable_qty, is_stop_like, match_price, stop_hit
from nseql.backtester.orders import Fill, Order, OrderStatus, OrderType
from nseql.backtester.portfolio import Account
from nseql.data.portal import Bar, DataPortal
from nseql.instruments.calendar import MIS_SQUARE_OFF, is_intraday
from nseql.instruments.models import Instrument
from nseql.instruments.rules import ChargeSchedule, MarginRules
from nseql.money import round_to_tick, to_paise
from nseql.risk.limits import KillSwitch, RiskLimits
from nseql.strategies.api import Strategy


@dataclass
class Allocation:
    strategy: Strategy
    book: str
    capital: float          # rupees


@dataclass
class EngineResult:
    account: Account
    orders: list[Order]
    events: list[dict]
    ts: np.ndarray                      # int64 ns
    equity: np.ndarray                  # paise, account total
    book_equity: dict[str, np.ndarray]
    cash: np.ndarray
    gross_exposure: np.ndarray
    margin_used: np.ndarray
    flags: list[str] = field(default_factory=list)
    error: str | None = None


class _Ctx:
    """Concrete strategy context for one book. Attributes with a leading underscore are
    engine internals; generated strategy code is statically forbidden from touching them."""

    def __init__(self, engine: "BacktestEngine", alloc: Allocation):
        self._e = engine
        self._book = alloc.book
        self.params = alloc.strategy.params
        self.interval = engine.portal.interval
        self.now: pd.Timestamp = pd.Timestamp(0)

    # ---- market data (point-in-time)
    def history(self, symbol, field="close", n=None, adjusted=True):
        return self._e.portal.history(symbol, field, n, adjusted)

    def last(self, symbol, field="close"):
        return self._e.portal.last(symbol, field)

    def has_bar(self, symbol):
        return self._e.portal.current(symbol) is not None

    def bars_seen(self, symbol):
        return self._e.portal.bars_seen(symbol)

    def instrument(self, symbol):
        return self._e.instruments[symbol]

    def universe(self, kind="EQ"):
        d = self.now.date()
        return [s for s in self._e.portal.listed_now(d) if self._e.instruments[s].kind.value == kind
                and self._e.portal.current(s) is not None]

    def expiries(self, underlying, kind="OPT"):
        d = self.now.date()
        return sorted({i.expiry for s in self._e.portal.listed_now(d)
                       if (i := self._e.instruments[s]).underlying == underlying and i.kind.value == kind
                       and self._e.portal.current(s) is not None})

    def chain(self, underlying, expiry, kind="OPT"):
        return sorted((i for s in self._e.portal.symbols
                       if (i := self._e.instruments[s]).underlying == underlying and i.kind.value == kind
                       and i.expiry == expiry and self._e.portal.current(s) is not None),
                      key=lambda i: (i.strike or 0, i.option_type or ""))

    # ---- account state
    def position(self, symbol):
        p = self._e.account.books[self._book].positions.get(symbol)
        return p.qty if p else 0

    def positions(self):
        return {s: p.qty for s, p in self._e.account.books[self._book].positions.items()}

    def entry_price(self, symbol):
        p = self._e.account.books[self._book].positions.get(symbol)
        return p.avg_price if p else 0.0

    def cash(self):
        return self._e.account.books[self._book].cash / 100

    def equity(self):
        return self._e.account.book_equity(self._book) / 100

    # ---- orders
    def order(self, symbol, qty, *, type="MARKET", limit=None, stop=None, product=None, tif="DAY", tag="", oco=None):
        if qty == 0:
            return None
        return self._e._submit(self._book, symbol, int(qty), OrderType(type), limit, stop, product, tif, tag, oco)

    def order_target(self, symbol, target_qty, **kw):
        pending = sum(o.side * o.remaining for o in self._e._active(self._book, symbol))
        return self.order(symbol, int(target_qty) - self.position(symbol) - pending, **kw)

    def order_target_value(self, symbol, value, **kw):
        px = self.last(symbol)
        if not px:
            return None
        lot = self._e.instruments[symbol].lot_size
        return self.order_target(symbol, int(value / px) // lot * lot, **kw)

    def multi_leg(self, legs, tag=""):
        return self._e._submit_group(self._book, legs, tag)

    def cancel(self, order_id):
        self._e._cancel(order_id, "cancelled by strategy", self._book)

    def cancel_all(self, symbol=None):
        for o in list(self._e._active(self._book, symbol)):
            self._e._cancel(o.id, "cancelled by strategy", self._book)

    def open_orders(self, symbol=None):
        return [o.to_dict() for o in self._e._active(self._book, symbol)]

    def is_last_bar_of_session(self):
        return self._e._last_of_session

    def log(self, message):
        self._e._event("LOG", book=self._book, message=str(message)[:500])


class BacktestEngine:
    def __init__(self, portal: DataPortal, allocations: list[Allocation], *, exec_cfg: ExecutionConfig | None = None,
                 charges: ChargeSchedule | None = None, margin_rules: MarginRules | None = None,
                 risk: RiskLimits | None = None, liquidate_on_margin_breach: bool = True,
                 kill_switch: KillSwitch | None = None, max_events: int = 2_000_000):
        if len({a.book for a in allocations}) != len(allocations):
            raise ValueError("each strategy needs its own book id")
        self.portal = portal
        self.instruments: dict[str, Instrument] = portal.instruments
        self.allocs = allocations
        self.cfg = exec_cfg or ExecutionConfig()
        self.charges = charges or ChargeSchedule()
        self.margin_rules = margin_rules or MarginRules()
        self.risk = risk or RiskLimits()
        self.liquidate = liquidate_on_margin_breach
        self.kill = kill_switch or KillSwitch()
        self.account = Account(self.instruments)
        self.orders: list[Order] = []
        self.events: list[dict] = []
        self.flags: set[str] = set()
        self.intraday = is_intraday(portal.interval)
        self._max_events = max_events
        self._open: list[Order] = []
        self._k = 0
        self._t = pd.Timestamp(0)
        self._last_of_session = False
        self._cur_date = None
        self._group_seq = 0
        self._prev_session_close: dict[str, float] = {}
        self._sod_equity: dict[str, int] = {}
        self._orders_today: dict[str, int] = defaultdict(int)
        self._halted_today: set[str] = set()
        self._liquidating: set[str] = set()      # books being wound down after a margin call
        self._mis_closed_today = False
        self._dp_charged: set[tuple] = set()
        self._ctx = {a.book: _Ctx(self, a) for a in allocations}
        self._strategy = {a.book: a.strategy for a in allocations}
        by_date: dict[date, list] = defaultdict(list)
        for ca in portal.actions:
            by_date[ca.ex_date].append(ca)
        self._actions = by_date
        self._action_dates = sorted(by_date)

    # =============================================================== main loop
    def run(self) -> EngineResult:
        tl = self.portal.timeline
        n = len(tl)
        eq = np.zeros(n, dtype="int64")
        cash = np.zeros(n, dtype="int64")
        gross = np.zeros(n, dtype="int64")
        mused = np.zeros(n, dtype="int64")
        beq = {a.book: np.zeros(n, dtype="int64") for a in self.allocs}
        if n == 0:
            return EngineResult(self.account, self.orders, self.events, tl, eq, beq, cash, gross, mused,
                                ["no data in range"], "no data in range")
        dates = tl.astype("datetime64[ns]").astype("datetime64[D]")
        self.begin(pd.Timestamp(tl[0]))
        error = None
        try:
            for k in range(n):
                bars = self.portal.advance(int(tl[k]))
                self.step(k, pd.Timestamp(tl[k]), bars, k == n - 1 or dates[k + 1] != dates[k])
                eq[k], cash[k], gross[k], mused[k] = self.snapshot()
                for a in self.allocs:
                    beq[a.book][k] = self.account.book_equity(a.book)
                if len(self.events) > self._max_events:
                    raise RuntimeError("event budget exceeded")
        except Exception as ex:  # a failing strategy must fail the run loudly, never silently
            error = f"{type(ex).__name__}: {ex}"
            self._event("ERROR", message=error)
            k_ok = self._k
            eq, cash, gross, mused = eq[:k_ok], cash[:k_ok], gross[:k_ok], mused[:k_ok]
            beq = {b: v[:k_ok] for b, v in beq.items()}
            tl = tl[:k_ok]
        if self.charges.extrapolated_dates:
            self.flags.add("charge schedule extrapolated before its coverage start")
        return EngineResult(self.account, self.orders, self.events, tl, eq, beq, cash, gross, mused,
                            sorted(self.flags), error)

    # =============================================================== stepping (shared by backtest and paper trading)
    def begin(self, t0: pd.Timestamp) -> None:
        self._t = t0
        self._cur_date = None
        for a in self.allocs:
            self.account.deposit(a.book, to_paise(a.capital), t0)
        for a in self.allocs:
            self._ctx[a.book].now = t0
            a.strategy.on_start(self._ctx[a.book])

    def step(self, k: int, t: pd.Timestamp, bars: dict[str, Bar], last_of_session: bool) -> None:
        """Process one timeline step. The caller must already have advanced the portal to `t`."""
        self._k = k
        self._t = t
        d = t.date()
        if d != self._cur_date:
            prev, self._cur_date = self._cur_date, d
            self._session_start(prev, d, t)
        self._last_of_session = last_of_session
        self._match(k, bars, t)
        for s, b in bars.items():
            self.account.mark(s, b.close)
        self._margin_watch(bars, t)
        if self.intraday and t.time() >= MIS_SQUARE_OFF:
            self._square_off_mis(bars, t)
        for a in self.allocs:
            ctx = self._ctx[a.book]
            ctx.now = t
            a.strategy.on_bar(ctx)
        if last_of_session:
            self._session_end(d, t, bars, k)

    def snapshot(self) -> tuple[int, int, int, int]:
        gross = sum(abs(p.qty) * self.account.marks.get(p.symbol, 0)
                    for b in self.account.books.values() for p in b.positions.values())
        return (self.account.equity(), self.account.cash, gross,
                sum(self._required(b.id) for b in self.account.books.values()))

    # =============================================================== helpers
    def _event(self, typ: str, **kw) -> None:
        self.events.append({"seq": len(self.events) + 1, "ts": str(self._t), "type": typ, **kw})

    def _active(self, book: str | None = None, symbol: str | None = None) -> list[Order]:
        return [o for o in self._open if o.active and (book is None or o.book == book)
                and (symbol is None or o.symbol == symbol)]

    def _positions_map(self, book: str) -> dict[str, tuple[int, str]]:
        return {s: (p.qty, p.product) for s, p in self.account.books[book].positions.items()}

    def _required(self, book: str, positions: dict | None = None, marks: dict | None = None) -> int:
        return margin_mod.required(positions if positions is not None else self._positions_map(book),
                                   self.instruments, marks or self.account.marks, self.margin_rules)

    def _excess_after(self, book: str, deltas: list[tuple[str, int, int, str]], est_charges: int = 0) -> int:
        """Excess margin after hypothetically applying (symbol, signed qty, price paise, product) deltas."""
        pos = self._positions_map(book)
        marks = dict(self.account.marks)
        for sym, dq, px, product in deltas:
            q, pr = pos.get(sym, (0, product))
            pos[sym] = (q + dq, pr)
            marks[sym] = px
        return self.account.book_equity(book) - est_charges - self._required(book, pos, marks)

    def _reduces(self, book: str, symbol: str, signed_qty: int) -> bool:
        p = self.account.books[book].positions.get(symbol)
        return bool(p) and p.qty * signed_qty < 0 and abs(signed_qty) <= abs(p.qty)

    # =============================================================== order entry
    def _reject(self, o: Order, reason: str) -> None:
        o.status = OrderStatus.REJECTED
        o.reason = reason
        self._event("ORDER_REJECTED", order_id=o.id, book=o.book, symbol=o.symbol, reason=reason)

    def _new_order(self, book, symbol, qty, typ, limit, stop, product, tif, tag, oco, group=None) -> Order:
        inst = self.instruments.get(symbol)
        tick = inst.tick_size if inst else 0.05
        side = 1 if qty > 0 else -1
        if product is None:
            product = "NRML" if inst and inst.is_derivative else "CNC"
        o = Order(len(self.orders) + 1, book, symbol, side, abs(qty), typ,
                  round_to_tick(limit, tick) if limit is not None else None,
                  round_to_tick(stop, tick) if stop is not None else None,
                  product, tif, tag, group, oco, self._t, self._k + 1 + self.cfg.latency_bars)
        self.orders.append(o)
        return o

    def _validate(self, o: Order) -> str | None:
        inst = self.instruments.get(o.symbol)
        d = self._t.date()
        if self.kill.engaged:
            return f"KILL_SWITCH: {self.kill.reason}"
        if inst is None:
            return "UNKNOWN_INSTRUMENT"
        if o.book in self._liquidating or any(x.forced for x in self._active(o.book, o.symbol)):
            return "LIQUIDATION_PENDING (a forced exit is working; new orders are refused)"
        if not inst.tradable:
            return "NOT_TRADABLE (index)"
        if not inst.listed_on(d):
            return "NOT_LISTED on this date"
        if o.symbol in self.risk.banned_symbols:
            return "BANNED_SYMBOL"
        if o.qty <= 0 or o.qty % inst.lot_size:
            return f"BAD_QUANTITY (lot size {inst.lot_size})"
        if o.type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and (o.limit_price is None or o.limit_price <= 0):
            return "LIMIT_PRICE_REQUIRED"
        if o.type in (OrderType.STOP, OrderType.STOP_LIMIT) and (o.stop_price is None or o.stop_price <= 0):
            return "STOP_PRICE_REQUIRED"
        if o.product == "MIS":
            if not self.intraday:
                return "MIS_REQUIRES_INTRADAY_DATA"
            if not inst.is_cash:
                return "MIS_ONLY_FOR_CASH_EQUITY"
            if self._mis_closed_today and not self._last_of_session:
                return "MIS_AFTER_SQUARE_OFF"
        pos = self.account.books[o.book].positions.get(o.symbol)
        if pos and pos.product != o.product:
            return f"PRODUCT_MISMATCH (position is {pos.product})"
        last = self.portal.last(o.symbol)
        if last is None:
            return "NO_PRICE (instrument has not traded yet)"
        reducing = self._reduces(o.book, o.symbol, o.side * o.qty)
        if inst.is_cash and o.side < 0 and o.product != "MIS" and not self.risk.allow_equity_short_overnight:
            held = pos.qty if pos else 0
            working = sum(x.remaining for x in self._active(o.book, o.symbol) if x.side < 0 and x.oco_group is None)
            if o.qty + (0 if o.oco_group else working) > max(held, 0):
                return "SHORT_SELL_NOT_ALLOWED (delivery sells need holdings; use MIS on intraday data)"
        ref = o.limit_price or o.stop_price or last
        band = inst.price_band_pct
        pc = self._prev_session_close.get(o.symbol)
        if band and pc and o.limit_price and not (pc * (1 - band) - 1e-9 <= o.limit_price <= pc * (1 + band) + 1e-9):
            return "PRICE_OUTSIDE_BAND"
        if reducing:
            return None
        if o.book in self._halted_today:
            return "DAILY_LOSS_LIMIT (book halted for the session)"
        if self._orders_today[o.book] >= self.risk.max_orders_per_day:
            return "ORDER_RATE_LIMIT"
        notional = o.qty * ref
        if notional > self.risk.max_order_notional:
            return "MAX_ORDER_NOTIONAL"
        cur = (pos.qty if pos else 0) + o.side * o.qty
        if abs(cur) * ref > self.risk.max_position_notional:
            return "MAX_POSITION_NOTIONAL"
        equity = self.account.book_equity(o.book) / 100
        gross = sum(abs(p.qty) * self.account.marks.get(p.symbol, 0) for p in
                    self.account.books[o.book].positions.values()) / 100
        if equity <= 0 or (gross + notional) / equity > self.risk.max_gross_leverage and not inst.is_option:
            return "MAX_GROSS_LEVERAGE" if equity > 0 else "NO_EQUITY"
        return None

    def _submit(self, book, symbol, qty, typ, limit, stop, product, tif, tag, oco) -> int:
        o = self._new_order(book, symbol, qty, typ, limit, stop, product, tif, tag, oco)
        reason = self._validate(o)
        if reason is None and not self._reduces(book, symbol, qty):
            ref = to_paise(o.limit_price or o.stop_price or self.portal.last(symbol))
            if self._excess_after(book, [(symbol, qty, ref, o.product)]) < 0:
                reason = "INSUFFICIENT_MARGIN (pre-trade)"
        if reason:
            self._reject(o, reason)
            return o.id
        self._orders_today[book] += 1
        self._open.append(o)
        self._event("ORDER_SUBMITTED", order_id=o.id, book=book, symbol=symbol, side=o.side, qty=o.qty,
                    order_type=o.type.value, limit=o.limit_price, stop=o.stop_price, tag=tag)
        return o.id

    def _submit_group(self, book, legs, tag) -> int:
        self._group_seq += 1
        gid = self._group_seq
        orders = [self._new_order(book, s, q, OrderType.MARKET, None, None, None, "DAY", tag, None, gid) for s, q in legs]
        reason = next((r for o in orders if (r := self._validate(o))), None)
        if reason is None:
            deltas = [(o.symbol, o.side * o.qty, to_paise(self.portal.last(o.symbol)), o.product) for o in orders]
            if self._excess_after(book, deltas) < 0:
                reason = "INSUFFICIENT_MARGIN (pre-trade, combined legs)"
        for o in orders:
            if reason:
                self._reject(o, f"group {gid}: {reason}")
            else:
                self._open.append(o)
                self._event("ORDER_SUBMITTED", order_id=o.id, book=book, symbol=o.symbol, side=o.side, qty=o.qty,
                            order_type="MARKET", group=gid, tag=tag)
        if not reason:
            self._orders_today[book] += 1
        return gid

    def _cancel(self, order_id: int, reason: str, book: str | None = None, status=OrderStatus.CANCELLED) -> None:
        o = self.orders[order_id - 1]
        if not o.active or (book is not None and (o.book != book or o.forced)):
            return                              # strategies cannot cancel other books' orders or a liquidation
        o.status = status
        o.reason = reason
        self._event("ORDER_EXPIRED" if status is OrderStatus.EXPIRED else "ORDER_CANCELLED",
                    order_id=o.id, book=o.book, symbol=o.symbol, reason=reason)

    # =============================================================== matching
    def _match(self, k: int, bars: dict[str, Bar], t: pd.Timestamp) -> None:
        try:
            self._match_bar(k, bars, t)
        finally:
            # stop-limit trigger discovery happens once per bar, after every matching pass has priced the bar
            for o in self._open:
                bar = bars.get(o.symbol)
                if o.active and not o.triggered and bar is not None and o.eligible_seq <= k and stop_hit(o, bar):
                    o.triggered = True

    def _match_bar(self, k: int, bars: dict[str, Bar], t: pd.Timestamp) -> None:
        """Match resting orders against this step's bars.

        Three passes, so that liquidity is only ever consumed by orders that actually fill:
          1. reachability — which orders could trade on this bar at all (price + some volume);
          2. OCO — if several siblings are reachable, exactly one survives (the stop, under the
             default worst-case policy), decided BEFORE any volume is handed out;
          3. allocation — in submission order, each survivor takes volume, passes its margin check
             and fills; an order that is rejected or cannot fill takes no volume. A multi-leg group
             reserves volume for all of its legs or for none.
        """
        self._open = [o for o in self._open if o.active]
        prev = self._prev_session_close
        reach: list[Order] = []
        for o in self._open:
            bar = bars.get(o.symbol)
            if bar is None or o.eligible_seq > k or bar.volume <= 0:
                continue
            if o.forced:                       # a liquidation order never exceeds what is still held
                pos = self.account.books[o.book].positions.get(o.symbol)
                need = -pos.qty if pos else 0
                if need * o.side <= 0:
                    self._cancel(o.id, "position already closed")
                    continue
                o.qty = o.filled_qty + min(o.remaining, abs(need))
            inst = self.instruments[o.symbol]
            probe = fillable_qty(o, inst, bar, self.cfg, 0)
            if probe > 0 and match_price(o, inst, bar, self.cfg, probe, prev.get(o.symbol)) is not None:
                reach.append(o)
        if not reach:
            return
        skip: set[int] = set()
        oco: dict[tuple, list[Order]] = defaultdict(list)
        for o in reach:
            if o.oco_group:
                oco[(o.book, o.oco_group)].append(o)
        for sibs in oco.values():
            if len(sibs) > 1:
                sibs.sort(key=lambda o: (0 if (self.cfg.oco_policy == "worst" and is_stop_like(o)) else 1, o.id))
                skip.update(x.id for x in sibs[1:])
        groups: dict[tuple, list[Order]] = defaultdict(list)
        for o in self._open:
            if o.group_id is not None:
                groups[(o.book, o.group_id)].append(o)
        reachable = {o.id for o in reach}
        used: dict[str, int] = defaultdict(int)
        done_groups: set[tuple] = set()
        for o in sorted(reach, key=lambda x: x.id):
            if not o.active or o.id in skip:
                continue
            if o.group_id is not None:
                key = (o.book, o.group_id)
                if key in done_groups:
                    continue
                done_groups.add(key)
                plan: list[tuple[Order, int, float]] | None = []
                local: dict[str, int] = defaultdict(int)
                for leg in groups[key]:
                    q = (fillable_qty(leg, self.instruments[leg.symbol], bars[leg.symbol], self.cfg, used[leg.symbol] + local[leg.symbol])
                         if leg.id in reachable else 0)
                    px = (match_price(leg, self.instruments[leg.symbol], bars[leg.symbol], self.cfg, q, prev.get(leg.symbol))
                          if q > 0 and q == leg.remaining else None)
                    if px is None:
                        plan = None
                        break
                    plan.append((leg, q, px))
                    local[leg.symbol] += q
                if plan is None:
                    continue                    # nothing reserved: other orders may use the volume
                if self._excess_after(o.book, [(leg.symbol, leg.side * q, to_paise(px), leg.product) for leg, q, px in plan]) < 0:
                    for leg, _, _ in plan:
                        self._reject(leg, "INSUFFICIENT_MARGIN (at fill, combined legs)")
                    continue
                for leg, q, px in plan:
                    self._fill(leg, q, px, t, bar_volume=bars[leg.symbol].volume)
                    used[leg.symbol] += q
                continue
            inst, bar = self.instruments[o.symbol], bars[o.symbol]
            q = fillable_qty(o, inst, bar, self.cfg, used[o.symbol])
            px = match_price(o, inst, bar, self.cfg, q, prev.get(o.symbol)) if q > 0 else None
            if px is None:
                continue
            if not o.forced and not self._reduces(o.book, o.symbol, o.side * q):
                if self._excess_after(o.book, [(o.symbol, o.side * q, to_paise(px), o.product)]) < 0:
                    self._reject(o, "INSUFFICIENT_MARGIN (at fill)")
                    continue
            self._fill(o, q, px, t, o.tag if o.forced else "order", bar_volume=bar.volume)
            used[o.symbol] += q
            if o.oco_group:
                for sib in self._active(o.book):
                    if sib.oco_group == o.oco_group and sib.id != o.id:
                        self._cancel(sib.id, f"OCO: order {o.id} filled")

    def _charges_for(self, book: str, inst: Instrument, side: int, qty: int, price: int, product: str,
                     order: Order | None) -> dict:
        d = self._t.date()
        dp_key = (book, inst.symbol, d)
        dp = inst.is_cash and product != "MIS" and side < 0 and dp_key not in self._dp_charged
        c = self.charges.compute(inst, side, qty, price, product, d,
                                 order_value_before_paise=order.filled_value if order else 0,
                                 brokerage_paid_before_paise=order.brokerage_paid if order else 0,
                                 dp_applicable=dp)
        if dp and c.dp:
            self._dp_charged.add(dp_key)
        if order:
            order.brokerage_paid += c.brokerage
        return c.as_dict()

    def _fill(self, o: Order, qty: int, px: float, t: pd.Timestamp, reason: str = "order",
              bar_volume: int | None = None) -> Fill:
        inst = self.instruments[o.symbol]
        price = to_paise(px)
        ch = self._charges_for(o.book, inst, o.side, qty, price, o.product, o)
        f = self.account.apply_fill(o.book, o.symbol, o.side, qty, price, t, charges=ch, order_id=o.id,
                                    reason=reason, tag=o.tag, product=o.product)
        o.filled_qty += qty
        o.filled_value += qty * price
        o.status = OrderStatus.FILLED if o.remaining == 0 else OrderStatus.PARTIAL
        self._event("FILL", fill_id=f.id, order_id=o.id, book=o.book, symbol=o.symbol, side=o.side, qty=qty,
                    price=px, charges=ch["total"] / 100, reason=reason, partial=o.remaining > 0,
                    participation=(qty / bar_volume) if bar_volume else None)
        strat = self._strategy.get(o.book)
        if strat is not None:
            strat.on_fill(self._ctx[o.book], f)
        return f

    def _liquidate(self, book: str, symbol: str, reason: str) -> bool:
        """Queue a forced market order that flattens a position. Like every other order it can first
        trade on the NEXT bar, needs that bar to have volume, is subject to the participation cap and
        pays spread and impact. It stays working until the position is flat. Returns True if a new
        order was created."""
        pos = self.account.books[book].positions.get(symbol)
        if not pos or any(o.forced for o in self._active(book, symbol)):
            return False
        o = Order(len(self.orders) + 1, book, symbol, -1 if pos.qty > 0 else 1, abs(pos.qty), OrderType.MARKET,
                  product=pos.product, tif="GTC", tag=reason, submitted_ts=self._t, eligible_seq=self._k + 1, forced=True)
        self.orders.append(o)
        self._open.append(o)
        self._event("LIQUIDATION_ORDER", order_id=o.id, book=book, symbol=symbol, side=o.side, qty=o.qty, reason=reason)
        return True

    # =============================================================== session events
    def _session_start(self, prev: date | None, d: date, t) -> None:
        for ex in self._action_dates:
            if (prev is None or ex > prev) and ex <= d:
                for ca in self._actions[ex]:
                    if prev is None and ex < d:
                        continue       # action predates the backtest window
                    if ca.kind in ("split", "bonus"):
                        for o in self._active(symbol=ca.symbol):
                            self._cancel(o.id, f"corporate action: {ca.kind}")
                        self.account.apply_split(ca.symbol, ca.ratio, t)
                        if ca.symbol in self._prev_session_close:
                            self._prev_session_close[ca.symbol] /= ca.ratio
                    elif ca.kind == "dividend":
                        self.account.apply_dividend(ca.symbol, ca.amount, t)
                    self._event("CORPORATE_ACTION", symbol=ca.symbol, action=ca.kind, ratio=ca.ratio, amount=ca.amount)
        self._sod_equity = {b: self.account.book_equity(b) for b in self.account.books}
        self._orders_today.clear()
        self._halted_today.clear()
        self._mis_closed_today = False

    def _margin_watch(self, bars: dict[str, Bar], t) -> None:
        for b in self.account.books.values():
            eq = self.account.book_equity(b.id)
            sod = self._sod_equity.get(b.id, 0)
            if b.id not in self._halted_today and sod > 0 and eq < sod * (1 - self.risk.max_daily_loss_pct):
                self._halted_today.add(b.id)
                self._event("DAILY_LOSS_HALT", book=b.id, equity=eq / 100, start_of_day=sod / 100)
            if not b.positions:
                self._liquidating.discard(b.id)
                continue
            if b.id not in self._liquidating:
                req = self._required(b.id)
                if eq - req < 0 and any(self.instruments[s].is_derivative or p.product == "MIS" or p.qty < 0
                                        for s, p in b.positions.items()):
                    self._event("MARGIN_CALL", book=b.id, equity=eq / 100, required=req / 100)
                    self.flags.add("margin call occurred")
                    if self.liquidate:
                        self._liquidating.add(b.id)
            if b.id in self._liquidating:       # the breach is known at this bar's close; exits start next bar
                for o in self._active(b.id):
                    if not o.forced:
                        self._cancel(o.id, "margin call")
                for s in sorted(b.positions):
                    self._liquidate(b.id, s, "margin_call")

    def _square_off_mis(self, bars: dict[str, Bar], t) -> None:
        """From the cut-off bar onward: cancel working intraday orders and queue liquidation orders."""
        self._mis_closed_today = True
        for o in self._active():
            if o.product == "MIS" and not o.forced:
                self._cancel(o.id, "intraday square-off")
        for b in self.account.books.values():
            for s in sorted(b.positions):
                if b.positions[s].product == "MIS" and self._liquidate(b.id, s, "squareoff"):
                    self._event("SQUARE_OFF", book=b.id, symbol=s)

    def _session_end(self, d: date, t, bars: dict[str, Bar], k: int) -> None:
        if self.intraday:
            self._square_off_mis(bars, t)
            for b in self.account.books.values():
                for sym, pos in b.positions.items():
                    if pos.product == "MIS":     # no bar with volume was left to trade on: do not invent a fill
                        self.flags.add("an intraday (MIS) position could not be squared off before the close and was "
                                       "carried to the next session (a real broker would auction/penalise it)")
                        self._event("MIS_CARRIED_OVERNIGHT", book=b.id, symbol=sym, qty=pos.qty)
        for o in self._active():
            if o.tif == "DAY" and o.eligible_seq <= k:
                self._cancel(o.id, "DAY order expired at session end", status=OrderStatus.EXPIRED)
        # expiry settlement
        for b in self.account.books.values():
            for s in sorted(b.positions):
                inst = self.instruments[s]
                if inst.expiry is None or inst.expiry > d:
                    continue
                pos = b.positions[s]
                und = inst.underlying
                und_ts = self.portal.last_ts(und) if und else None
                und_px = self.portal.last(und) if und_ts is not None and und_ts.date() == d else None
                if und_px is None:
                    self.flags.add("expiry settled on contract close (underlying price unavailable)")
                    settle = self.portal.last(s) or 0.0
                elif inst.is_option:
                    settle = inst.intrinsic(und_px)
                else:
                    settle = und_px
                if inst.expiry < d:
                    self.flags.add("position settled after its expiry date (data gap)")
                if inst.settlement == "physical":
                    self.flags.add("physically-settled contract approximated by cash settlement")
                price = to_paise(settle)
                self.account.marks[s] = price
                side = -1 if pos.qty > 0 else 1
                qty = abs(pos.qty)
                ch = {}
                if inst.is_option and pos.qty > 0 and price > 0:
                    ch = self.charges.exercise_stt(inst, qty, price, d, settlement_price_paise=to_paise(und_px if und_px is not None else settle)).as_dict()
                f = self.account.apply_fill(b.id, s, side, qty, price, t, charges=ch, reason="expiry", product=pos.product)
                self._event("EXPIRY_SETTLEMENT", fill_id=f.id, book=b.id, symbol=s, qty=qty, side=side,
                            settlement_price=settle, underlying_price=und_px)
        for o in self._active():
            inst = self.instruments[o.symbol]
            if inst.expiry is not None and inst.expiry <= d:
                self._cancel(o.id, "contract expired")
        var = self.account.mark_to_market_futures(t)
        if var:
            self._event("FUTURES_MTM", variation=var / 100)
        for s, bar in bars.items():
            self._prev_session_close[s] = bar.close
        self._dp_charged = {x for x in self._dp_charged if x[2] == d}

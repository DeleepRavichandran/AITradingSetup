"""Account, books, positions and the cash ledger.

Accounting model (all amounts integer paise, so every identity below is exact):

* An Account holds one Book per strategy. Capital is deposited into a book; a book can
  only spend its own cash, so two strategies can never spend the same rupee.
  account cash == sum(book cash) == sum(ledger amounts).
* Positions are FIFO lots. `realized` is FIFO gross P&L.
* Cash equities and options exchange full value for cash on every trade.
* Futures exchange no value on entry. They are marked to market at each session end:
  the variation is paid/received in cash and the position's `ref` (settlement reference)
  resets. On a closing trade the cash flow is measured against `ref`.

Invariant checked by `Account.reconcile()` (and by property-based tests):

    equity == deposits + realized + unrealized - charges + dividends
    where equity = cash + value(cash equities, options) + (futures value - ref)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import pandas as pd

from nseql.backtester.orders import Fill, LedgerEntry, Trade
from nseql.instruments.models import Instrument
from nseql.money import to_paise


@dataclass(slots=True)
class Lot:
    qty: int            # signed
    cost: int           # signed paise: qty * entry price
    ts: pd.Timestamp
    charges: int        # entry charges still attached to this lot
    fill_id: int
    unit_price: float   # rupees at entry, for reporting


@dataclass(slots=True)
class Position:
    symbol: str
    product: str
    qty: int = 0
    lots: deque = field(default_factory=deque)
    ref: int = 0        # futures only: signed settlement reference value
    realized: int = 0

    @property
    def cost(self) -> int:
        return sum(l.cost for l in self.lots)

    @property
    def avg_price(self) -> float:
        return self.cost / self.qty / 100 if self.qty else 0.0


@dataclass
class Book:
    id: str
    cash: int = 0
    deposits: int = 0
    realized: int = 0
    charges: int = 0
    dividends: int = 0
    positions: dict[str, Position] = field(default_factory=dict)


class Account:
    def __init__(self, instruments: dict[str, Instrument]):
        self.instruments = instruments
        self.books: dict[str, Book] = {}
        self.ledger: list[LedgerEntry] = []
        self.fills: list[Fill] = []
        self.trades: list[Trade] = []
        self.marks: dict[str, int] = {}     # last known price in paise
        self.lieu_realized = 0              # realised P&L from fractional-share cash, which has no fill

    # ------------------------------------------------------------------ cash
    def _post(self, book: Book, ts, typ: str, symbol: str, amount: int, ref: int | None = None, memo: str = ""):
        book.cash += amount
        self.ledger.append(LedgerEntry(len(self.ledger) + 1, ts, book.id, typ, symbol, amount, book.cash, ref, memo))

    def deposit(self, book_id: str, amount_paise: int, ts) -> Book:
        b = self.books.setdefault(book_id, Book(book_id))
        b.deposits += amount_paise
        self._post(b, ts, "DEPOSIT", "", amount_paise, memo="capital allocation")
        return b

    @property
    def cash(self) -> int:
        return sum(b.cash for b in self.books.values())

    # ------------------------------------------------------------------ marks
    def mark(self, symbol: str, price: float) -> None:
        self.marks[symbol] = to_paise(price)

    def position_value(self, pos: Position) -> int:
        """Contribution of a position to equity."""
        v = pos.qty * self.marks.get(pos.symbol, 0)
        return v - pos.ref if self.instruments[pos.symbol].is_future else v

    def unrealized(self, pos: Position) -> int:
        return pos.qty * self.marks.get(pos.symbol, 0) - pos.cost

    def book_equity(self, book_id: str) -> int:
        b = self.books[book_id]
        return b.cash + sum(self.position_value(p) for p in b.positions.values())

    def equity(self) -> int:
        return sum(self.book_equity(b) for b in self.books)

    # ------------------------------------------------------------------ trading
    def apply_fill(self, book_id: str, symbol: str, side: int, qty: int, price: int, ts, *, charges: dict | None = None,
                   order_id: int | None = None, reason: str = "order", tag: str = "", product: str = "CNC") -> Fill:
        """Apply an execution of `qty` units at `price` paise. Returns the Fill record."""
        assert qty > 0 and side in (1, -1)
        b = self.books[book_id]
        inst = self.instruments[symbol]
        pos = b.positions.get(symbol)
        if pos is None:
            pos = b.positions[symbol] = Position(symbol, product)
        fill = Fill(len(self.fills) + 1, order_id, book_id, ts, symbol, side, qty, price, charges or {}, reason, 0, 0, tag)
        total_ch = (charges or {}).get("total", 0)
        closing = min(qty, abs(pos.qty)) if pos.qty * side < 0 else 0
        opening = qty - closing
        ch_close = total_ch * closing // qty
        ch_open = total_ch - ch_close
        cash_delta = 0
        if closing:
            cash_delta += self._close(b, pos, inst, side, closing, price, ts, fill, ch_close, reason, tag)
        if opening:
            signed = side * opening
            pos.lots.append(Lot(signed, signed * price, ts, ch_open, fill.id, price / 100))
            pos.qty += signed
            if inst.is_future:
                pos.ref += signed * price
            else:
                cash_delta -= signed * price
        fill.cash_delta = cash_delta
        self.fills.append(fill)
        if cash_delta or not inst.is_future:
            self._post(b, ts, "TRADE", symbol, cash_delta, fill.id, f"{'BUY' if side > 0 else 'SELL'} {qty} @ {price / 100:.2f} ({reason})")
        if total_ch:
            b.charges += total_ch
            self._post(b, ts, "CHARGES", symbol, -total_ch, fill.id,
                       " ".join(f"{k}={v / 100:.2f}" for k, v in (charges or {}).items() if v and k != "total"))
        if pos.qty == 0:
            assert not pos.lots and pos.ref == 0
            del b.positions[symbol]
        return fill

    def _close(self, b: Book, pos: Position, inst: Instrument, side: int, qty: int, price: int, ts, fill: Fill,
               exit_charges: int, reason: str, tag: str) -> int:
        """Close `qty` units FIFO. Returns the cash delta (excluding charges)."""
        direction = 1 if pos.qty > 0 else -1
        total_before = abs(pos.qty)
        left = qty
        realized = 0
        ch_left = exit_charges
        while left:
            lot = pos.lots[0]
            q = min(left, abs(lot.qty))
            lot_cost = lot.cost if q == abs(lot.qty) else int(round(lot.cost * q / abs(lot.qty)))
            lot_ch = lot.charges if q == abs(lot.qty) else lot.charges * q // abs(lot.qty)
            gross = -side * q * price - lot_cost
            x_ch = ch_left if q == left else exit_charges * q // qty
            ch_left -= x_ch
            self.trades.append(Trade(b.id, pos.symbol, direction, q, lot.ts, lot.unit_price, ts, price / 100, gross,
                                     lot_ch + x_ch, reason, tag, lot.fill_id, fill.id))
            realized += gross
            lot.qty -= direction * q
            lot.cost -= lot_cost
            lot.charges -= lot_ch
            if lot.qty == 0:
                pos.lots.popleft()
            left -= q
        pos.realized += realized
        b.realized += realized
        fill.realized = realized
        if inst.is_future:
            ref_part = pos.ref if qty == total_before else int(round(pos.ref * qty / total_before))
            pos.ref -= ref_part
            cash = -side * qty * price - ref_part
        else:
            cash = -side * qty * price
        pos.qty += side * qty
        return cash

    # ------------------------------------------------------------------ lifecycle events
    def mark_to_market_futures(self, ts) -> int:
        """Daily futures settlement: pay/receive variation against the settlement reference."""
        total = 0
        for b in self.books.values():
            for pos in b.positions.values():
                if not self.instruments[pos.symbol].is_future or pos.symbol not in self.marks:
                    continue
                settle_value = pos.qty * self.marks[pos.symbol]
                var = settle_value - pos.ref
                if var:
                    pos.ref = settle_value
                    self._post(b, ts, "MTM", pos.symbol, var, memo=f"daily settlement @ {self.marks[pos.symbol] / 100:.2f}")
                    total += var
        return total

    def apply_split(self, symbol: str, ratio: float, ts) -> None:
        post_mark = int(round(self.marks.get(symbol, 0) / ratio))
        if symbol in self.marks:
            self.marks[symbol] = post_mark
        for b in self.books.values():
            pos = b.positions.get(symbol)
            if not pos:
                continue
            new_total = 0
            for lot in pos.lots:
                exact = lot.qty * ratio
                nq = int(exact)                      # truncates toward zero
                frac = exact - nq
                if abs(frac) > 1e-9:                 # fractional entitlement paid in cash at the post-split price
                    cost_part = int(round(lot.cost * frac / exact))
                    amount = int(round(frac * post_mark))
                    ch_part = lot.charges if nq == 0 else int(lot.charges * frac / exact)
                    lot.cost -= cost_part
                    lot.charges -= ch_part
                    pos.realized += amount - cost_part
                    b.realized += amount - cost_part
                    self.lieu_realized += amount - cost_part
                    self.trades.append(Trade(b.id, symbol, 1 if exact > 0 else -1, 0, lot.ts, lot.unit_price, ts,
                                             post_mark / 100, amount - cost_part, ch_part, "split_cash_in_lieu"))
                    self._post(b, ts, "CASH_IN_LIEU", symbol, amount, memo=f"fractional {frac:.4f} sh on split")
                lot.qty = nq
                lot.unit_price = lot.unit_price / ratio
                new_total += nq
            pos.lots = deque(l for l in pos.lots if l.qty != 0)
            pos.qty = new_total
            if pos.qty == 0:
                del b.positions[symbol]

    def apply_dividend(self, symbol: str, amount_rupees: float, ts) -> None:
        per = to_paise(amount_rupees)
        for b in self.books.values():
            pos = b.positions.get(symbol)
            if pos and pos.qty:
                amt = pos.qty * per
                b.dividends += amt
                self._post(b, ts, "DIVIDEND", symbol, amt, memo=f"{amount_rupees:.2f}/sh on {pos.qty} sh")

    # ------------------------------------------------------------------ audit
    def reconcile(self) -> dict:
        """Verify every accounting identity. Returns a report; `ok` is True only if all hold exactly."""
        checks = {}
        by_book: dict[str, int] = {}
        for e in self.ledger:
            by_book[e.book] = by_book.get(e.book, 0) + e.amount
        checks["ledger_sums_to_cash"] = all(by_book.get(b.id, 0) == b.cash for b in self.books.values())
        running: dict[str, int] = {}
        ok = True
        for e in self.ledger:
            running[e.book] = running.get(e.book, 0) + e.amount
            ok &= running[e.book] == e.cash_after
        checks["ledger_running_balance"] = ok
        ok = True
        for b in self.books.values():
            unreal = sum(self.unrealized(p) for p in b.positions.values())
            lhs = self.book_equity(b.id)
            rhs = b.deposits + b.realized + unreal - b.charges + b.dividends
            ok &= lhs == rhs
        checks["equity_identity"] = ok
        fill_ch = sum(f.charges.get("total", 0) for f in self.fills)
        checks["charges_match_fills"] = fill_ch == sum(b.charges for b in self.books.values())
        trade_gross = sum(t.gross for t in self.trades)
        checks["trades_match_fill_realized"] = trade_gross == sum(f.realized for f in self.fills) + self.lieu_realized
        checks["realized_matches_books"] = trade_gross == sum(b.realized for b in self.books.values())
        open_lot_ch = sum(l.charges for b in self.books.values() for p in b.positions.values() for l in p.lots)
        checks["charges_fully_attributed"] = sum(t.charges for t in self.trades) + open_lot_ch == fill_ch
        checks["position_qty_matches_lots"] = all(
            p.qty == sum(l.qty for l in p.lots) for b in self.books.values() for p in b.positions.values())
        checks["ok"] = all(v for v in checks.values())
        return checks

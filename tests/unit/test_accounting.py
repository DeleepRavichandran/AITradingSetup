"""Position, cash and P&L accounting — exact integer identities, including property-based tests."""
from datetime import date

import pandas as pd
from hypothesis import given, settings, strategies as st

from nseql.backtester.portfolio import Account
from nseql.instruments.models import Instrument, Kind
from tests.helpers import fut, opt

T = pd.Timestamp("2025-01-06 15:30")
EQ = Instrument("AAA", Kind.EQ)
FUT = fut(expiry=date(2030, 1, 1))
CE = opt(20000, "CE", expiry=date(2030, 1, 1))
INSTR = {i.symbol: i for i in (EQ, FUT, CE)}


def acct(cash=1_000_000_00):
    a = Account(INSTR)
    a.deposit("b", cash, T)
    return a


def test_long_round_trip_cash_and_pnl():
    a = acct()
    a.apply_fill("b", "AAA", +1, 100, 100_00, T)
    assert a.books["b"].cash == 1_000_000_00 - 100 * 100_00
    a.mark("AAA", 105.0)
    assert a.equity() == 1_000_000_00 + 100 * 5_00
    f = a.apply_fill("b", "AAA", -1, 100, 110_00, T)
    assert f.realized == 100 * 10_00 and a.books["b"].cash == 1_000_000_00 + 100 * 10_00
    assert not a.books["b"].positions and a.reconcile()["ok"]


def test_short_round_trip():
    a = acct()
    a.apply_fill("b", "AAA", -1, 100, 100_00, T)
    a.mark("AAA", 90.0)
    assert a.equity() == 1_000_000_00 + 100 * 10_00
    f = a.apply_fill("b", "AAA", +1, 100, 95_00, T)
    assert f.realized == 100 * 5_00 and a.reconcile()["ok"]


def test_fifo_lots_and_partial_close():
    a = acct()
    a.apply_fill("b", "AAA", +1, 100, 100_00, T)
    a.apply_fill("b", "AAA", +1, 100, 120_00, T)
    f = a.apply_fill("b", "AAA", -1, 150, 130_00, T)
    assert f.realized == 100 * 30_00 + 50 * 10_00                    # first lot fully, then half of the second
    assert [(t.qty, t.gross) for t in a.trades] == [(100, 100 * 30_00), (50, 50 * 10_00)]
    assert a.books["b"].positions["AAA"].qty == 50 and a.books["b"].positions["AAA"].avg_price == 120.0


def test_flip_through_zero_splits_into_close_and_open():
    a = acct()
    a.apply_fill("b", "AAA", +1, 100, 100_00, T)
    f = a.apply_fill("b", "AAA", -1, 250, 110_00, T, charges={"total": 500})
    pos = a.books["b"].positions["AAA"]
    assert f.realized == 100 * 10_00 and pos.qty == -150 and pos.avg_price == 110.0
    assert a.trades[0].charges == 200 and pos.lots[0].charges == 300   # charges apportioned 100:150
    assert a.reconcile()["ok"]


def test_futures_exchange_no_value_on_entry_and_settle_daily():
    a = acct()
    a.apply_fill("b", "IDXFUT", +1, 50, 20_000_00, T)
    assert a.books["b"].cash == 1_000_000_00                           # no cash moves on entry
    a.mark("IDXFUT", 20_100.0)
    assert a.equity() == 1_000_000_00 + 50 * 100_00
    assert a.mark_to_market_futures(T) == 50 * 100_00
    assert a.books["b"].cash == 1_000_000_00 + 50 * 100_00             # variation margin paid in cash
    a.mark("IDXFUT", 19_900.0)
    a.mark_to_market_futures(T)
    f = a.apply_fill("b", "IDXFUT", -1, 50, 19_950_00, T)
    assert f.realized == 50 * -50_00                                   # FIFO P&L vs the ORIGINAL entry
    assert a.books["b"].cash == 1_000_000_00 - 50 * 50_00              # total cash == total P&L
    assert a.reconcile()["ok"]


def test_option_premium_flows():
    a = acct()
    a.apply_fill("b", CE.symbol, -1, 50, 200_00, T)                    # write: premium received
    assert a.books["b"].cash == 1_000_000_00 + 50 * 200_00
    a.mark(CE.symbol, 200.0)
    assert a.equity() == 1_000_000_00                                  # liability offsets the cash
    a.apply_fill("b", CE.symbol, +1, 50, 50_00, T)
    assert a.books["b"].realized == 50 * 150_00 and a.reconcile()["ok"]


def test_split_preserves_value_and_cost():
    a = acct()
    a.apply_fill("b", "AAA", +1, 100, 1000_00, T)
    a.mark("AAA", 1000.0)
    before = a.equity()
    a.apply_split("AAA", 2.0, T)
    pos = a.books["b"].positions["AAA"]
    assert pos.qty == 200 and pos.cost == 100 * 1000_00 and a.equity() == before and a.reconcile()["ok"]


def test_fractional_split_pays_cash_in_lieu():
    a = acct()
    a.apply_fill("b", "AAA", +1, 3, 900_00, T)
    a.mark("AAA", 900.0)
    a.apply_split("AAA", 1.5, T)                                       # 4.5 shares -> 4 + cash for 0.5
    assert a.books["b"].positions["AAA"].qty == 4
    assert any(e.type == "CASH_IN_LIEU" and e.amount == 300_00 for e in a.ledger)
    assert a.reconcile()["ok"]


def test_dividend_credits_longs_and_debits_shorts():
    a = acct()
    a.deposit("c", 1_000_000_00, T)
    a.apply_fill("b", "AAA", +1, 100, 100_00, T)
    a.apply_fill("c", "AAA", -1, 40, 100_00, T)
    a.apply_dividend("AAA", 2.5, T)
    assert a.books["b"].dividends == 250_00 and a.books["c"].dividends == -100_00 and a.reconcile()["ok"]


def test_books_cannot_spend_each_others_cash():
    a = acct()
    a.deposit("c", 500_00, T)
    a.apply_fill("b", "AAA", +1, 10, 100_00, T)
    assert a.books["c"].cash == 500_00 and a.cash == a.books["b"].cash + a.books["c"].cash


# ---------------------------------------------------------------- property-based
fill_step = st.tuples(st.sampled_from(["AAA", "IDXFUT", CE.symbol]), st.sampled_from([1, -1]),
                      st.integers(1, 40), st.integers(1, 5_000_00), st.integers(0, 5_000))
event = st.one_of(
    st.tuples(st.just("fill"), fill_step),
    st.tuples(st.just("mark"), st.tuples(st.sampled_from(["AAA", "IDXFUT", CE.symbol]), st.integers(5, 5_000_00))),
    st.tuples(st.just("mtm"), st.none()),
    st.tuples(st.just("div"), st.integers(1, 2000)),
    st.tuples(st.just("split"), st.sampled_from([2.0, 3.0, 1.5, 0.5])),
)


@settings(max_examples=300, deadline=None)
@given(st.lists(event, min_size=1, max_size=60))
def test_accounting_identities_hold_for_any_event_sequence(events):
    a = acct()
    a.deposit("c", 5_000_000_00, T)
    deposits = a.cash
    for i, (kind, arg) in enumerate(events):
        book = "b" if i % 2 else "c"
        if kind == "fill":
            sym, side, lots, price, ch = arg
            qty = lots * INSTR[sym].lot_size
            a.apply_fill(book, sym, side, qty, price, T, charges={"total": ch, "stt": ch})
            a.marks.setdefault(sym, price)
        elif kind == "mark":
            a.marks[arg[0]] = arg[1]
        elif kind == "mtm":
            a.mark_to_market_futures(T)
        elif kind == "div":
            a.apply_dividend("AAA", arg / 100, T)
        else:
            a.apply_split("AAA", arg, T)
        rec = a.reconcile()
        assert rec["ok"], rec
    # cash conservation: every paisa that moved is in the ledger
    assert a.cash == sum(e.amount for e in a.ledger)
    assert sum(e.amount for e in a.ledger if e.type == "DEPOSIT") == deposits
    # flatten everything at the marks: equity must not change and must equal cash
    eq = a.equity()
    for b in list(a.books.values()):
        for sym, pos in list(b.positions.items()):
            a.apply_fill(b.id, sym, -1 if pos.qty > 0 else 1, abs(pos.qty), a.marks[sym], T)
    assert a.equity() == eq == a.cash and a.reconcile()["ok"]
    total_real = sum(b.realized for b in a.books.values())
    assert a.cash == deposits + total_real - sum(b.charges for b in a.books.values()) + sum(b.dividends for b in a.books.values())

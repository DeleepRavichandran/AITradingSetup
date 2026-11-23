"""Futures and options: expiry, settlement, payoffs, margin, multi-leg orders."""
from datetime import date

import pytest
from hypothesis import given, settings, strategies as st

from nseql.backtester import margin
from nseql.backtester.orders import OrderStatus
from nseql.instruments.rules import MarginRules
from tests.helpers import IDX, flat, frame, fut, opt, run

EXP = date(2025, 1, 10)       # 5th bar of the default calendar (Mon 6 Jan .. Fri 10 Jan)


def _mkt(idx_closes, contracts: dict):
    frames = {"IDX": frame(flat(idx_closes))}
    for inst, prices in contracts.items():
        frames[inst.symbol] = frame(flat(prices))
    return frames, [IDX, *contracts]


@pytest.mark.parametrize("kind,strike,final,expected_payoff", [
    ("CE", 20000, 20300, 300), ("CE", 20000, 19900, 0), ("PE", 20000, 19650, 350), ("PE", 20000, 20100, 0)])
def test_long_option_settles_at_intrinsic_of_the_underlying(kind, strike, final, expected_payoff):
    o = opt(strike, kind, EXP)
    frames, instr = _mkt([20000, 20000, 20000, 20000, final], {o: [100, 100, 100, 100, 1]})
    res = run(frames, instr, {0: lambda c: c.order(o.symbol, 50)})
    settle = [f for f in res.account.fills if f.reason == "expiry"][0]
    assert settle.price == expected_payoff * 100 and settle.side == -1 and settle.qty == 50
    assert res.equity[-1] == 1_000_000_00 + 50 * (expected_payoff - 100) * 100
    assert not res.account.books["t"].positions


def test_short_option_payoff_and_exercise_stt_only_for_long_itm():
    ce = opt(20000, "CE", EXP)
    frames, instr = _mkt([20000, 20000, 20000, 20000, 20250], {ce: [120, 120, 120, 120, 250]})
    short = run(frames, instr, {0: lambda c: c.order(ce.symbol, -50)}, fees=True)
    long_ = run(frames, instr, {0: lambda c: c.order(ce.symbol, 50)}, fees=True)
    s_fill = [f for f in short.account.fills if f.reason == "expiry"][0]
    l_fill = [f for f in long_.account.fills if f.reason == "expiry"][0]
    assert s_fill.side == +1 and s_fill.charges == {}
    assert l_fill.charges["stt"] == round(50 * 250_00 * 0.00125) == l_fill.charges["total"]
    assert short.account.books["t"].realized == 50 * (120 - 250) * 100


def test_future_final_settlement_uses_underlying_close():
    f = fut(expiry=EXP)
    frames, instr = _mkt([20000, 20050, 20100, 20150, 20200], {f: [20010, 20060, 20110, 20160, 20190]})
    res = run(frames, instr, {0: lambda c: c.order(f.symbol, 50)})
    settle = [x for x in res.account.fills if x.reason == "expiry"][0]
    assert settle.price == 20200_00                       # index close, not the future's own last (20190)
    assert res.account.books["t"].realized == 50 * (20200 - 20060) * 100
    mtm = [e.amount for e in res.account.ledger if e.type == "MTM"]
    assert mtm == [50 * 50_00, 50 * 50_00]                # daily variation on days 2 and 3 after the fill


def test_orders_on_expired_contracts_are_rejected():
    f = fut(expiry=date(2025, 1, 8))
    frames, instr = _mkt([100] * 5, {f: [100, 100, 100, 100, 100]})
    res = run(frames, instr, {3: lambda c: c.order(f.symbol, 50)})
    assert res.orders[0].reason.startswith("NOT_LISTED")


def test_multi_leg_order_is_atomic():
    a, b = opt(20000, "CE", EXP), opt(20500, "CE", EXP)
    frames = {"IDX": frame(flat([20000] * 5)), a.symbol: frame(flat([300] * 5)),
              b.symbol: frame([(100, 100, 100, 100, 1000), (100, 100, 100, 100, 0), (100, 100, 100, 100, 1000),
                               (100, 100, 100, 100, 1000), (0.05, 0.05, 0.05, 0.05, 1000)])}
    res = run(frames, [IDX, a, b], {0: lambda c: c.multi_leg([(a.symbol, 50), (b.symbol, -50)], tag="spread")})
    # leg b had no volume on bar 1, so NEITHER leg filled; the DAY group then expired
    assert not res.account.fills and all(o.status is OrderStatus.EXPIRED for o in res.orders)
    ok = run({**frames, b.symbol: frame(flat([100] * 5))}, [IDX, a, b],
             {0: lambda c: c.multi_leg([(a.symbol, 50), (b.symbol, -50)], tag="spread")})
    assert [(f.symbol, f.side) for f in ok.account.fills[:2]] == [(a.symbol, 1), (b.symbol, -1)]
    assert ok.account.fills[0].ts == ok.account.fills[1].ts


R = MarginRules()


def _req(legs, spot=20000.0):
    instr = {i.symbol: i for i, _, _ in legs}
    marks = {i.symbol: int(p * 100) for i, _, p in legs} | {"IDX": int(spot * 100)}
    return margin.required({i.symbol: (q, "NRML") for i, q, _ in legs}, instr, marks, R) / 100


def test_margin_naked_short_vs_defined_risk():
    sc, lc = opt(20000, "CE"), opt(20500, "CE")
    naked = _req([(sc, -50, 300)])
    assert naked == pytest.approx(0.12 * 20000 * 50)                       # SPAN-like 12% of notional
    spread = _req([(sc, -50, 300), (lc, 50, 100)])
    assert spread == pytest.approx(50 * (500 - (300 - 100)))   # max further loss = (width - credit) * qty
    assert spread < naked
    assert _req([(lc, 50, 100)]) == 50 * 100                               # long option: premium only
    f = fut(expiry=date(2030, 1, 1))
    assert _req([(f, 50, 20000)]) == pytest.approx(0.12 * 20000 * 50)


def test_iron_condor_margin_is_its_max_loss():
    legs = [(opt(20300, "CE"), -50, 120), (opt(20600, "CE"), 50, 40), (opt(19700, "PE"), -50, 110), (opt(19400, "PE"), 50, 35)]
    credit = 120 - 40 + 110 - 35
    net_value = -credit * 50
    assert _req(legs) == pytest.approx(net_value + 300 * 50)               # value_now - worst_expiry_value
    # equity effect: requirement relative to cash received equals max loss (width - credit)
    assert _req(legs) - net_value - credit * 50 == pytest.approx((300 - credit) * 50)


def test_undefined_risk_structure_falls_back_to_naive_margin():
    ratio = [(opt(20000, "CE"), 50, 300), (opt(20500, "CE"), -100, 100)]    # net short calls: unbounded
    assert _req(ratio) == pytest.approx(50 * 300 + 100 * (0.12 * 20000 - 500))


def test_short_option_rejected_without_margin():
    ce = opt(20000, "CE", EXP)
    frames, instr = _mkt([20000] * 5, {ce: [300] * 5})
    res = run(frames, instr, {0: lambda c: c.order(ce.symbol, -50)}, capital=50_000)
    assert res.orders[0].reason.startswith("INSUFFICIENT_MARGIN")


def test_margin_call_liquidates_a_losing_future():
    f = fut(expiry=date(2030, 1, 1))
    frames, instr = _mkt([20000, 20000, 17000, 17000, 17000], {f: [20000, 20000, 17000, 17000, 17000]})
    res = run(frames, instr, {0: lambda c: c.order(f.symbol, 50)}, capital=250_000)
    assert any(e["type"] == "MARGIN_CALL" for e in res.events)
    assert [x.reason for x in res.account.fills] == ["order", "margin_call"]
    assert res.equity[-1] == 250_000_00 - 50 * 3000_00            # the loss is real and can exceed margin posted


@settings(max_examples=200, deadline=None)
@given(st.sampled_from(["CE", "PE"]), st.integers(180, 220), st.integers(150, 250), st.integers(1, 3000),
       st.sampled_from([1, -1]), st.integers(1, 4))
def test_option_pnl_equals_textbook_payoff(kind, strike_h, final_h, premium_ticks, side, lots):
    """For any strike / final price / premium / direction: P&L == qty * (intrinsic - premium)."""
    strike, final, premium = strike_h * 100, final_h * 100, premium_ticks * 0.05
    o = opt(strike, kind, EXP)
    frames, instr = _mkt([20000, 20000, 20000, 20000, final], {o: [premium] * 5})
    res = run(frames, instr, {0: lambda c: c.order(o.symbol, side * lots * 50)}, capital=100_000_000)
    intrinsic = max(final - strike, 0) if kind == "CE" else max(strike - final, 0)
    expected = side * lots * 50 * (intrinsic * 100 - round(premium * 100))
    assert res.equity[-1] - 100_000_000_00 == expected

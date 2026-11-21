"""Audit item 3: expiry settlement and exercise tax against NSE's published rules and worked examples.

Rules (NSE equity-derivatives "Settlement Price" / "Settlement Mechanism" pages, STT page):
  * index futures and options: final settlement price = closing price of the underlying index in the
    capital-market segment on the contract's last trading day;
  * futures final P&L = final settlement price minus the previous day's settlement price;
  * an exercised (in-the-money long) option pays STT on its intrinsic value — 0.125%, 0.15% from
    2026-04-01. Before 2019-09-01 the tax base was the full settlement value (the "STT trap").
Worked examples are the ones brokers publish for those rules (figures in the test names).
"""
from datetime import date

import pandas as pd
import pytest

from nseql.backtester import margin
from nseql.instruments.rules import ChargeSchedule, MarginRules
from tests.helpers import IDX, flat, frame, fut, opt, run


def expiry_run(inst, premium, idx_final, start, qty, own_last=None):
    ts = list(pd.bdate_range(start, periods=3) + pd.Timedelta(hours=15, minutes=30))
    idx = frame(flat([22000, 22000, idx_final]), ts=ts)
    own = frame(flat([premium, premium, own_last if own_last is not None else premium]), ts=ts)
    res = run({"IDX": idx, inst.symbol: own}, [IDX, inst], {0: lambda c: c.order(inst.symbol, qty)}, fees=True, capital=50_000_000)
    return res, next(f for f in res.account.fills if f.reason == "expiry")


def test_published_example_22500_call_spot_22600_lot_50_pays_6_25_stt():
    o = opt(22500, "CE", date(2025, 1, 8), lot=50)
    _, f = expiry_run(o, 80.0, 22600, "2025-01-06", 50)
    assert f.price == 100_00 and f.charges["stt"] == 625 and f.charges["total"] == 625        # Rs 6.25


def test_published_example_intrinsic_200_lot_75_pays_18_75_stt():
    o = opt(22000, "PE", date(2025, 1, 8), lot=75)
    _, f = expiry_run(o, 50.0, 21800, "2025-01-06", 75)
    assert f.price == 200_00 and f.charges["stt"] == 1875                                     # Rs 18.75


def test_exercise_stt_is_0_15_percent_from_april_2026():
    o = opt(22500, "CE", date(2026, 4, 8), lot=50)
    _, f = expiry_run(o, 80.0, 22600, "2026-04-06", 50)
    assert f.charges["stt"] == round(50 * 100_00 * 0.0015)                                    # Rs 7.50


def test_before_september_2019_exercise_stt_was_charged_on_settlement_value():
    """The pre-2019 'STT trap': 0.125% of (settlement price x quantity), not of intrinsic value."""
    o = opt(22500, "CE", date(2018, 6, 6), lot=50)
    _, f = expiry_run(o, 80.0, 22600, "2018-06-04", 50)
    assert f.charges["stt"] == round(50 * 22600_00 * 0.00125)                                 # Rs 1,412.50, not Rs 6.25
    s = ChargeSchedule()
    assert s.exercise_stt(o, 50, 100_00, date(2019, 8, 30), settlement_price_paise=22600_00).stt == 141_250
    assert s.exercise_stt(o, 50, 100_00, date(2019, 9, 2), settlement_price_paise=22600_00).stt == 625


def test_index_option_settles_on_the_index_close_not_on_its_own_last_traded_premium():
    o = opt(22500, "CE", date(2025, 1, 8), lot=50)
    res, f = expiry_run(o, 80.0, 22600, "2025-01-06", 50, own_last=131.0)
    assert f.price == 100_00                                                                  # 22600 - 22500, not 131
    assert res.account.books["t"].realized == 50 * (100 - 80) * 100


def test_out_of_the_money_and_short_positions_pay_no_exercise_stt():
    o = opt(22500, "CE", date(2025, 1, 8), lot=50)
    _, otm = expiry_run(o, 80.0, 22400, "2025-01-06", 50)
    _, short = expiry_run(o, 80.0, 22600, "2025-01-06", -50)
    assert otm.price == 0 and otm.charges == {} and short.charges == {} and short.side == 1


def test_futures_final_settlement_is_prior_settlement_to_index_close_and_total_pnl_is_entry_to_fsp():
    f = fut(expiry=date(2025, 1, 9), lot=50)
    ts = list(pd.bdate_range("2025-01-06", periods=4) + pd.Timedelta(hours=15, minutes=30))
    idx = frame(flat([22000, 22100, 22050, 22300]), ts=ts)
    own = frame(flat([22010, 22110, 22060, 22290]), ts=ts)                     # future's own last differs from the index
    res = run({"IDX": idx, f.symbol: own}, [IDX, f], {0: lambda c: c.order(f.symbol, 50)}, capital=5_000_000)
    led = [(e.type, e.amount) for e in res.account.ledger if e.type in ("MTM", "TRADE")]
    # entry at 22110 (next open); day-2 settle 22110 -> no flow; day 3 settle 22060 (-50); final: 22300 - 22060 (+240)
    assert led == [("MTM", 50 * -50_00), ("TRADE", 50 * 240_00)]
    assert res.equity[-1] - 5_000_000_00 == 50 * (22300 - 22110) * 100


def test_stock_option_cash_settlement_is_flagged_as_an_approximation():
    from nseql.instruments.models import Instrument, Kind
    stk = Instrument("STK", Kind.EQ)
    o = Instrument("STK100CE", Kind.OPT, segment="FO", lot_size=100, underlying="STK", underlying_kind="EQ", expiry=date(2025, 1, 8),
                   strike=100.0, option_type="CE", settlement="physical")
    ts = list(pd.bdate_range("2025-01-06", periods=3) + pd.Timedelta(hours=15, minutes=30))
    res = run({"STK": frame(flat([100, 100, 110]), ts=ts), o.symbol: frame(flat([4, 4, 10]), ts=ts)}, [stk, o], {0: lambda c: c.order(o.symbol, 100)})
    assert any("physically-settled" in x for x in res.flags)


def test_margin_reconciliation_harness_reports_deviation_from_supplied_exchange_figures():
    """No exchange-published margin figures ship with the project (SPAN files are licensed), so the model
    CANNOT be claimed to match NSE. This checks the harness an operator uses to measure the gap."""
    f = fut(expiry=date(2030, 1, 1), lot=50)
    rows = margin.reconcile([{"name": "1 lot index future (illustrative, NOT an NSE figure)",
                              "positions": [{"symbol": f.symbol, "qty": 50}], "marks": {f.symbol: 22000.0, "IDX": 22000.0},
                              "exchange_margin": 150_000.0}], {f.symbol: f, "IDX": IDX}, MarginRules())
    assert rows[0]["model_margin"] == pytest.approx(0.12 * 22000 * 50)
    assert rows[0]["deviation_pct"] == pytest.approx((132_000 - 150_000) / 150_000)

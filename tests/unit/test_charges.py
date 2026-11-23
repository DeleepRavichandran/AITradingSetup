"""Statutory / exchange charges: hand-computed expectations and effective dating."""
from datetime import date

import pytest

from nseql.instruments.models import Instrument, Kind
from nseql.instruments.rules import BrokerageProfile, ChargeSchedule, RuleCoverageError
from tests.helpers import fut, opt

EQ = Instrument("AAA", Kind.EQ)
P = 100_000  # Rs 1000.00 in paise


def test_equity_delivery_buy_hand_computed():
    # 100 sh @ 1000 = Rs 1,00,000 turnover on 2025-01-15
    c = ChargeSchedule().compute(EQ, +1, 100, P, "CNC", date(2025, 1, 15))
    assert c.brokerage == 0
    assert c.stt == 10_000            # 0.1%      = Rs 100.00
    assert c.exchange_txn == 297      # 0.00297%  = Rs 2.97
    assert c.sebi == 10               # Rs 10/cr  = Rs 0.10
    assert c.stamp == 1_500           # 0.015%    = Rs 15.00 (buy side)
    assert c.gst == 55                # 18% of (0 + 2.97 + 0.10) = 0.5526
    assert c.total == 10_000 + 297 + 10 + 1_500 + 55


def test_equity_delivery_sell_has_no_stamp_but_dp_once():
    s = ChargeSchedule()
    c = s.compute(EQ, -1, 100, P, "CNC", date(2025, 1, 15), dp_applicable=True)
    assert c.stamp == 0 and c.stt == 10_000 and c.dp == 1_534


def test_intraday_equity_stt_sell_side_only_and_brokerage_cap():
    s = ChargeSchedule()
    buy = s.compute(EQ, +1, 1000, P, "MIS", date(2025, 1, 15))      # Rs 10,00,000
    sell = s.compute(EQ, -1, 1000, P, "MIS", date(2025, 1, 15))
    assert buy.stt == 0 and sell.stt == 25_000                      # 0.025% of 10 lakh = Rs 250
    assert buy.brokerage == 2_000 == sell.brokerage                 # min(0.03% = Rs 300, Rs 20)
    assert buy.stamp == 3_000 and sell.stamp == 0                   # 0.003% buy side


@pytest.mark.parametrize("d,fut_stt,opt_stt", [
    (date(2023, 3, 31), 0.0001, 0.0005),
    (date(2023, 4, 3), 0.000125, 0.000625),
    (date(2024, 9, 30), 0.000125, 0.000625),
    (date(2024, 10, 1), 0.0002, 0.001),
    (date(2026, 3, 31), 0.0002, 0.001),
    (date(2026, 4, 1), 0.0005, 0.0015),
])
def test_stt_changes_take_effect_on_the_right_date(d, fut_stt, opt_stt):
    s = ChargeSchedule(brokerage=BrokerageProfile.zero())
    f = s.compute(fut(expiry=date(2030, 1, 1)), -1, 50, 2_000_000, "NRML", d)       # Rs 10,00,000 notional
    o = s.compute(opt(20000, "CE", expiry=date(2030, 1, 1)), -1, 50, 20_000, "NRML", d)  # Rs 10,000 premium
    assert f.stt == round(50 * 2_000_000 * fut_stt)
    assert o.stt == round(50 * 20_000 * opt_stt)
    assert s.compute(fut(expiry=date(2030, 1, 1)), +1, 50, 2_000_000, "NRML", d).stt == 0   # buy side: no STT


def test_exchange_charges_change_on_2024_10_01():
    s = ChargeSchedule(brokerage=BrokerageProfile.zero())
    o = opt(20000, "CE", expiry=date(2030, 1, 1))
    before = s.compute(o, +1, 50, 20_000, "NRML", date(2024, 9, 30))
    after = s.compute(o, +1, 50, 20_000, "NRML", date(2024, 10, 1))
    assert before.exchange_txn == round(1_000_000 * 0.000495)
    assert after.exchange_txn == round(1_000_000 * 0.0003503)


def test_option_charges_are_on_premium_not_notional():
    c = ChargeSchedule().compute(opt(20000, "PE", expiry=date(2030, 1, 1)), -1, 50, 10_000, "NRML", date(2025, 6, 2))
    assert c.brokerage == 2_000                       # flat Rs 20
    assert c.stt == 500                               # 0.1% of Rs 5,000 premium
    assert c.stamp == 0


def test_exercise_stt_on_intrinsic():
    s = ChargeSchedule()
    o = opt(20000, "CE", expiry=date(2030, 1, 1))
    assert s.exercise_stt(o, 50, 30_000, date(2025, 6, 26)).stt == round(50 * 30_000 * 0.00125)
    assert s.exercise_stt(o, 50, 30_000, date(2026, 6, 25)).stt == round(50 * 30_000 * 0.0015)


def test_brokerage_cap_is_per_order_across_partial_fills():
    s = ChargeSchedule()
    a = s.compute(EQ, +1, 500, P, "MIS", date(2025, 1, 15))                      # Rs 5 lakh -> Rs 150 -> capped 20
    b = s.compute(EQ, +1, 500, P, "MIS", date(2025, 1, 15), order_value_before_paise=500 * P,
                  brokerage_paid_before_paise=a.brokerage)
    assert a.brokerage == 2_000 and b.brokerage == 0


def test_dates_before_coverage_are_refused_unless_extrapolation_is_explicit():
    with pytest.raises(RuleCoverageError):
        ChargeSchedule().compute(EQ, +1, 1, P, "CNC", date(2010, 1, 4))
    s = ChargeSchedule(allow_extrapolation=True)
    s.compute(EQ, +1, 1, P, "CNC", date(2010, 1, 4))
    assert date(2010, 1, 4) in s.extrapolated_dates


def test_cost_multiplier_scales_everything():
    base = ChargeSchedule().compute(EQ, +1, 100, P, "CNC", date(2025, 1, 15)).total
    assert ChargeSchedule(multiplier=0.0).compute(EQ, +1, 100, P, "CNC", date(2025, 1, 15)).total == 0
    assert abs(ChargeSchedule(multiplier=2.0).compute(EQ, +1, 100, P, "CNC", date(2025, 1, 15)).total - 2 * base) <= 5

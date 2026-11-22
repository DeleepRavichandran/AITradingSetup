"""Investment planning maths (checked against closed forms) and the AMFI NAV data contract."""
import math
from datetime import date, timedelta

import pytest
from nseql.analytics import planning as P
from nseql.data.funds import FundStore, parse_amfi


def test_xirr_matches_known_rates_and_rejects_undefined():
    assert P.xirr([(date(2025, 1, 1), -100), (date(2026, 1, 1), 110)]) == pytest.approx(0.10, abs=1e-6)       # 365-day year (as Excel XIRR)
    # monthly SIP of 1000 for 12 months returning exactly the 12%-annual future value gives 12%
    r = P.monthly_rate(0.12)
    flows = [(P.add_months(date(2024, 1, 1), k), -1000.0) for k in range(12)]
    fv = sum(1000 * (1 + r) ** (12 - k) for k in range(12))
    assert P.xirr(flows + [(date(2025, 1, 1), fv)]) == pytest.approx(0.12, abs=2e-3)       # day-count vs month-count
    assert P.xirr([(date(2024, 1, 1), -100), (date(2024, 6, 1), -50)]) is None
    assert P.xirr([]) is None


def test_sip_projection_equals_annuity_due_formula():
    r = P.monthly_rate(0.12)
    out = P.project_sip(10_000, 120, 0.12)
    closed = 10_000 * ((1 + r) ** 120 - 1) / r * (1 + r)
    assert out["value"] == pytest.approx(closed, abs=0.01)                 # outputs are rounded to the paisa
    assert out["invested"] == 1_200_000
    assert out["assumptions"]["nature"].startswith("user-assumed")
    up = P.project_sip(10_000, 24, 0.0, step_up=0.10)
    assert up["invested"] == pytest.approx(12 * 10_000 + 12 * 11_000)        # zero return: value = invested
    assert up["value"] == pytest.approx(up["invested"])


def test_swp_depletes_exactly_and_goal_plan_hits_target():
    s = P.project_swp(100_000, 10_000, 0.0)
    assert s["depleted_after_months"] == 10 and s["withdrawn"] == 100_000
    assert P.project_swp(1_000_000, 1_000, 0.10)["depleted_after_months"] is None     # withdrawal below growth: lasts
    g = P.goal_plan(1_000_000, 10, 0.10, 0.05, existing=100_000, step_up=0.05)
    target = 1_000_000 * 1.05 ** 10
    reached = P.project_sip(g["monthly_sip_needed"], 120, 0.10, step_up=0.05)["value"] + 100_000 * 1.10 ** 10
    assert g["target_future"] == pytest.approx(target, abs=0.01) and reached == pytest.approx(target, rel=1e-6)
    assert P.goal_plan(100, 5, 0.1, 0.0, existing=1_000)["monthly_sip_needed"] == 0


def test_stp_conserves_money_at_zero_returns():
    t = P.project_stp(120_000, 10_000, 0.0, 0.0)
    assert t["months"] == 12 and t["target_value"] == pytest.approx(120_000) and t["source_balance"] == 0


def _series(start: date, days: int, growth_per_day: float = 0.0004):
    d, v, x = [], [], 10.0
    cur = start
    while len(d) < days:
        if cur.weekday() < 5:
            d.append(cur)
            v.append(round(x, 4))
            x *= 1 + growth_per_day
        cur += timedelta(days=1)
    return d, v


def test_sip_on_nav_uses_next_nav_stamp_duty_and_unit_rounding():
    d, v = _series(date(2020, 6, 1), 400)
    out = P.sip_on_nav(d, v, 5_000, date(2020, 6, 1), date(2021, 6, 30), day=6)
    first, second = out["allotments"][0], out["allotments"][1]
    assert first["date"] == "2020-06-08"                                # 6 June 2020 was a Saturday -> next NAV, Monday 8th
    assert first["units"] == math.floor(5_000 / first["nav"] * 1000) / 1000          # before 1 July 2020: no stamp duty
    assert second["units"] == math.floor(5_000 * (1 - 0.00005) / second["nav"] * 1000) / 1000
    assert out["instalments"] == 13 and out["invested"] == 65_000 and out["xirr"] > 0
    assert out["value"] == pytest.approx(out["units"] * out["valuation_nav"], abs=0.01)


def test_nav_analytics_cagr_drawdown():
    d, v = _series(date(2018, 1, 1), 1600, 0.0003)
    v[800] = v[799] * 0.8                                               # one deep dip
    a = P.nav_analytics(d, v)
    assert a["cagr"]["1y"] is not None and a["cagr"]["10y"] is None and a["max_drawdown"] >= 0.2 - 1e-9
    assert 0 <= a["rolling_1y"]["share_negative"] <= 1


DAILY = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Large Cap Fund)

Example Mutual Fund

100001;INF000A01AA1;-;Example Bluechip Fund - Direct Plan - Growth;512.3456;21-Nov-2026
100002;INF000A01AB9;INF000A01AC7;Example Bluechip Fund - Regular Plan - IDCW;45.1200;21-Nov-2026
100003;INF000A01AD5;-;Example Closed Fund - Growth;N.A.;21-Nov-2026

Open Ended Schemes(Debt Scheme - Liquid Fund)

Other AMC

200001;INF000B01AA0;-;Other Liquid Fund - Direct Plan - Growth;3012.1;21-Nov-2026
"""
HISTORY = """Scheme Code;Scheme Name;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Net Asset Value;Repurchase Price;Sale Price;Date
100001;Example Bluechip Fund - Direct Plan - Growth;INF000A01AA1;;510.00;;;19-Nov-2026
100001;Example Bluechip Fund - Direct Plan - Growth;INF000A01AA1;;511.10;;;20-Nov-2026
100001;Example Bluechip Fund - Direct Plan - Growth;INF000A01AA1;;512.3456;;;21-Nov-2026
"""


def test_amfi_layouts_parse_and_import_is_idempotent(tmp_path):
    p = parse_amfi(DAILY)
    assert p["layout"] == "daily" and p["skipped"] == 1 and len(p["navs"]) == 3
    s = p["schemes"]["100001"]
    assert s["category"] == "Equity Scheme - Large Cap Fund" and s["amc"] == "Example Mutual Fund" and (s["plan"], s["option"]) == ("direct", "growth")
    assert p["schemes"]["100002"]["option"] == "idcw" and p["schemes"]["200001"]["amc"] == "Other AMC"
    with pytest.raises(ValueError):
        parse_amfi("not a nav file")
    fs = FundStore(tmp_path)
    a = fs.import_text(DAILY)
    b = fs.import_text(HISTORY)
    again = fs.import_text(HISTORY)
    assert a["new_navs"] == 3 and b["layout"] == "history" and b["new_navs"] == 2 and b["duplicates"] == 1
    assert again["new_navs"] == 0
    d, v = fs.series("100001")
    assert [x.isoformat() for x in d] == ["2026-11-19", "2026-11-20", "2026-11-21"] and v[-1] == 512.3456
    assert fs.scheme("100001")["category"] == "Equity Scheme - Large Cap Fund"     # history file did not wipe the category
    assert fs.search("bluechip")["total"] == 2 and fs.status()["navs"] == 5

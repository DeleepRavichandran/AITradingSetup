"""Audit items 2 and 5: forced liquidations and order-matching volume accounting."""
from datetime import date

import pandas as pd

from nseql.backtester.engine import Allocation
from nseql.backtester.orders import OrderStatus
from tests.helpers import EQ, IDX, Script, flat, frame, fut, opt, run

EXP = date(2025, 1, 10)


# ----------------------------------------------------------------- item 2: forced liquidations
def test_margin_call_liquidation_fills_on_the_next_bar_not_the_bar_that_triggered_it():
    f = fut(expiry=date(2030, 1, 1))
    px = [20000, 20000, 17000, 16500, 16500]
    res = run({"IDX": frame(flat(px)), f.symbol: frame(flat(px))}, [IDX, f], {0: lambda c: c.order(f.symbol, 50)}, capital=250_000)
    ts = frame(flat(px))["ts"]
    mc = [x for x in res.account.fills if x.reason == "margin_call"]
    call = next(e for e in res.events if e["type"] == "MARGIN_CALL")
    assert call["ts"] == str(ts[2])                               # breach is observed at the close of bar 2
    assert len(mc) == 1 and mc[0].ts == ts[3]                     # ...so the earliest possible exit is bar 3
    assert mc[0].price == 16500_00                                # at bar 3's open, not bar 2's close (17000)
    assert res.equity[-1] == 250_000_00 - 50 * 3500_00


def test_margin_call_liquidation_respects_liquidity():
    f = fut(expiry=date(2030, 1, 1))
    idx = frame(flat([20000, 20000, 17000, 17000, 17000, 17000]))
    thin = frame([(20000, 20000, 20000, 20000, 10_000), (20000, 20000, 20000, 20000, 10_000), (17000, 17000, 17000, 17000, 10_000),
                  (17000, 17000, 17000, 17000, 0), (17000, 17000, 17000, 17000, 500), (17000, 17000, 17000, 17000, 10_000)])
    res = run({"IDX": idx, f.symbol: thin}, [IDX, f], {0: lambda c: c.order(f.symbol, 100)}, capital=500_000,
              exec_kw={"max_participation": 0.10})
    mc = [(x.ts, x.qty) for x in res.account.fills if x.reason == "margin_call"]
    # bar 3 has no volume (no fill); bar 4 can absorb one lot (10% of 500 = 50); the rest goes on bar 5
    assert mc == [(thin["ts"][4], 50), (thin["ts"][5], 50)]


def _intraday(prices_by_time):
    ts = [pd.Timestamp(t) for t, *_ in prices_by_time]
    return frame([r[1:] for r in prices_by_time], ts=ts)


def test_mis_square_off_fills_on_the_bar_after_the_cutoff_and_needs_volume():
    rows = [("2025-01-06 15:18", 100, 100, 100, 100, 1000), ("2025-01-06 15:19", 100, 100, 100, 100, 1000),
            ("2025-01-06 15:20", 101, 101, 101, 101, 1000), ("2025-01-06 15:21", 103, 103, 103, 103, 0),
            ("2025-01-06 15:22", 104, 104, 104, 104, 1000), ("2025-01-06 15:23", 105, 105, 105, 105, 1000)]
    res = run({"AAA": _intraday(rows)}, [EQ], {0: lambda c: c.order("AAA", 100, product="MIS")}, interval="1m")
    sq = [(str(f.ts)[11:16], f.qty, f.price / 100) for f in res.account.fills if f.reason == "squareoff"]
    # cut-off seen at the 15:20 close -> order works from 15:21; 15:21 has no volume; fills at the 15:22 open
    assert sq == [("15:22", 100, 104.0)]
    assert not res.account.books["t"].positions


def test_mis_position_that_cannot_be_squared_off_is_carried_and_flagged_not_filled_at_a_stale_price():
    rows = [("2025-01-06 15:19", 100, 100, 100, 100, 1000), ("2025-01-06 15:20", 100, 100, 100, 100, 1000),
            ("2025-01-06 15:21", 100, 100, 100, 100, 0), ("2025-01-07 09:16", 90, 90, 90, 90, 1000), ("2025-01-07 09:17", 90, 90, 90, 90, 1000)]
    res = run({"AAA": _intraday(rows)}, [EQ], {0: lambda c: c.order("AAA", 100, product="MIS")}, interval="1m")
    sq = [(str(f.ts)[:16], f.price / 100) for f in res.account.fills if f.reason == "squareoff"]
    assert sq == [("2025-01-07 09:16", 90.0)]                      # no trade was possible on the 6th
    assert any("could not be squared off" in x for x in res.flags)
    assert any(e["type"] == "MIS_CARRIED_OVERNIGHT" for e in res.events)


def test_strategy_cannot_cancel_or_trade_against_a_pending_liquidation():
    f = fut(expiry=date(2030, 1, 1))
    px = [20000, 20000, 17000, 17000, 17000]

    def meddle(c):
        c.cancel_all()
        c.order(f.symbol, 50, tag="double_down")
    res = run({"IDX": frame(flat(px)), f.symbol: frame(flat(px))}, [IDX, f], {0: lambda c: c.order(f.symbol, 50), 2: meddle}, capital=250_000)
    assert [x.reason for x in res.account.fills] == ["order", "margin_call"]
    dd = next(o for o in res.orders if o.tag == "double_down")
    assert dd.status is OrderStatus.REJECTED and dd.reason.startswith("LIQUIDATION_PENDING")


# ----------------------------------------------------------------- item 5: volume reservation, OCO, multi-leg
def test_oco_worst_case_holds_even_when_volume_only_covers_one_sibling():
    """Bar reaches both target and stop; liquidity covers one order. The stop must still win."""
    rows = [(100, 100, 100, 100, 1000), (100, 100, 100, 100, 1000), (100, 112, 94, 105, 100), (105, 105, 105, 105, 1000)]

    def bracket(c):
        c.order("AAA", -10, type="LIMIT", limit=110, tif="GTC", oco="x", tag="target")
        c.order("AAA", -10, type="STOP", stop=95, tif="GTC", oco="x", tag="stop")
    res = run({"AAA": frame(rows)}, [EQ], {0: lambda c: c.order("AAA", 10), 1: bracket}, exec_kw={"max_participation": 0.10})
    exits = [(f.tag, f.price / 100) for f in res.account.fills[1:]]
    assert exits == [("stop", 95.0)]


def test_oco_loser_does_not_consume_volume_needed_by_another_order():
    rows = [(100, 100, 100, 100, 1000), (100, 100, 100, 100, 1000), (100, 112, 94, 105, 200), (105, 105, 105, 105, 1000)]

    def bracket(c):
        c.order("AAA", -10, type="LIMIT", limit=110, tif="GTC", oco="x", tag="target")
        c.order("AAA", -10, type="STOP", stop=95, tif="GTC", oco="x", tag="stop")
    a = Script({0: lambda c: c.order("AAA", 10), 1: bracket})
    b = Script({1: lambda c: c.order("AAA", 10, tag="other")})
    res = run({"AAA": frame(rows)}, [EQ], {}, strategies=[Allocation(a, "a", 1e6), Allocation(b, "b", 1e6)], exec_kw={"max_participation": 0.10})
    bar2 = frame(rows)["ts"][2]
    # cap on bar 2 is 20: the stop takes 10 and the unrelated buy takes the other 10; the cancelled target takes none
    assert sorted((f.tag, f.qty) for f in res.account.fills if f.ts == bar2) == [("other", 10), ("stop", 10)]


def test_unfillable_multi_leg_group_does_not_starve_other_orders():
    a_, b_ = opt(20000, "CE", EXP), opt(20500, "CE", EXP)
    liquid = frame(flat([300] * 5, vol=1000))
    dead = frame([(100, 100, 100, 100, 1000), (100, 100, 100, 100, 0), (100, 100, 100, 100, 1000), (100, 100, 100, 100, 1000), (0.05, 0.05, 0.05, 0.05, 1000)])
    s1 = Script({0: lambda c: c.multi_leg([(a_.symbol, 100), (b_.symbol, -100)], tag="spread")})
    s2 = Script({0: lambda c: c.order(a_.symbol, 100, tag="single")})
    res = run({"IDX": frame(flat([20000] * 5)), a_.symbol: liquid, b_.symbol: dead}, [IDX, a_, b_], {},
              strategies=[Allocation(s1, "s1", 1e7), Allocation(s2, "s2", 1e7)], exec_kw={"max_participation": 0.10})
    # leg b has no volume on bar 1 so the spread cannot fill; its leg a must not block the single order (cap = 100)
    assert [(f.tag, f.qty, f.ts) for f in res.account.fills if f.reason == "order"] == [("single", 100, liquid["ts"][1])]


def test_two_orders_share_a_bars_volume_in_submission_order_without_double_spending():
    rows = flat([100, 100, 100, 100], vol=300)
    s1 = Script({0: lambda c: c.order("AAA", 20, tif="GTC", tag="first")})
    s2 = Script({0: lambda c: c.order("AAA", 20, tif="GTC", tag="second")})
    res = run({"AAA": frame(rows)}, [EQ], {}, strategies=[Allocation(s1, "s1", 1e6), Allocation(s2, "s2", 1e6)], exec_kw={"max_participation": 0.10})
    by_bar: dict = {}
    for f in res.account.fills:
        by_bar[f.ts] = by_bar.get(f.ts, 0) + f.qty
    assert all(v <= 30 for v in by_bar.values())                  # never more than 10% of 300 per bar, across books
    assert [(f.tag, f.qty) for f in res.account.fills] == [("first", 20), ("second", 10), ("second", 10)]


def test_order_rejected_for_margin_at_fill_releases_its_volume():
    rows = [(100, 100, 100, 100, 1000), (150, 150, 150, 150, 100), (150, 150, 150, 150, 1000)]
    poor = Script({0: lambda c: c.order("AAA", 10, tag="poor")})         # affordable at 100, not at the 150 gap
    rich = Script({0: lambda c: c.order("AAA", 10, tag="rich")})
    res = run({"AAA": frame(rows)}, [EQ], {}, strategies=[Allocation(poor, "p", 1_200), Allocation(rich, "r", 1e6)], exec_kw={"max_participation": 0.10})
    assert res.orders[0].reason == "INSUFFICIENT_MARGIN (at fill)"
    assert [(f.tag, f.qty, f.ts) for f in res.account.fills] == [("rich", 10, frame(rows)["ts"][1])]


def test_atomic_group_reserves_volume_for_all_legs_or_none():
    a_, b_ = opt(20000, "CE", EXP), opt(20500, "CE", EXP)
    f = frame(flat([300] * 5, vol=1000))
    g1 = Script({0: lambda c: c.multi_leg([(a_.symbol, 100), (b_.symbol, -100)], tag="g1")})
    g2 = Script({0: lambda c: c.multi_leg([(a_.symbol, 100), (b_.symbol, -100)], tag="g2")})
    res = run({"IDX": frame(flat([20000] * 5)), a_.symbol: f, b_.symbol: frame(flat([100] * 5, vol=1000))}, [IDX, a_, b_], {},
              strategies=[Allocation(g1, "g1", 1e7), Allocation(g2, "g2", 1e7)], exec_kw={"max_participation": 0.10})
    # cap is 100 per symbol per bar: only one whole spread fits; the second expires rather than half-filling
    assert sorted((x.tag, x.symbol) for x in res.account.fills if x.reason == "order") == [("g1", a_.symbol), ("g1", b_.symbol)]
    assert all(o.status is OrderStatus.EXPIRED for o in res.orders if o.tag == "g2")

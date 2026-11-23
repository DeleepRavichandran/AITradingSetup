"""Fill rules: timing, order types, intrabar ambiguity, liquidity, price limits."""
import pandas as pd

from nseql.backtester.orders import OrderStatus
from nseql.instruments.models import Instrument, Kind
from tests.helpers import EQ, flat, frame, run


def _fills(res):
    return [(f.ts, f.side, f.qty, f.price / 100) for f in res.account.fills]


def test_market_order_fills_at_next_bar_open_never_same_bar():
    rows = [(100, 101, 99, 100), (110, 112, 108, 111), (120, 121, 119, 120)]
    res = run({"AAA": frame(rows)}, [EQ], {0: lambda c: c.order("AAA", 10)})
    (ts, side, qty, px), = _fills(res)
    assert px == 110.0 and qty == 10                       # open of bar 1, not close of bar 0 (100)
    assert ts == frame(rows)["ts"][1]
    assert res.orders[0].submitted_ts == frame(rows)["ts"][0]


def test_limit_buy_needs_trade_through_not_touch():
    rows = [(100, 100, 100, 100), (100, 101, 95, 99), (99, 100, 94, 96)]
    touch = run({"AAA": frame(rows)}, [EQ], {0: lambda c: c.order("AAA", 10, type="LIMIT", limit=95, tif="GTC")})
    assert _fills(touch)[0][3] == 95.0 and _fills(touch)[0][0] == frame(rows)["ts"][2]   # bar1 only touched 95
    allowed = run({"AAA": frame(rows)}, [EQ], {0: lambda c: c.order("AAA", 10, type="LIMIT", limit=95, tif="GTC")},
                  exec_kw={"limit_fill_on_touch": True})
    assert _fills(allowed)[0][0] == frame(rows)["ts"][1]


def test_limit_gap_through_gets_price_improvement_at_open():
    rows = [(100, 100, 100, 100), (90, 92, 89, 91)]
    res = run({"AAA": frame(rows)}, [EQ], {0: lambda c: c.order("AAA", 10, type="LIMIT", limit=95)})
    assert _fills(res)[0][3] == 90.0


def test_day_limit_order_expires_unfilled():
    rows = [(100, 100, 100, 100), (100, 101, 99, 100), (100, 100, 80, 85)]
    res = run({"AAA": frame(rows)}, [EQ], {0: lambda c: c.order("AAA", 10, type="LIMIT", limit=90)})
    assert not res.account.fills and res.orders[0].status is OrderStatus.EXPIRED


def test_stop_sell_fills_at_worse_of_stop_and_open():
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (99, 99, 90, 92), (80, 81, 79, 80)]
    acts = {0: lambda c: c.order("AAA", 10), 1: lambda c: c.order("AAA", -10, type="STOP", stop=95, tif="GTC")}
    assert _fills(run({"AAA": frame(rows)}, [EQ], acts))[1][3] == 95.0          # traded through intrabar
    gap = [(100, 100, 100, 100), (100, 100, 100, 100), (90, 91, 88, 89)]
    assert _fills(run({"AAA": frame(gap)}, [EQ], acts))[1][3] == 90.0           # gapped below the stop


def test_stop_limit_gap_beyond_limit_does_not_fill():
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (110, 112, 109, 111), (111, 111, 104, 105)]
    acts = {1: lambda c: c.order("AAA", 10, type="STOP_LIMIT", stop=103, limit=105, tif="GTC")}
    res = run({"AAA": frame(rows)}, [EQ], acts)
    # bar 2 gaps to 110 > limit 105: triggered but unfilled; bar 3 trades down through 105 -> fills at 105
    (ts, _, _, px), = _fills(res)
    assert px == 105.0 and ts == frame(rows)["ts"][3]


def test_oco_ambiguity_resolved_against_the_trader():
    # bar 2 reaches both the target (110) and the stop (95): the stop must be assumed to fill first
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (100, 112, 94, 105), (105, 105, 105, 105)]

    def bracket(c):
        c.order("AAA", -10, type="LIMIT", limit=110, tif="GTC", oco="x", tag="target")
        c.order("AAA", -10, type="STOP", stop=95, tif="GTC", oco="x", tag="stop")
    res = run({"AAA": frame(rows)}, [EQ], {0: lambda c: c.order("AAA", 10), 1: bracket})
    assert _fills(res)[1][3] == 95.0
    assert [o.status for o in res.orders[1:]] == [OrderStatus.CANCELLED, OrderStatus.FILLED]
    assert "t" in res.account.books and not res.account.books["t"].positions


def test_partial_fills_respect_participation_cap():
    rows = flat([100, 100, 100, 100, 100], vol=1000)
    res = run({"AAA": frame(rows)}, [EQ], {0: lambda c: c.order("AAA", 250, tif="GTC")},
              exec_kw={"max_participation": 0.10})
    assert [f.qty for f in res.account.fills] == [100, 100, 50]
    assert res.orders[0].status is OrderStatus.FILLED


def test_zero_volume_bar_fills_nothing():
    rows = [(100, 100, 100, 100, 1000), (100, 100, 100, 100, 0), (101, 101, 101, 101, 1000)]
    res = run({"AAA": frame(rows)}, [EQ], {0: lambda c: c.order("AAA", 10, tif="GTC")})
    assert _fills(res)[0][3] == 101.0


def test_slippage_is_adverse_and_on_the_tick_grid():
    rows = [(100, 100, 100, 100), (100.03, 101, 99, 100), (100, 100, 100, 100)]
    f = frame(rows)
    buy = run({"AAA": f}, [EQ], {0: lambda c: c.order("AAA", 10)}, exec_kw={"spread_bps_eq": 10})
    px = _fills(buy)[0][3]
    assert px == 100.10                                                          # 100.08 rounded UP to the tick


def test_upper_circuit_lock_blocks_buys():
    banded = Instrument("AAA", Kind.EQ, price_band_pct=0.10)
    rows = [(100, 100, 100, 100), (110, 110, 110, 110), (112, 113, 111, 112)]
    res = run({"AAA": frame(rows)}, [banded], {0: lambda c: c.order("AAA", 10, tif="GTC")})
    assert _fills(res)[0][0] == frame(rows)["ts"][2]                             # bar 1 was locked limit-up


def test_fill_ordering_is_by_submission_sequence():
    rows = flat([100, 100, 100], vol=100)

    def two(c):
        c.order("AAA", 8, tag="first", tif="GTC")
        c.order("AAA", 8, tag="second", tif="GTC")
    res = run({"AAA": frame(rows)}, [EQ], {0: two}, exec_kw={"max_participation": 0.10})
    assert [(f.tag, f.qty) for f in res.account.fills] == [("first", 8), ("second", 2), ("second", 6)]


def test_latency_delays_fills_by_extra_bars():
    rows = flat([100, 101, 102, 103])
    res = run({"AAA": frame(rows)}, [EQ], {0: lambda c: c.order("AAA", 10, tif="GTC")}, exec_kw={"latency_bars": 1})
    assert _fills(res)[0][3] == 102.0


def test_stop_limit_buy_never_fills_below_its_trigger_across_matching_passes():
    """Regression (review 2026-11-24): stop 105, limit 106; the bar opens at 100 and later reaches 106.
    The reachability pass marked the order triggered, and the allocation pass then priced it as a plain
    limit order at the 100 open, i.e. before the stop could have been hit. The fill must be at 105 or worse."""
    rows = [(100, 100, 100, 100), (100, 106, 99, 105), (105, 106, 104, 105)]
    res = run({"AAA": frame(rows)}, [EQ], {0: lambda c: c.order("AAA", 10, type="STOP_LIMIT", stop=105, limit=106, tif="GTC")})
    (ts, side, qty, px), = _fills(res)
    assert ts == frame(rows)["ts"][1] and 105.0 <= px <= 106.0, px


def test_stop_limit_triggered_on_one_bar_rests_as_a_limit_on_later_bars():
    """The stop is hit on bar 1 but the limit is not reachable there (opens above the limit); on bar 2 the order
    is a resting limit, so a lower open is a legitimate fill at that open."""
    rows = [(100, 100, 100, 100), (107, 108, 107, 107.5), (105, 105.5, 104, 105)]
    res = run({"AAA": frame(rows)}, [EQ], {0: lambda c: c.order("AAA", 10, type="STOP_LIMIT", stop=105, limit=106, tif="GTC")})
    (ts, side, qty, px), = _fills(res)
    assert ts == frame(rows)["ts"][2] and px == 105.0

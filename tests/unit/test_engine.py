"""Event loop behaviour: causality, determinism, order validation, sessions, corporate actions."""
import numpy as np
import pandas as pd
import pytest

from nseql.backtester.engine import Allocation, BacktestEngine
from nseql.backtester.orders import OrderStatus
from nseql.backtester.runner import result_hash
from nseql.data.portal import DataPortal
from nseql.instruments.models import CorporateAction, Instrument, Kind
from nseql.risk.limits import KillSwitch, RiskLimits
from nseql.strategies import registry
from tests.helpers import EQ, IDX, Script, flat, frame, run


def test_strategy_only_sees_bars_up_to_now():
    rows = flat([10, 20, 30, 40, 50])
    seen = []
    run({"AAA": frame(rows)}, [EQ], {i: (lambda c: seen.append(list(c.history("AAA")))) for i in range(5)})
    assert seen == [[10], [10, 20], [10, 20, 30], [10, 20, 30, 40], [10, 20, 30, 40, 50]]


def test_history_is_a_copy_and_cannot_corrupt_the_store():
    rows = flat([10, 20, 30])

    def vandal(c):
        h = c.history("AAA")
        h[:] = 999
    seen = []
    run({"AAA": frame(rows)}, [EQ], {0: vandal, 2: lambda c: seen.append(list(c.history("AAA")))})
    assert seen == [[10, 20, 30]]


def test_portal_cannot_move_backwards():
    p = DataPortal({"AAA": frame(flat([1, 2, 3]))}, {"AAA": EQ})
    p.advance(int(p.timeline[1]))
    with pytest.raises(ValueError):
        p.advance(int(p.timeline[0]))


def _synthetic_frames(seed=3, n=260):
    rng = np.random.default_rng(seed)
    out = {}
    for j, s in enumerate(["AAA", "BBB", "CCC", "DDD"]):
        c = 100 * (1 + j) * np.exp(np.cumsum(rng.normal(0.0004, 0.015, n)))
        o = np.roll(c, 1) * np.exp(rng.normal(0, 0.004, n)); o[0] = c[0]
        hi = np.maximum(o, c) * 1.006
        lo = np.minimum(o, c) * 0.994
        r = lambda x: np.round(x / 0.05) * 0.05
        out[s] = frame(list(zip(r(o), r(hi) + 0.05, r(lo) - 0.05, r(c), rng.integers(200_000, 1_500_000, n))), start="2024-01-01")
    return out, {s: Instrument(s, Kind.EQ) for s in out}


@pytest.mark.parametrize("name", ["sma_cross", "momentum_rank", "rsi_reversion", "donchian_breakout",
                                  "bollinger_reversion", "zscore_reversion", "prev_day_breakout", "volume_breakout"])
def test_decisions_do_not_depend_on_future_data(name):
    """Scramble every price after a cut date; orders submitted up to the cut must be identical."""
    frames, instr = _synthetic_frames()
    params = {"sma_cross": {"fast": 5, "slow": 20}, "momentum_rank": {"lookback": 40, "top_k": 2, "rebalance": 5},
              "rsi_reversion": {"trend": 30, "buy_below": 40}, "donchian_breakout": {"entry": 20, "exit": 10},
              "prev_day_breakout": {"trend": 20}, "volume_breakout": {"n": 10, "vol_mult": 1.2}}.get(name, {})

    def orders(portal):
        eng = BacktestEngine(portal, [Allocation(registry.create(name, params), "b", 1_000_000)])
        res = eng.run()
        assert res.error is None
        return res.orders

    base = DataPortal(frames, instr)
    for cut_i in (120, 200):
        cut = int(base.timeline[cut_i])
        a = [o.to_dict() for o in orders(DataPortal(frames, instr)) if o.submitted_ts.value <= cut]
        b = [o.to_dict() for o in orders(base.perturbed_after(cut, seed=cut_i)) if o.submitted_ts.value <= cut]
        strip = lambda rows: [{k: v for k, v in r.items() if k not in ("status", "filled_qty", "avg_fill_price", "reason")} for r in rows]
        assert len(a) > 0 and strip(a) == strip(b)


def test_truncating_the_data_does_not_change_earlier_fills():
    frames, instr = _synthetic_frames()
    full = BacktestEngine(DataPortal(frames, instr), [Allocation(registry.create("sma_cross", {"fast": 5, "slow": 20}), "b", 1e6)]).run()
    cut = int(DataPortal(frames, instr).timeline[150])
    part = BacktestEngine(DataPortal(frames, instr).truncated(cut),
                          [Allocation(registry.create("sma_cross", {"fast": 5, "slow": 20}), "b", 1e6)]).run()
    key = lambda fs: [(f.ts, f.symbol, f.side, f.qty, f.price) for f in fs if f.ts.value <= cut]
    assert key(full.account.fills) == key(part.account.fills) and len(part.account.fills) > 0


def test_replay_is_deterministic():
    frames, instr = _synthetic_frames()
    h = {result_hash(BacktestEngine(DataPortal(frames, instr), [Allocation(registry.create("momentum_rank", {"lookback": 40, "top_k": 2, "rebalance": 5}), "b", 1e6)]).run())
         for _ in range(3)}
    assert len(h) == 1


def test_order_validation_rejections():
    lot = Instrument("LOT", Kind.EQ, lot_size=25)
    rows = flat([100, 100, 100])
    res = run({"AAA": frame(rows), "LOT": frame(rows), "IDX": frame(rows)}, [EQ, lot, IDX], {0: lambda c: (
        c.order("LOT", 10), c.order("IDX", 1), c.order("AAA", -5), c.order("ZZZ", 1),
        c.order("AAA", 1, type="LIMIT"), c.order("AAA", 1_000_000), c.order("AAA", 5, product="MIS"))})
    reasons = [o.reason.split(" ")[0] for o in res.orders]
    assert reasons == ["BAD_QUANTITY", "NOT_TRADABLE", "SHORT_SELL_NOT_ALLOWED", "UNKNOWN_INSTRUMENT",
                       "LIMIT_PRICE_REQUIRED", "MAX_ORDER_NOTIONAL", "MIS_REQUIRES_INTRADAY_DATA"]
    assert all(o.status is OrderStatus.REJECTED for o in res.orders) and not res.account.fills


def test_insufficient_funds_rejected_pre_trade_and_at_fill():
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (130, 130, 130, 130)]
    pre = run({"AAA": frame(rows)}, [EQ], {0: lambda c: c.order("AAA", 1001)}, capital=100_000)
    assert pre.orders[0].reason.startswith("INSUFFICIENT_MARGIN (pre-trade)")
    gap = run({"AAA": frame(rows)}, [EQ], {1: lambda c: c.order("AAA", 1000)}, capital=100_000)
    assert gap.orders[0].reason == "INSUFFICIENT_MARGIN (at fill)" and not gap.account.fills


def test_not_listed_instrument_is_rejected():
    from datetime import date
    late = Instrument("AAA", Kind.EQ, listed_from=date(2025, 1, 8))
    res = run({"AAA": frame(flat([100, 100, 100, 100]))}, [late], {0: lambda c: c.order("AAA", 1), 3: lambda c: c.order("AAA", 1)})
    assert res.orders[0].reason.startswith("NOT_LISTED") and res.orders[1].status is not OrderStatus.REJECTED


def test_kill_switch_blocks_new_orders_and_needs_a_human_to_reset():
    ks = KillSwitch()
    ks.engage("test halt")
    p = DataPortal({"AAA": frame(flat([100, 100, 100]))}, {"AAA": EQ})
    res = BacktestEngine(p, [Allocation(Script({0: lambda c: c.order("AAA", 1)}), "t", 1e6)], kill_switch=ks).run()
    assert res.orders[0].reason.startswith("KILL_SWITCH")
    with pytest.raises(PermissionError):
        ks.reset("agent:research-director", "please")
    ks.reset("r.sharma", "verified feed restored")
    assert not ks.engaged


def test_daily_loss_limit_halts_new_entries():
    idx = pd.to_datetime(["2025-01-06 09:16", "2025-01-06 09:17", "2025-01-06 09:18", "2025-01-06 09:19", "2025-01-06 09:20"])
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (90, 90, 90, 90), (90, 90, 90, 90), (90, 90, 90, 90)]
    res = run({"AAA": frame(rows, ts=list(idx))}, [EQ], {0: lambda c: c.order("AAA", 5000), 3: lambda c: c.order("AAA", 1)},
              interval="1m", risk=RiskLimits(max_daily_loss_pct=0.02))
    assert any(e["type"] == "DAILY_LOSS_HALT" for e in res.events)
    assert res.orders[1].reason.startswith("DAILY_LOSS_LIMIT")


def test_intraday_mis_positions_are_squared_off_and_shorts_allowed():
    ts = list(pd.date_range("2025-01-06 15:15", periods=10, freq="1min")) + list(pd.date_range("2025-01-07 09:16", periods=3, freq="1min"))
    rows = flat([100] * 13)
    res = run({"AAA": frame(rows, ts=ts)}, [EQ], {0: lambda c: c.order("AAA", -100, product="MIS"),
                                                  7: lambda c: c.order("AAA", 10, product="MIS")}, interval="1m")
    sq = [f for f in res.account.fills if f.reason == "squareoff"]
    assert len(sq) == 1 and sq[0].ts == pd.Timestamp("2025-01-06 15:21") and sq[0].qty == 100   # bar after the cut-off
    late = [o for o in res.orders if not o.forced][1]
    assert late.reason == "MIS_AFTER_SQUARE_OFF" and not res.account.books["t"].positions


def test_split_keeps_equity_continuous_and_adjusts_history():
    rows = [(1000, 1000, 1000, 1000), (1000, 1000, 1000, 1000), (500, 500, 500, 500), (510, 510, 510, 510)]
    f = frame(rows)
    ca = [CorporateAction("AAA", f["ts"][2].date(), "split", ratio=2.0)]
    hist = {}
    res = run({"AAA": f}, [EQ], {0: lambda c: c.order("AAA", 10),
                                 3: lambda c: hist.update(adj=list(c.history("AAA")), raw=list(c.history("AAA", adjusted=False)))},
              actions_ca=ca)
    assert res.account.books["t"].positions["AAA"].qty == 20
    assert list(res.equity / 100) == [1_000_000, 1_000_000, 1_000_000, 1_000_200]
    assert hist["adj"] == [500, 500, 500, 510] and hist["raw"] == [1000, 1000, 500, 510]


def test_split_is_not_applied_to_history_before_it_happens():
    rows = flat([1000, 1000, 500])
    f = frame(rows)
    seen = {}
    run({"AAA": f}, [EQ], {1: lambda c: seen.update(h=list(c.history("AAA")))},
        actions_ca=[CorporateAction("AAA", f["ts"][2].date(), "split", ratio=2.0)])
    assert seen["h"] == [1000, 1000]          # the day before the split nobody knows about it


def test_dividend_is_credited_on_ex_date():
    f = frame(flat([100, 100, 100]))
    res = run({"AAA": f}, [EQ], {0: lambda c: c.order("AAA", 100)}, actions_ca=[CorporateAction("AAA", f["ts"][2].date(), "dividend", amount=3.0)])
    assert res.account.books["t"].dividends == 300_00 and res.equity[-1] == 1_000_000_00 + 300_00


def test_two_strategies_share_one_account_without_double_spending():
    rows = flat([100, 100, 100])
    a, b = Script({0: lambda c: c.order("AAA", 900)}), Script({0: lambda c: c.order("AAA", 900)})
    res = run({"AAA": frame(rows)}, [EQ], {}, strategies=[Allocation(a, "a", 100_000), Allocation(b, "b", 50_000)])
    assert [o.status for o in res.orders] == [OrderStatus.FILLED, OrderStatus.REJECTED]   # b only has 50k
    assert res.account.cash == res.account.books["a"].cash + res.account.books["b"].cash
    assert res.account.books["b"].cash == 50_000_00


def test_strategy_exception_fails_the_run_loudly():
    def boom(c):
        raise RuntimeError("bug in strategy")
    p = DataPortal({"AAA": frame(flat([100, 100, 100]))}, {"AAA": EQ})
    res = BacktestEngine(p, [Allocation(Script({1: boom}), "t", 1e6)]).run()
    assert res.error == "RuntimeError: bug in strategy"

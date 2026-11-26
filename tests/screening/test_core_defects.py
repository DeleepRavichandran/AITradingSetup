"""Regression tests for the four trading-core defects (review of 1376b45), reproduced through MinuteScheduler.

D1 kill switch      pending buys filled one minute after the halt was engaged
D2 exposure         two 8-share buys at Rs 100 both filled against a Rs 1,000 symbol cap
D3 cash             a Rs 10,000 delivery buy in a Rs 10,000 account left cash below zero after charges
D4 forward data     a whole session delivered at 15:31 produced 'live' fills stamped 09:21
"""
import numpy as np
import pandas as pd
import pytest

from nseql.screening.challenger import challenger_rules, factory as ch_factory
from nseql.screening.execution import ExecConfig
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols
from tests.screening.scripted import bar, factory, rules

DAY = "2026-11-16"


class Kill:
    def __init__(self):
        self.engaged, self.reason = False, None


def T(hm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{DAY}T{hm}:00")


def sched(tmp_path, cfgs, script, syms=("AAA", "BBB"), qty=None, mode="live", **kw):
    kw.setdefault("portfolio", rules(100_000))
    return MinuteScheduler(tmp_path / "acct", list(syms), mode=mode, engine_factory=factory(cfgs, script, qty), **kw)


def run(sch, bars_by_minute: dict[str, list[dict]], lag_s: float = 6.0):
    out = {}
    for hm, bars in bars_by_minute.items():
        sch.ingest(bars)
        out[hm] = sch.run_cycle(T(hm) + pd.Timedelta(seconds=lag_s))
    return out


def fills(sch, side=None):
    q = "SELECT config_id, side, ts, qty, price, charges_paise FROM paper_fills" + (f" WHERE side='{side}'" if side else "") + " ORDER BY ts"
    return sch.store.db.execute(q).fetchall()


def flat(px, n, start="09:16", syms=("AAA", "BBB"), vol=1_000_000):
    t0 = T(start)
    return {(t0 + pd.Timedelta(minutes=k)).strftime("%H:%M"): [bar(s, t0 + pd.Timedelta(minutes=k), px, vol) for s in syms] for k in range(n)}


# ---------------------------------------------------------------------------------------------------- D1 kill switch
def test_d1_kill_switch_blocks_pending_buys_before_the_next_bar(tmp_path):
    kill = Kill()
    sch = sched(tmp_path, [("AAA", True), ("BBB", True)], {"09:20": {0: "BUY", 1: "BUY"}}, kill=kill,
                portfolio=rules(100_000, per_trade_pct=0.1))
    bars = flat(100.0, 8)
    run(sch, dict(list(bars.items())[:5]))                       # 09:16..09:20: two buys decided at 09:20
    assert (sch.engine.pend == 1).sum() == 2
    kill.engaged = True                                          # halt engaged before the 09:21 bar
    run(sch, dict(list(bars.items())[5:]))
    assert fills(sch, "BUY") == []
    st = dict(sch.store.db.execute("SELECT status, COUNT(*) FROM paper_orders WHERE side='BUY' GROUP BY status").fetchall())
    assert st == {"cancelled": 2}
    assert sch.engine.x.reserved == 0 and sch.engine.x.cash == 100_000 * 100


def test_d1_kill_switch_cancels_the_rest_of_a_partial_buy_and_keeps_protective_exits(tmp_path):
    kill = Kill()
    sch = sched(tmp_path, [("AAA", True), ("BBB", True)], {"09:17": {1: "BUY"}, "09:20": {0: "BUY"}, "09:23": {1: "EXIT"}}, kill=kill,
                portfolio=rules(100_000, per_trade_pct=0.3))
    bars = flat(100.0, 12, vol=1_000_000)
    # execution 2.1: a buy decided at 09:20 first fills at the open of 09:22 (first price after the decision)
    bars["09:22"] = [bar("AAA", T("09:22"), 100.0, vol=1_000), bar("BBB", T("09:22"), 100.0)]     # thin bar: 100 of 500 fill
    run(sch, {k: v for k, v in bars.items() if k <= "09:22"})
    part = fills(sch, "BUY")
    assert [f[3] for f in part if f[2].endswith("09:22:00")] == [100]                              # partial before the halt
    kill.engaged = True
    run(sch, {k: v for k, v in bars.items() if k > "09:22"})
    after = [f for f in fills(sch, "BUY") if f[2] > f"{DAY}T09:22:00"]
    assert after == []                                                                             # remainder cancelled
    st = sch.store.db.execute("SELECT status, qty, reason FROM paper_orders WHERE side='BUY' AND symbol='AAA'").fetchone()
    assert st[0] == "partial" and st[1] == 100 and "kill switch" in st[2]
    # protective exit decided while halted is accepted and fills (reduce-only); the position is closed
    ex = fills(sch, "SELL")
    assert len(ex) == 1 and ex[0][2].endswith("09:25:00") and sch.engine.pos[1] == 0               # decided 09:23, filled 09:25


def test_d1_kill_switch_same_semantics_on_the_real_incumbent_engine(tmp_path):
    kill = Kill()
    syms = symbols(20)
    sch = MinuteScheduler(tmp_path / "inc", syms, kill=kill, mode="live")
    stream = minute_stream(20, [DAY], seed=4)
    for k, (ts, bars) in enumerate(stream):
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
        if k > 40 and (sch.engine.pend == 1).any():
            break
    assert (sch.engine.pend == 1).any(), "no pending buy to test with"
    halted_after = ts
    kill.engaged = True
    for ts, bars in stream[k + 1:k + 20]:
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    late = sch.store.db.execute("SELECT COUNT(*) FROM paper_fills WHERE side='BUY' AND ts > ?", (halted_after.strftime("%Y-%m-%dT%H:%M:%S"),)).fetchone()[0]
    assert late == 0 and not (sch.engine.pend == 1).any()


def test_d1_kill_switch_same_semantics_on_the_real_challenger_engine(tmp_path):
    kill = Kill()
    syms = [f"S{k}" for k in range(10)]
    sch = MinuteScheduler(tmp_path / "ch", syms, kill=kill, mode="live", portfolio=challenger_rules(1_000_000), engine_factory=ch_factory())
    t0 = T("09:16")
    for k in range(40):                                          # 30-minute range 99.5-100.5, then all break out to 102
        ts = t0 + pd.Timedelta(minutes=k)
        px = 100.0 if k < 30 else 102.0
        sch.ingest([bar(s, ts, px, vol=500_000, hi=px + 0.5, lo=px - 0.5) for s in syms])
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
        if (sch.engine.pend == 1).any():
            break
    assert (sch.engine.pend == 1).any(), "challenger did not decide a breakout buy"
    kill.engaged = True
    for j in range(k + 1, k + 6):
        ts = t0 + pd.Timedelta(minutes=j)
        sch.ingest([bar(s, ts, 102.0, vol=500_000) for s in syms])
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    assert fills(sch, "BUY") == [] and sch.engine.x.reserved == 0


# ---------------------------------------------------------------------------------------------------- D2 exposure
def test_d2_simultaneous_buys_respect_the_symbol_cap(tmp_path):
    sch = sched(tmp_path, [("AAA", True), ("AAA", True)], {"09:20": {0: "BUY", 1: "BUY"}}, qty={0: 8, 1: 8},
                portfolio=rules(10_000, max_symbol_pct=0.10))
    run(sch, flat(100.0, 8))
    val = sum(f[3] * f[4] for f in fills(sch, "BUY"))
    assert 0 < val <= 1_000


def test_d2_a_price_gap_is_rechecked_at_the_fill(tmp_path):
    sch = sched(tmp_path, [("AAA", True)], {"09:20": {0: "BUY"}}, qty={0: 9}, portfolio=rules(10_000, max_symbol_pct=0.10))
    bars = flat(100.0, 8)
    bars["09:22"] = [bar("AAA", T("09:22"), 120.0, op=120.0), bar("BBB", T("09:22"), 100.0)]     # gap up 20% on the 2.1 fill bar
    run(sch, bars)
    f = fills(sch, "BUY")
    assert f and min(x[4] for x in f) >= 120.0                                                     # the fill really faced the gap
    assert sum(x[3] * x[4] for x in f) <= 1_000 * (1 + 1e-9) and sum(x[3] for x in f) < 9         # cut to what the cap allows


def test_d2_partial_fill_then_retry_counts_what_is_already_held(tmp_path):
    sch = sched(tmp_path, [("AAA", True), ("AAA", True)], {"09:20": {0: "BUY"}, "09:30": {1: "BUY"}}, qty={0: 8, 1: 8},
                portfolio=rules(10_000, max_symbol_pct=0.10))
    bars = flat(100.0, 20)
    for hm in ("09:22", "09:23", "09:24", "09:25", "09:26"):                                       # thin: 5 then nothing (2.1: first fill bar 09:22)
        bars[hm] = [bar("AAA", T(hm), 100.0, vol=50 if hm == "09:22" else 0), bar("BBB", T(hm), 100.0)]
    run(sch, bars)
    held = sum(f[3] * f[4] for f in fills(sch, "BUY"))
    assert held <= 1_000
    st = sch.store.db.execute("SELECT status, qty FROM paper_orders WHERE side='BUY' ORDER BY decided_at").fetchall()
    assert tuple(st[0]) == ("partial", 5)


# ---------------------------------------------------------------------------------------------------- D3 cash
def test_d3_delivery_buy_never_takes_cash_below_zero_after_charges(tmp_path):
    sch = sched(tmp_path, [("AAA", False)], {"09:20": {0: "BUY"}}, portfolio=rules(10_000, per_trade_pct=0.99, cost_buffer=0.003))
    bars = flat(100.0, 8)
    bars["09:22"] = [bar("AAA", T("09:22"), 101.0, op=101.0), bar("BBB", T("09:22"), 100.0)]     # dearer on the 2.1 fill bar
    run(sch, bars)
    x, db = sch.engine.x, sch.store.db
    f = fills(sch, "BUY")
    assert f, "the buy should still fill (smaller)"
    assert min(q[4] for q in f) >= 101.0                                                           # at the higher price
    spent = sum(round(q * p * 100) + ch for _, _, _, q, p, ch in f)
    assert x.cash >= 0 and x.cash == 10_000 * 100 - spent and x.reserved == 0


def test_d3_reservation_covers_dated_charges_to_the_paisa(tmp_path):
    from nseql.instruments.models import Instrument, Kind
    from nseql.instruments.rules import ChargeSchedule
    sch = sched(tmp_path, [("AAA", False)], {"09:20": {0: "BUY"}}, portfolio=rules(10_000, per_trade_pct=0.99, cost_buffer=0.0))
    run(sch, flat(100.0, 5))
    x = sch.engine.x
    q = int(sch.engine.pend_qty[0]) if hasattr(sch.engine, "pend_qty") else int(x.pend_req[0])
    assert q > 0
    px = 100.0 * (1 + ExecConfig().half_spread_bps / 1e4) + 0.05
    worst = ChargeSchedule(allow_extrapolation=True).compute(Instrument("AAA", Kind.EQ), 1, q, round(px * 100), "CNC", pd.Timestamp(DAY).date())
    assert x.reserved >= q * round(px * 100) + worst.total - 1 and x.reserved <= x.cash


# ---------------------------------------------------------------------------------------------------- D4 forward data
def _session(n=60):
    return minute_stream(8, [DAY], seed=11)[:n]


def test_d4_whole_session_delivered_late_creates_no_live_orders(tmp_path):
    sch = MinuteScheduler(tmp_path / "late", symbols(8), mode="live")
    for ts, bars in _session(375):
        sch.ingest(bars)
    sch.run_cycle(pd.Timestamp(f"{DAY}T15:31:00"))
    db = sch.store.db
    assert db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0
    modes = dict(db.execute("SELECT mode, COUNT(*) FROM cycles GROUP BY mode").fetchall())
    assert modes.get("catch-up", 0) >= 370                                     # indicators rebuilt, decisions not traded


def test_d4_normal_latency_trades_and_records_the_timeline(tmp_path):
    sch = MinuteScheduler(tmp_path / "ok", symbols(8), mode="live")
    for ts, bars in _session(120):
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    db = sch.store.db
    assert db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] > 0
    row = db.execute("SELECT minute, decided_at, arrived_at, earliest_fill FROM cycles WHERE status='processed' ORDER BY minute LIMIT 1").fetchone()
    assert row[1] == pd.Timestamp(row[0]) + pd.Timedelta(seconds=6) or str(row[1]).endswith(":06")
    assert row[2] is not None and row[3] > row[0]
    bad = db.execute("SELECT COUNT(*) FROM paper_fills f JOIN paper_orders o ON o.order_key=f.order_key WHERE f.ts <= o.decided_at").fetchone()[0]
    assert bad == 0                                                            # no fill at or before its decision bar


def test_d4_outage_minutes_are_catch_up_and_trading_resumes_after(tmp_path):
    sch = MinuteScheduler(tmp_path / "out", symbols(8), mode="live")
    s = _session(150)
    for ts, bars in s[:60]:
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    for ts, bars in s[60:90]:                                                   # outage: 30 minutes arrive at once
        sch.ingest(bars)
    sch.run_cycle(s[89][0] + pd.Timedelta(seconds=6))
    for ts, bars in s[90:]:
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    db = sch.store.db
    cu = db.execute("SELECT MIN(minute), MAX(minute), COUNT(*) FROM cycles WHERE mode='catch-up'").fetchone()
    assert cu[2] == 29 and cu[0] == s[60][0].strftime("%Y-%m-%dT%H:%M:%S")      # the last of the batch was fresh
    new = db.execute("SELECT COUNT(*) FROM paper_orders WHERE decided_at >= ? AND decided_at < ?",
                     (cu[0], cu[1] + "Z")).fetchone()[0]
    assert new == 0


def test_d4_restart_reproduces_the_uninterrupted_ledger(tmp_path):
    s = _session(150)
    a = MinuteScheduler(tmp_path / "a", symbols(8), mode="live")
    for ts, bars in s:
        a.ingest(bars)
        a.run_cycle(ts + pd.Timedelta(seconds=6))
    b = MinuteScheduler(tmp_path / "b", symbols(8), mode="live", checkpoint_every=10_000)
    for ts, bars in s[:70]:
        b.ingest(bars)
        b.run_cycle(ts + pd.Timedelta(seconds=6))
    b.checkpoint()
    for ts, bars in s[70:80]:                                                   # processed, then a crash before the checkpoint
        b.ingest(bars)
        b.run_cycle(ts + pd.Timedelta(seconds=6))
    b.store.db.close()
    b2 = MinuteScheduler(tmp_path / "b", symbols(8), mode="live")
    b2.run_cycle(s[79][0] + pd.Timedelta(minutes=20))                           # restart much later: replays 70-79 from the log
    for ts, bars in s[80:]:
        b2.ingest(bars)
        b2.run_cycle(ts + pd.Timedelta(seconds=6))
    q = "SELECT fill_key, qty, price, charges_paise FROM paper_fills ORDER BY fill_key"
    assert a.store.db.execute(q).fetchall() == b2.store.db.execute(q).fetchall()


def test_d4_partly_stale_watchlist_trades_only_fresh_symbols(tmp_path):
    syms = symbols(8)
    sch = MinuteScheduler(tmp_path / "st", syms, mode="live")
    s = _session(120)
    stale = syms[0]
    held = []
    for ts, bars in s:
        fresh = [b for b in bars if not (b["symbol"] == stale and ts >= s[60][0])]
        held += [b for b in bars if b["symbol"] == stale and ts >= s[60][0]]
        sch.ingest(fresh)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    sch.ingest(held)                                                            # the stale symbol's bars arrive afterwards
    sch.run_cycle(s[-1][0] + pd.Timedelta(seconds=30))
    db = sch.store.db
    late = db.execute("SELECT COUNT(*) FROM events WHERE symbol=? AND late=1", (stale,)).fetchone()[0]
    assert late == len(held)
    traded = db.execute("SELECT COUNT(*) FROM paper_orders WHERE symbol=? AND decided_at >= ?", (stale, s[60][0].strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()[0]
    assert traded == 0
    cov = db.execute("SELECT MAX(symbols_with_data) FROM cycles WHERE minute >= ? AND status='processed'", (s[60][0].strftime("%Y-%m-%dT%H:%M:%S"),)).fetchone()[0]
    assert cov == len(syms) - 1

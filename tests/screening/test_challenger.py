"""Challenger trader (orb30-breadth-1.0): every frozen rule on hand-built markets."""
import pandas as pd
import pytest
from nseql.screening.challenger import CHALLENGER_CONFIG, CHALLENGER_VERSION, challenger_rules, factory
from nseql.screening.features import SESSION_OPEN_MIN
from nseql.screening.scheduler import MinuteScheduler

DAY = "2026-11-16"
SYMS = ["AAA", "BBB", "CCC", "DDD"]


def ts(day, m):
    return pd.Timestamp(day) + pd.Timedelta(minutes=SESSION_OPEN_MIN + m)


def market(path, days=(DAY,), drop=()):
    """path(sym, m) -> close for minute m (1..375); bars are tight around the close, volume large.
    drop: (sym, m) pairs with no bar."""
    out = []
    for d in days:
        for m in range(1, 376):
            bars = []
            for s in SYMS:
                if (s, m) in drop:
                    continue
                c = path(s, m, d)
                prev = path(s, m - 1, d) if m > 1 else c
                bars.append({"symbol": s, "ts": ts(d, m), "open": prev, "high": max(prev, c) + 0.05, "low": min(prev, c) - 0.05,
                             "close": c, "volume": 1_000_000.0})
            out.append((ts(d, m), bars))
    return out


def run(stream, root, capital=100_000):
    sch = MinuteScheduler(root, SYMS, portfolio=challenger_rules(capital), engine_factory=factory())
    for t, bars in stream:
        sch.ingest(bars)
        sch.run_cycle(t + pd.Timedelta(seconds=6))
    return sch


def orders(sch, side=None):
    q = "SELECT symbol, side, decided_at, fill_ts, qty, price, status FROM paper_orders" + (f" WHERE side='{side}'" if side else "") + " ORDER BY decided_at"
    return [dict(r) for r in sch.store.db.execute(q)]


# AAA: flat at 100 through the opening range (OR 99.95-100.05), breaks out to 101 at m=40, holds.
# Others: just above their session open all day and inside their own opening range (breadth 100%, no breakout).
def others(m, up=True):
    return 50.0 if m == 1 else (50.02 if up else 49.98)


def breakout(s, m, d):
    if s == "AAA":
        return 100.0 if m <= 37 else 101.0
    return others(m)


def test_config_is_versioned_and_frozen():
    assert CHALLENGER_VERSION == "orb30-breadth-1.0" and CHALLENGER_CONFIG.or_minutes == 30
    assert CHALLENGER_CONFIG.min_breadth_coverage == 0.8 and CHALLENGER_CONFIG.risk_per_trade == 0.0025
    with pytest.raises(Exception):
        CHALLENGER_CONFIG.or_minutes = 15                                          # frozen dataclass


def test_entry_on_first_5m_close_above_or_high_fills_next_bar_sized_by_risk(tmp_path):
    sch = run(market(breakout), tmp_path)
    buys = orders(sch, "BUY")
    assert len(buys) == 1 and buys[0]["symbol"] == "AAA"
    b = buys[0]
    assert b["decided_at"] == ts(DAY, 40).isoformat()                               # 5-minute close at 09:56 stamp (m=40)
    assert pd.Timestamp(b["fill_ts"]) > pd.Timestamp(b["decided_at"])               # earliest fill: the next bar
    or_low = 100.0 - 0.05
    planned = b["qty"] * (101.0 - or_low)
    assert 0 < planned <= 0.0025 * 100_000 + 1e-6                                   # planned loss at the stop <= 0.25% of equity
    assert b["qty"] * 101.0 <= 0.10 * 100_000 * 1.001                               # and within the incumbent's per-trade cap


def test_no_entry_when_breadth_fails(tmp_path):
    def weak(s, m, d):                                    # the other three stocks fall below their opens
        return breakout(s, m, d) if s == "AAA" else others(m, up=False)
    assert orders(run(market(weak), tmp_path), "BUY") == []


def test_no_entry_when_breadth_coverage_is_below_the_frozen_minimum(tmp_path):
    drop = {(s, m) for s in ("BBB", "CCC") for m in range(31, 376)}               # only 2 of 4 stocks fresh: 50% < 80%
    assert orders(run(market(breakout, drop=drop), tmp_path), "BUY") == []


def test_incomplete_opening_range_skips_the_symbol_for_the_day(tmp_path):
    assert orders(run(market(breakout, drop={("AAA", 12)}), tmp_path), "BUY") == []


def test_stop_at_or_low_and_a_gap_through_it_fills_at_the_executable_price(tmp_path):
    def gap(s, m, d):
        if s == "AAA":
            return 100.0 if m <= 37 else 101.0 if m < 60 else 95.0               # gaps from 101 to 95, through the 99.95 stop
        return others(m)
    sch = run(market(gap), tmp_path)
    sells = [o for o in orders(sch, "SELL") if o["status"] in ("filled", "partial")]
    assert len(sells) == 1 and sells[0]["price"] < 99.95 and sells[0]["price"] == pytest.approx(95.0, abs=0.1)
    sig = sch.store.db.execute("SELECT reason FROM signals WHERE decision='EXIT'").fetchone()[0]
    assert sig == "stop"


def test_one_entry_per_symbol_per_day_and_no_overnight_holding(tmp_path):
    def chop(s, m, d):                                    # AAA breaks out, is stopped, and breaks out again
        if s == "AAA":
            return 100.0 if m <= 37 else 101.0 if m < 60 else 99.0 if m < 80 else 102.0
        return others(m)
    sch = run(market(chop, days=(DAY, "2026-11-17")), tmp_path)
    buys = [o for o in orders(sch, "BUY")]
    assert [o["decided_at"][:10] for o in buys] == [DAY, "2026-11-17"]            # one per day, not re-entered on day one
    eng = sch.engine
    assert int((eng.pos == 1).sum()) == 0                                           # square-off: nothing held overnight


def test_restart_reproduces_the_challenger_ledger(tmp_path):
    stream = market(breakout, days=(DAY, "2026-11-17"))
    ref = run(stream, tmp_path / "ref")
    a = MinuteScheduler(tmp_path / "crash", SYMS, portfolio=challenger_rules(100_000), engine_factory=factory())
    for t, bars in stream[:420]:
        a.ingest(bars)
        a.run_cycle(t + pd.Timedelta(seconds=6))
    a.checkpoint()
    del a
    b = MinuteScheduler(tmp_path / "crash", SYMS, portfolio=challenger_rules(100_000), engine_factory=factory())
    for t, bars in stream:
        b.ingest(bars)
        b.run_cycle(t + pd.Timedelta(seconds=6))
    q = lambda s: [tuple(r) for r in s.store.db.execute("SELECT * FROM paper_fills ORDER BY 1")]
    assert q(b) == q(ref) and len(q(ref)) > 0

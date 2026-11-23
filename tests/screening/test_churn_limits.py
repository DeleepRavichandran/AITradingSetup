"""Churn limits for the live paper account (rules 1.1): cool-down after an exit, daily entry caps per stock
and per strategy, one position per stock, and the strategy-type cost gate. Learned from 2026-11-23
(708 trades by 12:23 on Rs 1 lakh)."""
from collections import defaultdict

import numpy as np
import pandas as pd
import pytest
from nseql.screening.execution import PortfolioRules
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols

DAYS = ["2026-11-16", "2026-11-17"]
LIVE = PortfolioRules.live(100_000)


@pytest.fixture(scope="module")
def stream():
    return minute_stream(10, DAYS, seed=7)


def _drive(root, stream, rules, upto=None, watch=None):
    sch = MinuteScheduler(root, symbols(10), portfolio=rules)
    for ts, bars in stream[:upto]:
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
        if watch is not None:
            e = sch.engine
            open_sym = np.bincount(e.reg.sym, weights=((e.pos == 1) | (e.pend == 1)).astype(float), minlength=len(e.reg.symbols))
            watch.append(int(open_sym.max()))
    return sch


@pytest.fixture(scope="module")
def limited(stream, tmp_path_factory):
    watch: list[int] = []
    sch = _drive(tmp_path_factory.mktemp("lim"), stream, LIVE, watch=watch)
    return sch, watch


@pytest.fixture(scope="module")
def unlimited(stream, tmp_path_factory):
    return _drive(tmp_path_factory.mktemp("free"), stream, PortfolioRules(capital=100_000))


def _rows(sch, sql):
    return [tuple(r) for r in sch.store.db.execute(sql)]


def test_live_rules_are_versioned_and_off_by_default():
    from nseql.screening.execution import LIVE_RULES_VERSION
    d = PortfolioRules()
    assert (d.cooldown_minutes, d.max_entries_per_symbol_day, d.max_entries_per_config_day, d.max_open_per_symbol, d.group_gate_trades) == (0, 0, 0, 0, 0)
    assert LIVE_RULES_VERSION == "1.1" and LIVE.capital == 100_000 and LIVE.cooldown_minutes == 15 and LIVE.max_open_per_symbol == 1


def test_limits_cut_trading_and_every_limit_holds(limited, unlimited):
    sch, watch = limited
    buys = _rows(sch, "SELECT config_id, symbol, decided_at FROM paper_orders WHERE side='BUY' AND status != 'rejected' ORDER BY decided_at")
    free = _rows(unlimited, "SELECT COUNT(*) FROM paper_orders WHERE side='BUY' AND status != 'rejected'")[0][0]
    assert 0 < len(buys) < free, (len(buys), free)
    per_sym, per_cfg = defaultdict(int), defaultdict(int)
    for cid, sym, dec in buys:
        per_sym[(sym, dec[:10])] += 1
        per_cfg[(cid, dec[:10])] += 1
    assert max(per_sym.values()) <= 4 and max(per_cfg.values()) <= 1
    assert max(watch) <= 1                                                  # one position or working entry per stock
    assert {d[:10] for _, _, d in buys} == set(DAYS)                       # caps reset: day two trades again
    sells = defaultdict(list)
    for sym, ts in _rows(sch, "SELECT symbol, ts FROM paper_fills WHERE side='SELL' ORDER BY ts"):
        sells[sym].append(pd.Timestamp(ts))
    for _, sym, dec in buys:
        d = pd.Timestamp(dec)
        before = [t for t in sells[sym] if t <= d]
        assert not before or (d - before[-1]) >= pd.Timedelta(minutes=15), (sym, dec, before[-1])


def test_refusals_are_counted_with_plain_reasons(limited):
    sch, _ = limited
    reasons = dict(_rows(sch, "SELECT reason, SUM(n) FROM refusals GROUP BY reason"))
    assert any(k.startswith("one at a time") for k in reasons)
    assert any(k.startswith("daily cap") or k.startswith("cool-down") for k in reasons)


def test_limits_survive_a_restart_exactly(stream, limited, tmp_path):
    ref, _ = limited
    a = _drive(tmp_path, stream, LIVE, upto=500)
    a.checkpoint()
    del a
    b = MinuteScheduler(tmp_path, symbols(10), portfolio=LIVE)
    for ts, bars in stream:
        b.ingest(bars)
        b.run_cycle(ts + pd.Timedelta(seconds=6))
    for sql in ("SELECT * FROM paper_fills ORDER BY 1", "SELECT * FROM refusals ORDER BY 1, 2"):
        assert _rows(b, sql) == _rows(ref, sql)


def test_group_gate_refuses_a_losing_strategy_type(stream, tmp_path):
    sch = _drive(tmp_path, stream, PortfolioRules(capital=100_000, group_gate_trades=30), upto=60)
    x = sch.engine.x
    g = x.group
    target = int(g[0])
    for i in np.flatnonzero(g == target)[:30]:
        x.alloc.record_round_trip(int(i), -0.004)                         # 30 losing round trips of one type
    for ts, bars in stream[60:]:
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    gated = _rows(sch, "SELECT SUM(n) FROM refusals WHERE reason LIKE 'strategy type losing%'")[0][0]
    assert gated and gated > 0


def test_what_if_replays_a_stored_day_under_both_rule_sets(stream, tmp_path):
    import json
    from nseql.screening.whatif import what_if
    acct = tmp_path / "acct"
    syms = symbols(10)
    live_from = DAYS[1] + "T09:16:00"
    sch = MinuteScheduler(acct, syms, portfolio=PortfolioRules(capital=100_000), live_from=live_from)
    for ts, bars in stream:
        sch.ingest(bars)
    (acct / "live.json").write_text(json.dumps({"live_from": live_from, "symbols": syms, "capital": 100_000}))
    rep = what_if(acct, DAYS[1], tmp_path / "out", progress=lambda *_: None)
    old, new = rep["runs"].values()
    assert old["trades"] > new["trades"] > 0 and "not evidence" in rep["caution"]
    assert any(k.startswith("one at a time") for k in new["refused_buys"])
    with pytest.raises(ValueError, match="no stored bars"):
        what_if(acct, "2026-12-01", tmp_path / "out2", progress=lambda *_: None)

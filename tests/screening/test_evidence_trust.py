"""Section 2 of the review of 1376b45: is the answer to "which strategy won?" trustworthy?

E1 Daily Review must describe the account's own trades (reconciled by order identity before any headline)
E2 standalone strategy replays: each configuration has its own liquidity allowance (order independence)
E3 forward contest: manifest frozen at inception; a session counts only with predeclared coverage in BOTH accounts
E4 execution assumptions are versioned; old ledgers are kept and flagged for recomputation
"""
import json

import pandas as pd
import pytest

from nseql.screening.scheduler import MinuteScheduler
from tests.screening.scripted import bar, factory, rules

DAY = "2026-11-16"
T = lambda hm: pd.Timestamp(f"{DAY}T{hm}:00")


def _bars(n, thin_at=None, vol=1_000_000):
    out = {}
    for k in range(n):
        t = T("09:16") + pd.Timedelta(minutes=k)
        hm = t.strftime("%H:%M")
        out[hm] = [bar(s, t, 100.0, vol=(1_000 if hm == thin_at else vol)) for s in ("AAA", "BBB")]
    return out


def _run(root, cfgs, script, bars, portfolio):
    sch = MinuteScheduler(root, ["AAA", "BBB"], mode="replay", portfolio=portfolio, engine_factory=factory(cfgs, script, {0: 100, 1: 100}))
    for hm, b in bars.items():
        sch.ingest(b)
        sch.run_cycle(T(hm) + pd.Timedelta(seconds=6))
    return sch


# ---------------------------------------------------------------------------------------------------- E2
def test_e2_standalone_fills_do_not_depend_on_unrelated_configurations(tmp_path):
    """Config 0 alone vs config 0 with config 1 (same stock, decided first): standalone, each has its own 10% of the bar."""
    bars = _bars(8, thin_at="09:21")                                            # 1,000 shares traded: 100 each allowed
    alone = _run(tmp_path / "a", [("AAA", True), ("AAA", True)], {"09:20": {1: "BUY"}}, bars, None)
    both = _run(tmp_path / "b", [("AAA", True), ("AAA", True)], {"09:20": {0: "BUY", 1: "BUY"}}, bars, None)    # config 0 is matched first
    q = lambda s: s.store.db.execute("SELECT qty FROM paper_fills WHERE config_id=? AND ts=?", (str(s.reg.ids[1]), f"{DAY}T09:21:00")).fetchall()
    assert [r[0] for r in q(alone)] == [100] and [r[0] for r in q(both)] == [100]


def test_e2_a_shared_portfolio_still_shares_the_bar(tmp_path):
    bars = _bars(8, thin_at="09:21")
    sch = _run(tmp_path / "p", [("AAA", True), ("BBB", True), ("AAA", True)], {"09:20": {0: "BUY", 2: "BUY"}}, bars,
               rules(1_000_000, per_trade_pct=0.05))
    tot = sch.store.db.execute("SELECT COALESCE(SUM(qty),0) FROM paper_fills WHERE symbol='AAA' AND ts=?", (f"{DAY}T09:21:00",)).fetchone()[0]
    assert tot <= 100                                                           # competing inside one portfolio: one allowance


# ---------------------------------------------------------------------------------------------------- E3
def test_e3_one_candle_is_not_a_completed_session_and_zero_trade_sessions_count(tmp_path):
    from nseql.screening.contest import COVERAGE, session_coverage
    from nseql.screening.synthetic import minute_stream, symbols
    syms = symbols(4)
    days = ["2026-11-16", "2026-11-17", "2026-11-18"]
    stream = minute_stream(4, days, seed=3)
    a = MinuteScheduler(tmp_path / "inc", syms, mode="replay")
    b = MinuteScheduler(tmp_path / "ch", syms, mode="replay")
    for ts, bars in stream:
        if ts.strftime("%Y-%m-%d") == days[2] and ts.strftime("%H:%M") > "09:16":
            continue                                                            # day 3: a single 09:16 candle
        for s in (a, b):
            s.ingest(bars)
            s.run_cycle(ts + pd.Timedelta(seconds=6))
    cov = session_coverage(a, b, days)
    assert [c["complete"] for c in cov] == [True, True, False]
    assert cov[2]["incumbent_minutes"] == 1 and "coverage" in cov[2]["reason"]
    assert COVERAGE["min_share"] >= 0.9


def test_e3_forward_manifest_is_frozen_at_inception_and_changes_are_detected(tmp_path):
    from nseql.screening.contest import freeze_forward, verify_forward
    meta = {"contest_from": "2026-11-25", "start_cash": 97_751.87, "challenger": "orb30-breadth-1.0", "incumbent_account": "x"}
    m = freeze_forward(meta)
    assert m["coverage_rule"]["min_share"] and m["verdict_rules"]["min_sessions"] == 60 and m["verdict_rules"]["min_challenger_trades"] == 100
    ok = verify_forward({**meta, "manifest": m, "manifest_sha256": m["sha256"]})
    assert ok["status"] == "frozen" and ok["matches_current_code"]
    tampered = {**m, "challenger_config": {**m["challenger_config"], "or_minutes": 15}}
    bad = verify_forward({**meta, "manifest": tampered, "manifest_sha256": m["sha256"]})
    assert bad["status"] == "altered"
    late = verify_forward(meta)
    assert late["status"] == "not frozen at inception"


# ---------------------------------------------------------------------------------------------------- E4
def test_e4_execution_version_is_stamped_and_old_ledgers_are_flagged(tmp_path):
    from nseql.screening.execution import EXECUTION_VERSION
    from nseql.screening.ledger_audit import audit
    sch = MinuteScheduler(tmp_path / "new", ["AAA", "BBB"], mode="replay")
    st = json.loads((tmp_path / "new" / "execution.json").read_text())
    assert st["version"] == EXECUTION_VERSION and st["history"][0]["version"] == EXECUTION_VERSION
    old = tmp_path / "old"
    o = MinuteScheduler(old, ["AAA", "BBB"], mode="replay")
    for hm, b in list(_bars(3).items()):
        o.ingest(b)
        o.run_cycle(T(hm) + pd.Timedelta(seconds=6))
    o.store.db.close()
    (old / "execution.json").unlink()                                          # an account created before versioning
    (old / "live.json").write_text("{}")
    rep = audit(old)
    assert rep["ledger_modified"] is False
    x = [f for f in rep["findings"] if f["id"] == "X"]
    assert x and "recompute" in x[0]["limitation"]
    MinuteScheduler(old, ["AAA", "BBB"], mode="replay")                         # reopened by new code: history keeps the gap
    h = json.loads((old / "execution.json").read_text())["history"]
    assert h[0]["version"].startswith("1.") and h[-1]["version"] == EXECUTION_VERSION

"""Incumbent vs challenger contest: identical events, isolated accounts, predeclared measure and verdict, frozen manifest."""
import json

import numpy as np
import pandas as pd
import pytest
from nseql.screening import contest as C
from nseql.screening.scheduler import MinuteScheduler, ReplayMinuteFeed
from nseql.screening.synthetic import minute_stream, symbols


@pytest.fixture(scope="module")
def fixture_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("contest")
    return root, C.run_fixture(root, symbols_n=5, dev_n=1, eval_n=3, seed=3)


def test_both_accounts_receive_identical_events_and_stay_isolated(fixture_run):
    from nseql.screening.store import ScreeningStore
    root, rep = fixture_run
    a = ScreeningStore(root / "evaluation" / "incumbent" / "screening.sqlite").db
    b = ScreeningStore(root / "evaluation" / "challenger" / "screening.sqlite").db
    ev = "SELECT symbol, ts, open, high, low, close, volume FROM events ORDER BY 1, 2"
    assert [tuple(r) for r in a.execute(ev)] == [tuple(r) for r in b.execute(ev)]
    cyc = "SELECT minute FROM cycles ORDER BY 1"
    assert [r[0] for r in a.execute(cyc)] == [r[0] for r in b.execute(cyc)]          # same timestamps, traded or not
    ka = {r[0] for r in a.execute("SELECT config_id FROM paper_orders")}
    kb = {r[0] for r in b.execute("SELECT config_id FROM paper_orders")}
    assert not (ka & kb)                                                            # separate strategies, separate ledgers
    assert rep["incumbent"]["start_equity"] == rep["challenger"]["start_equity"] == rep["capital"]


def test_report_has_every_measure_for_both_accounts_and_cash(fixture_run):
    root, rep = fixture_run
    for k in ("incumbent", "challenger"):
        a = rep[k]
        for f in ("net_pnl_inr", "max_drawdown", "worst_day", "turnover", "charges_inr", "trades_closed", "exposure_avg",
                  "orders_partial", "orders_cancelled_or_rejected", "buys_refused_by_limits", "residual_positions", "daily", "curve"):
            assert f in a, (k, f)
        assert len(a["daily"]) == len(rep["sessions"]) == 3                       # zero-trade days are included
    assert rep["cash"]["net_pnl_inr"] == 0 and rep["kind"] == "synthetic" and "SYNTHETIC" in rep["kind_label"]
    assert rep["verdict"]["status"] == "not a forward verdict"
    d = rep["primary"]["daily_differences"]
    assert d == pytest.approx([c["return"] - i["return"] for c, i in zip(rep["challenger"]["daily"], rep["incumbent"]["daily"])])
    assert rep["challenger"]["residual_positions"] == []                            # intraday by design: nothing left open
    assert (root / "report.json").exists() and (root / "manifest.json").exists()


def test_manifest_is_frozen_before_the_holdout_and_cannot_be_silently_changed(fixture_run, tmp_path):
    root, rep = fixture_run
    m = json.loads((root / "manifest.json").read_text())
    assert rep["manifest_sha256"] == C.sha(m)
    for k in ("code", "incumbent", "challenger", "challenger_config", "execution", "evaluation_days", "development_days", "data", "bootstrap",
              "verdict_rules"):
        assert k in m, k
    assert set(m["development_days"]).isdisjoint(m["evaluation_days"]) and max(m["development_days"]) < min(m["evaluation_days"])
    C.freeze(tmp_path / "m.json", capital=1.0, evaluation_days=["2026-01-06"])
    with pytest.raises(RuntimeError, match="different manifest"):
        C.freeze(tmp_path / "m.json", capital=1.0, evaluation_days=["2026-01-07"])


def test_block_bootstrap_is_deterministic_and_brackets_the_mean():
    rng = np.random.default_rng(1)
    d = rng.normal(0.001, 0.01, 80)
    a, b = C.block_bootstrap(d), C.block_bootstrap(d)
    assert a == b and a["low"] < a["mean"] < a["high"] and a["n"] == 80
    pos = C.block_bootstrap(np.full(70, 0.002) + rng.normal(0, 1e-4, 70))
    assert pos["low"] > 0


def _acct(pnl, dd, trades):
    return {"net_pnl_inr": pnl, "max_drawdown": dd, "trades_closed": trades}


@pytest.mark.parametrize("sessions,ch,ci,expected", [
    (59, _acct(5000, -0.02, 150), {"low": 0.001, "high": 0.003}, "inconclusive"),          # too few sessions
    (60, _acct(5000, -0.02, 99), {"low": 0.001, "high": 0.003}, "inconclusive"),           # too few trades
    (60, _acct(5000, -0.02, 100), {"low": 0.001, "high": 0.003}, "challenger ahead"),
    (60, _acct(-10, -0.02, 100), {"low": 0.001, "high": 0.003}, "inconclusive"),           # interval > 0 but net P&L negative
    (60, _acct(5000, -0.08, 100), {"low": 0.001, "high": 0.003}, "inconclusive"),          # drawdown worse than incumbent's
    (60, _acct(-5000, -0.02, 100), {"low": -0.003, "high": -0.001}, "incumbent ahead"),
    (60, _acct(100, -0.02, 100), {"low": -0.001, "high": 0.002}, "inconclusive"),
])
def test_predeclared_verdict_rules(sessions, ch, ci, expected):
    inc = _acct(1000, -0.05, 300)
    assert C.verdict("forward", inc, ch, ci, sessions)["status"] == expected
    assert C.verdict("synthetic", inc, ch, ci, sessions)["status"] == "not a forward verdict"
    assert C.verdict("historical", inc, ch, ci, sessions)["status"] == "not a forward verdict"


def test_forward_follower_gets_exactly_the_incumbents_bars(tmp_path):
    from nseql.screening.challenger import challenger_rules, factory
    from nseql.screening.execution import PortfolioRules
    days = ["2026-01-05"]
    stream = minute_stream(4, days, seed=9)
    frames = {}
    for t, bars in stream:
        for b in bars:
            frames.setdefault(b["symbol"], []).append(b)
    frames = {s: pd.DataFrame(v) for s, v in frames.items()}
    inc = MinuteScheduler(tmp_path / "inc", symbols(4), feed=ReplayMinuteFeed(frames, "replay"), portfolio=PortfolioRules.live(100_000))
    ch = MinuteScheduler(tmp_path / "ch", symbols(4), portfolio=challenger_rules(100_000), engine_factory=factory())
    inc.followers.append(ch)
    for t, _ in stream:
        inc.run_cycle(t + pd.Timedelta(seconds=6))
    q = "SELECT symbol, ts, close FROM events ORDER BY 1, 2"
    assert [tuple(r) for r in inc.store.db.execute(q)] == [tuple(r) for r in ch.store.db.execute(q)]
    assert ch.watermark == inc.watermark

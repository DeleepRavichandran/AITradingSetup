"""docs/VISION.md phases 0 and 1: benchmarks next to the account, and the opportunity ledger kept out of trading decisions."""
import json
import re
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from nseql.screening.benchmarks import compare, stats
from nseql.screening.execution import PortfolioRules
from nseql.screening.opportunities import research_db, run as opportunities
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols

DAYS = ["2026-11-16", "2026-11-17", "2026-11-18"]


@pytest.fixture(scope="module")
def acct(tmp_path_factory):
    root = tmp_path_factory.mktemp("p01")
    acct = root / "exp" / "screening" / "live_p01"
    syms = symbols(6)
    sch = MinuteScheduler(acct, syms, portfolio=PortfolioRules(capital=100_000), live_from=f"{DAYS[0]}T09:16:00", mode="live")
    sch.verbose = False
    for ts, bars in minute_stream(6, DAYS, seed=21):
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    sch.store.db.commit()
    (acct / "live.json").write_text(json.dumps({"live_from": f"{DAYS[0]}T09:16:00", "symbols": syms, "capital": 100_000, "start_cash": 100_000}))
    return root, acct, sch


def test_benchmarks_sit_next_to_the_account_after_costs(acct):
    root, a, sch = acct
    b = compare(a)
    assert b["sessions"] == DAYS
    s = b["series"]
    assert {"account (after costs)", "cash", "buy and hold watchlist", "trend rule (above 20-day average)"} <= set(s)
    eq_end = sch.store.db.execute("SELECT equity_inr FROM cycles WHERE equity_inr IS NOT NULL ORDER BY minute DESC LIMIT 1").fetchone()[0]
    assert s["account (after costs)"]["return"] == pytest.approx(eq_end / 100_000 - 1, abs=1e-4)
    assert s["cash"]["return"] == 0 and any("not meaningful" in n for n in b["notes"]) and b["plain"].startswith("Over 3 session(s)")


def test_stats_drawdown_and_sharpe():
    r = pd.Series([0.01, -0.02, 0.01, 0.0])
    x = stats(r)
    assert x["max_drawdown"] == pytest.approx(-0.02, abs=1e-9) and x["sessions"] == 4 and x["sharpe"] is not None


def test_opportunity_ledger_measures_taken_refused_and_missed_in_a_separate_store(acct):
    root, a, _ = acct
    r = opportunities(a, DAYS[1], root / "exp")
    decisions = {x["decision"] for x in r["gate_review"]}
    assert "accepted" in decisions and decisions & {"rejected", "deferred"}
    assert all(x["avg_30min_net"] is None or -1 < x["avg_30min_net"] < 1 for x in r["gate_review"])
    db = sqlite3.connect(research_db(root / "exp"))
    assert db.execute("SELECT COUNT(*) FROM signal_outcomes WHERE day=?", (DAYS[1],)).fetchone()[0] == sum(x["setups"] for x in r["gate_review"])
    assert "never read by any trading decision" in r["note"]


def test_no_trading_component_can_read_hindsight():
    """The engine, scheduler, executor, allocator, challenger and features never reference the research store or outcomes."""
    base = Path(__file__).resolve().parents[2] / "services" / "screening"
    for f in ("engine.py", "scheduler.py", "execution.py", "allocator.py", "challenger.py", "features.py"):
        src = (base / f).read_text()
        assert not re.search(r"opportunities|research_db|signal_outcomes|benchmarks", src), f


def test_daily_review_carries_benchmarks_and_the_ledger(acct):
    from nseql.screening.daily_review import run as review
    root, a, _ = acct
    rev = review(a, DAYS[2], root / "exp", steps=("trade_report", "benchmarks", "opportunities"))
    assert rev["benchmarks"]["sessions"] and rev["opportunities"]["gate_review"]
    assert any(h.startswith("Versus benchmarks:") for h in rev["headline"])

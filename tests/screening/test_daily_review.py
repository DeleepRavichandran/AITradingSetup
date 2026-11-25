"""Automatic daily review: runs by itself after the close, one step failing never stops the others, never twice."""
import json
import threading
import time

import pandas as pd
import pytest
from nseql.screening import daily_review as R
from nseql.screening.execution import PortfolioRules
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols

DAY = "2026-11-16"


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    exp = tmp_path_factory.mktemp("exp")
    acct = exp / "screening" / "live_test"
    syms = symbols(5)
    live_from = f"{DAY}T09:16:00"
    sch = MinuteScheduler(acct, syms, portfolio=PortfolioRules(capital=100_000), live_from=live_from)
    sch.verbose = False
    closed = []
    sch.on_session_close = closed.append
    for ts, bars in minute_stream(5, [DAY], seed=13):
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    (acct / "live.json").write_text(json.dumps({"live_from": live_from, "symbols": syms, "capital": 100_000}))
    ch = exp / "screening" / "live_test_challenger"                    # challenger accounts are not reviewed
    ch.mkdir()
    (ch / "live.json").write_text(json.dumps({"challenger": "x"}))
    sch.store.db.commit()
    return exp, acct, closed


def test_close_hook_fires_once_per_session(lab):
    exp, acct, closed = lab
    assert closed == [DAY]
    assert [a.name for a in R.live_accounts(exp)] == ["live_test"]
    assert R.closed_sessions(acct) == [DAY]
    meta = json.loads((acct / "live.json").read_text())
    (acct / "live.json").write_text(json.dumps({**meta, "live_from": f"{DAY}T17:00:00"}))     # started after that close: warm-up only
    assert R.closed_sessions(acct) == []
    (acct / "live.json").write_text(json.dumps(meta))


def test_review_runs_every_step_and_writes_plain_findings(lab):
    exp, acct, _ = lab
    rev = R.run(acct, DAY, exp)
    out = R.review_dir(exp, "live_test", DAY)
    st = json.loads((out / "status.json").read_text())
    assert st["state"] == "done" and all(v["state"] == "done" for v in st["steps"].values())
    assert rev["loss_study"]["reconciliation"]["reconciled"] and rev["day_backtest"]["strategies"] > 0
    assert rev["monte_carlo"]["trades"] == rev["loss_study"]["trades"]
    assert rev["monte_carlo"]["net_pnl_inr"] == pytest.approx(rev["loss_study"]["net_inr"], abs=0.05)
    assert rev["headline"][0].startswith(f"Today: {rev['trade_report']['round_trips']} trades")          # the trade list first
    assert any(h.startswith(f"The bot closed {rev['loss_study']['trades']} round trips") for h in rev["headline"])
    assert rev["trade_report"]["round_trips"] == rev["loss_study"]["trades"]
    assert any("by luck alone" in h for h in rev["headline"]) and "not proof" in rev["caution"]
    assert not [p for p in out.rglob("*") if p.is_dir() and p.parent != out]       # scratch replay ledgers removed
    assert not list(out.rglob("*.sqlite"))
    before = (out / "review.json").read_text()
    assert R.run(acct, DAY, exp) == json.loads(before)                            # finished: not run again


def test_a_failing_step_is_recorded_and_the_others_still_run(lab, monkeypatch):
    exp, acct, _ = lab
    from nseql.screening import loss_study
    monkeypatch.setattr(loss_study, "run", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    rev = R.run(acct, DAY, exp, steps=("loss_study", "monte_carlo"), force=True)
    st = R.status(exp, "live_test", DAY)
    assert st["state"] == "partial" and st["steps"]["loss_study"]["state"] == "failed" and "boom" in st["steps"]["loss_study"]["error"]
    assert st["steps"]["monte_carlo"]["state"] == "done" and rev["monte_carlo"]["trades"] > 0
    assert any(h.startswith("The bot closed") for h in rev["headline"]) and any("Not available: loss study" in h for h in rev["headline"])


def test_queue_waits_for_the_close_runs_one_at_a_time_and_never_twice(tmp_path, lab):
    exp, acct, _ = lab
    busy = threading.Event()
    busy.set()
    ran, active, peak = [], [0], [0]

    def runner(account, day):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        ran.append((account, day))
        active[0] -= 1
        return True

    q = R.ReviewQueue(tmp_path, busy=busy.is_set, runner=runner, log=lambda m: None, poll_s=0.02)
    assert q.submit("a", "2026-11-16") == "queued" and q.submit("a", "2026-11-16") == "queued"
    q.submit("a", "2026-11-17")
    time.sleep(0.15)
    assert ran == []                                         # market session on: nothing runs
    busy.clear()
    for _ in range(100):
        if len(ran) == 2:
            break
        time.sleep(0.02)
    assert ran == [("a", "2026-11-16"), ("a", "2026-11-17")] and peak[0] == 1
    # catch-up queues closed sessions without a finished review; a finished one is skipped
    q2 = R.ReviewQueue(exp, busy=lambda: True, runner=runner, log=lambda m: None)
    assert q2.catch_up() == []                               # DAY already reviewed (partial counts as reviewed)

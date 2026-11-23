"""Trade journal (market state at decision time for every fill) and the per-day root-cause report."""
import pandas as pd
import pytest
from nseql.screening.execution import PortfolioRules
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols
from nseql.screening.trade_report import report

DAY = "2026-11-16"


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    root = tmp_path_factory.mktemp("j")
    sch = MinuteScheduler(root, symbols(8), portfolio=PortfolioRules(capital=100_000))
    calls = []
    sch.news_count = lambda s, t: calls.append((s, t)) or 2
    for ts, bars in minute_stream(8, [DAY], seed=11):
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    return sch, root, calls


def test_rca_is_written_automatically_at_the_close(run):
    sch, _, _ = run
    assert sch.store.db.execute("SELECT COUNT(*) FROM trade_rca").fetchone()[0] > 0


def test_every_fill_has_a_journal_row_with_point_in_time_context(run):
    sch, _, calls = run
    db = sch.store.db
    fills = db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
    j = pd.read_sql_query("SELECT * FROM journal", db)
    assert fills > 0 and len(j) == fills
    assert set(j.regime) <= {"calm", "normal", "volatile", "unknown"} and (j.news_60m == 2).all() and len(calls) == fills
    assert j.breadth.dropna().between(0, 1).all() and j.minute_of_session.between(1, 375).all()


def test_context_is_taken_before_the_fill_minute_bar(run, tmp_path):
    """Changing the close of a fill minute's own bars must not change that fill's journal context (no look-ahead).
    Fills use the open and volume, so the same fills happen in both runs."""
    sch, _, _ = run
    m = sch.store.db.execute("SELECT ts FROM journal GROUP BY ts ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    ref = pd.read_sql_query("SELECT * FROM journal WHERE ts = ? ORDER BY fill_key", sch.store.db, params=(m,))
    b = MinuteScheduler(tmp_path, symbols(8), portfolio=PortfolioRules(capital=100_000))
    b.news_count = lambda s, t: 2
    for ts, bars in minute_stream(8, [DAY], seed=11):
        if ts == pd.Timestamp(m):
            bars = [{**x, "close": x["close"] * 1.05, "high": max(x["high"], x["close"] * 1.05)} for x in bars]
        b.ingest(bars)
        b.run_cycle(ts + pd.Timedelta(seconds=6))
        if ts == pd.Timestamp(m):
            break
    got = pd.read_sql_query("SELECT * FROM journal WHERE ts = ? ORDER BY fill_key", b.store.db, params=(m,))
    assert len(ref) > 0
    pd.testing.assert_frame_equal(got, ref)


def test_trade_report_groups_every_round_trip(run):
    sch, root, _ = run
    rep = report(root, DAY)
    n = rep["totals"]["round_trips"]
    assert n > 0 and sum(g["trades"] for g in rep["by"]["exit_reason"]) == n
    assert {g["group"] for g in rep["by"]["exit_reason"]} <= {"rule", "stop", "square-off", "unknown"}
    sells = sch.store.db.execute("SELECT COALESCE(SUM(realized_paise),0) FROM paper_orders WHERE side='SELL' AND status IN ('filled','partial')").fetchone()[0]
    assert rep["totals"]["net_inr"] == pytest.approx(sells / 100, abs=0.05)
    assert rep["findings"] and "not proven" in rep["caution"]
    with pytest.raises(ValueError):
        report(root, "2026-12-01")


def test_every_round_trip_gets_a_root_cause_won_or_lost(run):
    from nseql.screening.rca import analyse, classify, summary
    sch, root, _ = run
    with sch.store._lock:
        rc = analyse(sch.store.db, DAY)
    n = report(root, DAY)["totals"]["round_trips"]
    assert len(rc) == n and all(t["primary_cause"] and t["summary"] for t in rc)
    assert {t["outcome"] for t in rc} <= {"won", "lost"} and all((t["outcome"] == "won") == (t["net_inr"] > 0) for t in rc)
    stored = sch.store.db.execute("SELECT COUNT(*) FROM trade_rca WHERE day = ?", (DAY,)).fetchone()[0]
    assert stored == n
    sm = summary(rc)
    assert sm["won"] + sm["lost"] == n and sum(g["trades"] for g in sm["by_cause"]) == n
    base = {"net_inr": -5.0, "gross_inr": -2.0, "charges_inr": 3.0, "exit_reason": "stop", "hold_min": 3.0, "stop_pct": 0.005,
            "entry_px": 100.0, "qty": 10, "best_move": 0.001, "worst_move": -0.006, "recovered_after_exit": 1, "regime": "volatile", "trend": -0.01}
    o, cause, contrib, _ = classify(base)
    assert o == "lost" and cause.startswith("stopped out by noise") and any("tight 0.5% stop" in c for c in contrib)
    assert classify({**base, "gross_inr": 1.0})[1] == "charges were larger than the gain"
    assert classify({**base, "net_inr": 4.0, "gross_inr": 7.0})[0] == "won"


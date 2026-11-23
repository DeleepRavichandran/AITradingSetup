"""Headline numbers: one definition each, with its window and denominator (review 2026-11-24)."""
import json

import pandas as pd
import pytest
from nseql.screening.execution import PortfolioRules
from nseql.screening.metrics import account_figures, reason_category, session_decisions
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.store import ScreeningStore
from nseql.screening.synthetic import minute_stream, symbols


def test_flat_day_on_a_continued_account_is_not_called_since_start(tmp_path):
    """Reviewed snapshot: original Rs 1,00,000, a continued account starting at Rs 97,751.87, no trade today.
    Today = 0; since start = -2,248.13 (not 0)."""
    sch = MinuteScheduler(tmp_path, symbols(3), portfolio=PortfolioRules.live(97_751.87))
    meta = {"start_cash": 97_751.87, "original_capital": 100_000, "carried_from": {"from_account": "live_angelone_100000_r2"}}
    f = account_figures(sch, meta, "2026-11-24")
    assert f["equity_now"] == pytest.approx(97_751.87) and f["pnl_today"] == 0
    assert f["pnl_since_start"] == pytest.approx(-2_248.13) and f["return_since_start"] == pytest.approx(-0.0224813, abs=1e-6)
    assert f["starting_capital"] == 100_000 and "live_angelone_100000_r2" in f["windows"]["pnl_since_start"]


def test_day_open_is_the_last_equity_before_today(tmp_path):
    sch = MinuteScheduler(tmp_path, symbols(4), portfolio=PortfolioRules(capital=100_000))
    for ts, bars in minute_stream(4, ["2026-11-16", "2026-11-17"], seed=2):
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    prev = sch.store.db.execute("SELECT equity_inr FROM cycles WHERE minute LIKE '2026-11-16%' AND equity_inr IS NOT NULL ORDER BY minute DESC LIMIT 1").fetchone()[0]
    f = account_figures(sch, {}, "2026-11-17")
    assert f["day_open_equity"] == pytest.approx(prev) and f["pnl_today"] == pytest.approx(f["equity_now"] - prev, abs=0.01)
    assert f["pnl_since_start"] == pytest.approx(f["equity_now"] - 100_000, abs=0.01)


@pytest.mark.parametrize("reason,cat", [
    ("warm-up history (before live start)", "warm-up history"), ("kill switch engaged", "kill switch"),
    ("daily loss limit reached", "daily loss limit"), ("not enough free cash", "cash / position / exposure limits"),
    ("max 8 positions", "cash / position / exposure limits"), ("max 8 positions after higher-ranked signals this minute", "higher-ranked signal first"),
    ("cool-down: this stock was sold less than 15 min ago", "churn limits"), ("daily cap: this stock already bought 4x today", "churn limits"),
    ("expected net return after costs below zero over at least 5 forward trades", "losing-strategy gates"),
    ("price above trade size", "size or liquidity"), ("redundant: a higher-ranked configuration already entered this symbol this minute", "higher-ranked signal first"),
    ("", "not recorded")])
def test_recorded_reasons_map_to_one_category(reason, cat):
    assert reason_category(reason) == cat


def test_session_decisions_separate_warm_up_from_eligible_signals(tmp_path):
    """The contest once labelled 4,68,874 buys 'refused by limits' and My Trading blamed safety limits: most were warm-up."""
    st = ScreeningStore(tmp_path / "s.sqlite")
    rows = [("c1", "2026-11-23T09:20:00", "intraday", "AAA", "BUY", 1.0, "rule", "rejected", "warm-up history (before live start)", None),
            ("c2", "2026-11-23T09:21:00", "intraday", "AAA", "BUY", 1.0, "rule", "rejected", "warm-up history (before live start)", None),
            ("c3", "2026-11-23T10:00:00", "intraday", "AAA", "BUY", 1.0, "rule", "accepted", "rank 1/2", 3.0),
            ("c4", "2026-11-23T10:00:00", "intraday", "AAA", "BUY", 1.0, "rule", "deferred", "redundant: a higher-ranked configuration already entered this symbol this minute", 1.0),
            ("c5", "2026-11-23T10:05:00", "intraday", "BBB", "BUY", 1.0, "rule", "rejected", "price above trade size", None),
            ("c6", "2026-11-23T11:55:00", "intraday", "BBB", "BUY", 1.0, "rule", "rejected", "daily loss limit reached", None),
            ("c7", "2026-11-22T10:00:00", "intraday", "BBB", "BUY", 1.0, "rule", "accepted", "rank 1/1", 1.0)]          # another day: excluded
    st.db.executemany("INSERT INTO signals (config_id, bar_ts, horizon, symbol, decision, price, reason, alloc_status, alloc_reason, alloc_score) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    d = session_decisions(st.db, "2026-11-23", "2026-11-23T09:30:00")
    assert d["warm_up"] == 2 and d["eligible"] == 4 and d["accepted"] == 1 and d["deferred"] == 1 and d["rejected"] == 2
    assert d["rejected_by_category"] == {"size or liquidity": 1, "daily loss limit": 1}
    assert all(r["category"] != "warm-up history" for r in d["top_reasons"])

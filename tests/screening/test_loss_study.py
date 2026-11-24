"""Loss study: why trades lost, from the entry context of every round trip."""
import json

import numpy as np
import pandas as pd
import pytest
from nseql.screening import loss_study as L
from nseql.screening.execution import PortfolioRules
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols

DAY = "2026-11-16"


def test_findings_need_enough_trades_and_a_clear_difference():
    rng = np.random.default_rng(1)
    n = 120
    trend = np.where(np.arange(n) % 2 == 0, -0.01, 0.01)
    net = np.where(trend < 0, rng.normal(-30, 5, n), rng.normal(5, 5, n))        # downtrend buys lose clearly
    t = pd.DataFrame({"net_inr": net, "trend": trend, "regime": "normal", "family": "rsi", "timeframe": "1m", "stop": 0.005,
                      "stock_move": 0.0, "breadth": 0.5, "market_move": 0.0, "session_part": 100})
    rep = L.study(t)
    f = rep["findings"]
    assert f and f[0]["bucket"] == "below average (downtrend)" and f[0]["trades"] == 60 and f[0]["ci95"][1] < 0
    assert all(x["bucket"] != "above average (uptrend)" for x in f)
    small = L.study(t.head(10))
    assert small["findings"] == []                                                     # too few trades: no finding


def test_replay_reproduces_the_live_day_and_reports_factors(tmp_path):
    acct = tmp_path / "acct"
    syms = symbols(6)
    live_from = f"{DAY}T09:16:00"
    sch = MinuteScheduler(acct, syms, portfolio=PortfolioRules(capital=100_000), live_from=live_from)
    sch.verbose = False
    for ts, bars in minute_stream(6, [DAY], seed=13):
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    (acct / "live.json").write_text(json.dumps({"live_from": live_from, "symbols": syms, "capital": 100_000}))
    rep = L.run(acct, DAY, tmp_path / "study")
    assert rep["replay_matches_live"] and rep["trades"] > 0
    assert {f["factor"] for f in rep["factors"]} >= {"trend", "breadth", "session_part", "family", "timeframe"}
    assert all(sum(b["trades"] for b in f["buckets"]) == rep["trades"] for f in rep["factors"])
    assert (tmp_path / "study" / "loss_study.json").exists() and "hypotheses" in rep["caution"]
    with pytest.raises(FileExistsError):
        L.run(acct, DAY, tmp_path / "study")

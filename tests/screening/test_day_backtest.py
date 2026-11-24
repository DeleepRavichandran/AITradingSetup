"""Day backtest: every strategy standalone over one stored session, plus the challenger."""
import json

import pandas as pd
import pytest
from nseql.screening.day_backtest import run
from nseql.screening.execution import PortfolioRules
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols

DAYS = ["2026-11-16", "2026-11-17"]


def test_every_strategy_is_scored_on_its_own_for_the_day(tmp_path):
    acct = tmp_path / "acct"
    syms = symbols(4)
    live_from = f"{DAYS[1]}T09:16:00"
    sch = MinuteScheduler(acct, syms, portfolio=PortfolioRules(capital=100_000), live_from=live_from)
    sch.verbose = False
    for ts, bars in minute_stream(4, DAYS, seed=8):
        sch.ingest(bars)
    (acct / "live.json").write_text(json.dumps({"live_from": live_from, "symbols": syms, "capital": 100_000}))
    r = run(acct, DAYS[1], tmp_path / "out")
    assert r["strategies"] == len(sch.reg) and r["traded"] > 0 and 0 <= r["positive"] <= r["traded"]
    assert sum(g["strategies"] for g in r["by_family"]) == r["strategies"]
    assert sum(g["trades"] for g in r["by_timeframe"]) == r["trades"]
    assert r["net_inr"] == pytest.approx(sum(g["net_inr"] for g in r["by_family"]), abs=0.05)
    assert len(r["best_10"]) <= 10 and r["best_10"][0]["net_inr"] >= r["worst_10"][0]["net_inr"]
    assert "challenger" in r and "luck" in r["caution"]
    with pytest.raises(ValueError, match="no stored bars"):
        run(acct, "2026-12-01", tmp_path / "out2")

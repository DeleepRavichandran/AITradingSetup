"""Carrying a live paper account's money and track record into a new rules version (no fresh start)."""
import json

import numpy as np
import pandas as pd
import pytest
from nseql.screening.carry import CarryError, apply_evidence, carry_state, find_previous, series
from nseql.screening.execution import PortfolioRules
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols

DAY = "2026-11-16"


@pytest.fixture(scope="module")
def prev(tmp_path_factory):
    base = tmp_path_factory.mktemp("screening")
    root = base / "live_angelone_100000_r2"
    sch = MinuteScheduler(root, symbols(8), portfolio=PortfolioRules(capital=100_000))
    for ts, bars in minute_stream(8, [DAY], seed=5):
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    (root / "live.json").write_text(json.dumps({"capital": 100_000, "symbols": symbols(8)}))
    (base / "live_angelone_100000_r2_v11").mkdir()                       # a newer folder that never traded is skipped
    (base / "live_angelone_100000_r2_v11" / "live.json").write_text("{}")
    return base, root, sch


def test_series_and_previous_account(prev):
    base, root, _ = prev
    assert series("live_angelone_100000_r2_v12") == "live_angelone_100000" and series("live_angelone_r2") == "live_angelone"
    assert find_previous(base, "live_angelone_100000_r2_v12") == root
    assert find_previous(base, "live_angelone_r2_v12") is None                                  # another capital series


def test_carries_cash_original_capital_and_evidence(prev):
    _, root, sch = prev
    c = carry_state(root)
    x = sch.engine.x
    assert c["cash_inr"] == pytest.approx(x.cash / 100, abs=0.01) and c["original_capital"] == 100_000
    assert c["round_trips"] == int(x.alloc.ev_n.sum()) > 0                                      # same book the allocator built
    new = MinuteScheduler(root.parent / "new", symbols(8), portfolio=PortfolioRules.live(c["cash_inr"]))
    assert apply_evidence(new, c["evidence"]) == int((x.alloc.ev_n > 0).sum())
    assert np.allclose(new.engine.x.alloc.ev_n, x.alloc.ev_n) and np.allclose(new.engine.x.alloc.ev_sum, x.alloc.ev_sum, atol=1e-9)
    assert new.engine.x.cash == round(c["cash_inr"] * 100)


def test_open_positions_block_the_carry(prev, tmp_path):
    _, root, _ = prev
    import shutil
    import sqlite3
    dst = tmp_path / "held"
    shutil.copytree(root, dst)
    con = sqlite3.connect(dst / "screening.sqlite")
    con.execute("INSERT INTO paper_fills VALUES ('k','o','cfg','AAA','BUY','2026-11-16T10:00:00',5,100.0,0,0)")
    con.commit()
    con.close()
    with pytest.raises(CarryError, match="still holds positions in AAA"):
        carry_state(dst)

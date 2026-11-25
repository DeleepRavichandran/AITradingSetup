"""Opportunity radar, dossier and decision stream (radar.py): recorded decisions only, ranked by state then expected edge."""
import re
from pathlib import Path

import pandas as pd
import pytest

from nseql.screening import radar as R
from nseql.screening.execution import PortfolioRules
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols

DAYS = ["2026-11-16", "2026-11-17"]


@pytest.fixture(scope="module")
def sch(tmp_path_factory):
    syms = symbols(6)
    s = MinuteScheduler(tmp_path_factory.mktemp("rad") / "acct", syms, portfolio=PortfolioRules.live(100_000),
                        live_from=f"{DAYS[0]}T09:16:00", mode="live")
    s.verbose = False
    for ts, bars in minute_stream(6, DAYS, seed=21):
        s.ingest(bars)
        s.run_cycle(ts + pd.Timedelta(seconds=6))
    s.store.db.commit()
    return s


def _pos(s):
    e, r = s.engine, s.reg
    import numpy as np
    return [{"symbol": r.symbols[r.sym[i]], "qty": int(e.qty[i]), "value_inr": float(e.qty[i] * e.fe.last_close[r.sym[i]]),
             "unrealised_inr": 0.0, "entry_price": float(e.entry_px[i]), "stop_price": float(e.stop_px[i]), "rule": "x"} for i in np.flatnonzero(e.pos == 1)]


def test_radar_ranks_by_state_then_edge_and_counts_every_stock(sch):
    now = pd.Timestamp(f"{DAYS[1]}T15:31:00").to_pydatetime()
    v = R.radar(sch, {"live_from": f"{DAYS[0]}T09:16:00"}, _pos(sch), now, kill=False, market_open=False)
    assert v["day"] == DAYS[1] and len(v["rows"]) == len(sch.reg.symbols)
    order = {"HOLD": 0, "TAKEN": 1, "REFUSED": 2, "WATCH": 3}
    states = [order[r["state"]] for r in v["rows"]]
    assert states == sorted(states)
    assert sum(v["counts"].values()) == len(v["rows"])
    assert "market closed" in v["limits"]["blocked"] and v["limits"]["new_buys_possible"] is False
    names = {u["name"] for u in v["limits"]["usage"]}
    assert {"Gross exposure", "Open positions", "Largest stock", "Daily loss", "New trades today"} <= names
    taken = [r for r in v["rows"] if r["state"] in ("HOLD", "TAKEN")]
    assert all(r["buys"] > 0 or r["position"] for r in taken)


def test_kill_switch_blocks_and_dossier_explains(sch):
    now = pd.Timestamp(f"{DAYS[1]}T11:00:00").to_pydatetime()
    v = R.radar(sch, {}, _pos(sch), now, kill=True, market_open=True)
    assert any(b.startswith("kill switch") for b in v["limits"]["blocked"])
    sym = v["rows"][0]["symbol"]
    d = R.dossier(sch, {}, sym, _pos(sch), now, kill=True, market_open=True)
    assert d["gates"][0]["ok"] is False and d["chart"]["read"]
    assert all(s["category"] != "warm-up history" for s in d["signals"])
    with pytest.raises(KeyError):
        R.dossier(sch, {}, "NOPE", [], now, kill=False, market_open=True)


def test_stream_newest_first(sch):
    s = R.stream(sch, {}, 30)
    ts = [e["ts"] for e in s["events"]]
    assert ts == sorted(ts, reverse=True) and {e["kind"] for e in s["events"]} <= {"BUY", "SELL", "REFUSED", "GATE"}


def test_radar_reads_no_outcomes():
    src = (Path(R.__file__)).read_text()
    assert not re.search(r"opportunities\.sqlite|research_db|signal_outcomes|fwd30", src)

"""Scheduled events (results, RBI, Budget, FOMC, F&O expiry): known-from discipline and blackout masks."""
from datetime import date

import numpy as np
import pandas as pd
import pytest
from nseql.data import scheduled as sch
from nseql.data.events import EventStore


@pytest.fixture
def store(tmp_path):
    return EventStore(tmp_path / "e.sqlite")


def test_monthly_expiry_rule_and_holiday_shift():
    assert sch.monthly_expiries(date(2024, 11, 1), date(2024, 11, 30)) == [date(2024, 11, 28)]     # last Thursday
    assert sch.monthly_expiries(date(2025, 10, 1), date(2025, 10, 31)) == [date(2025, 10, 28)]     # last Tuesday after the switch
    shifted = sch.monthly_expiries(date(2024, 11, 1), date(2024, 11, 30), is_session=lambda d: d.weekday() < 5 and d != date(2024, 11, 28))
    assert shifted == [date(2024, 11, 27)]
    assert len(sch.monthly_expiries(date(2024, 1, 1), date(2024, 12, 31))) == 12


def test_csv_needs_known_from_and_symbols_for_results(store, tmp_path):
    p = tmp_path / "ev.csv"
    p.write_text("event_date,kind,known_from,label,symbol\n2026-01-20,results,,Q3 results,TCS\n")
    with pytest.raises(ValueError, match="known_from is required"):
        sch.ingest_schedule_csv(store, p, source="s", licence="l")
    p.write_text("event_date,kind,known_from,label,symbol\n2026-01-20,results,2026-01-05,Q3 results,\n")
    with pytest.raises(ValueError, match="needs a symbol"):
        sch.ingest_schedule_csv(store, p, source="s", licence="l")
    p.write_text("event_date,kind,known_from,label,symbol\n2026-01-20,rbi_policy,2026-02-01,MPC,\n")
    with pytest.raises(ValueError, match="after the event"):
        sch.ingest_schedule_csv(store, p, source="s", licence="l")
    p.write_text("event_date,kind,known_from,label,symbol\n2026-01-20,results,2026-01-05,Q3 results,tcs\n"
                 "2026-02-01,union_budget,2026-01-10,Union Budget,\n2026-02-06,rbi_policy,2025-03-28,MPC decision,\n")
    assert sch.ingest_schedule_csv(store, p, source="exchange filings", licence="public") == 3
    kinds = sorted(r["kind"] for r in sch.scheduled(store))
    assert kinds == ["rbi_policy", "results", "union_budget"]


def test_blackout_covers_the_days_before_the_event_once_it_is_public(store):
    days = pd.bdate_range("2026-01-01", "2026-01-30") + pd.Timedelta(hours=15, minutes=30)
    syms = ["INFY", "TCS"]
    sch.add_event(store, kind="results", event_date=date(2026, 1, 20), known_from=date(2026, 1, 5), label="Q3", symbol="TCS",
                  source="s", licence="l")
    sch.add_event(store, kind="rbi_policy", event_date=date(2026, 1, 14), known_from=date(2026, 1, 12), label="MPC", source="s", licence="l")
    m = sch.blackout(store, days, syms, before=2)
    d = [x.date() for x in days]
    res, macro = m["ctx_no_results"], m["ctx_no_macro_event"]
    off = [d[i] for i in range(len(d)) if res[i, 1] == 0]
    assert off == [date(2026, 1, 16), date(2026, 1, 19)] and res[:, 0].all()           # TCS only; the 2 sessions before
    # the RBI date became public after the close of the 12th, so only the 13th can be blacked out (the 12th could not know)
    assert [d[i] for i in range(len(d)) if macro[i] == 0] == [date(2026, 1, 13)]
    assert m["counts"] == {"results": 1, "macro": 1, "fno_expiry": 0} and m["ctx_no_expiry"].all()


def test_no_store_means_no_blackout():
    days = pd.bdate_range("2026-01-01", periods=10)
    m = sch.blackout(None, days, ["A"])
    assert m["ctx_no_results"].all() and m["ctx_no_macro_event"].all() and m["counts"]["results"] == 0


def test_expiries_are_generated_into_the_store(store):
    assert sch.add_expiries(store, date(2026, 1, 1), date(2026, 3, 31)) == 3
    m = sch.blackout(store, pd.bdate_range("2026-01-01", "2026-03-31"), ["A"])
    assert m["counts"]["fno_expiry"] == 3 and (m["ctx_no_expiry"] == 0).sum() == 6


def test_screener_uses_per_symbol_results_masks(data_dir, tmp_path):
    from pathlib import Path

    from nseql.data.store import Dataset
    from nseql.evaluation import cache as cache_mod
    from nseql.evaluation import space
    from nseql.evaluation.vector import weights
    ds = Dataset(Path(data_dir) / "synthetic_v1")
    raw = cache_mod.raw_panel(ds)
    st = EventStore(tmp_path / "ev.sqlite")
    sym = raw["symbols"][0]
    ev_day = pd.Timestamp(raw["ts"][300]).date()
    sch.add_event(st, kind="results", event_date=ev_day, known_from=pd.Timestamp(raw["ts"][250]).date(), label="r", symbol=sym,
                  source="s", licence="l")
    P = cache_mod.load(cache_mod.build(ds, tmp_path / "c", events=st))
    assert P.meta["context"]["events"]["results"] == 1 and P.meta["context"]["coverage"]["ctx_vix_calm"] == 0.0
    cfg = next(c for c in space.universe(True) if c.regime == "ctx_no_results" and c.family == "trend" and c.universe == "all" and c.vol is None)
    w, w0 = weights(cfg, P), weights(space.universe()[space.twins()[cfg.index]], P)
    assert np.all(w[298:300, 0] == 0)
    others = np.ones(w.shape, dtype=bool)
    others[298:300, 0] = False
    assert np.array_equal(w[others] > 0, w0[others] > 0)

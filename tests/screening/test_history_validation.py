"""Historical validation (history_validation.py): one minute replay of the live rules and the challenger over stored
history, a registered protocol whose holdout only grows forward, contaminated sessions kept out of the holdout,
benchmarks, and nothing filled in. Synthetic data here: this proves the machinery, not an edge."""
import json

import numpy as np
import pandas as pd
import pytest

from nseql.data.store import Dataset, DatasetWriter
from nseql.instruments.models import Instrument, Kind
from nseql.screening import history_validation as H
from nseql.screening import integrity as I
from nseql.screening.synthetic import minute_stream, symbols


def _dataset(data_dir, days, n_sym=4, ds_id="hist_test", seed=5):
    w = DatasetWriter(data_dir, ds_id, description="synthetic minute history for tests", source="nseql synthetic", licence="test",
                      synthetic=True, overwrite=True)
    rows = {}
    for ts, bars in minute_stream(n_sym, days, seed=seed):
        for b in bars:
            rows.setdefault(b["symbol"], []).append({"ts": b["ts"], "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
                                                     "volume": int(b["volume"])})
    for s, r in rows.items():
        w.write_bars(s, "1m", pd.DataFrame(r))
        w.add_instrument(Instrument(s, Kind.EQ))
    return w.finish()


DAYS = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-10-26", periods=14)] + ["2026-11-23", "2026-11-24"]


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    root = tmp_path_factory.mktemp("hv")
    data, exp = root / "data", root / "exp"
    _dataset(data, DAYS)
    rep = H.validate(data, exp, "hist_test", studies=True)
    return data, exp, rep


def test_protocol_segments_and_contaminated_days(run):
    data, exp, rep = run
    p = rep["protocol"]
    seg = rep["segments"]
    counted = DAYS[H.WARMUP:]
    assert sum(len(seg[s]["sessions"]) for s in H.SEGMENTS) == len(counted)
    assert seg["contaminated"]["sessions"] == ["2026-11-23", "2026-11-24"]          # rules were shaped on these: never holdout
    assert all(d < "2026-11-23" for d in seg["holdout"]["sessions"]) and seg["holdout"]["sessions"]
    assert p["development_until"] < p["holdout_from"] and rep["kind"].startswith("SYNTHETIC")


def test_holdout_is_registered_once_and_never_resplit(run, tmp_path):
    data, exp, rep = run
    first = rep["protocol"]
    _dataset(data, DAYS + ["2026-11-25"], ds_id="hist_test")                   # more data arrives
    rep2 = H.validate(data, exp, "hist_test", studies=False, out=tmp_path / "r2")
    assert rep2["protocol"] == first                                           # same cut-off; the holdout only grows
    hist = [e for e in I.Ledger(exp).entries("protocol") if e["subject"].startswith("HIST ")]
    assert len(hist) == 1                                                      # study partitions are separate protocols
    keys = [e["subject"] for e in I.Ledger(exp).entries("protocol")]
    assert len(keys) == len(set(keys))                                         # and each is registered once


def test_every_segment_has_after_cost_results_and_benchmarks(run):
    data, exp, rep = run
    h = rep["segments"]["holdout"]
    for who in ("incumbent", "challenger", "cash", "buy and hold watchlist"):
        assert h[who]["sessions"] == len(h["sessions"])
    assert h["cash"]["total_return"] == 0
    assert set(h["vs"]["incumbent"]) >= {"cash", "buy and hold watchlist"}
    assert all(r in ("ahead (interval above zero)", "behind (interval below zero)", "indistinguishable", None) or r.startswith("too few sessions")
               for r in (v.get("reading") for v in h["vs"]["incumbent"].values()))
    assert len(rep["curves"]["days"]) == len(rep["curves"]["incumbent"]) == len(DAYS) - H.WARMUP


def test_results_reach_ledger_and_knowledge_and_studies_ran(run):
    data, exp, rep = run
    L = I.Ledger(exp)
    assert L.verify()["ok"] and {e["subject"] for e in L.entries("result")} >= {"HIST-incumbent", "HIST-challenger"}
    from nseql.screening.brain import Knowledge
    k = Knowledge(L).current()["HIST-incumbent"]
    assert k["status"] in ("contradicted finding", "experimental finding", "observed fact") and k["status"] != "validated finding"
    assert rep["concepts"]["run"] and rep["features"]["run"]
    assert H.latest(exp)["report"]["run"] == rep["run"] and H.status(exp)["state"] == "done"


def test_brain_variants_and_ablations_are_reported_honestly(run):
    """B, B0 and the three ablations exist in every segment; with fewer than 20 sessions no ablation is read as ahead or
    behind; a B variant with fewer than 30 round trips is flagged as measuring cash, not selection; C and D are not run
    without a holdout-supported addition."""
    data, exp, rep = run
    v = rep["variants"]
    assert set(v["trade_quality"]) == {"incumbent", "challenger", "B uncertainty-aware", "B0 matched control"}
    assert v["rules"]["B0"]["max_entries_day"] >= 1 and v["rules"]["B"] != v["rules"]["B0"]
    # B must be able to trade while evidence accrues (bc-1.0 deadlock: refused every unqualified configuration, so it never
    # traded and could never qualify). Where the incumbent trades, B trades at exploration size.
    assert not v["rules"]["B"]["fund_qualified_only"] and v["rules"]["B"]["explore_fraction"] == 0.25
    if v["trade_quality"]["incumbent"]["round_trips"]:
        assert v["trade_quality"]["B uncertainty-aware"]["round_trips"] > 0
    # B0 matches B's size as well as its count: B explores at a quarter size, so B0's ticket is scaled down with it
    if v["B_size_ratio"] is not None:
        assert 0 < v["B_size_ratio"] < 1
        expect = v["rules"]["B"]["per_trade_pct"] * max(0.05, v["B_size_ratio"])               # the report rounds the ratio to 3 places
        assert abs(v["rules"]["B0"]["per_trade_pct"] - expect) <= 0.0006 * v["rules"]["B"]["per_trade_pct"]
    for name, seg in rep["segments"].items():
        assert set(seg["ablation"]) == {n for n, _, _ in H.ABLATIONS}
        for x in seg["ablation"].values():
            if x.get("sessions", 0) < H.MIN_READING_SESSIONS:
                assert x.get("reading") is None or x["reading"].startswith("too few sessions")
    for k, q in v["trade_quality"].items():
        if k.startswith("B") and (q.get("round_trips") or 0) < I.MIN_TRADES:
            assert any(w.startswith(k) for w in v["warnings"])
    assert v["C_market_context"]["status"] in ("not run", "pending")
    assert all("B uncertainty-aware" in rep["curves"] and "B0 matched control" in rep["curves"] for _ in [0])


def test_coverage_is_reported_not_filled(tmp_path):
    ds = _dataset(tmp_path / "d", DAYS[:6], n_sym=2, ds_id="gaps")
    c = H.coverage(Dataset(tmp_path / "d" / "gaps"))
    assert {r["sessions"] for r in c["symbols"]} == {6} and c["synthetic"] is True

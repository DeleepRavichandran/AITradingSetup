"""Heatmap intelligence (heatmaps.py) and its feature study (feature_study.py): real data only, unavailable data stated,
point in time for playback and features, and a study that rejects noise and finds a planted signal."""
import re
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nseql.screening import feature_study as FS
from nseql.screening import heatmaps as HM
from nseql.screening import integrity as I
from tests.screening.test_integrity import _account

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    exp = tmp_path_factory.mktemp("hm") / "exp"
    return exp, _account(exp / "screening" / "live_h_v14", 21, {"rules_version": "1.4"})


def test_every_heatmap_is_computed_or_honestly_unavailable(lab):
    exp, acc = lab
    for c in HM.catalog():
        h = HM.build(c["id"], acc, exp, positions=[], equity=100_000)
        assert h["availability"] in ("available", "limited", "unavailable"), c["id"]
        if h["availability"] == "unavailable":
            assert h["values"] == [] and h["why"]                                   # nothing drawn, and the reason given
        else:
            assert len(h["values"]) == len(h["rows"]) and all(len(r) == len(h["cols"]) for r in h["values"]), c["id"]
            assert h["source"], c["id"]
    assert {c["id"] for c in HM.catalog() if c["availability"] == "unavailable"} == {"orderbook_liquidity", "order_flow", "options_oi"}


def test_inferred_liquidity_is_labelled_inferred_and_market_map_states_its_proxy(lab):
    exp, acc = lab
    h = HM.build("inferred_liquidity", acc, exp)
    assert h["availability"] == "limited" and any("INFERRED" in x for x in h["limitations"])
    m = HM.build("market_cap", acc, exp)
    assert any("market capitalisation is not stored" in x for x in m["limitations"]) and m["tiles"]


def test_playback_uses_only_data_up_to_the_chosen_time(lab, tmp_path):
    exp, acc = lab
    e2 = tmp_path / "exp"
    shutil.copytree(exp / "screening", e2 / "screening")
    a2 = e2 / "screening" / acc.name
    cut = "2026-11-17T12:00:00"
    before = HM.build("relative_strength", a2, e2, as_of=cut)
    db = sqlite3.connect(a2 / "screening.sqlite")
    db.execute("UPDATE events SET close = close * 2, high = high * 2 WHERE ts > ?", (cut,))
    db.commit()
    db.close()
    after = HM.build("relative_strength", a2, e2, as_of=cut)
    assert before["values"] == after["values"] and before["as_of"].startswith("2026-11-17 12:00")


def test_features_are_point_in_time(lab, tmp_path):
    exp, acc = lab
    e2 = tmp_path / "exp"
    shutil.copytree(exp / "screening", e2 / "screening")
    a2 = e2 / "screening" / acc.name
    p0 = FS.panel(a2)
    cut = pd.Timestamp("2026-11-17T12:00:00")
    db = sqlite3.connect(a2 / "screening.sqlite")
    db.execute("UPDATE events SET close = close * 1.5, volume = volume * 9 WHERE ts > ?", (str(cut).replace(" ", "T"),))
    db.commit()
    db.close()
    p1 = FS.panel(a2)
    cols = [f for f in FS.FEATURES]
    a = p0[p0.time <= cut].set_index(["time", "symbol"])[cols]
    b = p1[p1.time <= cut].set_index(["time", "symbol"])[cols]
    pd.testing.assert_frame_equal(a, b)                                            # features before the cut never change


def _synthetic_panel(seed: int, signal: float) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(30):
        for k, hm in enumerate(FS.TIMES):
            t = pd.Timestamp("2026-01-05") + pd.Timedelta(days=d, hours=int(hm[:2]), minutes=int(hm[3:]))
            for s in range(20):
                x = {f: rng.normal() for f in FS.FEATURES}
                fwd = 0.004 * signal * x["rel_volume"] + 0.003 * x["mom_60"] * 0 + rng.normal(0, 0.004) + 0.002 * signal
                rows.append({"time": t, "day": str(t)[:10], "symbol": f"S{s}", **x, "fwd": fwd})
    return pd.DataFrame(rows)


def test_feature_study_rejects_noise_and_finds_a_planted_signal(tmp_path):
    noise = FS.study(_synthetic_panel(1, 0.0))
    assert not any(r["verdict"] == "candidate" for r in noise)
    planted = {r["feature"]: r for r in FS.study(_synthetic_panel(2, 1.0))}
    assert planted["rel_volume"]["verdict"] == "candidate" and planted["rel_volume"]["mean_ic"] > 0.2
    assert sum(r["verdict"] == "candidate" for r in planted.values()) <= 2            # unrelated features stay rejected


def test_feature_results_reach_the_ledger_and_knowledge(tmp_path):
    FS.run(None, tmp_path, df=_synthetic_panel(3, 0.0))
    L = I.Ledger(tmp_path)
    assert any(e["subject"].startswith("HM-") for e in L.entries("result")) and L.verify()["ok"]
    from nseql.screening.brain import Knowledge
    assert all(k["status"] != "validated finding" for k in Knowledge(L).current().values())
    assert FS.latest(tmp_path)["results"]


def test_trading_never_reads_heatmaps_or_features():
    for f in I.TRADING_SOURCES:
        assert not re.search(r"heatmaps|feature_study", (ROOT / "services" / "screening" / f).read_text()), f
    src = (ROOT / "services" / "screening" / "heatmaps.py").read_text()
    assert not re.search(r"[\"'](bid|ask|depth|delta|open_interest)[\"']\s*:", src)

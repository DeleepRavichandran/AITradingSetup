"""Heatmap intelligence (heatmaps.py) and its feature study (feature_study.py): real data only, unavailable data stated,
point in time for playback and features, and a study that rejects noise and finds a planted signal."""
import json
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
    assert not any(r["verdict"] == "holdout-supported finding" for r in noise)
    planted = {r["feature"]: r for r in FS.study(_synthetic_panel(2, 1.0))}
    assert planted["rel_volume"]["verdict"] == "holdout-supported finding" and planted["rel_volume"]["mean_ic"] > 0.2
    assert sum(r["verdict"] == "holdout-supported finding" for r in planted.values()) <= 2   # unrelated features stay out


def test_feature_results_reach_the_ledger_and_knowledge(tmp_path):
    FS.run(None, tmp_path, df=_synthetic_panel(3, 0.0))
    L = I.Ledger(tmp_path)
    assert any(e["subject"].startswith("HM-") for e in L.entries("result")) and L.verify()["ok"]
    from nseql.screening.brain import Knowledge
    assert all(k["status"] != "validated finding" for k in Knowledge(L).current().values())
    assert any(e["subject"].startswith("features ") for e in L.entries("protocol"))          # the partition was registered
    assert FS.latest(tmp_path)["results"]


def test_trading_never_reads_heatmaps_or_features():
    for f in I.TRADING_SOURCES:
        assert not re.search(r"heatmaps|feature_study", (ROOT / "services" / "screening" / f).read_text()), f
    src = (ROOT / "services" / "screening" / "heatmaps.py").read_text()
    assert not re.search(r"[\"'](bid|ask|depth|delta|open_interest)[\"']\s*:", src)


# ------------------------------------------------------------------ contamination (phase "brain validation")
def _dev_only(seed):
    df = _synthetic_panel(seed, 1.0)
    days = sorted(df.day.unique())
    late = df.day >= days[int(len(days) * 0.6)]
    rng = np.random.default_rng(100 + seed)
    df.loc[late, "fwd"] = rng.normal(0, 0.004, late.sum()) + 0.002                    # no signal at all after development
    return df


def test_a_feature_that_works_only_in_development_fails_independent_evaluation():
    # the old whole-sample test accepted this in 14 of 20 seeds; the staged protocol in none
    for seed in range(5):
        r = {x["feature"]: x for x in FS.study(_dev_only(seed))}["rel_volume"]
        assert r["verdict"] == "rejected" and "holdout" in r["reason"], (seed, r["verdict"], r["reason"])


def test_a_feature_with_no_holdout_cannot_qualify():
    df = _synthetic_panel(2, 1.0)
    part = {"dev_until": "2099-01-01", "embargo": [], "holdout_from": None}          # everything is development
    r = {x["feature"]: x for x in FS.study(df, part)}["rel_volume"]
    assert r["verdict"] == "validation candidate" and r["holdout_days"] == 0


def test_random_walk_success_is_not_accepted_across_seeds():
    hits = sum(any(x["verdict"] == "holdout-supported finding" for x in FS.study(_synthetic_panel(s, 0.0))) for s in range(10, 20))
    assert hits <= 1


def test_repeated_holdout_access_by_tuned_definitions_is_detected(tmp_path):
    from nseql.screening import validation_protocol as V
    df = _synthetic_panel(2, 1.0)
    FS.run(None, tmp_path, df=df)                                                    # first look at the holdout
    orig = FS.HORIZON_MIN
    try:
        FS.HORIZON_MIN = 45                                                          # 'tuned' definitions, same data
        out = FS.run(None, tmp_path, df=df)
    finally:
        FS.HORIZON_MIN = orig
    r = {x["feature"]: x for x in out["results"]}["rel_volume"]
    assert r["verdict"] == "validation candidate" and "already inspected" in r["reason"]
    acc = [e["body"] for e in I.Ledger(tmp_path).entries("holdout_access")]
    assert len(acc) == 2 and acc[-1]["reused_by_other_definitions"] is True


# ------------------------------------------------------------------ playback leakage (phase "brain validation")
T_PLAY = "2026-11-17T11:00:00"


def _snapshots(acc, exp):
    out = {}
    for c in HM.catalog():
        h = HM.build(c["id"], acc, exp, as_of=T_PLAY, positions=[{"symbol": "NOW", "value_inr": 1, "unrealised_inr": 0, "last_price": 1, "stop_price": 1}],
                     equity=1e5, news_items=_NEWS)
        out[c["id"]] = (h.get("rows"), h.get("cols"), h.get("values"), h.get("tiles"), h.get("levels"))
    return out


_NEWS = [{"title": "early", "symbols": ["SYN001"], "first_seen": "2026-11-17T10:00:00"}]


def test_every_playback_view_is_unchanged_by_anything_observable_later(lab, tmp_path):
    exp, acc = lab
    e2 = tmp_path / "exp"
    shutil.copytree(exp / "screening", e2 / "screening")
    a2 = e2 / "screening" / acc.name
    before = _snapshots(a2, e2)
    assert "NOW" not in json.dumps(before["portfolio_risk"])                     # today's positions never leak into the past
    # everything below became observable only AFTER the playback time
    db = sqlite3.connect(a2 / "screening.sqlite")
    db.execute("UPDATE events SET close = close * 1.3, high = high * 1.3, volume = volume * 7 WHERE ts > ?", (T_PLAY,))        # later prices, same day
    db.execute("UPDATE signals SET alloc_score = alloc_score + 900 WHERE bar_ts > ?", (T_PLAY,))                              # later decisions
    db.execute("UPDATE paper_orders SET realized_paise = realized_paise + 777777 WHERE fill_ts > ?", (T_PLAY,))              # later exits
    db.execute("UPDATE paper_fills SET qty = qty * 3 WHERE ts > ?", (T_PLAY,))                                               # overnight / later positions
    db.execute("INSERT INTO event_quarantine VALUES ('SYN001','2026-11-17T12:00:00',1,1,1,1,1,'x',?, 'later')", (pd.Timestamp('2026-11-17T12:00:00').timestamp(),))
    db.commit()
    db.close()
    from nseql.screening import concept_study as CS
    from tests.screening.test_market_structure import walk
    CS.run(None, e2, frames=[("S0", walk(10, 3), None, True)])                       # a research run completed later
    _NEWS.append({"title": "late", "symbols": ["SYN001"], "first_seen": "2026-11-17T14:00:00"})                               # delayed news
    try:
        after = _snapshots(a2, e2)
    finally:
        _NEWS.pop()
    for k in before:
        assert before[k] == after[k], k


def test_live_bars_count_only_once_they_have_arrived(tmp_path):
    from nseql.screening.market_structure import arrival_mode, observable
    from nseql.screening.store import ScreeningStore
    st = ScreeningStore(tmp_path / "s")
    t0 = pd.Timestamp("2026-11-17T10:00:00")
    st.add_events([{"symbol": "A", "ts": t0, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}], processed_upto=None, source="t")
    arrived = t0.tz_localize("Asia/Kolkata").timestamp() + 40                     # the bar arrived 40 s after its candle closed
    st.db.execute("UPDATE events SET received_at=?", (arrived,))
    st.db.commit()
    assert arrival_mode(st.db)
    cond, params = observable(st.db, t0 + pd.Timedelta(seconds=10))
    assert st.db.execute(f"SELECT COUNT(*) FROM events WHERE {cond}", params).fetchone()[0] == 0     # closed, not yet arrived
    cond, params = observable(st.db, t0 + pd.Timedelta(seconds=60))
    assert st.db.execute(f"SELECT COUNT(*) FROM events WHERE {cond}", params).fetchone()[0] == 1

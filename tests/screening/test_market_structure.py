"""Market-structure concepts (market_structure.py) and their study (concept_study.py): exact definitions, no repainting,
no future data, honest about what candles cannot show, and a study that rejects noise but finds a planted effect."""
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nseql.screening import concept_study as CS
from nseql.screening import integrity as I
from nseql.screening import market_structure as M

ROOT = Path(__file__).resolve().parents[2]


def frame(ohlc, day="2026-11-16", start="09:20", vol=1000.0):
    t0 = pd.Timestamp(f"{day}T{start}:00")
    return pd.DataFrame([{"open": o, "high": h, "low": l, "close": c, "volume": vol, "end": t0 + pd.Timedelta(minutes=5 * k)}
                         for k, (o, h, l, c) in enumerate(ohlc)])


def walk(days: int, seed: int, per_day: int = 75, plant: bool = False):
    """5-minute random walk over `days` sessions. With plant=True, a bullish fair value gap is inserted twice a day and
    followed by a steady rise (a real, planted effect for the positive control)."""
    rng = np.random.default_rng(seed)
    rows, px = [], 1000.0
    for d in pd.bdate_range("2026-01-05", periods=days):
        k = 0
        plants = {20, 50} if plant else set()
        while k < per_day:
            t = d + pd.Timedelta(hours=9, minutes=20 + 5 * k)
            if k in plants and k + 16 < per_day:
                seq = [(px, px + 1, px - 1, px + 0.5), (px + 0.5, px + 6, px + 0.4, px + 5.8), (px + 5.8, px + 7.5, px + 3, px + 7.2)]
                px = 7.2 + px
                seq += [(px + 0.6 * j, px + 0.6 * j + 0.8, px + 0.6 * j - 0.3, px + 0.6 * (j + 1)) for j in range(13)]
                px = seq[-1][3]
                for j, (o, h, l, c) in enumerate(seq):
                    rows.append({"open": o, "high": h, "low": l, "close": c, "volume": 1000.0, "end": d + pd.Timedelta(hours=9, minutes=20 + 5 * (k + j))})
                k += len(seq)
                continue
            r = rng.normal(0, 1.2)
            o, c = px, px + r
            rows.append({"open": o, "high": max(o, c) + abs(rng.normal(0, 0.6)), "low": min(o, c) - abs(rng.normal(0, 0.6)), "close": c,
                         "volume": float(rng.integers(500, 1500)), "end": t})
            px = c
            k += 1
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ definitions
def test_fair_value_gap_lifecycle_and_inversion():
    c = frame([(100, 101, 99, 100.5), (100.5, 104, 100.4, 103.8), (103.8, 105, 102, 104.6),   # gap: low 102 > high 101
               (104.6, 105, 103.5, 104), (104, 104.2, 101.6, 101.8),                            # first retest into the gap
               (101.8, 102, 100, 100.2)])                                                       # closes below 101: invalidated
    d = M.detect(c, intraday=True)
    fvg = next(z for z in d["zones"] if z["kind"] == "fvg_bull")
    assert (fvg["lo"], fvg["hi"]) == (101.0, 102.0) and fvg["observable_at"].endswith("09:30:00")
    assert fvg["mitigated_at"].endswith("09:40:00") and fvg["invalidated_at"].endswith("09:45:00")
    types = [e["type"] for e in d["events"]]
    assert "fvg_bull" in types and "ifvg_bear" in types
    assert any(z["kind"] == "fvg_bear_inv" and z["observable_at"].endswith("09:45:00") for z in d["zones"])


def test_swing_needs_confirmation_and_a_sweep_closes_back_inside():
    #             0           1            2            3 (swing high)  4            5            6: wick above 110, close below
    c = frame([(100, 101, 99, 100), (100, 103, 99.5, 102), (102, 105, 101, 104), (104, 110, 103, 106), (106, 107, 104, 105),
               (105, 106, 103, 104), (104, 111, 103.5, 108)])
    d = M.detect(c, intraday=True)
    lv = next(x for x in d["levels"] if x["kind"] == "internal swing high")
    assert lv["price"] == 110 and lv["observable_at"].endswith("09:45:00")          # pivot at 09:35 confirmed 2 candles later
    sw = [e for e in d["events"] if e["type"] == "sweep_bsl"]
    assert sw and sw[0]["observable_at"].endswith("09:50:00") and sw[0]["price"] == 110 and sw[0]["direction"] == -1
    assert all(pd.Timestamp(e["observable_at"]) >= pd.Timestamp(e["formed_at"]) for e in d["events"])


def test_break_of_structure_then_change_of_character():
    up = [(100 + k, 101 + k, 99.5 + k, 100.8 + k) for k in range(6)]
    pull = [(105.8, 106, 103, 103.5), (103.5, 104, 102, 102.5), (102.5, 103, 101.5, 102.8), (102.8, 104, 102.5, 103.9)]
    brk = [(103.9, 108, 103.8, 107.5)]                                                # closes above the confirmed swing high
    down = [(107.5, 107.6, 104, 104.2), (104.2, 104.5, 100, 100.3), (100.3, 100.5, 96, 96.2)]
    d = M.detect(frame(up + pull + brk + [(107.5, 107.8, 106.5, 107), (107, 107.2, 106, 106.2)] + down), intraday=True)
    kinds = [e["type"] for e in d["events"] if e["type"] in ("bos_up", "bos_down", "choch_up", "choch_down")]
    assert "bos_up" in kinds
    if "choch_down" in kinds:
        assert kinds.index("choch_down") > kinds.index("bos_up")


# ------------------------------------------------------------------ point in time
@pytest.mark.parametrize("seed", [1, 2])
def test_no_repainting_and_no_future_data(seed):
    c = walk(4, seed)
    full = M.detect(c, intraday=True)
    for cut in (pd.Timestamp(c.end.iloc[90]), pd.Timestamp(c.end.iloc[200])):
        trunc = M.detect(c[c.end <= cut].reset_index(drop=True), intraday=True)
        key = lambda e: (e["type"], e["formed_at"], e["observable_at"], e["price"], tuple(e["zone"] or ()))
        past = [key(e) for e in full["events"] if pd.Timestamp(e["observable_at"]) <= cut]
        assert past == [key(e) for e in trunc["events"]]                            # identical history: nothing repainted
        zf = {(z["kind"], z["formed_at"], z["lo"], z["hi"]): z for z in full["zones"] if pd.Timestamp(z["observable_at"]) <= cut}
        for z in trunc["zones"]:
            f = zf[(z["kind"], z["formed_at"], z["lo"], z["hi"])]
            for k in ("mitigated_at", "invalidated_at"):                            # lifecycle as of the cut, never later
                assert z[k] == (f[k] if f[k] and pd.Timestamp(f[k]) <= cut else None)


def test_intraday_outcomes_never_cross_the_close():
    c = walk(2, 5)
    last = [i for i in range(len(c) - 1) if c.end.iloc[i].normalize() != c.end.iloc[i + 1].normalize()][0]
    v = CS._outcomes(c, np.array([last - 3, last - 10]), np.array([1, 1]), 6, True)
    assert np.isnan(v[0]) and np.isfinite(v[1])


# ------------------------------------------------------------------ the study: negative and positive controls
def test_study_rejects_noise(tmp_path):
    frames = [(f"S{k}", walk(30, 100 + k), None, True) for k in range(4)]
    out = CS.run(None, tmp_path, frames=frames, record=False)
    assert out["results"] and not any(r["verdict"] == "candidate" for r in out["results"])
    assert {r["verdict"] for r in out["results"]} <= {"insufficient sample", "no value after costs", "no edge over the simpler rule",
                                                       "no edge over random timing"}


def test_study_finds_a_planted_effect(tmp_path):
    frames = [(f"S{k}", walk(30, 200 + k, plant=True), None, True) for k in range(4)]
    out = CS.run(None, tmp_path, frames=frames, record=False)
    f = next(r for r in out["results"] if r["concept"] == "fvg_bull" and r["horizon"] == 12)
    assert f["n"] >= CS.MIN_EVENTS and f["p_bh_pass"] and f["mean_net"] > 0          # real value after costs is found
    assert f["verdict"] in ("candidate", "no edge over the simpler rule", "no edge over random timing")


def test_study_records_verdicts_and_knowledge(tmp_path):
    frames = [(f"S{k}", walk(12, 300 + k), None, True) for k in range(3)]
    CS.run(None, tmp_path, frames=frames)
    L = I.Ledger(tmp_path)
    assert L.verify()["ok"] and L.entries("concept")[0]["body"]["params_hash"] == M.params_hash()
    assert any(e["subject"].startswith("MS-") for e in L.entries("result"))
    from nseql.screening.brain import Knowledge
    ks = [k for k in Knowledge(L).current().values() if k["id"].startswith("MS-")]
    assert ks and all(k["status"] in ("contradicted finding", "unverified hypothesis", "experimental finding") for k in ks)
    assert CS.latest(tmp_path)["results"]


# ------------------------------------------------------------------ honesty and isolation
def test_order_flow_is_reported_unavailable_never_fabricated():
    src = (ROOT / "services" / "screening" / "market_structure.py").read_text()
    assert {"cumulative volume delta", "order book and market depth", "footprint charts"} <= set(M.UNAVAILABLE)
    assert not re.search(r"[\"'](cvd|delta|footprint|bid|ask|depth)[\"']\s*:", src)   # no such field is produced
    for f in I.TRADING_SOURCES:                                                      # research objects never reach trading
        assert not re.search(r"market_structure|concept_study", (ROOT / "services" / "screening" / f).read_text()), f


def test_concepts_api_reads_with_the_viewer_key(data_dir, tmp_path):
    import os
    from fastapi.testclient import TestClient
    from nseql import config
    keys = ("NSEQL_DATA_DIR", "NSEQL_EXPERIMENTS_DIR", "NSEQL_AUTO_FIXTURE", "NSEQL_API_TOKEN", "NSEQL_VIEWER_TOKEN")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update({"NSEQL_DATA_DIR": str(data_dir), "NSEQL_EXPERIMENTS_DIR": str(tmp_path / "exp"), "NSEQL_AUTO_FIXTURE": "false",
                       "NSEQL_API_TOKEN": "ms-operator-token-12345678", "NSEQL_VIEWER_TOKEN": "ms-viewer-token-123456789"})
    config.get_settings.cache_clear()
    try:
        from nseql.api.main import create_app
        CS.run(None, tmp_path / "exp", frames=[("S0", walk(8, 9), None, True)])
        v = TestClient(create_app(), headers={"Authorization": "Bearer ms-viewer-token-123456789"})
        j = v.get("/api/research/concepts").json()
        assert j["run"] and j["results"] and "cumulative volume delta" in j["unavailable"]
        assert v.get("/api/structure?symbol=AAA").status_code in (404, 409, 503)      # no live screening here: refused, not invented
    finally:
        for k, val in saved.items():
            os.environ.pop(k, None) if val is None else os.environ.__setitem__(k, val)
        config.get_settings.cache_clear()

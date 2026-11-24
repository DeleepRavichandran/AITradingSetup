"""Fundamental Analysis Agent: point in time, never invents, and must beat a placebo before it may matter."""
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from nseql.agents import fundamental as F
from nseql.data.events import EventStore
from nseql.data.financials import Financials, FinancialsError
from nseql.data.scheduled import add_event

CSV = """symbol,period_end,published_at,revenue_cr,operating_profit_cr,net_profit_cr,eps,total_debt_cr,equity_cr,source
AAA,2025-09-30,2025-10-20 16:10,1000,200,100,10,500,1000,test
AAA,2026-09-30,2026-10-22 17:05,1200,264,130,13,400,1100,test
"""


def _fin(tmp_path, text=CSV):
    p = tmp_path / "r.csv"
    p.write_text(text)
    f = Financials(tmp_path / "fin.sqlite")
    return f, f.import_csv(p, "test")


def test_results_are_used_only_after_their_filing_time(tmp_path):
    fin, n = _fin(tmp_path)
    assert n == {"added": 2, "updated": 0, "rows": 2}
    before = F.lens_reports(fin, "AAA", pd.Timestamp("2026-10-22 17:00"))
    after = F.lens_reports(fin, "AAA", pd.Timestamp("2026-10-22 17:06"))
    assert before["status"] == "unavailable" or before["evidence"]["quarter"] == "2025-09-30"
    ev = after["evidence"]
    assert ev["quarter"] == "2026-09-30" and ev["revenue_yoy"] == pytest.approx(0.2) and ev["profit_yoy"] == pytest.approx(0.3)
    assert ev["margin_change_pp"] == pytest.approx(2.0) and ev["debt_to_equity"] == pytest.approx(400 / 1100, abs=1e-3)
    assert after["score"] > 0 and "revenue +20% YoY" in after["plain"]


def test_rows_without_a_filing_time_or_filed_before_the_quarter_ended_are_refused(tmp_path):
    with pytest.raises(FinancialsError, match="published_at"):
        _fin(tmp_path, "symbol,period_end,published_at,revenue_cr\nAAA,2026-09-30,,10\n")
    with pytest.raises(FinancialsError, match="before the quarter ended"):
        _fin(tmp_path, "symbol,period_end,published_at,revenue_cr\nAAA,2026-09-30,2026-09-01 10:00,10\n")


def test_news_tone_reads_only_headlines_seen_before_as_of(tmp_path):
    db = sqlite3.connect(":memory:")
    db.executescript("CREATE TABLE headlines (id TEXT, first_seen TEXT, published TEXT, source TEXT, topic TEXT, title TEXT, link TEXT, symbols TEXT, themes TEXT);")
    rows = [("1", "2026-11-20T10:00:00", "AAA profit jumps, beats estimates"), ("2", "2026-11-21T11:00:00", "AAA wins big order"),
            ("3", "2026-11-23T09:00:00", "AAA plunges after fraud probe")]                       # after as_of: must be ignored
    db.executemany("INSERT INTO headlines VALUES (?,?,?,?,?,?,?,?,?)", [(i, t, t, "wire", "", title, "", json.dumps(["AAA"]), "[]") for i, t, title in rows])
    r = F.lens_news(db, "AAA", pd.Timestamp("2026-11-22 15:35"))
    assert r["evidence"]["headlines"] == 2 and r["score"] > 0 and r["evidence"]["negative_words"] == 0
    later = F.lens_news(db, "AAA", pd.Timestamp("2026-11-23 15:35"))
    assert later["evidence"]["negative_words"] >= 2 and later["score"] < r["score"]


def test_events_count_only_once_public_and_in_the_window(tmp_path):
    st = EventStore(tmp_path / "ev.sqlite")
    add_event(st, kind="results", event_date=pd.Timestamp("2026-11-25").date(), known_from=pd.Timestamp("2026-11-10").date(),
              label="AAA Q2 results", source="test", licence="test", symbol="AAA")
    add_event(st, kind="rbi_policy", event_date=pd.Timestamp("2026-11-24").date(), known_from=pd.Timestamp("2026-11-23").date(),
              label="RBI policy", source="test", licence="test")
    r = F.lens_events(st, "AAA", pd.Timestamp("2026-11-23 15:35"))            # RBI known only from the end of 23 Nov
    assert [x["kind"] for x in r["evidence"]["upcoming"]] == ["results"] and r["risk"] == 1.0
    assert F.lens_events(st, "BBB", pd.Timestamp("2026-11-24 15:35"))["evidence"]["upcoming"][0]["kind"] == "rbi_policy"


def _daily(n=300, seed=1, crash_last=False):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2025-09-01", periods=n)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, n)))
    v = rng.integers(9000, 11000, n).astype(float)
    if crash_last:
        c[-1] = c[-2] * 0.92
        v[-1] = 40000
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"ts": days, "open": o, "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * 0.998, "close": c, "volume": v})


def test_panic_day_is_fear_and_future_bars_are_never_read():
    syms = [f"S{k}" for k in range(5)]
    data = {s: _daily(seed=k, crash_last=True) for k, s in enumerate(syms)}
    last = data["S0"].ts.iloc[-1]
    as_of = last + pd.Timedelta(hours=15, minutes=35)
    src = F.Sources(daily=lambda s: data[s])
    d = F.analyse(syms, as_of, src)
    m = d["market"]["emotion"]
    assert m["mood"].startswith("fear") and m["evidence"]["panic_selling_share"] == 1.0
    b = d["stocks"]["S0"]["lenses"]["behaviour"]
    assert any("panic selling" in p["pattern"] for p in b["evidence"]["patterns"])
    # the same session before its close: the crash bar is not complete yet and must not be used
    early = F.analyse(syms, last + pd.Timedelta(hours=11), src)
    assert early["market"]["emotion"]["evidence"]["panic_selling_share"] == 0.0
    # a later bar added to the data changes nothing as of the earlier time
    future = {s: pd.concat([df, df.tail(1).assign(ts=df.ts.iloc[-1] + pd.Timedelta(days=3), close=1.0)]) for s, df in data.items()}
    again = F.analyse(syms, as_of, F.Sources(daily=lambda s: future[s]))
    assert again["market"]["emotion"] == m


def test_missing_sources_are_reported_not_invented():
    d = F.analyse(["AAA"], pd.Timestamp("2026-11-25 15:35"), F.Sources())
    L = d["stocks"]["AAA"]["lenses"]
    assert {k: v["status"] for k, v in L.items() if k != "emotion"} == {"reports": "unavailable", "news": "unavailable", "events": "unavailable",
                                                                       "behaviour": "unavailable"}
    assert d["stocks"]["AAA"]["stance"] == "no view (not enough data)" and "observation only" in d["use"]


def _frames(informative: bool, days=30, per_day=8, seed=3):
    rng = np.random.default_rng(seed)
    snaps, trades = [], []
    D = pd.bdate_range("2026-01-05", periods=days)
    for i, d in enumerate(D):
        for s in range(per_day):
            score = rng.choice([-0.6, 0.6])
            snaps.append({"as_of": f"{d:%Y-%m-%d}T15:35", "symbol": f"S{s}", "view": score, "reports": None, "news": score, "behaviour": None,
                          "emotion": 0.0, "event_risk": 0.0})
            if i + 1 < days:
                edge = (30 if score > 0 else -30) if informative else 0
                trades.append({"symbol": f"S{s}", "entry_ts": D[i + 1] + pd.Timedelta(hours=10), "net_inr": edge + rng.normal(0, 20)})
    return pd.DataFrame(trades), pd.DataFrame(snaps)


def test_placebo_test_finds_a_real_signal_and_rejects_noise():
    rt, S = _frames(True)
    r = F.evaluate_frames(rt, S)
    assert r["lenses"]["news"]["verdict"].startswith("informative") and r["lenses"]["news"]["difference_inr_per_trade"] > 40
    assert r["lenses"]["reports"]["verdict"] == "not enough evidence"              # no data: never a verdict
    rt0, S0 = _frames(False)
    assert F.evaluate_frames(rt0, S0)["lenses"]["news"]["verdict"] == "no usable information"
    few = F.evaluate_frames(rt.head(40), S)
    assert few["lenses"]["news"]["verdict"] == "not enough evidence"


def test_snapshots_round_trip(tmp_path):
    syms = ["S0", "S1", "S2"]
    data = {s: _daily(seed=k) for k, s in enumerate(syms)}
    d = F.analyse(syms, data["S0"].ts.iloc[-1] + pd.Timedelta(hours=15, minutes=35), F.Sources(daily=lambda s: data[s]))
    assert F.save(d, tmp_path / "snap.sqlite") == 3
    db = sqlite3.connect(tmp_path / "snap.sqlite")
    assert db.execute("SELECT COUNT(*), MIN(agent) FROM snapshots").fetchone() == (3, F.AGENT_VERSION)


def test_api_dossier_and_placebo_status(data_dir, tmp_path):
    import os
    from fastapi.testclient import TestClient
    from nseql import config
    keys = ("NSEQL_DATA_DIR", "NSEQL_EXPERIMENTS_DIR", "NSEQL_AUTO_FIXTURE", "NSEQL_API_TOKEN")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update({"NSEQL_DATA_DIR": str(data_dir), "NSEQL_EXPERIMENTS_DIR": str(tmp_path / "exp"), "NSEQL_AUTO_FIXTURE": "false",
                       "NSEQL_API_TOKEN": "fundamental-agent-test-token-123"})
    config.get_settings.cache_clear()
    try:
        from nseql.api.main import create_app
        c = TestClient(create_app(), headers={"Authorization": "Bearer fundamental-agent-test-token-123"})
        assert c.get("/api/fundamental").status_code == 404                     # no live watchlist: said plainly
        d = c.get("/api/fundamental", params={"symbol": "infy"}).json()
        assert list(d["stocks"]) == ["INFY"] and d["stocks"]["INFY"]["lenses"]["reports"]["status"] == "unavailable"
        assert any("no company results imported" in n for n in d["notes"]) and "observation only" in d["use"]
        t = c.get("/api/fundamental/test").json()
        assert t["verdict"] == "not enough evidence" and t["min_trades"] == 100
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        config.get_settings.cache_clear()

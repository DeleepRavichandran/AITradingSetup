"""One-minute screening slice: streaming/batch parity, idempotency, recovery, gaps, kill switch, cadence."""
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nseql.data.store import Dataset
from nseql.risk.limits import KillSwitch
from nseql.screening.engine import LAST_ENTRY_MIN, SQUARE_OFF_MIN, TIE_EPS, ScreeningEngine
from nseql.screening.features import TIMEFRAMES, batch_features, bucket_closes, minute_of_session
from nseql.screening.registry import FAMILIES, FEATURES, TF_NAMES, build
from nseql.screening.scheduler import MinuteScheduler, ReplayMinuteFeed

DAYS = 12


@pytest.fixture(scope="module")
def frames(data_dir):
    """The fixture's 1-minute bars (2 symbols), first DAYS sessions, with some minutes removed from one
    symbol so that illiquid gaps are exercised."""
    ds = Dataset(Path(data_dir) / "synthetic_v1")
    out = {}
    for s in ds.symbols("1m"):
        df = ds.bars(s, "1m")
        days = sorted(df["ts"].dt.normalize().unique())[:DAYS]
        df = df[df["ts"].dt.normalize().isin(days)]
        out[s] = df.reset_index(drop=True)
    s0 = sorted(out)[0]
    rng = np.random.default_rng(3)
    keep = rng.random(len(out[s0])) > 0.06
    keep[out[s0]["ts"].dt.minute.to_numpy() % 15 == 0] &= rng.random((out[s0]["ts"].dt.minute.to_numpy() % 15 == 0).sum()) > 0.3
    out[s0] = out[s0][keep].reset_index(drop=True)
    return out


def _run(frames, root, n=None, upto=None, **kw):
    feed = ReplayMinuteFeed(frames)
    kw.setdefault("execution", "simple")          # the parity reference is defined on whole fills at the next open
    sch = MinuteScheduler(root, sorted(frames), n, feed=feed, **kw)
    for m in feed.minutes()[:upto]:
        sch.run_cycle(m + pd.Timedelta(seconds=6))
    return sch


@pytest.fixture(scope="module")
def full_run(frames, tmp_path_factory):
    return _run(frames, tmp_path_factory.mktemp("full"))


def test_incremental_features_equal_the_batch_reference(frames):
    eng = ScreeningEngine(build(sorted(frames)))
    fe = eng.fe
    ref = {s: batch_features(df) for s, df in frames.items()}
    allm = sorted(set().union(*[set(df["ts"]) for df in frames.values()]))
    by = {s: df.set_index("ts") for s, df in frames.items()}
    checked = 0
    for ts in allm:
        syms = [s for s in sorted(frames) if ts in by[s].index]
        rows = [by[s].loc[ts] for s in syms]
        eng.step(ts, syms, *[np.array([r[k] for r in rows]) for k in ("open", "high", "low", "close", "volume")])
        for tf in TIMEFRAMES:
            if not bucket_closes(tf, minute_of_session(ts)):
                continue
            for s in sorted(frames):
                i = fe.index[s]
                if not fe.updated[tf][i]:
                    assert ts not in ref[s][tf].index
                    continue
                want = ref[s][tf].loc[ts]
                got = np.array([fe.feat[tf][k][i] for k in want.index])
                assert np.allclose(got, want.to_numpy(dtype=float), equal_nan=True, rtol=1e-12, atol=1e-9), (s, tf, ts)
                checked += 1
    assert checked > 3000


def _reference_signals(frames, reg) -> set:
    """Independent per-configuration state machine on the BATCH features (no FeatureEngine, no vectors)."""
    feats = {s: batch_features(df) for s, df in frames.items()}
    bars = {s: df.set_index("ts") for s, df in frames.items()}
    allm = sorted(set().union(*[set(df["ts"]) for df in frames.values()]))
    out = set()
    for i in range(len(reg)):
        d = reg.describe(i)
        s, tf, fam, p = d["symbol"], d["timeframe"], d["family"], d["params"]
        F, B = feats[s][tf], bars[s]
        intraday = d["horizon"] == "intraday"
        pos = pend = 0
        stop = 0.0
        for ts in allm:
            has = ts in B.index
            m = minute_of_session(ts)
            if pend and has:
                o = float(B.at[ts, "open"])
                if pend > 0:
                    if int(100_000 // o) > 0:
                        pos, stop = 1, o * (1 - d["stop_pct"])
                else:
                    pos = 0
                pend = 0
            rule = ts in F.index
            open_pos = pos == 1 and has
            stop_hit = open_pos and float(B.at[ts, "low"]) <= stop
            sq = open_pos and intraday and m >= SQUARE_OFF_MIN
            if pend or not (rule or stop_hit or sq):
                continue
            dec = None
            if rule:
                f = F.loc[ts]
                gt = lambda a, b: bool(a > b + TIE_EPS * max(1.0, abs(b))) if not (np.isnan(a) or np.isnan(b)) else False
                if fam == "cross":
                    enter, ex = gt(f[f"sma{p['fast']}"], f[f"sma{p['slow']}"]), gt(f[f"sma{p['slow']}"], f[f"sma{p['fast']}"])
                elif fam == "breakout":
                    enter, ex = gt(f["close"], f[f"hh{p['entry']}"]), gt(f[f"ll{p['exit']}"], f["close"])
                elif fam == "rsi":
                    enter, ex = gt(float(p["buy"]), f[f"rsi{p['n']}"]), gt(f[f"rsi{p['n']}"], float(p["sell"]))
                elif fam == "zscore":
                    enter, ex = gt(float(p["z_entry"]), f["z20"]), gt(f["z20"], float(p["z_exit"]))
                elif fam == "bollinger":
                    enter, ex = gt(f["close"], f["bbu20"]), gt(f["sma20"], f["close"])
                elif fam == "macd":
                    m_, s_ = f[f"macd_{p['fast']}_{p['slow']}"], f[f"macds_{p['fast']}_{p['slow']}"]
                    enter, ex = gt(m_, s_), gt(s_, m_)
                elif fam == "momentum":
                    enter, ex = gt(f[f"roc{p['n']}"], float(p["min_return"])), gt(0.0, f[f"roc{p['n']}"])
                elif fam == "volume":
                    enter, ex = gt(f[f"vbo{p['n']}"], 0.5), gt(f["ll10"], f["close"])
                elif fam == "vwap":
                    enter, ex = gt(float(p["below_vwap"]), f["vwapdev"]), gt(f["vwapdev"], 0.0)
                elif fam == "orb":
                    enter, ex = gt(f["orb_up"], 0.0), gt(0.0, f["orb_mid"])
                elif fam == "gap" and p["kind"] == "gap-and-go":
                    enter, ex = gt(f["gapgo"], float(p["min_gap"])), gt(0.0, f["dayret"])
                elif fam == "gap":
                    enter, ex = gt(float(p["max_gap"]), f["gapdn"]), gt(f["gapfill"], 0.0)
                else:
                    assert fam == "prevday", fam
                    enter, ex = gt(f["pdh_dev"], float(p["above"])), gt(float(p["exit_below"]), f["pdh_dev"])
                if pos == 0:
                    dec = "BUY" if enter and not (intraday and m > LAST_ENTRY_MIN) else None
                else:
                    dec = "EXIT" if ex else None
            if stop_hit or sq:
                dec = "EXIT"
            if dec:
                out.add((d["id"], ts.strftime("%Y-%m-%dT%H:%M:%S"), dec))
                pend = 1 if dec == "BUY" else -1
    return out


def test_streaming_decisions_equal_an_independent_batch_reference(frames, full_run):
    got = {(r[0], r[1], r[2]) for r in full_run.store.db.execute("SELECT config_id, bar_ts, decision FROM signals")}
    want = _reference_signals(frames, full_run.reg)
    assert len(want) > 500 and got == want
    hz = dict(full_run.store.db.execute("SELECT horizon, COUNT(*) FROM signals GROUP BY horizon").fetchall())
    assert hz.get("intraday", 0) > 0 and hz.get("swing", 0) > 0


def test_intraday_positions_are_flat_after_the_close_and_swing_positions_carry(full_run):
    eng, reg = full_run.engine, full_run.reg
    assert not np.any((eng.pos == 1) & reg.intraday)
    rows = full_run.store.db.execute("SELECT product, COUNT(*) FROM paper_orders WHERE status='filled' GROUP BY product").fetchall()
    assert dict(rows).keys() == {"MIS", "CNC"}
    sq = full_run.store.db.execute("SELECT COUNT(*) FROM signals WHERE reason='square-off'").fetchone()[0]
    assert sq > 0
    first_buy = full_run.store.db.execute("SELECT MIN(bar_ts) FROM signals WHERE horizon='intraday' AND decision='BUY' AND substr(bar_ts,12) > '15:00:00'").fetchone()[0]
    assert first_buy is None                                       # no intraday entries after 15:00


def test_paper_ledger_is_consistent(full_run):
    db = full_run.store.db
    rows = db.execute("SELECT config_id, side, decided_at, fill_ts, qty, price, charges_paise, realized_paise FROM paper_orders "
                      "WHERE status='filled' ORDER BY config_id, decided_at").fetchall()
    assert rows and all(r["fill_ts"] > r["decided_at"] for r in rows)      # filled strictly after the decision
    assert all(r["charges_paise"] > 0 for r in rows)
    open_buy = {}
    for r in rows:
        if r["side"] == "BUY":
            open_buy[r["config_id"]] = r
        else:
            b = open_buy.pop(r["config_id"])
            gross = (round(r["price"] * 100) - round(b["price"] * 100)) * b["qty"]
            assert r["realized_paise"] == gross - b["charges_paise"] - r["charges_paise"] and r["qty"] == b["qty"]


def test_the_same_event_twice_never_duplicates_anything(frames, tmp_path):
    sch = _run(frames, tmp_path, upto=400)
    db = sch.store.db
    snap = lambda: (sch.store.counts(), sch.engine.evals.copy(), db.execute("SELECT COUNT(*), MAX(minute) FROM cycles").fetchone()[:])
    before = snap()
    last = pd.Timestamp(sch.watermark)
    # the feed delivers the same bars again, the scheduler runs again for the same minute, twice
    again = [dict(symbol=s, ts=t, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume)
             for s, df in frames.items() for t, r in df.set_index("ts").loc[:last].tail(3).iterrows()]
    got = sch.ingest(again)
    assert got["accepted"] == 0 and got["duplicate"] == len(again)
    sch.run_cycle(last + pd.Timedelta(seconds=6))
    sch.run_cycle(last + pd.Timedelta(seconds=6))
    after = snap()
    assert before[0] == after[0] and np.array_equal(before[1], after[1]) and before[2] == after[2]
    # the engine itself refuses an old minute
    res = sch.engine.step(last, [], *[np.array([])] * 5)
    assert res.status == "duplicate" and np.array_equal(sch.engine.evals, before[1])


def test_conflicting_and_late_events_are_quarantined_not_applied(frames, tmp_path):
    sch = _run(frames, tmp_path, upto=50)
    wm = pd.Timestamp(sch.watermark)
    s = sorted(frames)[1]
    old = frames[s].set_index("ts").loc[wm]
    out = sch.ingest([dict(symbol=s, ts=wm, open=old.open, high=old.high * 1.1, low=old.low, close=old.close, volume=old.volume)])
    assert out["conflict"] == 1
    out = sch.ingest([dict(symbol="NEWSYM", ts=wm - pd.Timedelta(minutes=5), open=1, high=1, low=1, close=1, volume=1)])
    assert out["late"] == 1
    c = sch.store.counts()
    assert c["event_conflicts"] == 1 and c["late_events"] == 1


def _snapshot(sch):
    db = sch.store.db
    q = lambda s: [tuple(r) for r in db.execute(s)]
    return (q("SELECT config_id, bar_ts, decision, price, reason FROM signals ORDER BY 1, 2"),
            q("SELECT order_key, status, fill_ts, qty, price, charges_paise, realized_paise FROM paper_orders ORDER BY 1"),
            q("SELECT minute, status, eligible, evaluated, counts FROM cycles ORDER BY 1"),
            sch.engine.evals.tolist(), sch.engine.pos.tolist(), sch.engine.qty.tolist())


def test_crash_and_restart_reproduce_the_uninterrupted_run(frames, full_run, tmp_path):
    feed_minutes = ReplayMinuteFeed(frames).minutes()
    crash_at = 1234                                  # not a multiple of the checkpoint interval: work after it is lost
    a = _run(frames, tmp_path, upto=crash_at, checkpoint_every=15)
    assert a.ckpt.exists() and pd.Timestamp(a.watermark) == feed_minutes[crash_at - 1]
    del a                                            # crash: no final checkpoint
    b = MinuteScheduler(tmp_path, sorted(frames), feed=ReplayMinuteFeed(frames), execution="simple")
    assert b.recovery["checkpoint"] is not None and pd.Timestamp(b.recovery["checkpoint"]) < feed_minutes[crash_at - 1]
    for m in feed_minutes:                           # the feed restarts from the beginning: everything old is a duplicate
        b.run_cycle(m + pd.Timedelta(seconds=6))
    assert _snapshot(b) == _snapshot(full_run)


def test_missing_minutes_are_recorded_as_gaps(frames, tmp_path):
    cut = {s: df[(df["ts"].dt.strftime("%H:%M") < "11:00") | (df["ts"].dt.strftime("%H:%M") > "11:04")] for s, df in frames.items()}
    sch = _run(cut, tmp_path, upto=400)
    gaps = [r[0] for r in sch.store.db.execute("SELECT minute FROM cycles WHERE status='no_data' ORDER BY minute")]
    day = gaps[0][:10]
    assert gaps == [f"{day}T11:0{k}:00" for k in range(5)]


def test_kill_switch_stops_simulated_orders_but_not_screening(frames, tmp_path):
    k = KillSwitch()
    k.engage("test")
    sch = _run(frames, tmp_path, upto=600, kill=k)
    c = sch.store.counts()
    assert c["signals"] > 0 and c["paper_orders"] == 0 and sch.status()["paper_orders_enabled"] is False


def test_registry_ids_are_deterministic_and_reach_50k():
    syms = [f"S{i:03d}" for i in range(300)]
    a, b = build(syms, 50_000), build(list(reversed(syms)), 50_000)
    assert len(a) == 50_000 and set(a.ids) == set(b.ids) and len(set(a.ids)) == 50_000
    assert {TF_NAMES[t] for t in np.unique(a.tf)} == set(TIMEFRAMES) and {FAMILIES[f] for f in np.unique(a.fam)} == set(FAMILIES)
    assert len(FEATURES) == 37 and len(FAMILIES) == 12


def test_background_clock_runs_a_cycle_after_each_minute_boundary(frames, tmp_path):
    feed = ReplayMinuteFeed(frames)
    first = feed.minutes()[0]                        # 09:16 on a session day
    target = first + pd.Timedelta(seconds=58)        # the clock starts 2 s before 09:17
    offset = target.to_pydatetime() - datetime.now()
    sch = MinuteScheduler(tmp_path, sorted(frames), feed=feed, grace_seconds=0.5, clock=lambda: datetime.now() + offset)
    sch.start()
    deadline = time.time() + 15
    while time.time() < deadline and sch.store.counts()["cycles"] < 1:
        time.sleep(0.2)
    sch.stop()
    assert sch.store.counts()["cycles"] >= 1 and sch.watermark == (first + pd.Timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    assert sch.status()["overruns"] == 0
    assert isinstance(sch.lock, type(threading.Lock()))
    assert timedelta(seconds=0) < offset or offset <= timedelta(seconds=0)

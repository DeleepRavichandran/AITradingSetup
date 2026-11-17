"""Persistent paper sessions, the persistent kill switch, the daily Kite feed and the scheduler."""
import os
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from nseql.backtester.engine import Allocation
from nseql.paper.feed import FeedError, KiteDailyFeed
from nseql.paper.scheduler import PaperScheduler
from nseql.paper.session import ManualBook, PaperSession
from nseql.paper.store import SessionStore
from nseql.risk.limits import KillSwitch
from nseql.strategies import registry

TOKEN = "persistence-test-operator-token-01"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def boot(data_dir, tmp_path):
    """boot(name) starts a fresh API process-equivalent on the experiments dir `name` (same name = restart)."""
    from nseql import config
    keys = ("NSEQL_DATA_DIR", "NSEQL_EXPERIMENTS_DIR", "NSEQL_AUTO_FIXTURE", "NSEQL_API_TOKEN", "NSEQL_ALLOW_INSECURE_LOCAL", "NSEQL_SCHEDULER")
    saved = {k: os.environ.get(k) for k in keys}

    def start(name="a"):
        os.environ.update({"NSEQL_DATA_DIR": str(data_dir), "NSEQL_EXPERIMENTS_DIR": str(tmp_path / name), "NSEQL_AUTO_FIXTURE": "false", "NSEQL_API_TOKEN": TOKEN})
        os.environ.pop("NSEQL_ALLOW_INSECURE_LOCAL", None)
        os.environ.pop("NSEQL_SCHEDULER", None)
        config.get_settings.cache_clear()
        from nseql.api.main import create_app
        return TestClient(create_app(), headers=H)
    yield start
    for k, v in saved.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    config.get_settings.cache_clear()


def snapshot(c, sid):
    s = c.get(f"/api/paper/sessions/{sid}").json()
    return {k: s[k] for k in ("now", "steps", "remaining", "equity", "cash", "halted", "positions", "open_orders", "orders", "fills")} | {"curve": s["curve"]["equity"]}


def drive(c, sid, steps):
    assert c.post(f"/api/paper/sessions/{sid}/advance", json={"steps": steps}).status_code == 200


def test_session_survives_restart_and_continues_identically(boot):
    def scenario(c, restart):
        sid = c.post("/api/paper/sessions", json={"strategy": "sma_cross", "params": {"fast": 5, "slow": 20}, "window": "validation"}).json()["session_id"]
        c.post(f"/api/paper/sessions/{sid}/orders", json={"symbol": "SYNBANK", "qty": 7})
        drive(c, sid, 6)
        c.post(f"/api/paper/sessions/{sid}/orders", json={"symbol": "SYNIT", "qty": 3, "type": "LIMIT", "limit": 1.0})   # rejected: outside band
        c.post(f"/api/paper/sessions/{sid}/orders", json={"symbol": "SYNAUTO", "qty": 11})                                 # queued, not yet submitted
        mid = snapshot(c, sid)
        if restart:
            c = restart()
            assert snapshot(c, sid) == mid                      # identical the moment it comes back
        drive(c, sid, 9)
        return snapshot(c, sid)
    control = scenario(boot("control"), None)
    restarted = scenario(boot("restarted"), lambda: boot("restarted"))
    for k in control:
        if k not in ("orders", "fills", "open_orders"):
            assert control[k] == restarted[k], k
    strip = lambda rows: [{k: v for k, v in r.items()} for r in rows]
    assert strip(control["fills"]) == strip(restarted["fills"]) and strip(control["orders"]) == strip(restarted["orders"])
    assert any(p["book"] == "manual" and p["symbol"] == "SYNAUTO" and p["qty"] == 11 for p in restarted["positions"])


def test_kill_switch_and_halts_survive_restart(boot, tmp_path):
    c = boot()
    sid = c.post("/api/paper/sessions", json={"strategy": "sma_cross", "window": "validation"}).json()["session_id"]
    c.post("/api/risk/kill", json={"reason": "drill", "operator": "tester"})
    c = boot()                                                   # restart while engaged
    assert c.get("/api/risk").json()["kill_switch"]["engaged"] is True
    s = c.get(f"/api/paper/sessions/{sid}").json()
    assert s["halted"] and c.post(f"/api/paper/sessions/{sid}/orders", json={"symbol": "SYNBANK", "qty": 1}).json()["status"] == "REJECTED"
    c.post("/api/risk/reset", json={"reason": "drill over", "operator": "tester"})
    c = boot()                                                   # restart after a human reset
    assert c.get("/api/risk").json()["kill_switch"]["engaged"] is False       # restoring the halted session must not re-engage it
    assert c.get(f"/api/paper/sessions/{sid}").json()["halted"]                # ...but that session stays halted for good
    assert c.post(f"/api/paper/sessions/{sid}/orders", json={"symbol": "SYNBANK", "qty": 1}).json()["status"] == "REJECTED"
    n = len(c.get(f"/api/paper/sessions/{sid}").json()["fills"])
    drive(c, sid, 5)
    assert len(c.get(f"/api/paper/sessions/{sid}").json()["fills"]) == n      # a halted session never trades again
    fresh = c.post("/api/paper/sessions", json={"strategy": "sma_cross", "window": "validation"}).json()
    assert fresh["halted"] is None                                            # new sessions work once the switch is clear


def test_kill_switch_file_round_trip(tmp_path):
    p = tmp_path / "k.json"
    a = KillSwitch(p)
    a.engage("feed stale", "t1")
    b = KillSwitch(p)
    assert b.engaged and b.reason == "feed stale" and b.history[0]["event"] == "ENGAGED"
    b.reset("r.sharma", "checked")
    assert not KillSwitch(p).engaged


def test_torn_final_journal_line_is_ignored_and_closed_sessions_are_not_restored(boot, tmp_path):
    c = boot()
    sid = c.post("/api/paper/sessions", json={"strategy": "sma_cross", "window": "validation"}).json()["session_id"]
    drive(c, sid, 3)
    before = snapshot(c, sid)
    with open(tmp_path / "a" / "paper" / sid / "journal.jsonl", "a") as f:
        f.write('{"t":"bar","ts":"2023-0')                        # crash in the middle of a write
    c = boot()
    assert snapshot(c, sid) == before
    assert c.post(f"/api/paper/sessions/{sid}/close").json() == {"closed": sid}
    c = boot()
    assert c.get(f"/api/paper/sessions/{sid}").status_code == 404 and (tmp_path / "a" / "paper" / sid / "journal.jsonl").exists()


def test_session_that_cannot_be_restored_is_reported_not_dropped(boot, tmp_path):
    c = boot()
    sid = c.post("/api/paper/sessions", json={"strategy": "sma_cross", "window": "validation"}).json()["session_id"]
    store = SessionStore(tmp_path / "a" / "paper")
    m = store.meta(sid); m["strategy"] = "no_such_strategy"; store.write_meta(sid, m)
    c = boot()
    assert sid in c.get("/api/paper/scheduler").json()["restore_errors"]
    assert "could not be restored" in c.get(f"/api/paper/sessions/{sid}").json()["detail"]
    assert any(a["action"] == "PAPER_SESSION_RESTORE_FAILED" for a in c.get("/api/audit").json())


# ------------------------------------------------------------------ daily feed + scheduler
class FakeKite:
    """Serves daily candles from a fixed calendar; `fail` simulates an expired login."""

    def __init__(self, days, fail=False):
        self.days, self.fail, self.calls = days, fail, 0
        rng = np.random.default_rng(5)
        self.close = {101: 100 * np.exp(np.cumsum(rng.normal(0.002, 0.01, len(days)))), 102: 200 * np.exp(np.cumsum(rng.normal(-0.001, 0.01, len(days))))}

    def candles(self, token, interval, start, end):
        self.calls += 1
        if self.fail:
            raise PermissionError("Kite rejected the access token")
        rows = [(pd.Timestamp(f"{d.date()} 15:30"), c * 0.995, c * 1.01, c * 0.99, c, 500_000, 0)
                for d, c in zip(self.days, self.close[token]) if pd.Timestamp(start).normalize() <= d <= pd.Timestamp(end).normalize()]
        return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "oi"])


TOKENS = [{"symbol": "AAA", "token": 101, "kind": "EQ", "tick": 0.05}, {"symbol": "BBB", "token": 102, "kind": "EQ", "tick": 0.05}]


def daily_session(api, live_from, sent, journal=None):
    feed = KiteDailyFeed(TOKENS, live_from, api_factory=lambda: api, warmup_days=120)
    ps = PaperSession(feed, [Allocation(registry.create("sma_cross", {"fast": 3, "slow": 10}), "algo", 1_000_000), Allocation(ManualBook(), "manual", 100_000)],
                      label="daily", journal=journal)
    sched = PaperScheduler(lambda: {"s1": ps}, notify=lambda e, t: sent.append((e, t)), poll_minutes=15)
    return ps, sched


def test_daily_feed_warms_up_without_trading_then_steps_once_per_completed_day():
    days = pd.bdate_range("2026-06-01", "2026-11-20")
    api, sent = FakeKite(days), []
    ps, sched = daily_session(api, "2026-11-16", sent)
    sched.tick(datetime(2026, 11, 16, 11, 0))                    # Monday mid-session: today's candle is not final
    assert ps.curve[-1][0].date() == pd.Timestamp("2026-11-13").date()
    assert not ps.engine.account.fills and all(o.reason.startswith("KILL_SWITCH: WARMUP") or o.status.value == "REJECTED" for o in ps.engine.orders)
    assert sent == []                                            # warm-up days are not reported
    assert sched.tick(datetime(2026, 11, 16, 11, 5)) == []       # inside the poll interval: no API call
    sched.tick(datetime(2026, 11, 16, 16, 0))                    # after the close: Monday becomes a live step
    assert ps.curve[-1][0].date() == pd.Timestamp("2026-11-16").date() and len(sent) == 1 and sent[0][0] == "daily"
    assert "Simulated orders only" in sent[0][1]
    sched.tick(datetime(2026, 11, 17, 16, 0))
    sched.tick(datetime(2026, 11, 18, 16, 0))
    live_orders = [o for o in ps.engine.orders if o.submitted_ts.date() >= pd.Timestamp("2026-11-16").date() and o.status.value != "REJECTED"]
    assert live_orders, "the strategy should be able to trade once live"
    assert all(f.ts.date() >= pd.Timestamp("2026-11-17").date() for f in ps.engine.account.fills)   # first fill = day after first live order
    assert ps.reconcile()["ok"] and len(sent) == 3


def test_missed_days_are_caught_up_in_order_and_a_failed_login_is_reported_once_a_day():
    days = pd.bdate_range("2026-06-01", "2026-11-20")
    api, sent = FakeKite(days), []
    ps, sched = daily_session(api, "2026-11-16", sent)
    sched.tick(datetime(2026, 11, 16, 16, 0))
    api.fail = True                                              # token expired; nobody logs in for two days
    for hh in (16, 17, 18):
        sched.tick(datetime(2026, 11, 17, hh, 0))
    sched.tick(datetime(2026, 11, 18, 16, 0))
    warn = [t for e, t in sent if e == "feed"]
    assert len(warn) == 2 and "Kite login" in warn[0]            # once per day, not once per tick
    assert ps.halted_reason is None                              # a late daily candle is not a reason to halt
    api.fail = False
    sched.tick(datetime(2026, 11, 19, 16, 0))                    # login done: 17th, 18th and 19th arrive in order
    assert [t.date().isoformat() for t, _ in ps.curve[-4:]] == ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"]
    assert sched.status()["errors"] == {}


def test_daily_session_journal_replays_to_the_same_state(tmp_path):
    days = pd.bdate_range("2026-06-01", "2026-11-20")
    store = SessionStore(tmp_path)
    store.create("s1", {"feed": "kite_daily"})
    ps, sched = daily_session(FakeKite(days), "2026-11-10", [], journal=lambda r: store.append("s1", r))
    for d in (10, 11, 12, 13):
        sched.tick(datetime(2026, 11, d, 16, 0))
    ps.submit_manual({"symbol": "AAA", "qty": 5})
    sched.tick(datetime(2026, 11, 16, 16, 0))
    twin, _ = daily_session(FakeKite(days), "2026-11-10", [])
    twin.replaying = True
    for rec in store.records("s1"):
        twin.apply_record(rec)
    key = lambda s: ([(str(f.ts), f.book, f.symbol, f.side, f.qty, f.price) for f in s.engine.account.fills], s.engine.account.equity(), len(s.curve))
    assert key(twin) == key(ps) and len(ps.engine.account.fills) > 0


def test_scheduler_auto_advances_replay_sessions_and_isolates_failures(boot):
    c = boot()
    a = c.post("/api/paper/sessions", json={"strategy": "sma_cross", "window": "validation", "auto_bars": 3}).json()
    b = c.post("/api/paper/sessions", json={"strategy": "rsi_reversion", "window": "validation"}).json()      # auto_bars 0: manual only
    out = c.post("/api/paper/scheduler/tick").json()
    assert out["done"] == [{"session": a["session_id"], "action": "advanced", "bars": 3}]
    assert c.get(f"/api/paper/sessions/{a['session_id']}").json()["steps"] == a["steps"] + 3
    assert c.get(f"/api/paper/sessions/{b['session_id']}").json()["steps"] == b["steps"]
    st = c.get("/api/paper/scheduler").json()
    assert st["running"] is False and st["holiday_calendar"].startswith("NOT SUPPLIED") and st["sessions"] == 2


def test_kite_daily_session_needs_symbols_and_credentials(boot, monkeypatch):
    for k in ("KITE_API_KEY", "KITE_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    c = boot()
    assert c.post("/api/paper/sessions", json={"strategy": "sma_cross", "feed": "kite_daily"}).status_code == 400
    r = c.post("/api/paper/sessions", json={"strategy": "sma_cross", "feed": "kite_daily", "symbols": ["RELIANCE"]})
    assert r.status_code == 400 and "KITE_API_KEY" in r.json()["detail"]
    assert c.post("/api/paper/sessions", json={"strategy": "futures_trend", "feed": "kite_daily", "symbols": ["RELIANCE"]}).status_code == 400
    assert c.get("/api/paper/sessions").json() == []             # nothing half-created

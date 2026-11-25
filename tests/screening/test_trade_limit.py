"""Operator's daily trade limit (rules 1.4): at most N new buys a day; more only with a recorded approval; exits never blocked."""
import json
import os

import pandas as pd
from fastapi.testclient import TestClient

from nseql.screening.scheduler import MinuteScheduler, approved_extra
from tests.screening.scripted import bar, factory, rules

DAY = "2026-11-16"
T = lambda hm: pd.Timestamp(f"{DAY}T{hm}:00")
SYMS = [f"S{k}" for k in range(6)]


def _run(root, script, exp=None, limit=3):
    cfgs = [(s, True) for s in SYMS]
    sch = MinuteScheduler(root, SYMS, mode="live", experiments_dir=exp, portfolio=rules(1_000_000, per_trade_pct=0.05, max_entries_day=limit),
                          engine_factory=factory(cfgs, script))
    for k in range(20):
        t = T("09:16") + pd.Timedelta(minutes=k)
        sch.ingest([bar(s, t, 100.0) for s in SYMS])
        sch.run_cycle(t + pd.Timedelta(seconds=6))
    return sch


def test_the_fourth_buy_is_refused_and_exits_still_work(tmp_path):
    script = {"09:20": {0: "BUY", 1: "BUY", 2: "BUY", 3: "BUY"}, "09:25": {0: "EXIT"}, "09:28": {4: "BUY"}}
    sch = _run(tmp_path / "a", script)
    db = sch.store.db
    buys = db.execute("SELECT COUNT(*) FROM paper_orders WHERE side='BUY'").fetchone()[0]
    assert buys == 3
    refused = db.execute("SELECT COUNT(*) FROM signals WHERE alloc_reason LIKE 'daily trade limit%'").fetchone()[0]
    assert refused == 2                                                       # the 4th at 09:20 and the one at 09:28
    assert db.execute("SELECT COUNT(*) FROM paper_fills WHERE side='SELL'").fetchone()[0] == 1   # the exit was not blocked
    assert sch.engine.x.snapshot()["entries_today"] == 3


def test_an_approval_allows_more_for_that_account_only(tmp_path):
    exp = tmp_path / "exp"
    (exp / "approvals").mkdir(parents=True)
    (exp / "approvals" / f"{DAY}.json").write_text(json.dumps([{"account": "a", "extra": 1, "reason": "x" * 20},
                                                               {"account": "other", "extra": 5, "reason": "y" * 20}]))
    assert approved_extra(exp, DAY, "a") == 1 and approved_extra(exp, DAY, "zzz") == 0
    sch = _run(tmp_path / "a", {"09:20": {0: "BUY", 1: "BUY", 2: "BUY", 3: "BUY", 4: "BUY"}}, exp=exp)
    assert sch.store.db.execute("SELECT COUNT(*) FROM paper_orders WHERE side='BUY'").fetchone()[0] == 4


def test_only_the_operator_can_approve_and_needs_a_real_reason(data_dir, tmp_path):
    from nseql import config
    keys = ("NSEQL_DATA_DIR", "NSEQL_EXPERIMENTS_DIR", "NSEQL_AUTO_FIXTURE", "NSEQL_API_TOKEN", "NSEQL_VIEWER_TOKEN")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update({"NSEQL_DATA_DIR": str(data_dir), "NSEQL_EXPERIMENTS_DIR": str(tmp_path / "exp"), "NSEQL_AUTO_FIXTURE": "false",
                       "NSEQL_API_TOKEN": "limit-operator-token-1234567", "NSEQL_VIEWER_TOKEN": "limit-viewer-token-12345678"})
    config.get_settings.cache_clear()
    try:
        from nseql.api.main import create_app
        app = create_app()
        op = TestClient(app, headers={"Authorization": "Bearer limit-operator-token-1234567"})
        viewer = TestClient(app, headers={"Authorization": "Bearer limit-viewer-token-12345678"})
        body = {"extra": 2, "reason": "Strong trend day, all filters agree, approved"}
        assert viewer.post("/api/risk/extra-trades", json=body).status_code in (401, 403)      # the assistant's key cannot approve
        assert op.post("/api/risk/extra-trades", json={"extra": 2, "reason": "too short"}).status_code == 422
        assert op.post("/api/risk/extra-trades", json=body).status_code == 409                  # no live account running here
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        config.get_settings.cache_clear()

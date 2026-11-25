"""The assistant's daily trade report and its read-only live status endpoint."""
import json
import os

import pandas as pd
from fastapi.testclient import TestClient

from nseql.screening.daily_report import run as trade_report
from nseql.screening.execution import PortfolioRules
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols

DAY = "2026-11-16"


def _account(root):
    syms = symbols(6)
    sch = MinuteScheduler(root, syms, portfolio=PortfolioRules.live(100_000), live_from=f"{DAY}T09:16:00", mode="live")
    sch.verbose = False
    for ts, bars in minute_stream(6, [DAY], seed=13):
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    sch.store.db.commit()
    (root / "live.json").write_text(json.dumps({"live_from": f"{DAY}T09:16:00", "symbols": syms, "capital": 100_000}))
    return sch


def test_every_trade_is_listed_with_why_and_next_time_matches_the_ledger(tmp_path):
    sch = _account(tmp_path / "acct")
    r = trade_report(tmp_path / "acct", DAY)
    led = sch.store.db.execute("SELECT COUNT(*), COALESCE(SUM(realized_paise),0) FROM paper_orders WHERE side='SELL' AND status IN ('filled','partial')").fetchone()
    assert r["round_trips"] == led[0] > 0 and abs(r["net_inr"] - led[1] / 100) < 0.05
    assert r["won"] + r["lost"] <= r["round_trips"] and len(r["trades"]) == r["round_trips"]
    t = r["trades"][0]
    assert {"symbol", "strategy", "bought", "sold", "buy_price", "sell_price", "net_inr", "charges_inr", "outcome", "why"} <= set(t)
    assert all(x["next_time"] and x["decided_by"] and x["trades"] > 0 for x in r["next_time"])
    assert r["plain"].startswith(f"{r['round_trips']} trades") and "before it beats" not in r["plain"]


def test_assistant_status_is_read_only_and_complete(data_dir, tmp_path):
    from nseql import config
    exp = tmp_path / "exp"
    acct = exp / "screening" / "live_x"
    _account(acct)
    keys = ("NSEQL_DATA_DIR", "NSEQL_EXPERIMENTS_DIR", "NSEQL_AUTO_FIXTURE", "NSEQL_API_TOKEN", "NSEQL_VIEWER_TOKEN")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update({"NSEQL_DATA_DIR": str(data_dir), "NSEQL_EXPERIMENTS_DIR": str(exp), "NSEQL_AUTO_FIXTURE": "false",
                       "NSEQL_API_TOKEN": "assistant-operator-token-123456", "NSEQL_VIEWER_TOKEN": "assistant-viewer-token-1234567"})
    config.get_settings.cache_clear()
    try:
        from nseql.api.main import create_app
        app = create_app()
        op = TestClient(app, headers={"Authorization": "Bearer assistant-operator-token-123456"})
        op.post("/api/screening/open", json={"root": str(acct)})
        v = TestClient(app, headers={"Authorization": "Bearer assistant-viewer-token-1234567"})
        s = v.get("/api/assistant/status")
        assert s.status_code == 200
        d = s.json()
        assert {"asked_at", "market", "bot", "account", "live_trading"} <= set(d) and d["live_trading"] is False
        assert v.post("/api/risk/kill", json={"reason": "x"}).status_code in (401, 403)   # read-only key cannot act
        assert TestClient(app).get("/api/assistant/status").status_code == 401                              # no key: nothing
    finally:
        for k, val in saved.items():
            os.environ.pop(k, None) if val is None else os.environ.__setitem__(k, val)
        config.get_settings.cache_clear()

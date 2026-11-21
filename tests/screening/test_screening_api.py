"""Screening API: replay job, paginated and filtered lists, read-only viewer, reopening after a restart."""
import os
import time

import pytest
from fastapi.testclient import TestClient

TOKEN = "screening-api-test-operator-token-1"
VIEWER = "screening-api-test-viewer-token-123"


@pytest.fixture
def boot(data_dir, tmp_path):
    from nseql import config
    keys = ("NSEQL_DATA_DIR", "NSEQL_EXPERIMENTS_DIR", "NSEQL_AUTO_FIXTURE", "NSEQL_API_TOKEN", "NSEQL_VIEWER_TOKEN", "NSEQL_SCHEDULER")
    saved = {k: os.environ.get(k) for k in keys}

    def start(token=TOKEN):
        os.environ.update({"NSEQL_DATA_DIR": str(data_dir), "NSEQL_EXPERIMENTS_DIR": str(tmp_path / "exp"), "NSEQL_AUTO_FIXTURE": "false",
                           "NSEQL_API_TOKEN": TOKEN, "NSEQL_VIEWER_TOKEN": VIEWER})
        os.environ.pop("NSEQL_SCHEDULER", None)
        config.get_settings.cache_clear()
        from nseql.api.main import create_app
        return TestClient(create_app(), headers={"Authorization": f"Bearer {token}"})
    yield start
    for k, v in saved.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    config.get_settings.cache_clear()


def _wait(c, jid):
    for _ in range(600):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.2)
    raise AssertionError("replay job did not finish")


def test_replay_lists_filters_and_restart(boot):
    c = boot()
    assert c.get("/api/screening/status").json()["started"] is False
    assert c.get("/api/screening/signals").status_code == 404
    j = _wait(c, c.post("/api/screening/replay", json={"sessions": 5}).json()["id"])
    assert j["status"] == "done", j
    st = c.get("/api/screening/status").json()
    assert st["started"] and st["registered"] == 342 and st["live_trading"] is False and "replay" in st["feed"]
    sm = c.get("/api/screening/summary").json()
    assert set(sm["horizons"]) == {"intraday", "swing"} and sm["horizons"]["intraday"]["open_positions"] == 0
    page1 = c.get("/api/screening/signals?limit=20&offset=0").json()
    page2 = c.get("/api/screening/signals?limit=20&offset=20").json()
    assert page1["total"] == st["signals"] > 40 and len(page1["rows"]) == 20
    assert not {(r["config_id"], r["bar_ts"]) for r in page1["rows"]} & {(r["config_id"], r["bar_ts"]) for r in page2["rows"]}
    ex = c.get("/api/screening/signals?horizon=intraday&decision=EXIT&limit=500").json()
    assert 0 < ex["total"] < page1["total"] and all(r["horizon"] == "intraday" and r["decision"] == "EXIT" for r in ex["rows"])
    assert c.get("/api/screening/configs?horizon=swing&limit=1").json()["total"] == 2 * 19 * 3
    cf = c.get("/api/screening/configs?horizon=intraday&family=rsi&limit=5").json()
    assert cf["total"] == 2 * 3 * 9 * 2 and all(r["family"] == "rsi" and r["horizon"] == "intraday" for r in cf["rows"])
    assert c.get("/api/screening/orders?status=filled&limit=5").json()["total"] > 0
    assert c.get("/api/screening/cycles?limit=3").json()["total"] == 1875
    pf = c.get("/api/screening/portfolio").json()
    assert pf["shared_capital"] and pf["mode"] == "realistic" and pf["cash_inr"] - pf["reserved_inr"] >= 0
    buys = c.get("/api/screening/signals?decision=BUY&limit=1").json()["total"]
    assert sum(pf["refused_signals"].values()) + c.get("/api/screening/orders?side=BUY&limit=1").json()["total"] == buys
    fl = c.get("/api/screening/fills?limit=5").json()
    assert fl["total"] >= c.get("/api/screening/orders?status=filled").json()["total"]
    ov = c.get("/api/screening/overview").json()
    assert ov["data_source"]["kind"] == "synthetic_replay" and ov["configurations"]["registered"] == 342
    assert ov["configurations"]["qualified"] + ov["configurations"]["unverified"] == 342
    assert ov["cycles"]["by_status"].get("processed", 0) > 0 and ov["cycles"]["latency_ms"]["p99"] > 0
    assert sum(ov["signals"]["buy_allocation"].values()) == buys and set(ov["signals"]["buy_allocation"]) <= {"accepted", "rejected", "deferred"}
    assert ov["capital"]["starting_capital_inr"] == 1_000_000 and ov["paper_performance"]["note"].startswith("simulated fills on Replay of SYNTHETIC")
    assert ov["learning_brain"]["verdict"] in ("out-of-sample improvement shown", "no reliable out-of-sample improvement yet", "not benchmarked yet", "unavailable")
    rd = {m["module"]: m for m in c.get("/api/readiness").json()["modules"]}
    assert rd["Live order routing"]["status"] == "disabled" and rd["Angel One adapter"]["status"] in ("not configured", "unverified")
    assert rd["Market data"]["status"] == "ready" and "SYNTHETIC" in rd["Market data"]["detail"]
    acc = c.get("/api/screening/signals?decision=BUY&alloc_status=accepted&limit=5").json()
    assert acc["total"] == ov["signals"]["buy_allocation"].get("accepted", 0) and all(r["alloc_reason"] for r in acc["rows"])
    # read-only viewer can look but cannot start a replay
    v = boot(VIEWER)
    assert v.get("/api/screening/summary").status_code == 200 and v.post("/api/screening/replay", json={}).status_code == 403
    # a restart reopens the run from its checkpoint, with nothing lost or duplicated
    c2 = boot()
    st2 = c2.get("/api/screening/status").json()
    assert st2["started"] and st2["signals"] == st["signals"] and st2["watermark"] == st["watermark"]
    assert st2["recovery"]["checkpoint"] == st["watermark"]
    assert st["data_source"]["kind"] == "synthetic_replay" and st2["data_source"]["kind"] == "synthetic_replay"   # not "none" after a restart
    b = c2.get("/api/brokers").json()["market_data"]
    assert {"Angel One 1-minute polling (screening loop)": "UNVERIFIED"}.items() <= {m["name"]: m["status"] for m in b}.items()


def test_replay_needs_minute_bars(boot):
    c = boot()
    r = c.post("/api/screening/replay", json={"dataset_id": "nope"})
    assert r.status_code in (400, 404)

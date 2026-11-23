"""The sixteen acceptance criteria, exercised through the public HTTP API wherever possible.

Criteria that need licensed data or broker credentials are asserted to be reported as BLOCKED
by the system itself — they are not simulated.
"""
import os
import time

import pytest
from fastapi.testclient import TestClient

from nseql.research import sandbox

BLOCKED = pytest.mark.blocked
TOKEN = "acceptance-test-operator-token-0001"


@pytest.fixture(scope="module")
def client(data_dir, tmp_path_factory):
    from nseql import config
    exp = tmp_path_factory.mktemp("experiments")
    env = {"NSEQL_DATA_DIR": str(data_dir), "NSEQL_EXPERIMENTS_DIR": str(exp), "NSEQL_AUTO_FIXTURE": "false", "NSEQL_API_TOKEN": TOKEN}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    for k in ("LIVE_TRADING_ENABLED", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "NSEQL_ALLOW_INSECURE_LOCAL", "NSEQL_VIEWER_TOKEN"):
        os.environ.pop(k, None)
    config.get_settings.cache_clear()
    from nseql.api.main import create_app
    c = TestClient(create_app(), headers={"Authorization": f"Bearer {TOKEN}"})
    yield c
    for k, v in old.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    config.get_settings.cache_clear()


def bt(client, name="sma_cross", params=None, **kw):
    r = client.post("/api/backtests", json={"strategies": [{"name": name, "params": params or {}}], "end": "2022-12-30", **kw})
    assert r.status_code == 200, r.text
    return r.json()


def features(client) -> dict:
    return {f["feature"]: f["status"] for f in client.get("/api/system").json()["features"]}


def wait(client, job, timeout=600):
    t = time.time()
    while time.time() - t < timeout:
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.5)
    raise TimeoutError


def test_api_requires_the_token(client):
    assert TestClient(client.app).get("/api/system").status_code == 401
    assert TestClient(client.app).get("/api/health").json()["auth_required"] is True


@BLOCKED
def test_01_import_authorised_real_dataset_is_reported_blocked(client):
    f = features(client)
    assert f["Import of an authorised real NSE dataset"] == "BLOCKED"
    assert all(d["synthetic"] for d in client.get("/api/datasets").json())          # nothing pretends to be real


def test_02_reproducible_equity_backtest(client):
    a, b = bt(client), bt(client)
    assert a["summary"]["result_hash"] == b["summary"]["result_hash"] and a["summary"]["fingerprint"] == b["summary"]["fingerprint"]
    assert a["run_id"] != b["run_id"] and a["summary"]["metrics"]["trades"] > 0
    assert a["summary"]["flags"][0].startswith("SYNTHETIC DATA")


def test_03_every_trade_reconciles_to_the_ledger(client):
    r = bt(client, "momentum_rank", {"lookback": 60})
    rid = r["run_id"]
    assert all(r["summary"]["reconciliation"].values())
    fills = client.get(f"/api/runs/{rid}/fills").json()["items"]
    ledger = client.get(f"/api/runs/{rid}/ledger").json()["items"]
    trades = client.get(f"/api/runs/{rid}/trades").json()["items"]
    by_fill: dict = {}
    for e in ledger:
        if e["ref"] is not None:
            by_fill.setdefault(int(e["ref"]), []).append(e)
    for f in fills:                                           # each fill has its TRADE and CHARGES ledger lines
        rows = by_fill[f["id"]]
        assert round(sum(x["amount"] for x in rows if x["type"] == "TRADE"), 2) == round(f["cash_delta"], 2)
        assert round(-sum(x["amount"] for x in rows if x["type"] == "CHARGES"), 2) == round(f["charges"], 2)
    assert round(sum(t["gross"] for t in trades), 2) == round(sum(f["realized"] for f in fills), 2)
    assert round(sum(e["amount"] for e in ledger), 2) == round(ledger[-1]["cash_after"], 2)
    m = r["summary"]["metrics"]
    opens = sum(p["unrealized"] for p in r["summary"]["open_positions"])
    dividends = sum(e["amount"] for e in ledger if e["type"] == "DIVIDEND")
    assert abs(m["final_equity"] - (m["initial_capital"] + sum(f["realized"] for f in fills) + opens - m["total_costs"] + dividends)) < 0.02


def test_04_valid_historical_charges_are_applied(client):
    r = bt(client)
    cb = r["summary"]["charges_breakdown"]
    assert cb["stt"] > 0 and cb["stamp"] > 0 and cb["exchange_txn"] > 0 and cb["gst"] > 0
    fills = client.get(f"/api/runs/{r['run_id']}/fills").json()["items"]
    buy = next(f for f in fills if f["side"] == 1)
    d = buy["charges_detail"]
    turnover = round(buy["qty"] * buy["price"] * 100)
    assert d["stt"] == round(turnover * 0.001) and d["stamp"] == round(turnover * 0.00015)       # 2022 delivery rates
    free = bt(client, cost_multiplier=0.0)
    assert free["summary"]["metrics"]["total_costs"] == 0
    assert free["summary"]["metrics"]["final_equity"] > r["summary"]["metrics"]["final_equity"]
    old = client.post("/api/backtests", json={"strategies": [{"name": "sma_cross"}], "start": "2010-01-01", "end": "2022-06-30"})
    assert old.status_code == 200                              # fixture starts 2022, inside schedule coverage


def test_05_futures_contract_simulation(client):
    r = bt(client, "futures_trend", {"fast": 10, "slow": 40}, end="2023-06-30")
    ev = client.get(f"/api/runs/{r['run_id']}/events").json()["items"]
    led = client.get(f"/api/runs/{r['run_id']}/ledger").json()["items"]
    fills = client.get(f"/api/runs/{r['run_id']}/fills").json()["items"]
    assert any(e["type"] == "FUTURES_MTM" for e in ev) and any(x["type"] == "MTM" for x in led)
    assert len({f["symbol"] for f in fills}) >= 3 and any(f["tag"] == "roll_out" for f in fills)    # rolled across contracts
    assert all(f["symbol"].endswith("FUT") and f["qty"] % 50 == 0 for f in fills) and all(r["summary"]["reconciliation"].values())


def test_06_option_contract_uses_its_own_premium_history(client):
    r = bt(client, "option_structure", {"structure": "long_straddle", "dte_exit": 0, "profit_target": 99, "loss_limit": 99}, end="2023-06-30")
    fills = client.get(f"/api/runs/{r['run_id']}/fills").json()["items"]
    entry = next(f for f in fills if f["reason"] == "order")
    bars = client.get("/api/datasets/synthetic_v1/bars", params={"symbol": entry["symbol"]}).json()
    i = bars["ts"].index(entry["ts"])
    assert bars["low"][i] * 0.97 <= entry["price"] <= bars["high"][i] * 1.03          # filled inside that contract's own bar (+slippage)
    exp = [f for f in fills if f["reason"] == "expiry"]
    assert exp, "positions must be settled by the engine at expiry"
    inst = client.get("/api/datasets/synthetic_v1/instruments", params={"q": exp[0]["symbol"]}).json()["items"][0]
    und = client.get("/api/datasets/synthetic_v1/bars", params={"symbol": "SYNNIFTY", "end": inst["expiry"]}).json()["close"][-1]
    intrinsic = max(und - inst["strike"], 0) if inst["option_type"] == "CE" else max(inst["strike"] - und, 0)
    assert abs(exp[0]["price"] - intrinsic) < 0.011 and all(r["summary"]["reconciliation"].values())
    ch = client.get("/api/datasets/synthetic_v1/chain", params={"underlying": "SYNNIFTY", "date": "2023-03-01"}).json()
    assert ch["rows"] and any(x["delta"] is not None for x in ch["rows"])


def test_07_strategies_compared_under_identical_assumptions(client):
    ids = [bt(client, n)["run_id"] for n in ("sma_cross", "rsi_reversion", "buy_and_hold")]
    c = client.post("/api/compare", json={"run_ids": ids}).json()
    assert c["warnings"] == [] and len(c["rows"]) == 3 and len({r["dataset_hash"] for r in c["rows"]}) == 1
    assert len(c["correlation"]["matrix"]) == 3
    other = bt(client, "sma_cross", cost_multiplier=3.0)["run_id"]
    assert client.post("/api/compare", json={"run_ids": [ids[0], other]}).json()["warnings"]


@pytest.fixture(scope="module")
def research(client):
    j = client.post("/api/research/run", json={"max_iterations": 2, "max_backtests": 200, "max_sealed_evaluations": 1, "walk_forward": False, "seed": 3})
    assert j.status_code == 200, j.text
    done = wait(client, j.json())
    assert done["status"] == "done", done["error"]
    return done["result"]


@pytest.mark.slow
def test_08_ai_agent_generates_a_new_strategy(client, research):
    hid = research["results"][0]["hypothesis_id"]
    h = client.get(f"/api/research/hypotheses/{hid}").json()
    assert h["hypothesis"]["falsification"] and h["code"]["source"].startswith('"""GENERATED STRATEGY')
    assert h["hypothesis"]["spec"]["entry"] and any(a["type"] == "CodeArtifact" for a in h["artifacts"])
    assert client.get("/api/strategies").json()["generated"]


@pytest.mark.slow
def test_09_ai_generated_code_is_tested_safely(client, research):
    hid = research["results"][0]["hypothesis_id"]
    ev = next(a["payload"] for a in client.get(f"/api/research/hypotheses/{hid}").json()["artifacts"] if a["type"] == "Evidence")
    assert ev["isolation"]["static_validation"] and ev["isolation"]["env_scrubbed"] and ev["lookahead"]["passed"]
    bad = client.post("/api/code/validate", json={"source": "import os\nfrom nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n        os.system('id')\n", "run": True}).json()
    assert bad["validation"]["ok"] is False and bad["run"] is None


@pytest.mark.slow
def test_10_flawed_strategies_are_independently_rejected(client, research):
    """Covered in depth by tests/integration/test_research.py (leaky strategy + 11 flawed-evidence cases).
    Here: every decision made by the live loop carries an independent audit artefact."""
    for r in research["results"]:
        arts = {a["type"]: a["payload"] for a in client.get(f"/api/research/hypotheses/{r['hypothesis_id']}").json()["artifacts"]}
        assert "AuditReport" in arts and arts["AuditReport"]["verdict"] in ("pass", "fail")
        if r["decision"] == "rejected":
            assert arts["AuditReport"]["verdict"] == "fail" or r["reasons"]


def test_11_auditable_walk_forward_report(client):
    j = client.post("/api/walkforward", json={"strategy": "sma_cross", "grid": {"fast": [5, 20], "slow": [40, 80]}, "train_days": 150, "test_days": 50})
    wf = wait(client, j.json())["result"]
    assert wf["folds"] and wf["dataset_hash"] and wf["oos"]["sessions"] > 0 and "sealed window is excluded" in wf["window_note"]
    for f in wf["folds"]:
        assert f["train"][1] < f["test"][0]
        if f.get("run_id"):
            assert client.get(f"/api/runs/{f['run_id']}").json()["summary"]["fingerprint"]       # links to a stored run
    assert client.get("/api/walkforward").json()[0]["id"] == wf["artifact_id"]


def test_12_dashboard_endpoints_serve_real_results(client):
    for path in ("/api/system", "/api/datasets", "/api/datasets/synthetic_v1", "/api/datasets/synthetic_v1/quality", "/api/market/overview",
                 "/api/market/structure", "/api/strategies", "/api/runs", "/api/research/agents", "/api/research/experiments",
                 "/api/research/hypotheses", "/api/research/memory", "/api/brokers", "/api/risk", "/api/settings", "/api/audit",
                 "/api/jobs", "/api/paper/sessions", "/api/notifications", "/api/events", "/api/execution/gate", "/api/walkforward"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:200])
    rid = bt(client)["run_id"]
    d = client.get(f"/api/runs/{rid}").json()
    assert d["summary"]["dataset"]["content_hash"] and d["config"]["strategies"] and "events.jsonl" in d["artefacts"]
    assert client.get(f"/api/runs/{rid}/equity").json()["equity"]
    rp = client.get(f"/api/replay/{rid}/0").json()
    assert rp["bars"]["ts"] and rp["fills"] and rp["orders"] and rp["ledger"]
    assert client.post("/api/chat", json={"question": "what happened?", "run_id": rid}).json()["mode"].startswith("digest")
    p = client.post("/api/portfolio/simulate", json={"strategies": [{"name": "sma_cross"}, {"name": "rsi_reversion"}, {"name": "donchian_breakout"}],
                                                      "method": "risk_parity", "capital": 3_000_000})
    assert p.status_code == 200, p.text
    pj = p.json()
    assert abs(sum(pj["weights"].values()) - 1) < 1e-9 and pj["capital_check"]["reconciliation_ok"]
    assert pj["capital_check"]["sum_of_book_cash"] == pj["capital_check"]["account_cash"]


@BLOCKED
def test_13_real_time_streaming_is_reported_blocked(client):
    assert features(client)["Authorised real-time streaming feed"] == "BLOCKED"
    assert {m["name"]: m["status"] for m in client.get("/api/brokers").json()["market_data"]}["KiteTicker WebSocket"] == "NOT IMPLEMENTED"


def test_14_paper_orders_and_position_reconciliation(client):
    s = client.post("/api/paper/sessions", json={"strategy": "sma_cross", "params": {"fast": 5, "slow": 20}, "window": "validation"}).json()
    sid = s["session_id"]
    assert s["feed"] == "replay" and s["steps"] > 100
    assert client.post(f"/api/paper/sessions/{sid}/orders", json={"symbol": "SYNBANK", "qty": 7}).json()["status"] == "QUEUED"
    st = client.post(f"/api/paper/sessions/{sid}/advance", json={"steps": 3}).json()
    assert any(p["book"] == "manual" and p["qty"] == 7 for p in st["positions"])
    assert st["reconciliation"]["ok"] and st["reconciliation"]["position_breaks"] == []
    assert client.post("/api/paper/sessions", json={"strategy": "generated:code_x"}).status_code == 400


def test_15_execution_stops_safely_on_failures(client):
    """Stale-feed, feed-error and loss-limit halts are covered in tests/integration/test_paper_and_execution.py.
    Here: the operator kill switch halts every session, blocks new orders and needs a named human to clear."""
    sid = client.post("/api/paper/sessions", json={"strategy": "sma_cross", "window": "validation"}).json()["session_id"]
    assert client.post("/api/risk/kill", json={"reason": "drill", "operator": "tester"}).json()["engaged"]
    st = client.get(f"/api/paper/sessions/{sid}").json()
    assert st["halted"] and st["open_orders"] == []
    assert client.post(f"/api/paper/sessions/{sid}/orders", json={"symbol": "SYNBANK", "qty": 1}).json()["status"] == "REJECTED"
    assert client.post("/api/risk/reset", json={"reason": "let me trade", "operator": "agent-007"}).status_code == 403
    assert client.post("/api/risk/reset", json={"reason": "drill complete", "operator": "tester"}).json()["engaged"] is False
    actions = [a["action"] for a in client.get("/api/audit").json()]
    assert {"KILL_SWITCH_ENGAGED", "KILL_SWITCH_RESET_DENIED", "KILL_SWITCH_RESET"} <= set(actions)


def test_16_ai_generated_code_cannot_enable_real_money_trading(client):
    """Four independent barriers; the last three hold even if the one before fails."""
    attempts = ["import os\nos.environ['LIVE_TRADING_ENABLED'] = 'true'\n",
                "from nseql.config import get_settings\n", "from nseql.execution.router import OrderRouter\n",
                "from nseql.execution.gate import sign_approval\n", "import nseql.execution.brokers.zerodha\n", "import httpx\n", "import socket\n"]
    body = "from nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n        pass\n"
    for a in attempts:                                                             # 1. static validation
        assert not sandbox.validate_source(a + body).ok
    s = client.get("/api/settings").json()                                         # 2. no API can flip the flag
    assert s["live_trading_enabled"] is False and s["credentials"]["human_approval_secret_visible_to_api"] is False
    routes = {r.path for r in client.app.routes[-1].app.routes}
    assert not [p for p in routes if "approve" in p or "live" in p]                # there is no approval or enable endpoint
    g = client.get("/api/execution/gate", params={"sha256": "ab" * 32}).json()     # 3. the gate is closed by default
    assert g["decision"]["allowed"] is False and len(g["decision"]["reasons"]) >= 3
    assert client.get("/api/brokers").json()["gate_default"]["allowed"] is False   # 4. sandbox cannot import execution: test_research

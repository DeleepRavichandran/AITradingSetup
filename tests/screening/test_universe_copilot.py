"""V6 read models over a real replay: Strategy Universe (summary, paging, dossier), Command Center,
platform status, Vega copilot intents (grounded, with evidence links), planning and fund endpoints."""
import os
import time

import pytest
from fastapi.testclient import TestClient

TOKEN = "universe-copilot-test-operator-token"
VIEWER = "universe-copilot-test-viewer-token-1"
QUESTIONS = {
    "market": "What changed in the market?",
    "strongest": "Which strategies have the strongest validated evidence?",
    "correlated": "Which strategies are correlated?",
    "loss": "What caused today's simulated portfolio loss?",
    "failed": "Which research experiments failed?",
    "brain": "What did the Learning Brain discover?",
    "next": "What should the research system investigate next?",
    "stale": "Is the current data feed stale?",
    "incomplete": "Which market or investment modules are incomplete?",
}


@pytest.fixture(scope="module")
def client(tmp_path_factory, request):
    from nseql import config
    from nseql.data.fixtures import ensure_fixture
    root = tmp_path_factory.mktemp("v6")
    data = root / "data"
    ensure_fixture(data)
    keys = ("NSEQL_DATA_DIR", "NSEQL_EXPERIMENTS_DIR", "NSEQL_AUTO_FIXTURE", "NSEQL_API_TOKEN", "NSEQL_VIEWER_TOKEN", "NSEQL_SCHEDULER")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update({"NSEQL_DATA_DIR": str(data), "NSEQL_EXPERIMENTS_DIR": str(root / "exp"), "NSEQL_AUTO_FIXTURE": "false",
                       "NSEQL_API_TOKEN": TOKEN, "NSEQL_VIEWER_TOKEN": VIEWER})
    os.environ.pop("NSEQL_SCHEDULER", None)
    config.get_settings.cache_clear()
    from nseql.api.main import create_app
    app = create_app()
    c = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
    v = TestClient(app, headers={"Authorization": f"Bearer {VIEWER}"})
    assert c.get("/api/universe/summary").status_code == 404           # nothing before a run
    jid = c.post("/api/screening/replay", json={"sessions": 5}).json()["id"]
    for _ in range(600):
        if c.get(f"/api/jobs/{jid}").json()["status"] != "running":
            break
        time.sleep(0.2)
    yield c, v
    for k, val in saved.items():
        os.environ.pop(k, None) if val is None else os.environ.__setitem__(k, val)
    config.get_settings.cache_clear()


def test_universe_summary_counts_add_up(client):
    c, _ = client
    s = c.get("/api/universe/summary").json()
    assert s["registered"] == 342 and s["rules"] == 19 and s["families"] == 3 and "19 rules" in s["note"]
    assert sum(s["by_stage"].values()) == 342 and sum(cell["configs"] for cell in s["map"]) == 342
    assert len(s["map"]) == 3 * 4                                         # 3 families x 4 timeframes
    f = [x["n"] for x in s["funnel"]]
    assert f == sorted(f, reverse=True) and f[0] == 342                    # a funnel only narrows


def test_universe_paging_filters_and_sorting(client):
    c, _ = client
    p1 = c.get("/api/universe/configs?limit=50&offset=0&sort=trades").json()
    p2 = c.get("/api/universe/configs?limit=50&offset=50&sort=trades").json()
    assert p1["total"] == 342 and len(p1["rows"]) == 50 and not {r["id"] for r in p1["rows"]} & {r["id"] for r in p2["rows"]}
    t = [r["trades"] for r in p1["rows"]]
    assert t == sorted(t, reverse=True)
    rsi = c.get("/api/universe/configs?family=rsi&timeframe=5m&limit=500").json()
    assert rsi["total"] == 2 * 9 * 2 and all(r["family"] == "rsi" and r["timeframe"] == "5m" for r in rsi["rows"])
    assert c.get("/api/universe/configs?stage=bogus").status_code == 400
    sym = p1["rows"][0]["symbol"]
    assert c.get(f"/api/universe/configs?q={sym.lower()}&limit=1").json()["total"] == 171


def test_dossier_is_complete_and_honest(client):
    c, _ = client
    top = c.get("/api/universe/configs?limit=1&sort=trades").json()["rows"][0]
    d = c.get(f"/api/universe/configs/{top['id']}").json()
    assert d["config"]["id"] == top["id"] and d["config"]["definition"].startswith(("Buy when", "Buy when"))
    assert d["data_provenance"]["kind"] == "synthetic_replay" and any("SYNTHETIC" in w for w in d["weaknesses"])
    assert d["forward_evidence"]["trades"] == top["trades"] and d["historical_backtest"]["status"] == "not run"
    assert d["execution"]["allocation"] == "ranked" and d["version_history"][0]["id"] == top["id"]
    assert len(d["related"]["same_rule_other_symbols"]) <= 6 and all(r["id"] != top["id"] for r in d["related"]["same_symbol_other_rules"])
    assert c.get("/api/universe/configs/0000000000000000").status_code == 404


def test_command_center_and_platform_status(client):
    c, v = client
    cc = c.get("/api/command-center").json()
    p = cc["platform"]
    assert p["data_source"]["kind"] == "synthetic_replay" and "state" in p["market"] and p["risk"]["live_trading_enabled"] is False
    assert {m["module"] for m in p["readiness"]} >= {"API", "Screening loop", "Live order routing"}
    s = cc["strategies"]["screening"]
    assert s["registered"] == 342 and s["by_asset_class"] == {"cash equity": 342} and s["latency_ms"]["p99"] > 0
    pf = cc["portfolio"]
    assert pf["starting_capital_inr"] == 1_000_000 and pf["drawdown"]["max_pct"] >= 0 and pf["provenance"]["data"]["synthetic"]
    assert "learning_verdict" in cc["ai"]
    ps = v.get("/api/platform/status").json()                              # viewers can read it
    assert ps["role"] == "viewer" and ps["modules"]["total"] >= 8 and ps["kill_switch"]["engaged"] is False


@pytest.mark.parametrize("intent", list(QUESTIONS))
def test_copilot_routes_each_question_and_grounds_it(client, intent):
    _, v = client
    a = v.get("/api/copilot/ask", params={"q": QUESTIONS[intent]}).json()      # read-only: works for viewers
    assert a["intent"] == intent, a
    assert a["answer"] and a["method"].startswith("deterministic") and "did_not_read" in a["grounding"]
    assert all(e["href"].startswith("/") for e in a["evidence"])


def test_copilot_explains_rejections_and_refuses_to_guess(client):
    c, _ = client
    cid = c.get("/api/universe/configs?stage=gated&limit=1").json()["rows"]
    if cid:
        a = c.get("/api/copilot/ask", params={"q": f"Why was {cid[0]['id']} rejected?"}).json()
        assert a["intent"] == "why_rejected" and cid[0]["id"] in a["answer"] and a["evidence"][0]["href"] == f"/universe?config={cid[0]['id']}"
    u = c.get("/api/copilot/ask", params={"q": "Will RELIANCE go up tomorrow?"}).json()
    assert u["intent"] is None and u["suggestions"] and "nothing was looked up" in u["method"]
    assert c.get("/api/copilot/ask", params={"q": " "}).status_code == 400


def test_planning_and_funds_endpoints(client):
    c, v = client
    g = v.get("/api/planning/goal", params={"target_today": 1_000_000, "years": 10, "annual_return": 0.1}).json()
    assert g["monthly_sip_needed"] > 0 and g["assumptions"]["nature"].startswith("user-assumed")
    assert c.get("/api/planning/sip", params={"monthly": -5, "years": 1, "annual_return": 0.1}).status_code == 400
    assert c.get("/api/funds/status").json()["navs"] == 0
    text = ("Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date\n"
            "Open Ended Schemes(Equity Scheme - Flexi Cap Fund)\nSome AMC\n"
            "300001;INF0;-;Some Flexi Cap Fund - Direct Plan - Growth;100.5;20-Nov-2026\n")
    assert v.post("/api/funds/import", json={"text": text}).status_code == 403        # viewers cannot import
    r = c.post("/api/funds/import", json={"text": text, "file": "test.txt"}).json()
    assert r["new_navs"] == 1
    sc = c.get("/api/funds/schemes/300001").json()
    assert sc["scheme"]["category"] == "Equity Scheme - Flexi Cap Fund" and sc["provenance"]["points"] == 1
    assert c.get("/api/funds/schemes?q=flexi").json()["total"] == 1

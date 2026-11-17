"""Learning Brain over HTTP: read access, operator-only writes, human-only promotion, protected settings."""
import os

import pytest
from fastapi.testclient import TestClient

OP, VIEW = "brain-api-operator-token-000000001", "brain-api-viewer-token-0000000001"


@pytest.fixture
def client(data_dir, tmp_path):
    from nseql import config
    keys = ("NSEQL_DATA_DIR", "NSEQL_EXPERIMENTS_DIR", "NSEQL_AUTO_FIXTURE", "NSEQL_API_TOKEN", "NSEQL_VIEWER_TOKEN", "NSEQL_ALLOW_INSECURE_LOCAL")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update({"NSEQL_DATA_DIR": str(data_dir), "NSEQL_EXPERIMENTS_DIR": str(tmp_path), "NSEQL_AUTO_FIXTURE": "false",
                       "NSEQL_API_TOKEN": OP, "NSEQL_VIEWER_TOKEN": VIEW})
    os.environ.pop("NSEQL_ALLOW_INSECURE_LOCAL", None)
    config.get_settings.cache_clear()
    from nseql.api.main import create_app
    app = create_app()
    yield TestClient(app, headers={"Authorization": f"Bearer {OP}"}), TestClient(app, headers={"Authorization": f"Bearer {VIEW}"})
    for k, v in saved.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    config.get_settings.cache_clear()


def test_summary_kpis_and_verify(client):
    op, viewer = client
    s = viewer.get("/api/brain/summary").json()
    assert s["champion"] == "v1" and s["protocol_version"] == 3
    assert {k["key"] for k in s["kpis"]} >= {"oos_lift", "calibration", "research_efficiency", "regime_robustness", "diversification",
                                              "multiple_testing", "negative_control", "paper_forward"}
    assert all(k["status"] == "not measured" for k in s["kpis"])          # nothing is claimed before a benchmark has run
    assert viewer.get("/api/brain/verify").json()["memory"]["ok"] is True


def test_writes_need_the_operator_and_protected_settings_are_refused(client):
    op, viewer = client
    assert viewer.post("/api/brain/versions/propose", json={"changes": {"top_k": 12}}).status_code == 403
    r = op.post("/api/brain/versions/propose", json={"changes": {"max_order_notional": 1e9}, "rationale": "try"}).json()
    assert r["rejected"] and "protected" in r["rejected"]
    ok = op.post("/api/brain/versions/propose", json={"changes": None}).json()        # meta-optimiser variation
    assert ok["rejected"] is None and ok["changes"]
    v = {x["id"]: x for x in op.get("/api/brain/summary").json()["versions"]}
    assert v[r["version"]]["status"] == "rejected" and v[ok["version"]]["status"] == "proposed"


def test_promotion_is_human_only_and_requires_readiness(client):
    op, viewer = client
    vid = op.post("/api/brain/versions/propose", json={"changes": {"top_k": 12}, "rationale": "test"}).json()["version"]
    assert viewer.post(f"/api/brain/versions/{vid}/promote", json={"approver": "R. Sharma", "reason": "looks fine"}).status_code == 403
    assert op.post(f"/api/brain/versions/{vid}/promote", json={"approver": "agent-director", "reason": "auto approve"}).status_code == 403
    assert op.post(f"/api/brain/versions/{vid}/promote", json={"approver": "R. Sharma", "reason": "looks fine"}).status_code == 409
    assert any(a["action"] == "BRAIN_APPROVAL_DENIED" for a in op.get("/api/audit").json())

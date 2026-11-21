"""Audit item 4: the API must never serve unauthenticated requests in a deployed environment."""
import os

import pytest
from fastapi.testclient import TestClient

OPERATOR = "operator-token-0123456789abcdef"
VIEWER = "viewer-token-0123456789abcdefgh"
KEYS = ("NSEQL_API_TOKEN", "NSEQL_VIEWER_TOKEN", "NSEQL_ALLOW_INSECURE_LOCAL", "NSEQL_REQUIRE_AUTH")


@pytest.fixture
def make_app(data_dir, tmp_path):
    from nseql import config
    saved = {k: os.environ.get(k) for k in (*KEYS, "NSEQL_DATA_DIR", "NSEQL_EXPERIMENTS_DIR", "NSEQL_AUTO_FIXTURE")}

    def build(**env):
        for k in KEYS:
            os.environ.pop(k, None)
        os.environ.update({"NSEQL_DATA_DIR": str(data_dir), "NSEQL_EXPERIMENTS_DIR": str(tmp_path), "NSEQL_AUTO_FIXTURE": "false", **env})
        config.get_settings.cache_clear()
        from nseql.api.main import create_app
        return create_app()
    yield build
    for k, v in saved.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    config.get_settings.cache_clear()


def test_api_refuses_to_start_without_a_token(make_app):
    with pytest.raises(RuntimeError, match="NSEQL_API_TOKEN"):
        make_app()


def test_weak_or_duplicate_tokens_are_refused(make_app):
    with pytest.raises(RuntimeError, match="at least"):
        make_app(NSEQL_API_TOKEN="short")
    with pytest.raises(RuntimeError, match="differ"):
        make_app(NSEQL_API_TOKEN=OPERATOR, NSEQL_VIEWER_TOKEN=OPERATOR)


def test_every_route_requires_the_token(make_app):
    app = make_app(NSEQL_API_TOKEN=OPERATOR)
    anon = TestClient(app)
    paths = sorted({r.path for r in app.routes[-1].app.routes if "{" not in r.path and "GET" in getattr(r, "methods", ())})
    assert len(paths) > 15
    for p in paths:
        assert anon.get("/api" + p).status_code == 401, p
    assert anon.post("/api/risk/kill", json={"reason": "xxx", "operator": "me"}).status_code == 401
    assert TestClient(app, headers={"Authorization": "Bearer wrong-token-wrong-token-wrong"}).get("/api/system").status_code == 401
    assert anon.get("/api/health").json() == {"status": "ok", "auth_required": True}      # liveness only, no details


def test_viewer_token_is_read_only(make_app):
    app = make_app(NSEQL_API_TOKEN=OPERATOR, NSEQL_VIEWER_TOKEN=VIEWER)
    viewer = TestClient(app, headers={"Authorization": f"Bearer {VIEWER}"})
    op = TestClient(app, headers={"Authorization": f"Bearer {OPERATOR}"})
    assert viewer.get("/api/system").status_code == 200 and viewer.get("/api/runs").status_code == 200
    for path, body in [("/api/backtests", {"strategies": [{"name": "sma_cross"}]}), ("/api/risk/kill", {"reason": "xxx", "operator": "me"}),
                       ("/api/risk/reset", {"reason": "xxx", "operator": "me"}), ("/api/research/run", {}), ("/api/paper/sessions", {}),
                       ("/api/code/validate", {"source": "x=1"}), ("/api/notifications/test", {})]:
        r = viewer.post(path, json=body)
        assert r.status_code == 403 and "read-only" in r.json()["detail"], path
    assert op.post("/api/risk/kill", json={"reason": "drill", "operator": "tester"}).status_code == 200
    assert op.get("/api/system").json()["auth"] == {"mode": "token", "role": "operator", "viewer_token_configured": True}
    assert viewer.get("/api/system").json()["auth"]["role"] == "viewer"


def test_insecure_local_mode_is_explicit_and_loopback_only(make_app):
    app = make_app(NSEQL_ALLOW_INSECURE_LOCAL="true")
    assert TestClient(app).get("/api/system").status_code == 403                                   # not a loopback client
    local = TestClient(app, client=("127.0.0.1", 50000))
    assert local.get("/api/system").json()["auth"]["mode"] == "insecure-local"
    assert local.get("/api/system", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 403  # proxied from outside
    assert local.get("/api/system", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 200
    with pytest.raises(RuntimeError, match="not both"):
        make_app(NSEQL_ALLOW_INSECURE_LOCAL="true", NSEQL_API_TOKEN=OPERATOR)

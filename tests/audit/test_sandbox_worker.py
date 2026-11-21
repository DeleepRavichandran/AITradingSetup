"""The separate sandbox worker: the API delegates generated code to it, it requires its token, ignores
caller paths, caps limits, and research fails closed when it is unreachable."""
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from nseql.research import sandbox, sandbox_remote, sandbox_service

STRAT = "from nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n        pass\n"
JOB = [{"id": 0, "config": {"strategies": [{"name": "g"}], "end": "2022-03-31"}}]
TOKEN = "sandbox-worker-test-token-0123456789"


@pytest.fixture
def worker(monkeypatch, data_dir, tmp_path):
    art = tmp_path / "artifacts"
    art.mkdir()
    monkeypatch.delenv("NSEQL_SANDBOX_URL", raising=False)
    monkeypatch.setenv("NSEQL_SANDBOX_TOKEN", TOKEN)
    monkeypatch.setenv("NSEQL_DATA_DIR", str(data_dir))
    monkeypatch.setenv("NSEQL_SANDBOX_ARTIFACTS", str(art))
    app = sandbox_service.create_app()
    client = TestClient(app)
    monkeypatch.setenv("NSEQL_SANDBOX_URL", "http://sandbox-worker")       # the API side now delegates
    def forward(req: httpx.Request) -> httpx.Response:
        r = client.request(req.method, req.url.path, headers=dict(req.headers), content=req.content)
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers.get("content-type", "")})
    monkeypatch.setattr(sandbox_remote, "_transport", httpx.MockTransport(forward))
    monkeypatch.setattr(sandbox_remote, "_status", None)
    calls = []
    real = sandbox.run_jobs_local
    monkeypatch.setattr(sandbox, "run_jobs_local", lambda *a, **k: calls.append(a[2]) or real(*a, **k))
    return {"client": client, "artifacts": art, "calls": calls, "data_dir": data_dir}


def test_worker_refuses_to_start_without_a_strong_token_or_while_delegating(monkeypatch):
    monkeypatch.setenv("NSEQL_SANDBOX_TOKEN", "short")
    with pytest.raises(RuntimeError):
        sandbox_service.create_app()
    monkeypatch.setenv("NSEQL_SANDBOX_TOKEN", TOKEN)
    monkeypatch.setenv("NSEQL_SANDBOX_URL", "http://x")
    with pytest.raises(RuntimeError):
        sandbox_service.create_app()


def test_requests_need_the_token(worker):
    c = worker["client"]
    assert c.get("/status").status_code == 401 and c.post("/run", json={"source": STRAT, "jobs": JOB}).status_code == 401
    assert c.get("/status", headers={"Authorization": "Bearer wrong-token-wrong-token-xx"}).status_code == 401
    assert c.get("/health").status_code == 200


def test_api_side_delegates_and_artifacts_come_back(worker, tmp_path):
    ok, detail = sandbox.isolation_status(refresh=True)
    assert ok and detail.startswith("remote sandbox worker")
    out = sandbox.run_jobs(STRAT, [{**JOB[0], "save": True}], Path("/caller/supplied/path"), out_dir=tmp_path / "runs")
    assert out["ok"], out["error"]
    assert out["isolation"]["remote_worker"] and out["isolation"]["network_namespace"]
    assert worker["calls"] == [Path(worker["data_dir"]).resolve()]               # the worker used its own data dir
    p = Path(out["results"][0]["path"])
    assert p.parent == tmp_path / "runs" and (p / "summary.json").exists()
    assert not any(worker["artifacts"].iterdir())                               # staged copy cleaned up


def test_rejected_code_never_leaves_the_api_process(worker):
    out = sandbox.run_jobs("import os\n", JOB, Path("/x"))
    assert out["ok"] is False and "static validation" in out["error"] and worker["calls"] == []


def test_limits_cannot_be_raised_by_the_caller(worker, monkeypatch):
    seen = []
    monkeypatch.setattr(sandbox, "run_jobs_local", lambda s, j, d, limits, out_dir: seen.append(limits) or {"ok": True, "results": []})
    r = worker["client"].post("/run", headers={"Authorization": f"Bearer {TOKEN}"},
                              json={"source": STRAT, "jobs": JOB, "limits": {"cpu_seconds": 10**6, "wall_seconds": 5}})
    assert r.status_code == 200 and seen[0].cpu_seconds == sandbox.SandboxLimits().cpu_seconds and seen[0].wall_seconds == 5


def test_unreachable_worker_fails_closed(monkeypatch):
    monkeypatch.setenv("NSEQL_SANDBOX_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("NSEQL_SANDBOX_TOKEN", TOKEN)
    monkeypatch.setattr(sandbox_remote, "_transport", None)
    monkeypatch.setattr(sandbox_remote, "_status", None)
    ok, detail = sandbox.isolation_status(refresh=True)
    assert not ok and "unreachable" in detail
    out = sandbox.run_jobs(STRAT, JOB, Path("/x"))
    assert out["ok"] is False and "NOT executed" in out["error"]

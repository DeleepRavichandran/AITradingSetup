"""Guards that let the platform start on Windows (checked on Linux by simulating the platform)."""
import sys
import tomllib
from pathlib import Path

from nseql.research import sandbox

ROOT = Path(__file__).resolve().parents[2]


def test_sandbox_fails_closed_with_a_clear_reason_off_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    try:
        ok, detail = sandbox.isolation_status(refresh=True)
        assert not ok and "win32" in detail and "Docker Desktop" in detail
        src = "from nseql.strategies.api import Strategy\n\n\nclass S(Strategy):\n    name = 's'\n\n    def on_bar(self, ctx):\n        pass\n"
        res = sandbox.run_jobs(src, [], ROOT)
        assert res["ok"] is False and "NOT executed" in res["error"]
    finally:
        monkeypatch.undo()
        sandbox._ISOLATION = None                 # re-probe on the real platform next time


def test_windows_gets_the_time_zone_database():
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    assert any(d.replace(" ", "").startswith("tzdata") and "win32" in d for d in deps)


def test_windows_scripts_never_load_the_approval_secret_or_enable_live_trading():
    env = (ROOT / "scripts" / "windows" / "env.ps1").read_text()
    assert '"HUMAN_APPROVAL_SECRET", "LIVE_TRADING_ENABLED"' in env and '$env:LIVE_TRADING_ENABLED = "false"' in env
    for f in (ROOT / "scripts" / "windows").glob("*.ps1"):
        text = f.read_text().lower()
        assert "live_trading_enabled=true" not in text.replace(" ", "") and "approve_strategy" not in text, f.name


def test_research_refuses_up_front_without_a_sandbox_and_records_nothing(data_dir, tmp_path, monkeypatch):
    """On a machine without the sandbox (e.g. native Windows) research must not record 'rejected' hypotheses:
    nothing was tested, and the learning agent would learn from failures that never happened."""
    import pytest as _pt
    from fastapi.testclient import TestClient

    from nseql import config
    from nseql.research import loop as L
    from nseql.research.registry import Registry
    monkeypatch.setattr(sandbox, "isolation_status", lambda refresh=False: (False, "Linux namespaces are not available on win32"))
    reg = Registry(tmp_path / "r.sqlite")
    with _pt.raises(L.SandboxUnavailable, match="not available on this machine"):
        L.ResearchLoop(data_dir, tmp_path, reg, dataset_id="synthetic_v1").run()
    assert reg.hypotheses(100) == [] and reg.experiments(100) == []
    tok = "sandbox-refusal-test-operator-token-1"
    for k, v in {"NSEQL_DATA_DIR": str(data_dir), "NSEQL_EXPERIMENTS_DIR": str(tmp_path / "exp"), "NSEQL_AUTO_FIXTURE": "false",
                 "NSEQL_API_TOKEN": tok}.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()
    from nseql.api.main import create_app
    c = TestClient(create_app(), headers={"Authorization": f"Bearer {tok}"})
    r = c.post("/api/research/run", json={})
    assert r.status_code == 409 and "Docker Desktop" in r.json()["detail"] and "still work" in r.json()["detail"]
    config.get_settings.cache_clear()

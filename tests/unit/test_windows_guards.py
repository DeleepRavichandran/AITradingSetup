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

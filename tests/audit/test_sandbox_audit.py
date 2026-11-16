"""Audit item 1: untrusted generated code must not run without network AND filesystem isolation."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nseql.research import sandbox

STRAT = "from nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n        pass\n"
JOB = [{"id": 0, "config": {"strategies": [{"name": "g"}], "end": "2022-03-31"}}]

PROBE = r'''
import json, os, socket, sys
out = {"uid": os.getuid(), "secret": None, "repo_env": None, "net": None, "write_data": None, "write_root": None, "ps": None}
def read(p):
    try:
        return open(p).read()
    except Exception as e:
        return "DENIED:" + type(e).__name__
out["secret"] = read(sys.argv[1])
out["repo_env"] = read(sys.argv[2])
out["home_listing"] = sorted(os.listdir("/home")) if os.path.isdir("/home") else "absent"
try:
    socket.create_connection(("1.1.1.1", 53), timeout=2); out["net"] = "open"
except Exception as e:
    out["net"] = "blocked"
try:
    open(os.path.join(sys.argv[3], "x"), "w").write("x"); out["write_data"] = "allowed"
except Exception as e:
    out["write_data"] = "DENIED:" + type(e).__name__
try:
    open("/usr/pwned", "w").write("x"); out["write_root"] = "allowed"
except Exception as e:
    out["write_root"] = "DENIED:" + type(e).__name__
out["pids"] = len([p for p in os.listdir("/proc") if p.isdigit()])
import nseql.indicators
open("results.json", "w").write(json.dumps(out))
'''


def run_probe(tmp_path, data_dir):
    secret = tmp_path / "host_secret.txt"
    secret.write_text("TOP-SECRET-BROKER-TOKEN")
    repo_env = Path(sandbox.__file__).resolve().parents[2] / ".env"
    created = not repo_env.exists()
    if created:
        repo_env.write_text("KITE_API_SECRET=leak-me\n")
    work = tmp_path / "work"
    work.mkdir()
    (work / "probe.py").write_text(PROBE)
    sandbox.prepare_workdir(work)
    try:
        cmd = sandbox.isolated_command([sys.executable, "-I", "probe.py", str(secret), str(repo_env), str(data_dir)], work,
                                       read_only=[Path(data_dir)], limits=sandbox.SandboxLimits())
        p = subprocess.run(cmd, cwd=work, env=sandbox.child_env(work), capture_output=True, text=True, timeout=120,
                           preexec_fn=sandbox._preexec(sandbox.SandboxLimits()))
        assert p.returncode == 0, p.stderr[-2000:]
        return json.loads((work / "results.json").read_text())
    finally:
        if created:
            repo_env.unlink()


def test_untrusted_code_cannot_read_host_files_reach_the_network_or_write_outside_its_workdir(tmp_path, data_dir):
    out = run_probe(tmp_path, data_dir)
    assert out["secret"].startswith("DENIED"), "a file elsewhere on the host was readable from the sandbox"
    assert out["repo_env"].startswith("DENIED"), "the repository's .env was readable from the sandbox"
    assert out["net"] == "blocked"
    assert out["write_data"].startswith("DENIED"), "datasets must be read-only inside the sandbox"
    assert out["write_root"].startswith("DENIED")
    assert out["pids"] <= 3, "host processes are visible from the sandbox"


def test_sandbox_drops_root_when_the_launcher_is_root(tmp_path, data_dir):
    if os.getuid() != 0:
        pytest.skip("launcher is not root; the child runs as the unprivileged caller inside a user namespace")
    assert run_probe(tmp_path, data_dir)["uid"] != 0


def test_no_isolation_means_no_execution(monkeypatch, data_dir):
    """Fail closed: if isolation cannot be established, generated code must not run at all."""
    monkeypatch.setattr(sandbox, "_ISOLATION", (False, "simulated: namespaces unavailable"))
    out = sandbox.run_jobs(STRAT, JOB, data_dir)
    assert out["ok"] is False and out["results"] == [] and "isolation" in out["error"].lower()
    assert out["isolation"]["network_namespace"] is False and out["isolation"]["filesystem"] is False


def test_isolated_run_still_works_and_reports_its_isolation(data_dir, tmp_path):
    out = sandbox.run_jobs(STRAT, [{**JOB[0], "save": True}], data_dir, out_dir=tmp_path / "runs")
    assert out["ok"], out["error"]
    iso = out["isolation"]
    assert iso["network_namespace"] and iso["filesystem"] and iso["pid_namespace"] and iso["env_scrubbed"]
    assert Path(out["results"][0]["path"], "summary.json").exists()      # the one writable output location works

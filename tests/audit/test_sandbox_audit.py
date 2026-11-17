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

ROOT_ONLY_FALLBACK = ("the chroot fallback is only needed, and only possible, when the launcher is root inside a container; "
                      "an unprivileged launcher uses pivot_root in its own user namespace")

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


def run_probe(tmp_path, data_dir, no_pivot=False):
    secret = tmp_path / "host_secret.txt"
    secret.write_text("TOP-SECRET-BROKER-TOKEN")
    repo_env = Path(sandbox.__file__).resolve().parents[2] / ".env"
    created = False
    try:
        if not repo_env.exists():
            repo_env.write_text("KITE_API_SECRET=leak-me\n")
            created = True
    except PermissionError:          # repository not writable by this user: probe an equivalent host file instead
        repo_env = secret
    work = tmp_path / "work"
    work.mkdir()
    (work / "probe.py").write_text(PROBE)
    sandbox.prepare_workdir(work)
    try:
        cmd = sandbox.isolated_command([sys.executable, "-I", "probe.py", str(secret), str(repo_env), str(data_dir)], work,
                                       read_only=[Path(data_dir)], limits=sandbox.SandboxLimits(), no_pivot=no_pivot)
        p = subprocess.run(cmd, cwd=work, env=sandbox.child_env(work), capture_output=True, text=True, timeout=120,
                           preexec_fn=sandbox._preexec(sandbox.SandboxLimits()))
        assert p.returncode == 0, p.stderr[-2000:]
        return json.loads((work / "results.json").read_text())
    finally:
        if created:
            repo_env.unlink()


@pytest.mark.parametrize("no_pivot", [False, True], ids=["pivot_root", "chroot-fallback (containers)"])
def test_untrusted_code_cannot_read_host_files_reach_the_network_or_write_outside_its_workdir(tmp_path, data_dir, no_pivot):
    if no_pivot and os.getuid() != 0:
        pytest.skip(ROOT_ONLY_FALLBACK)
    out = run_probe(tmp_path, data_dir, no_pivot)
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


def test_chroot_fallback_cannot_be_escaped_by_the_unprivileged_child(tmp_path, data_dir):
    """The classic chroot escape (chroot into a subdirectory, then walk up) needs CAP_SYS_CHROOT."""
    if os.getuid() != 0:
        pytest.skip(ROOT_ONLY_FALLBACK)
    work = tmp_path / "work"
    work.mkdir()
    (work / "esc.py").write_text(
        "import os, json\nout = {}\n"
        "try:\n    os.mkdir('j'); os.chroot('j'); out['chroot'] = 'allowed'\nexcept Exception as e:\n    out['chroot'] = type(e).__name__\n"
        "try:\n    os.setuid(0); out['setuid0'] = 'allowed'\nexcept Exception as e:\n    out['setuid0'] = type(e).__name__\n"
        "out['caps'] = [l.split()[1] for l in open('/proc/self/status') if l.startswith(('CapEff', 'CapPrm', 'CapBnd'))]\n"
        "out['nnp'] = [l.split()[1] for l in open('/proc/self/status') if l.startswith('NoNewPrivs')]\n"
        "open('results.json', 'w').write(json.dumps(out))\n")
    sandbox.prepare_workdir(work)
    cmd = sandbox.isolated_command([sys.executable, "-I", "esc.py"], work, no_pivot=True)
    p = subprocess.run(cmd, cwd=work, env=sandbox.child_env(work), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-800:]
    out = json.loads((work / "results.json").read_text())
    assert out["chroot"] == "PermissionError" and out["caps"] == ["0000000000000000"] * 3 and out["nnp"] == ["1"]
    if os.getuid() == 0:
        assert out["setuid0"] == "PermissionError"

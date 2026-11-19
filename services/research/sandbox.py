"""Isolation for AI-generated strategy code.

Two independent layers:

1. Static validation (`validate_source`): an AST whitelist. Generated code may import only
   math / numpy / nseql.indicators / nseql.strategies.api, may not touch any attribute or name
   beginning with an underscore (which blocks dunder escapes and engine internals such as
   ctx._e), and may not use eval/exec/open/getattr/import machinery or global state.

2. Process isolation (`run_jobs`), MANDATORY — if it cannot be established the code is not run:
   - new network namespace with no interfaces (no network at all);
   - new mount namespace pivoted into a private root that contains only: the Python runtime and
     system libraries (read-only), the research packages the child needs (read-only), the
     dataset directory (read-only) and one throw-away working directory (writable). The rest
     of the host filesystem — home directories, the repository, .env files, the experiment
     registry, approval files — does not exist inside it;
   - new PID / IPC / UTS namespaces (no view of host processes);
   - all capabilities dropped, no_new_privs set, and uid/gid 65534 when the launcher is root;
   - environment stripped to PATH/HOME; CPU, memory, file-size and open-file rlimits; a
     wall-clock timeout.
   The execution/broker, API, agent and notifier packages are not mounted in the sandbox, and
   `nseql.execution` additionally refuses to import when NSEQL_SANDBOX=1.

There is deliberately no "run without isolation" switch. On a host where namespaces are not
available (Windows, macOS, an unprivileged container) generated code cannot be executed; run
the research service on Linux or in the provided container.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ALLOWED_IMPORTS = {"math", "numpy", "nseql.indicators", "nseql.strategies.api", "nseql"}
ALLOWED_FROM = {"nseql": {"indicators"}, "nseql.strategies.api": {"Strategy"}, "nseql.indicators": None, "math": None,
                "numpy": None}
FORBIDDEN_NAMES = {"eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr", "setattr",
                   "delattr", "input", "breakpoint", "memoryview", "exit", "quit", "help", "dir", "type", "object",
                   "super", "classmethod", "staticmethod", "property", "print"}
FORBIDDEN_NODES = (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith, ast.With,
                   ast.Try, ast.Raise, ast.Lambda, ast.Delete, ast.Yield, ast.YieldFrom, ast.Match)
MAX_SOURCE_BYTES = 40_000


@dataclass
class ValidationReport:
    ok: bool
    violations: list[str] = field(default_factory=list)
    class_name: str | None = None
    sha256: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "violations": self.violations, "class_name": self.class_name, "sha256": self.sha256}


def validate_source(source: str) -> ValidationReport:
    v: list[str] = []
    sha = hashlib.sha256(source.encode()).hexdigest()
    if len(source.encode()) > MAX_SOURCE_BYTES:
        return ValidationReport(False, ["source too large"], None, sha)
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return ValidationReport(False, [f"syntax error line {e.lineno}: {e.msg}"], None, sha)
    classes = []
    for node in ast.walk(tree):
        ln = getattr(node, "lineno", "?")
        if isinstance(node, FORBIDDEN_NODES):
            v.append(f"line {ln}: {type(node).__name__} statements are not allowed")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS:
                    v.append(f"line {ln}: import of '{a.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            allowed = ALLOWED_FROM.get(node.module or "", "missing")
            if node.level or allowed == "missing":
                v.append(f"line {ln}: import from '{node.module}' is not allowed")
            elif allowed is not None:
                for a in node.names:
                    if a.name not in allowed:
                        v.append(f"line {ln}: '{a.name}' may not be imported from '{node.module}'")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            v.append(f"line {ln}: access to private attribute '{node.attr}' is not allowed")
        elif isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or node.id.startswith("__")):
            v.append(f"line {ln}: use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
            v.append(f"line {ln}: string constants containing '__' are not allowed")
        elif isinstance(node, ast.While):
            v.append(f"line {ln}: while loops are not allowed (use bounded for loops)")
        elif isinstance(node, ast.ClassDef):
            classes.append(node)
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef, ast.Assign, ast.Expr)):
            v.append(f"line {node.lineno}: top-level {type(node).__name__} is not allowed")
        if isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant):
            v.append(f"line {node.lineno}: top-level executable expressions are not allowed")
    strat = [c for c in classes if any(isinstance(b, ast.Name) and b.id == "Strategy" for b in c.bases)]
    if len(strat) != 1 or len(classes) != 1:
        v.append("module must define exactly one class, deriving from Strategy")
    else:
        if any(c.decorator_list or c.keywords for c in classes):
            v.append("class decorators / metaclasses are not allowed")
        methods = {n.name for n in strat[0].body if isinstance(n, ast.FunctionDef)}
        if "on_bar" not in methods:
            v.append("strategy must implement on_bar(self, ctx)")
        if any(m.startswith("__") for m in methods):
            v.append("dunder methods may not be defined")
    return ValidationReport(not v, v, strat[0].name if len(strat) == 1 else None, sha)


def load_strategy_class(source: str):
    """Compile validated source into a class. Only ever called inside the sandbox child."""
    rep = validate_source(source)
    if not rep.ok:
        raise PermissionError("generated code failed validation: " + "; ".join(rep.violations[:5]))
    ns: dict = {"__name__": "nseql_generated"}
    exec(compile(source, "<generated>", "exec"), ns)  # noqa: S102 - validated, sandboxed child only
    cls = ns[rep.class_name]
    cls.__nseql_source__ = source
    return cls


# ----------------------------------------------------------------------------- process isolation
@dataclass
class SandboxLimits:
    cpu_seconds: int = 120
    memory_mb: int = 2048
    wall_seconds: int = 180
    max_output_mb: int = 256


SANDBOX_UID = 65534        # "nobody": used when the launcher itself is root
SYSTEM_READ_ONLY = ("/usr", "/lib", "/lib64", "/lib32", "/bin", "/sbin", "/etc/ld.so.cache", "/etc/ld.so.conf",
                    "/etc/ld.so.conf.d", "/etc/alternatives")
CHILD_PACKAGES = ("nseql", "nseql.indicators", "nseql.instruments", "nseql.analytics", "nseql.contracts", "nseql.strategies",
                  "nseql.data", "nseql.backtester", "nseql.risk", "nseql.research")

# Runs as root *inside* fresh namespaces. $1 = private root, $2 = working directory, rest = command.
_LAUNCHER = r"""
set -eu
R="$1"; W="$2"; shift 2
mount --make-rprivate /
mount -t tmpfs -o size=16m,mode=755,nosuid,nodev tmpfs "$R"
mkdir -p "$R/tmp" "$R/proc" "$R/dev"
mount -t tmpfs -o size=64m,mode=1777,nosuid,nodev tmpfs "$R/tmp"
place() { if [ -d "$1" ]; then mkdir -p "$R$1"; else mkdir -p "$R$(dirname "$1")"; : > "$R$1"; fi; }
ro() { [ -e "$1" ] || return 0; place "$1"; mount --bind "$1" "$R$1"; mount -o remount,bind,ro,nosuid,nodev "$R$1"; }
OLDIFS="$IFS"; IFS=:
for p in $NSEQL_SBX_RO; do ro "$p"; done
IFS="$OLDIFS"
mkdir -p "$R$W"
mount --bind "$W" "$R$W"; mount -o remount,bind,nosuid,nodev "$R$W"
mount -t proc -o nosuid,nodev,noexec proc "$R/proc"
for d in null zero urandom random; do : > "$R/dev/$d"; mount --bind "/dev/$d" "$R/dev/$d"; done
cd "$R"; mkdir -p .old
DROP="setpriv $NSEQL_SBX_SETPRIV --no-new-privs --bounding-set=-all --inh-caps=-all --"
if [ -z "${NSEQL_SBX_NO_PIVOT:-}" ] && pivot_root . .old 2>/dev/null; then
  umount -l /.old; rmdir /.old
  mount -o remount,bind,ro,nosuid,nodev /
  cd "$W"
  exec $DROP "$@"
fi
# pivot_root is refused when the current root is not a mount point (inside most containers). Fall back
# to moving the private root over "/" and chroot-ing into it. The old tree is then unreachable by path,
# and the process that follows has no capabilities (so no CAP_SYS_CHROOT) and cannot gain any.
rmdir .old
# (per-mount read-only flag: remounting the tmpfs superblock itself is refused inside a user namespace)
mount -o remount,bind,ro,nosuid,nodev "$R"
mount --move "$R" /
exec chroot . $DROP sh -c 'cd "$0" && exec "$@"' "$W" "$@"
"""

_ISOLATION: tuple[bool, str] | None = None


def _package_dirs() -> list[str]:
    import importlib
    out = []
    for name in CHILD_PACKAGES:
        for p in importlib.import_module(name).__path__:
            out.append(str(Path(p).resolve()))
    return out


def child_env(work: Path) -> dict:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(work), "NSEQL_SANDBOX": "1", "LIVE_TRADING_ENABLED": "false",
            "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MPLCONFIGDIR": "/tmp"}


def isolated_command(argv: list[str], work: Path, *, read_only: list[Path] | None = None,
                     limits: SandboxLimits | None = None, no_pivot: bool = False) -> list[str]:
    """Wrap `argv` so that it runs with no network, a private read-only filesystem view, no view of
    host processes and no privileges. `work` is the only writable location. Requires util-linux
    (unshare, mount, pivot_root, setpriv) and either root or unprivileged user namespaces."""
    tools = {t: shutil.which(t) for t in ("unshare", "sh", "mount", "pivot_root", "chroot", "setpriv")}
    missing = [t for t, path in tools.items() if not path]
    if missing:
        raise OSError(f"sandbox tools not found: {', '.join(missing)}")
    root = os.getuid() == 0
    ro = [*SYSTEM_READ_ONLY, sys.prefix, sys.base_prefix, *_package_dirs(), *(str(Path(p).resolve()) for p in (read_only or []))]
    kept: list[str] = []
    for p in sorted(dict.fromkeys(os.path.realpath(x) if not x.startswith(("/lib", "/bin", "/sbin")) else x for x in ro), key=len):
        if not any(p == k or p.startswith(k.rstrip("/") + "/") for k in kept):
            kept.append(p)
    newroot = Path(work).resolve().parent / (Path(work).name + ".root")
    newroot.mkdir(exist_ok=True)
    setpriv = f"--reuid={SANDBOX_UID} --regid={SANDBOX_UID} --clear-groups" if root else ""
    ns = ["--net", "--mount", "--pid", "--fork", "--kill-child", "--ipc", "--uts"]
    if not root:
        ns = ["--user", "--map-root-user", *ns]
    return [tools["unshare"], *ns, "--", "env", "-i", f"NSEQL_SBX_RO={':'.join(kept)}", f"NSEQL_SBX_SETPRIV={setpriv}", *(["NSEQL_SBX_NO_PIVOT=1"] if no_pivot else []),
            "PATH=/usr/sbin:/usr/bin:/sbin:/bin", *(f"{k}={v}" for k, v in child_env(Path(work).resolve()).items() if k != "PATH"),
            tools["sh"], "-c", _LAUNCHER, "nseql-sandbox", str(newroot), str(Path(work).resolve()), *argv]


def prepare_workdir(work: Path) -> None:
    os.chmod(work, 0o700)
    if os.getuid() == 0:
        os.chown(work, SANDBOX_UID, SANDBOX_UID)


def isolation_status(refresh: bool = False) -> tuple[bool, str]:
    """(available, detail). Probes once by actually running a no-op inside the full sandbox."""
    global _ISOLATION
    if not sys.platform.startswith("linux"):
        _ISOLATION = (False, f"Linux namespaces are not available on {sys.platform}; generated code cannot be run here. "
                             "Run the platform in Docker Desktop (WSL2) to enable it.")
        return _ISOLATION
    if _ISOLATION is None or refresh:
        try:
            with tempfile.TemporaryDirectory(prefix="nseql_sbx_probe_") as tmp:
                work = Path(tmp) / "w"
                work.mkdir()
                prepare_workdir(work)
                p = subprocess.run(isolated_command([sys.executable, "-I", "-c", "import numpy"], work), capture_output=True,
                                   text=True, timeout=60, cwd=work)
                ok = p.returncode == 0
                _ISOLATION = (ok, "network, filesystem and PID namespaces active" if ok
                              else f"could not establish namespaces: {(p.stderr or '').strip().splitlines()[-1:] or ['unknown error']}")
        except Exception as e:
            _ISOLATION = (False, f"could not establish namespaces: {type(e).__name__}: {e}")
    return _ISOLATION


def network_isolation_available() -> bool:
    return isolation_status()[0]


def _preexec(limits: SandboxLimits):
    def fn():
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
        mem = limits.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        out = limits.max_output_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (out, out))
        resource.setrlimit(resource.RLIMIT_NOFILE, (512, 512))
        os.setsid()
    return fn


def run_jobs(source: str, jobs: list[dict], data_dir: Path, *, limits: SandboxLimits | None = None,
             out_dir: Path | None = None) -> dict:
    """Run backtest / audit jobs for one piece of generated code in an isolated child process.

    Returns {"ok": bool, "results": [...], "error": str|None, "isolation": {...}}. Never raises
    for failures inside the child: a crash, timeout or limit breach is reported as ok=False.
    Fails closed: without working isolation nothing is executed.
    """
    limits = limits or SandboxLimits()
    rep = validate_source(source)
    available, detail = isolation_status()
    iso = {"static_validation": rep.ok, "network_namespace": available, "filesystem": available, "pid_namespace": available,
           "privileges_dropped": available, "env_scrubbed": True, "detail": detail,
           "cpu_seconds": limits.cpu_seconds, "memory_mb": limits.memory_mb, "wall_seconds": limits.wall_seconds}
    if not rep.ok:
        return {"ok": False, "results": [], "error": "static validation failed: " + "; ".join(rep.violations), "isolation": iso}
    if not available:
        return {"ok": False, "results": [], "isolation": iso,
                "error": f"sandbox isolation is unavailable on this host, so generated code was NOT executed ({detail})"}
    data_dir = Path(data_dir).resolve()
    with tempfile.TemporaryDirectory(prefix="nseql_sbx_") as tmp:
        work = Path(tmp) / "w"
        (work / "out").mkdir(parents=True)
        (work / "strategy_source.txt").write_text(source)
        (work / "jobs.json").write_text(json.dumps({"jobs": jobs, "data_dir": str(data_dir), "out_dir": str(work / "out")}))
        prepare_workdir(work)
        if os.getuid() == 0:
            os.chown(work / "out", SANDBOX_UID, SANDBOX_UID)
        cmd = isolated_command([sys.executable, "-I", "-m", "nseql.research.sandbox_child", str(work)], work, read_only=[data_dir], limits=limits)
        try:
            proc = subprocess.run(cmd, cwd=work, env=child_env(work), capture_output=True, text=True, timeout=limits.wall_seconds,
                                  preexec_fn=_preexec(limits))
        except subprocess.TimeoutExpired:
            return {"ok": False, "results": [], "error": f"wall-clock limit of {limits.wall_seconds}s exceeded", "isolation": iso}
        res_file = work / "results.json"
        if proc.returncode != 0 or not res_file.exists():
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            return {"ok": False, "results": [], "isolation": iso,
                    "error": f"sandbox child exited with code {proc.returncode}: {' | '.join(tail)[:600]}"}
        out = json.loads(res_file.read_text())
        for r in out.get("results", []):       # artefacts are moved out by the trusted parent, never written by the child
            staged = r.get("path")
            if staged and out_dir is not None and Path(staged).parent == work / "out" and Path(staged).is_dir():
                dest = Path(out_dir) / Path(staged).name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(staged, dest, dirs_exist_ok=True)
                r["path"] = str(dest)
            elif staged:
                r["path"] = None
        out["isolation"] = iso
        return out

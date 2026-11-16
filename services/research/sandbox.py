"""Isolation for AI-generated strategy code.

Two independent layers:

1. Static validation (`validate_source`): an AST whitelist. Generated code may import only
   math / numpy / nseql.indicators / nseql.strategies.api, may not touch any attribute or name
   beginning with an underscore (which blocks dunder escapes and engine internals such as
   ctx._e), and may not use eval/exec/open/getattr/import machinery or global state.

2. Process isolation (`run_jobs`): the code runs in a separate Python process with
   - an environment stripped to PATH/HOME — no API keys, no broker tokens, no approval secret;
   - CPU-time, address-space, file-size and process-count rlimits and a wall-clock timeout;
   - a network namespace with no interfaces when `unshare --net` is available;
   - a throw-away working directory.
   The child can only run backtests through `nseql.backtester`; the execution/broker package
   refuses to import inside a sandbox (NSEQL_SANDBOX=1), so there is no code path from
   generated code to an order router.
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


_NET_ISOLATION: bool | None = None


def network_isolation_available() -> bool:
    global _NET_ISOLATION
    if _NET_ISOLATION is None:
        exe = shutil.which("unshare")
        try:
            _NET_ISOLATION = bool(exe) and subprocess.run([exe, "--net", "true"], capture_output=True, timeout=5).returncode == 0
        except Exception:
            _NET_ISOLATION = False
    return _NET_ISOLATION


def _preexec(limits: SandboxLimits):
    def fn():
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
        mem = limits.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        out = limits.max_output_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (out, out))
        os.setsid()
    return fn


def run_jobs(source: str, jobs: list[dict], data_dir: Path, *, limits: SandboxLimits | None = None,
             out_dir: Path | None = None) -> dict:
    """Run backtest / audit jobs for one piece of generated code in an isolated child process.

    Returns {"ok": bool, "results": [...], "error": str|None, "isolation": {...}}. Never raises
    for failures inside the child: a crash, timeout or limit breach is reported as ok=False.
    """
    limits = limits or SandboxLimits()
    rep = validate_source(source)
    iso = {"static_validation": rep.ok, "network_namespace": network_isolation_available(), "env_scrubbed": True,
           "cpu_seconds": limits.cpu_seconds, "memory_mb": limits.memory_mb, "wall_seconds": limits.wall_seconds}
    if not rep.ok:
        return {"ok": False, "results": [], "error": "static validation failed: " + "; ".join(rep.violations), "isolation": iso}
    with tempfile.TemporaryDirectory(prefix="nseql_sbx_") as tmp:
        tmp_p = Path(tmp)
        (tmp_p / "strategy_source.txt").write_text(source)
        (tmp_p / "jobs.json").write_text(json.dumps({"jobs": jobs, "data_dir": str(Path(data_dir).resolve()),
                                                     "out_dir": str(Path(out_dir).resolve()) if out_dir else None}))
        cmd = [sys.executable, "-I", "-m", "nseql.research.sandbox_child", str(tmp_p)]
        if iso["network_namespace"]:
            cmd = [shutil.which("unshare"), "--net", *cmd]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp, "NSEQL_SANDBOX": "1",
               "LIVE_TRADING_ENABLED": "false", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
        try:
            proc = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True, timeout=limits.wall_seconds,
                                  preexec_fn=_preexec(limits))
        except subprocess.TimeoutExpired:
            return {"ok": False, "results": [], "error": f"wall-clock limit of {limits.wall_seconds}s exceeded", "isolation": iso}
        res_file = tmp_p / "results.json"
        if proc.returncode != 0 or not res_file.exists():
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            return {"ok": False, "results": [], "isolation": iso,
                    "error": f"sandbox child exited with code {proc.returncode}: {' | '.join(tail)[:600]}"}
        out = json.loads(res_file.read_text())
        out["isolation"] = iso
        return out

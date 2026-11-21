"""Client for the separate sandbox worker (services/research/sandbox_service.py).

When NSEQL_SANDBOX_URL is set, generated strategy code is never executed in the API process: it is sent
to the worker, which is the only container with the privileges needed to build namespaces. Requests carry
NSEQL_SANDBOX_TOKEN. If the worker cannot be reached, research fails closed (nothing runs).

Artefacts (saved backtest results) come back through a volume both containers mount at
NSEQL_SANDBOX_ARTIFACTS; the worker returns paths relative to it and this client copies them to the
requested output directory. A path that would leave that directory is ignored.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import asdict
from pathlib import Path

import httpx

URL_ENV, TOKEN_ENV, ARTIFACTS_ENV = "NSEQL_SANDBOX_URL", "NSEQL_SANDBOX_TOKEN", "NSEQL_SANDBOX_ARTIFACTS"
_transport: httpx.BaseTransport | None = None          # tests inject the worker app here
_status: tuple[float, tuple[bool, str]] | None = None


def configured() -> bool:
    return bool(os.environ.get(URL_ENV))


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(base_url=os.environ[URL_ENV], transport=_transport, timeout=timeout,
                        headers={"Authorization": f"Bearer {os.environ.get(TOKEN_ENV, '')}"})


def status(refresh: bool = False) -> tuple[bool, str]:
    global _status
    if _status and not refresh and _status[0] > time.time() - 30:
        return _status[1]
    try:
        with _client(10) as c:
            r = c.get("/status")
        if r.status_code != 200:
            v = (False, f"sandbox worker refused the status request ({r.status_code}); check NSEQL_SANDBOX_TOKEN")
        else:
            d = r.json()
            v = (bool(d["available"]), f"remote sandbox worker: {d['detail']}")
    except httpx.HTTPError as e:
        v = (False, f"sandbox worker unreachable at {os.environ.get(URL_ENV)} ({type(e).__name__}); research is disabled")
    _status = (time.time(), v)
    return v


def run(source: str, jobs: list[dict], limits, out_dir: Path | None) -> dict:
    iso_fail = {"static_validation": True, "network_namespace": False, "filesystem": False, "remote_worker": True}
    try:
        with _client(limits.wall_seconds + 60) as c:
            r = c.post("/run", json={"source": source, "jobs": jobs, "limits": asdict(limits), "save": out_dir is not None})
    except httpx.HTTPError as e:
        return {"ok": False, "results": [], "isolation": iso_fail,
                "error": f"sandbox worker unreachable ({type(e).__name__}); generated code was NOT executed"}
    if r.status_code != 200:
        return {"ok": False, "results": [], "isolation": iso_fail, "error": f"sandbox worker refused the request ({r.status_code}): {r.text[:200]}"}
    out = r.json()
    root = Path(os.environ.get(ARTIFACTS_ENV, "/nonexistent")).resolve()
    for res in out.get("results", []):
        rel = res.pop("artifact", None)
        res["path"] = None
        if not rel or out_dir is None:
            continue
        src = (root / rel).resolve()
        if root not in src.parents or not src.is_dir():
            continue
        dest = Path(out_dir) / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, dirs_exist_ok=True)
        shutil.rmtree(src, ignore_errors=True)
        if src.parent != root:
            try:
                src.parent.rmdir()                       # the per-request staging directory, once empty
            except OSError:
                pass
        res["path"] = str(dest)
    out.setdefault("isolation", {})["remote_worker"] = True
    return out

"""Sandbox worker: the only service that executes AI-generated strategy code.

It runs in its own container with the privileges needed to build namespaces (CAP_SYS_ADMIN), so the API
container can run as an unprivileged user with every capability dropped. The worker:
  * holds no broker, LLM or notification credentials and cannot reach the kill switch, ledgers or orders;
  * sits on an internal network with no route to the internet; only the API can reach it;
  * mounts the datasets read-only and uses ITS OWN data directory (paths from the caller are ignored);
  * caps resource limits at its own maximum (a caller cannot raise them);
  * requires NSEQL_SANDBOX_TOKEN (>= 24 characters) on every request.
Each job still runs inside the namespace sandbox (no network, private read-only filesystem, uid 65534,
no capabilities), exactly as before.

    uvicorn nseql.research.sandbox_service:app_factory --factory --host 0.0.0.0 --port 8100
"""
from __future__ import annotations

import hmac
import os
import uuid
from dataclasses import asdict, fields
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from nseql.research import sandbox
from pydantic import BaseModel, Field


class RunIn(BaseModel):
    source: str = Field(max_length=sandbox.MAX_SOURCE_BYTES)
    jobs: list[dict] = Field(max_length=500)
    limits: dict = {}
    save: bool = False


def create_app() -> FastAPI:
    token = os.environ.get("NSEQL_SANDBOX_TOKEN", "")
    if len(token) < 24:
        raise RuntimeError("NSEQL_SANDBOX_TOKEN must be set (at least 24 characters)")
    if os.environ.get("NSEQL_SANDBOX_URL"):
        raise RuntimeError("the sandbox worker must not itself delegate (unset NSEQL_SANDBOX_URL)")
    data_dir = Path(os.environ.get("NSEQL_DATA_DIR", "/data/datasets")).resolve()
    artifacts = Path(os.environ.get("NSEQL_SANDBOX_ARTIFACTS", "/data/artifacts")).resolve()
    ceiling = sandbox.SandboxLimits()
    app = FastAPI(title="NSE AI Quant Lab sandbox worker", docs_url=None, redoc_url=None, openapi_url=None)

    def auth(request: Request):
        got = request.headers.get("authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "invalid sandbox token")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/status", dependencies=[Depends(auth)])
    def status():
        ok, detail = sandbox.local_isolation_status()
        return {"available": ok, "detail": detail, "limits": asdict(ceiling)}

    @app.post("/run", dependencies=[Depends(auth)])
    def run(body: RunIn):
        names = {f.name for f in fields(sandbox.SandboxLimits)}
        lim = {k: min(int(v), getattr(ceiling, k)) for k, v in body.limits.items() if k in names and int(v) > 0}
        limits = sandbox.SandboxLimits(**{**asdict(ceiling), **lim})
        out_dir = artifacts / uuid.uuid4().hex if body.save else None
        out = sandbox.run_jobs_local(body.source, body.jobs, data_dir, limits=limits, out_dir=out_dir)
        owner = os.environ.get("NSEQL_SANDBOX_ARTIFACTS_UID")
        if owner and out_dir is not None and out_dir.exists() and os.getuid() == 0:   # hand artefacts to the unprivileged API user
            for p in [out_dir, *out_dir.rglob("*")]:
                os.chown(p, int(owner), int(owner))
        for r in out.get("results", []):                  # never hand back absolute paths of this container
            p = r.pop("path", None)
            if p and out_dir is not None and Path(p).resolve().parent == out_dir.resolve():
                r["artifact"] = str(Path(p).resolve().relative_to(artifacts))
        return out

    return app


def app_factory() -> FastAPI:
    return create_app()

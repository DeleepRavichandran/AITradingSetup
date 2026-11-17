"""HTTP API for the dashboard. Every page of the web app reads from these endpoints; there is
no static demo data anywhere in the UI.

Auth is mandatory. The service refuses to start without NSEQL_API_TOKEN (>= 24 characters); every
/api route except the bare liveness probe requires `Authorization: Bearer <token>`.
  operator  NSEQL_API_TOKEN      everything
  viewer    NSEQL_VIEWER_TOKEN   optional, read-only (GET); writes are refused and audited
The only unauthenticated mode is NSEQL_ALLOW_INSECURE_LOCAL=true, which answers loopback
clients only (and refuses requests forwarded from elsewhere) — for one person on one machine.
"""

import hmac
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from nseql import __version__
from nseql.agents import analyst, llm as llm_mod
from nseql.backtester.engine import Allocation
from nseql.backtester.runner import BacktestConfig, StrategyRef, code_version, run_backtest
from nseql.config import get_settings
from nseql.contracts.artifacts import AGENTS
from nseql.data.events import EventStore
from nseql.data.fixtures import FIXTURE_ID, ensure_fixture
from nseql.data.quality import dataset_quality
from nseql.data.store import Dataset, list_datasets
from nseql.execution.brokers.stubs import AngelOneSmartAPI, Upstox
from nseql.execution.brokers.zerodha import ZerodhaKite
from nseql.execution.gate import COMPLIANCE_ITEMS, LiveGate
from nseql.instruments import structure
from nseql.notify.whatsapp import WhatsAppNotifier
from nseql.paper.feed import ReplayFeed
from nseql.paper.session import ManualBook, PaperSession
from nseql.research import sandbox, validation as V
from nseql.research.loop import ResearchBudget, ResearchLoop, get_partition
from nseql.research.registry import Registry
from nseql.risk import portfolio as pf
from nseql.risk.limits import KillSwitch, RiskLimits
from nseql.strategies import registry as strategy_registry

MIN_TOKEN_LENGTH = 24
LOOPBACK = {"127.0.0.1", "::1", "localhost"}

FEATURES = [
    ("Event-driven backtester (equities, futures, options, multi-leg)", "IMPLEMENTED", "tested on synthetic fixtures"),
    ("Dated NSE charge schedule", "IMPLEMENTED", "entries before 2023 are flagged unverified; see DATA_PROVENANCE.md"),
    ("Synthetic fixture dataset", "IMPLEMENTED", "explicitly labelled; not market data"),
    ("NSE bhavcopy / CSV import", "IMPLEMENTED", "parsers tested on layout samples; no real exchange file imported here"),
    ("Import of an authorised real NSE dataset", "BLOCKED", "needs files the operator is licensed to use"),
    ("Kite historical-data adapter", "UNVERIFIED", "mock-tested; needs credentials"),
    ("AI research loop (9 agents, sandboxed code)", "IMPLEMENTED", "LLM optional; deterministic agents by default"),
    ("LLM-backed inventor / analyst", "UNVERIFIED", "needs ANTHROPIC_API_KEY; stub-tested"),
    ("Walk-forward, bootstrap, DSR, sealed out-of-sample", "IMPLEMENTED", ""),
    ("Portfolio simulation on one shared ledger", "IMPLEMENTED", "library strategies only"),
    ("Paper trading on replayed data", "IMPLEMENTED", "replay, not a live market"),
    ("Authorised real-time streaming feed", "BLOCKED", "needs broker credentials and a data entitlement; adapter not written"),
    ("Live order routing", "DISABLED", "gate closed by default; Kite adapter mock-tested only"),
    ("Upstox / Angel One adapters", "NOT IMPLEMENTED", "interface stubs"),
    ("WhatsApp notifications", "UNVERIFIED", "mock-tested; needs WhatsApp Business Cloud API credentials"),
    ("News / event store", "IMPLEMENTED", "ingestion + point-in-time queries; ships with no content"),
    ("Currency / commodity / interest-rate derivatives", "NOT IMPLEMENTED", "instrument kinds exist; no adapters or fixtures"),
    ("Tick / sub-minute simulation", "NOT IMPLEMENTED", "bar-based only; 1-minute bars cannot reproduce tick-level scalping"),
    ("PostgreSQL / TimescaleDB / Redis / MLflow", "NOT IMPLEMENTED", "SQLite + Parquet are used; migration path in ARCHITECTURE.md"),
]


class State:
    def __init__(self):
        self.settings = get_settings()
        self.data_dir = self.settings.data_dir
        self.exp_dir = self.settings.experiments_dir
        self.exp_dir.mkdir(parents=True, exist_ok=True)
        self.registry = Registry(self.settings.registry_path)
        self.kill = KillSwitch()
        self.notifier = WhatsAppNotifier.from_env()
        self.gate = LiveGate(self.exp_dir / "approvals", self.exp_dir / "compliance.json", self.kill, self.settings)
        self.events = EventStore(self.exp_dir / "events.sqlite")
        self.jobs: dict[str, dict] = {}
        self.sessions: dict[str, PaperSession] = {}
        self.started = time.time()
        self.lock = threading.Lock()
        if os.environ.get("NSEQL_AUTO_FIXTURE", "true").lower() == "true":
            ensure_fixture(self.data_dir)

    def dataset(self, dataset_id: str) -> Dataset:
        p = self.data_dir / dataset_id
        if "/" in dataset_id or ".." in dataset_id or not (p / "manifest.json").exists():
            raise HTTPException(404, f"dataset '{dataset_id}' not found")
        return Dataset(p)

    def notify(self, event: str, text: str) -> None:
        self.notifier.send(event, text)
        self.registry.audit("notifier", "NOTIFICATION", event=event, text=text[:300], delivered=self.notifier.log[-1]["ok"])

    def job(self, kind: str, fn, **meta) -> dict:
        jid = f"job_{uuid.uuid4().hex[:10]}"
        j = {"id": jid, "kind": kind, "status": "running", "started": datetime.now().isoformat(timespec="seconds"),
             "finished": None, "meta": meta, "progress": [], "result": None, "error": None}
        self.jobs[jid] = j

        def run():
            try:
                j["result"] = fn(j)
                j["status"] = "done"
            except Exception as e:  # surfaced to the UI, never swallowed
                j["status"], j["error"] = "failed", f"{type(e).__name__}: {e}"
            j["finished"] = datetime.now().isoformat(timespec="seconds")
        threading.Thread(target=run, daemon=True).start()
        return j


def create_app() -> FastAPI:
    st = State()
    token = st.settings.api_token
    viewer_token = os.environ.get("NSEQL_VIEWER_TOKEN") or None
    insecure_local = os.environ.get("NSEQL_ALLOW_INSECURE_LOCAL", "").lower() == "true"
    if token and insecure_local:
        raise RuntimeError("set NSEQL_API_TOKEN or NSEQL_ALLOW_INSECURE_LOCAL=true, not both")
    if not token and not insecure_local:
        raise RuntimeError("NSEQL_API_TOKEN is required. The API does not start unauthenticated. For a single-user machine "
                           "only, NSEQL_ALLOW_INSECURE_LOCAL=true serves loopback clients without a token.")
    for name, value in (("NSEQL_API_TOKEN", token), ("NSEQL_VIEWER_TOKEN", viewer_token)):
        if value and len(value) < MIN_TOKEN_LENGTH:
            raise RuntimeError(f"{name} must be at least {MIN_TOKEN_LENGTH} characters")
    if token and viewer_token and hmac.compare_digest(token, viewer_token):
        raise RuntimeError("NSEQL_VIEWER_TOKEN must differ from NSEQL_API_TOKEN")
    app = FastAPI(title="NSE AI Quant Lab", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.st = st
    origins = [o for o in os.environ.get("NSEQL_CORS_ORIGINS", "http://localhost:3000").split(",") if o]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    def auth(request: Request):
        """Two roles. operator: everything. viewer: read-only (GET). Anything else: refused."""
        if insecure_local:
            hops = [request.client.host if request.client else "", *(h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip())]
            if not all(h in LOOPBACK for h in hops):
                raise HTTPException(403, "this server runs in insecure local mode and only answers loopback clients; "
                                         "set NSEQL_API_TOKEN to use it from another machine")
            request.state.role = "operator"
            return
        got = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.headers.get("x-api-key", "")
        if hmac.compare_digest(got.encode(), token.encode()):
            request.state.role = "operator"
        elif viewer_token and hmac.compare_digest(got.encode(), viewer_token.encode()):
            request.state.role = "viewer"
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                st.registry.audit("viewer", "FORBIDDEN_WRITE", method=request.method, path=request.url.path)
                raise HTTPException(403, "this is a read-only token; the action needs the operator token")
        else:
            raise HTTPException(401, "invalid or missing API token")

    # interactive docs and the schema are disabled: they would be served without the auth dependency
    api = FastAPI(dependencies=[Depends(auth)], docs_url=None, redoc_url=None, openapi_url=None)
    reg = st.registry

    def clean(o):
        """JSON-safe: NaN/inf -> None, numpy scalars -> python."""
        if isinstance(o, dict):
            return {str(k): clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (np.floating, float)):
            return None if (o != o or o in (float("inf"), float("-inf"))) else float(o)
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, (pd.Timestamp, datetime)):
            return str(o)
        return o

    def run_dir(run_id: str) -> Path:
        r = reg.run(run_id)
        if not r or not r.get("path") or not Path(r["path"]).exists():
            raise HTTPException(404, f"no stored artefacts for run '{run_id}'")
        return Path(r["path"])

    # ------------------------------------------------------------------ system
    @app.get("/api/health")
    def health():
        return {"status": "ok", "auth_required": not insecure_local}

    @api.get("/system")
    def system(request: Request):
        du = shutil.disk_usage(st.exp_dir)
        return clean({
            "auth": {"mode": "insecure-local" if insecure_local else "token", "role": request.state.role,
                     "viewer_token_configured": bool(viewer_token)},
            "version": __version__, "code_version": code_version(), "uptime_seconds": round(time.time() - st.started),
            "live_trading_enabled": st.settings.live_trading_enabled, "auth_required": not insecure_local,
            "llm": {"configured": llm_mod.credentials_present(), "model": llm_mod.MODEL},
            "sandbox": {"network_namespace": sandbox.isolation_status()[0], "detail": sandbox.isolation_status()[1], **sandbox.SandboxLimits().__dict__},
            "kill_switch": {"engaged": st.kill.engaged, "reason": st.kill.reason},
            "counts": {"runs": len(reg.runs(limit=100000)), "hypotheses": sum(h["origin"] != "system" for h in reg.hypotheses(100000)), "lessons": len(reg.lessons(100000)),
                       "experiments": len(reg.experiments(100000)), "paper_sessions": len(st.sessions), "trials_for_dsr": reg.trial_count(),
                       "jobs_running": sum(j["status"] == "running" for j in st.jobs.values())},
            "storage": {"experiments_dir": str(st.exp_dir), "data_dir": str(st.data_dir), "disk_free_gb": round(du.free / 1e9, 1),
                        "registry_mb": round(st.settings.registry_path.stat().st_size / 1e6, 2)},
            "datasets": [d.id for d in list_datasets(st.data_dir)],
            "features": [{"feature": f, "status": s, "note": n} for f, s, n in FEATURES],
            "notifications": st.notifier.status(),
        })

    @api.get("/audit")
    def audit(limit: int = 300):
        return reg.audit_entries(limit)

    @api.get("/settings")
    def settings():
        return {"live_trading_enabled": st.settings.live_trading_enabled,
                "live_trading_note": "read from the environment at start-up; cannot be changed through this API or by any agent",
                "data_dir": str(st.data_dir), "experiments_dir": str(st.exp_dir), "auth_required": not insecure_local,
                "credentials": {"anthropic": llm_mod.credentials_present(), "kite": st.settings.kite_credentials_present,
                                "whatsapp": st.notifier.configured,
                                "human_approval_secret_visible_to_api": bool(os.environ.get("HUMAN_APPROVAL_SECRET"))},
                "risk_limits_default": RiskLimits().to_dict(), "sandbox_limits": sandbox.SandboxLimits().__dict__,
                "compliance": [{"key": k, "text": v, "done": st.gate.compliance()[k]} for k, v in COMPLIANCE_ITEMS.items()]}

    # ------------------------------------------------------------------ data
    @api.get("/datasets")
    def datasets():
        out = []
        for d in list_datasets(st.data_dir):
            kinds: dict[str, int] = {}
            for i in d.instruments.values():
                kinds[i.kind.value] = kinds.get(i.kind.value, 0) + 1
            out.append({**{k: getattr(d.manifest, k) for k in ("dataset_id", "description", "source", "licence", "synthetic",
                                                               "created_at", "redistribution_allowed", "content_hash")},
                        "intervals": d.intervals(), "instruments": len(d.instruments), "kinds": kinds,
                        "raw_files": len(d.manifest.raw_files), "audit_entries": len(d.manifest.audit),
                        "range": {iv: d.date_range(iv) for iv in d.intervals()} if len(d.instruments) < 3000 else {}})
        return out

    @api.get("/datasets/{dataset_id}")
    def dataset(dataset_id: str):
        d = st.dataset(dataset_id)
        part = get_partition(reg, d).to_dict() if d.intervals() else None
        return {"manifest": {**d.manifest.to_dict(), "files": len(d.manifest.files)}, "partition": part,
                "corporate_actions": [c.to_dict() for c in d.corporate_actions()],
                "index_membership": [{"index": m.index, "symbol": m.symbol, "start": str(m.start), "end": str(m.end) if m.end else None}
                                     for m in d.index_membership()]}

    @api.get("/datasets/{dataset_id}/quality")
    def quality(dataset_id: str, interval: str = "1d", kind: str = "EQ"):
        d = st.dataset(dataset_id)
        syms = [s for s in d.symbols(interval) if kind == "ALL" or d.instruments[s].kind.value in kind.split(",")]
        return clean(dataset_quality(d, interval, symbols=syms, max_symbols=300))

    @api.get("/datasets/{dataset_id}/instruments")
    def instruments(dataset_id: str, kind: str | None = None, q: str = "", underlying: str | None = None, limit: int = 500):
        d = st.dataset(dataset_id)
        rows = [i.to_dict() for i in d.instruments.values()
                if (not kind or i.kind.value == kind) and q.upper() in i.symbol and (not underlying or i.underlying == underlying)]
        return {"total": len(rows), "items": sorted(rows, key=lambda r: r["symbol"])[:limit]}

    @api.get("/datasets/{dataset_id}/bars")
    def bars(dataset_id: str, symbol: str, interval: str = "1d", start: str | None = None, end: str | None = None, limit: int = 2000):
        d = st.dataset(dataset_id)
        if not d.has_bars(symbol, interval):
            raise HTTPException(404, f"no {interval} bars for {symbol}")
        df = d.bars(symbol, interval, start, end).tail(limit)
        return clean({"symbol": symbol, "interval": interval, "synthetic": d.synthetic, "instrument": d.instruments[symbol].to_dict(),
                      "ts": [str(t) for t in df["ts"]], **{c: df[c].tolist() for c in ("open", "high", "low", "close", "volume", "oi")}})

    @api.get("/datasets/{dataset_id}/chain")
    def chain(dataset_id: str, underlying: str, date: str | None = None):
        """Option chain as of one session, with implied volatility and Greeks computed from the stored premiums."""
        from nseql.analytics.greeks import bs_greeks, implied_vol, year_fraction
        d = st.dataset(dataset_id)
        und = d.bars(underlying, "1d", end=date).tail(1)
        if und.empty:
            raise HTTPException(404, "no underlying price on or before that date")
        day, spot = und["ts"].iloc[0], float(und["close"].iloc[0])
        rows = []
        for s, i in d.instruments.items():
            if i.underlying != underlying or not i.is_option or not i.listed_on(day.date()):
                continue
            b = d.bars(s, "1d", day.date(), day.date())
            if b.empty:
                continue
            px = float(b["close"].iloc[0])
            t = year_fraction((i.expiry - day.date()).days)
            iv = implied_vol(px, spot, i.strike, t, kind=i.option_type)
            g = bs_greeks(spot, i.strike, t, iv, kind=i.option_type) if iv == iv else None
            rows.append({"symbol": s, "expiry": str(i.expiry), "strike": i.strike, "type": i.option_type, "close": px,
                         "volume": int(b["volume"].iloc[0]), "oi": int(b["oi"].iloc[0]), "iv": iv,
                         "delta": g.delta if g else None, "gamma": g.gamma if g else None, "theta": g.theta if g else None,
                         "vega": g.vega if g else None})
        return clean({"underlying": underlying, "date": str(day.date()), "spot": spot, "synthetic": d.synthetic,
                      "greeks_note": "computed from stored premiums with Black-Scholes; not vendor data",
                      "expiries": sorted({r["expiry"] for r in rows}), "rows": sorted(rows, key=lambda r: (r["expiry"], r["strike"], r["type"]))})

    @api.get("/market/overview")
    def market_overview(dataset_id: str = FIXTURE_ID):
        from nseql.agents.team import MarketIntelligenceAgent
        d = st.dataset(dataset_id)
        part = get_partition(reg, d)
        brief = MarketIntelligenceAgent().brief(d, part.dev_start, part.dev_end)
        rows, rets = [], {}
        for s in d.symbols("1d"):
            i = d.instruments[s]
            if not (i.is_cash or i.kind.value == "INDEX"):
                continue
            b = d.bars(s, "1d", part.dev_start, part.val_end)
            if len(b) < 30:
                continue
            c = b["close"].to_numpy()
            for ca in d.corporate_actions():
                if ca.symbol == s and ca.kind in ("split", "bonus"):
                    c = np.where(b["ts"].dt.date.to_numpy() < ca.ex_date, c / ca.ratio, c)
            r = pd.Series(c, index=b["ts"]).pct_change()
            rets[s] = r
            rows.append({"symbol": s, "kind": i.kind.value, "sector": i.sector, "last": float(c[-1]), "ret_1m": float(c[-1] / c[-22] - 1),
                         "ret_3m": float(c[-1] / c[-64] - 1) if len(c) > 64 else None, "vol_20d": float(r.tail(20).std() * np.sqrt(252)),
                         "above_50d": bool(c[-1] > c[-50:].mean()), "traded_value": float((b["close"] * b["volume"]).tail(20).median()),
                         "listed_to": str(i.listed_to) if i.listed_to else None})
        eq = [r for r in rows if r["kind"] != "INDEX"]
        corr = pd.DataFrame({k: v for k, v in rets.items() if d.instruments[k].is_cash}).tail(120).dropna(axis=1, thresh=60).corr()
        return clean({"dataset_id": d.id, "synthetic": d.synthetic, "as_of": part.val_end,
                      "as_of_note": "statistics use development + validation data only; the sealed window is never shown here",
                      "brief": brief.model_dump(), "symbols": rows,
                      "breadth": {"above_50d": sum(r["above_50d"] for r in eq), "total": len(eq)},
                      "correlation": {"labels": list(corr.columns), "matrix": corr.round(3).fillna(0).to_numpy().tolist()}})

    @api.get("/market/structure")
    def market_structure():
        return structure.timeline()

    @api.get("/events")
    def events(as_of: str | None = None, symbol: str | None = None, category: str | None = None):
        return {"count": st.events.count(), "note": "no news content ships with the platform; ingest a licensed feed",
                "items": st.events.as_of(as_of or datetime.now(), symbol=symbol, category=category)}

    # ------------------------------------------------------------------ strategies & code
    @api.get("/strategies")
    def strategies():
        gen = []
        for h in reg.hypotheses(500):
            c = reg.code_for(h["id"])
            if c:
                gen.append({"hypothesis_id": h["id"], "code_id": c["code_id"], "sha256": c["sha256"], "family": h["family"],
                            "status": h["status"], "statement": h["statement"], "origin": h["origin"], "created_at": c["created_at"]})
        return {"library": [c.describe() for c in strategy_registry.STRATEGIES.values()], "generated": gen}

    @api.get("/code/{code_id}")
    def code(code_id: str):
        c = reg.code(code_id)
        if not c:
            raise HTTPException(404, "unknown code id")
        return {**c, "hypothesis": reg.hypothesis(c["hypothesis_id"]), "gate": st.gate.check(c["sha256"]).to_dict()}

    @api.get("/strategies/{name}/source")
    def library_source(name: str):
        import inspect
        try:
            cls = strategy_registry.get(name)
        except KeyError:
            raise HTTPException(404, "unknown strategy") from None
        return {"name": name, "source": inspect.getsource(cls), "describe": cls.describe(), "editable": False}

    class SourceIn(BaseModel):
        source: str = Field(max_length=sandbox.MAX_SOURCE_BYTES)
        dataset_id: str = FIXTURE_ID
        run: bool = False

    @api.post("/code/validate")
    def validate(body: SourceIn):
        """Static validation; with run=true also a sandboxed development-window smoke test + look-ahead trap."""
        rep = sandbox.validate_source(body.source)
        out = {"validation": rep.to_dict(), "run": None}
        if rep.ok and body.run:
            d = st.dataset(body.dataset_id)
            part = get_partition(reg, d)
            cfg = {"dataset_id": d.id, "start": part.dev_start, "end": part.dev_end, "strategies": [{"name": "editor", "params": {}}]}
            res = sandbox.run_jobs(body.source, [{"id": "bt", "config": cfg}, {"id": "la", "kind": "lookahead_audit", "config": cfg}], st.data_dir)
            bt = next((r for r in res["results"] if r["id"] == "bt"), None)
            la = next((r for r in res["results"] if r["id"] == "la"), None)
            out["run"] = {"ok": res["ok"], "error": res["error"] or (bt or {}).get("error"), "isolation": res["isolation"],
                          "window": [part.dev_start, part.dev_end], "metrics": (bt or {}).get("summary", {}).get("metrics") if bt else None,
                          "flags": (bt or {}).get("summary", {}).get("flags") if bt else None,
                          "lookahead": {k: la.get(k) for k in ("passed", "orders_checked", "first_difference", "error")} if la else None}
            reg.audit("user", "CODE_EDITOR_RUN", sha256=rep.sha256, ok=res["ok"])
        return clean(out)

    # ------------------------------------------------------------------ backtests & runs
    def _execute(cfg: BacktestConfig, kind: str = "backtest", partition: str = "user") -> dict:
        gen = [s for s in cfg.strategies if s.name.startswith("generated:")]
        if gen:
            if len(cfg.strategies) != 1:
                raise HTTPException(400, "a generated strategy can only be backtested on its own (it runs in the sandbox)")
            c = reg.code(gen[0].name.split(":", 1)[1])
            if not c:
                raise HTTPException(404, "unknown generated code id")
            ev = V.Evaluator(st.data_dir, "generated", source=c["source"], out_dir=st.exp_dir / "runs", capital=gen[0].capital,
                             base={k: v for k, v in cfg.model_dump().items() if k not in ("strategies", "symbols")})
            r = ev.run(params=gen[0].params, save=True)
            if not r.get("summary"):
                raise HTTPException(400, f"sandbox run failed: {r.get('error')}")
            reg.record_run(r["run_id"], r["summary"], kind=kind, partition=partition, path=r["path"], hypothesis_id=c["hypothesis_id"])
            return {"run_id": r["run_id"], "summary": r["summary"]}
        try:
            r = run_backtest(cfg, st.data_dir)
        except (KeyError, ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e)) from None
        path = r.save(st.exp_dir / "runs" / r.run_id)
        reg.record_run(r.run_id, r.summary, kind=kind, partition=partition, path=str(path))
        reg.audit("user", "BACKTEST_RUN", run_id=r.run_id, strategies=[s.name for s in cfg.strategies])
        return {"run_id": r.run_id, "summary": r.summary}

    @api.post("/backtests")
    def backtest(cfg: BacktestConfig):
        return clean(_execute(cfg))

    @api.get("/runs")
    def runs(limit: int = 200, kind: str | None = None, hypothesis_id: str | None = None):
        return clean(reg.runs(limit=limit, kind=kind, hypothesis_id=hypothesis_id))

    @api.get("/runs/{run_id}")
    def run_detail(run_id: str):
        r = reg.run(run_id)
        if not r:
            raise HTTPException(404, "unknown run")
        p = Path(r["path"]) if r.get("path") else None
        out = {"registry": r, "summary": None, "config": None, "artefacts": []}
        if p and p.exists():
            out["summary"] = json.loads((p / "summary.json").read_text())
            out["config"] = json.loads((p / "config.json").read_text())
            out["artefacts"] = sorted(f.name for f in p.iterdir())
        return clean(out)

    @api.get("/runs/{run_id}/equity")
    def run_equity(run_id: str, max_points: int = 1500):
        df = pd.read_parquet(run_dir(run_id) / "equity.parquet")
        if len(df) > max_points:
            df = df.iloc[np.unique(np.linspace(0, len(df) - 1, max_points).astype(int))]
        peak = df["equity"].cummax()
        return clean({"ts": [str(t) for t in df["ts"]], "equity": df["equity"].tolist(), "cash": df["cash"].tolist(),
                      "gross_exposure": df["gross_exposure"].tolist(), "margin_used": df["margin_used"].tolist(),
                      "drawdown": (df["equity"] / peak - 1).tolist(),
                      "books": {c[5:]: df[c].tolist() for c in df.columns if c.startswith("book:")}})

    @api.get("/runs/{run_id}/{what}")
    def run_table(run_id: str, what: str, limit: int = 5000, offset: int = 0):
        d = run_dir(run_id)
        if what in ("fills", "trades", "orders"):
            rows = json.loads((d / f"{what}.json").read_text())
        elif what == "ledger":
            rows = pd.read_parquet(d / "ledger.parquet").to_dict("records")
        elif what == "events":
            rows = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines() if l]
        else:
            raise HTTPException(404, "unknown table")
        return clean({"total": len(rows), "items": rows[offset:offset + limit]})

    @api.get("/replay/{run_id}/{trade_index}")
    def replay(run_id: str, trade_index: int, pad: int = 15):
        """Everything needed to replay one trade: bars around it, the orders and fills that made it,
        engine events for the symbol, and the cash-ledger entries it produced."""
        d = run_dir(run_id)
        trades = json.loads((d / "trades.json").read_text())
        if not 0 <= trade_index < len(trades):
            raise HTTPException(404, "trade index out of range")
        t = trades[trade_index]
        summary = json.loads((d / "summary.json").read_text())
        ds = st.dataset(summary["dataset"]["id"])
        iv = summary["period"]["interval"]
        b = ds.bars(t["symbol"], iv)
        i0 = int(b["ts"].searchsorted(pd.Timestamp(t["entry_ts"]))) - pad
        i1 = int(b["ts"].searchsorted(pd.Timestamp(t["exit_ts"]))) + pad
        w = b.iloc[max(i0, 0):i1 + 1]
        fills = [f for f in json.loads((d / "fills.json").read_text()) if f["symbol"] == t["symbol"] and f["book"] == t["book"]
                 and t["entry_ts"] <= f["ts"] <= t["exit_ts"]]
        oids = {f["order_id"] for f in fills}
        orders = [o for o in json.loads((d / "orders.json").read_text()) if o["id"] in oids]
        events = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()
                  if f'"{t["symbol"]}"' in l and t["entry_ts"][:10] <= json.loads(l)["ts"][:10] <= t["exit_ts"][:10]][:300]
        led = pd.read_parquet(d / "ledger.parquet")
        fid = {f["id"] for f in fills}
        ledger = led[led["ref"].isin(fid)].to_dict("records")
        return clean({"trade": t, "index": trade_index, "total_trades": len(trades), "synthetic": ds.synthetic,
                      "bars": {"ts": [str(x) for x in w["ts"]], **{c: w[c].tolist() for c in ("open", "high", "low", "close", "volume")}},
                      "fills": fills, "orders": orders, "events": events, "ledger": ledger,
                      "signal_note": "an order is created at the close of the bar BEFORE its first fill; fills use the next bar"})

    class CompareIn(BaseModel):
        run_ids: list[str] = Field(min_length=1, max_length=12)

    @api.post("/compare")
    def compare(body: CompareIn):
        rows, curves, rets, warn = [], {}, {}, []
        for rid in body.run_ids:
            r = reg.run(rid)
            if not r:
                raise HTTPException(404, f"unknown run {rid}")
            rows.append({"run_id": rid, "strategy": r["strategy"], "params": r["params"], "partition": r["partition"],
                         "dataset_hash": r["dataset_hash"], "period": [r["period_start"], r["period_end"]], "metrics": r["metrics"],
                         "flags": r["flags"]})
            if r.get("path") and Path(r["path"]).exists():
                df = pd.read_parquet(Path(r["path"]) / "equity.parquet")
                s = df.groupby(df["ts"].dt.normalize())["equity"].last()
                curves[rid] = {"ts": [str(x.date()) for x in s.index], "value": (s / s.iloc[0]).tolist()}
                rets[rid] = s.pct_change()
        if len({(x["dataset_hash"], *x["period"]) for x in rows}) > 1:
            warn.append("runs use different datasets or periods — metrics are not directly comparable")
        ex = [json.loads((Path(reg.run(i)["path"]) / "summary.json").read_text())["execution"] for i in body.run_ids
              if reg.run(i).get("path") and Path(reg.run(i)["path"]).exists()]
        cf = [json.loads((Path(reg.run(i)["path"]) / "config.json").read_text()) for i in body.run_ids
              if reg.run(i).get("path") and Path(reg.run(i)["path"]).exists()]
        keys = ("cost_multiplier", "brokerage", "margin", "risk", "interval")
        if len({json.dumps(e, sort_keys=True) for e in ex}) > 1 or len({json.dumps({k: c.get(k) for k in keys}, sort_keys=True) for c in cf}) > 1:
            warn.append("runs use different execution / cost assumptions")
        corr = pf.correlation_report(pd.DataFrame(rets).dropna()) if len(rets) > 1 else None
        return clean({"rows": rows, "curves": curves, "correlation": corr, "warnings": warn,
                      "ranking_note": "rank on risk-adjusted, cost-inclusive, out-of-sample evidence — never on total profit alone"})

    class WalkForwardIn(BaseModel):
        strategy: str
        grid: dict[str, list] = Field(default_factory=dict)
        dataset_id: str = FIXTURE_ID
        train_days: int = Field(250, ge=60, le=2000)
        test_days: int = Field(60, ge=10, le=500)
        mode: str = "rolling"
        max_points: int = Field(12, ge=1, le=40)

    @api.post("/walkforward")
    def walkforward(body: WalkForwardIn):
        d = st.dataset(body.dataset_id)
        part = get_partition(reg, d)
        if body.strategy.startswith("generated:"):
            c = reg.code(body.strategy.split(":", 1)[1])
            if not c:
                raise HTTPException(404, "unknown generated code id")
            src, name, grid = c["source"], "generated", body.grid or (reg.hypothesis(c["hypothesis_id"])["spec"] or {}).get("param_grid", {})
        else:
            try:
                cls = strategy_registry.get(body.strategy)
            except KeyError as e:
                raise HTTPException(404, str(e)) from None
            src, name, grid = None, body.strategy, body.grid or cls.param_grid

        def work(job):
            ev = V.Evaluator(st.data_dir, name, source=src, base={"dataset_id": d.id}, out_dir=st.exp_dir / "runs")
            wf = V.walk_forward(ev, grid, start=part.dev_start, end=part.val_end, train_days=body.train_days, test_days=body.test_days,
                                mode=body.mode, max_points=body.max_points, dataset=d)
            wf.update(strategy=body.strategy, dataset_id=d.id, dataset_hash=d.content_hash, synthetic=d.synthetic,
                      window=[part.dev_start, part.val_end],
                      window_note="walk-forward uses development + validation data only; the sealed window is excluded")
            for f in wf.get("folds", []):
                if f.get("run_id"):
                    rr = ev.out_dir / f["run_id"] / "summary.json"
                    if rr.exists():
                        reg.record_run(f["run_id"], json.loads(rr.read_text()), kind="walkforward", partition="dev+validation", path=str(rr.parent))
            wid = reg.add_artifact("WalkForwardReport", "user", clean(wf))
            reg.audit("user", "WALK_FORWARD", strategy=body.strategy, artifact=wid, backtests=wf.get("backtests_run"))
            return {"artifact_id": wid, **clean(wf)}
        return st.job("walkforward", work, strategy=body.strategy)

    @api.get("/walkforward")
    def walkforward_reports():
        return clean([{"id": a["id"], "created_at": a["created_at"], **a["payload"]} for a in reg.artifacts(typ="WalkForwardReport")][::-1])

    class PortfolioIn(BaseModel):
        strategies: list[StrategyRef] = Field(min_length=1, max_length=8)
        method: str = "equal"
        capital: float = Field(3_000_000, gt=0)
        dataset_id: str = FIXTURE_ID
        target_vol: float = 0.10

    @api.post("/portfolio/simulate")
    def portfolio(body: PortfolioIn):
        """Weights are estimated on the development window, then every strategy trades the validation
        window together on ONE shared account and event stream."""
        if body.method not in pf.METHODS:
            raise HTTPException(400, f"method must be one of {pf.METHODS}")
        if any(s.name.startswith("generated:") for s in body.strategies):
            raise HTTPException(400, "portfolio simulation supports library strategies only (generated code runs sandboxed, one at a time)")
        d = st.dataset(body.dataset_id)
        part = get_partition(reg, d)
        rets, labels = {}, []
        for i, s in enumerate(body.strategies):
            lab = f"{s.name}#{i + 1}"
            labels.append(lab)
            r = run_backtest(BacktestConfig(dataset_id=d.id, start=part.dev_start, end=part.dev_end,
                                            strategies=[StrategyRef(name=s.name, params=s.params, capital=1_000_000)]), st.data_dir)
            e = r.equity_frame()
            rets[lab] = e.groupby(e["ts"].dt.normalize())["equity"].last().pct_change()
        R = pd.DataFrame(rets).dropna()
        w = pf.weights(R, body.method, target_vol=body.target_vol)
        refs = [StrategyRef(name=s.name, params=s.params, capital=max(round(body.capital * w[l], 2), 1.0), book=l)
                for s, l in zip(body.strategies, labels)]
        res = run_backtest(BacktestConfig(dataset_id=d.id, start=part.dev_start, end=part.val_end, strategies=refs,
                                          label=f"portfolio:{body.method}"), st.data_dir)
        path = res.save(st.exp_dir / "runs" / res.run_id)
        reg.record_run(res.run_id, res.summary, kind="portfolio", partition="dev+validation", path=str(path))
        acct = res.engine.account
        e = res.equity_frame()
        books = pd.DataFrame({b: e.groupby(e["ts"].dt.normalize())[f"book:{b}"].last().pct_change() for b in labels}).dropna()
        val = books[books.index >= pd.Timestamp(part.val_start)]
        pos = {b.id: {p.symbol: p.qty * acct.marks.get(p.symbol, 0) / 100 for p in b.positions.values()} for b in acct.books.values()}
        flat = [{"symbol": p.symbol, "qty": p.qty, "asof": str(e["ts"].iloc[-1])} for b in acct.books.values() for p in b.positions.values()]
        tot = e.groupby(e["ts"].dt.normalize())["equity"].last()
        vt = tot[tot.index >= pd.Timestamp(part.val_start)].pct_change().dropna()
        return clean({
            "run_id": res.run_id, "method": body.method, "weights": w, "cash_weight": 1 - sum(w.values()),
            "weights_estimated_on": [part.dev_start, part.dev_end], "evaluated_on": [part.val_start, part.val_end],
            "synthetic": d.synthetic, "metrics_full_run": res.metrics, "per_book": res.summary["per_book"],
            "validation_window": {"sharpe": V.sharpe(vt.to_numpy()), "total_return": float((1 + vt).prod() - 1),
                                  "max_drawdown": V.drawdown_stats((1 + vt).cumprod())["max_drawdown"] if len(vt) else None},
            "correlation_dev": pf.correlation_report(R), "correlation_validation": pf.correlation_report(val),
            "overlap": pf.overlap_report(pos),
            "exposure": pf.exposure_report(flat, acct.instruments, {k: v / 100 for k, v in acct.marks.items()}),
            "capital_check": {"sum_of_book_cash": sum(b.cash for b in acct.books.values()) / 100, "account_cash": acct.cash / 100,
                              "reconciliation_ok": res.summary["reconciliation"]["ok"],
                              "margin_used_peak": float(e["margin_used"].max()), "gross_exposure_peak": float(e["gross_exposure"].max())},
            "rejected_for_capital": sum(1 for o in res.engine.orders if o.reason.startswith("INSUFFICIENT_MARGIN")),
        })

    # ------------------------------------------------------------------ research
    class ResearchIn(BaseModel):
        dataset_id: str = FIXTURE_ID
        max_iterations: int = Field(3, ge=1, le=12)
        max_backtests: int = Field(300, ge=10, le=3000)
        max_wall_seconds: float = Field(900, ge=30, le=7200)
        max_sealed_evaluations: int = Field(1, ge=0, le=5)
        walk_forward: bool = True
        seed: int = 0
        use_llm: bool = True

    @api.post("/research/run")
    def research_run(body: ResearchIn):
        st.dataset(body.dataset_id)
        if any(j["kind"] == "research" and j["status"] == "running" for j in st.jobs.values()):
            raise HTTPException(409, "a research run is already in progress")

        def work(job):
            loop = ResearchLoop(st.data_dir, st.exp_dir, reg, dataset_id=body.dataset_id, seed=body.seed,
                                llm=llm_mod.default_client() if body.use_llm else None, notify=st.notify,
                                live_enabled=st.settings.live_trading_enabled,
                                budget=ResearchBudget(body.max_iterations, body.max_backtests, body.max_wall_seconds,
                                                      body.max_sealed_evaluations, walk_forward=body.walk_forward))
            loop.progress = lambda m: job["progress"].append({**m, "t": round(time.time() - st.started)}) or job["progress"].__delitem__(slice(0, -60))
            job["meta"]["experiment_id"] = None
            out = loop.run()
            job["meta"]["experiment_id"] = out["experiment_id"]
            return out
        return st.job("research", work, budget=body.model_dump())

    @api.get("/jobs")
    def jobs():
        return clean([{k: v for k, v in j.items() if k != "result"} | {"has_result": j["result"] is not None}
                      for j in sorted(st.jobs.values(), key=lambda j: j["started"], reverse=True)][:50])

    @api.get("/jobs/{job_id}")
    def job(job_id: str):
        if job_id not in st.jobs:
            raise HTTPException(404, "unknown job")
        return clean(st.jobs[job_id])

    @api.get("/research/agents")
    def agents():
        msgs = reg.messages(limit=3000)
        role = {"research_director": "Plans experiments, allocates the compute budget, decides promote / reject, alone may open the sealed set",
                "market_intelligence": "Describes the development sample: volatility, correlation, momentum / reversal tendencies, liquidity",
                "strategy_inventor": "Proposes falsifiable hypotheses as declarative specs (LLM if configured, else a hypothesis grammar)",
                "strategy_developer": "Turns a spec into a Python strategy module and statically validates it",
                "backtest_controller": "Runs sandboxed development / validation tests and captures every result",
                "quant_auditor": "Independent critic: look-ahead trap, overfitting, cost fragility, multiple-testing, baselines",
                "risk_portfolio": "Drawdown, leverage, tail risk, redundancy against existing candidates",
                "learning": "Diagnoses failures, chooses the next research direction, never sees sealed results",
                "execution_readiness": "Checks a candidate against broker capability and compliance preconditions"}
        out = []
        for a in AGENTS:
            mine = [m for m in msgs if m["sender"] == a]
            out.append({"name": a, "role": role[a], "messages_sent": len(mine), "last_active": mine[-1]["created_at"] if mine else None,
                        "last_task": mine[-1]["task_type"] if mine else None,
                        "implementation": ("LLM (" + llm_mod.MODEL + ") with grammar fallback" if llm_mod.credentials_present() else "hypothesis grammar (no LLM configured)")
                        if a == "strategy_inventor" else "deterministic"})
        return {"agents": out, "recent_messages": msgs[-150:][::-1],
                "boundaries": ["agents exchange typed artefacts only", "generated code runs only in the sandbox",
                               "no agent can import the execution service, read credentials, or alter LIVE_TRADING_ENABLED",
                               "only the research director can open the sealed set, once per hypothesis"]}

    @api.get("/research/experiments")
    def experiments():
        return clean(reg.experiments(100))

    @api.get("/research/experiments/{eid}")
    def experiment(eid: str):
        e = reg.experiment(eid)
        if not e:
            raise HTTPException(404, "unknown experiment")
        return clean({"experiment": e, "messages": reg.messages(eid), "artifacts": reg.artifacts(experiment_id=eid),
                      "runs": reg.runs(limit=2000, experiment_id=eid)})

    @api.get("/research/hypotheses")
    def hypotheses(status: str | None = None):
        return clean(reg.hypotheses(500, status))

    @api.get("/research/hypotheses/{hid}")
    def hypothesis(hid: str):
        h = reg.hypothesis(hid)
        if not h:
            raise HTTPException(404, "unknown hypothesis")
        return clean({"hypothesis": h, "code": reg.code_for(hid), "artifacts": reg.artifacts(hypothesis_id=hid),
                      "runs": reg.runs(limit=500, hypothesis_id=hid), "lessons": [l for l in reg.lessons(500) if l["hypothesis_id"] == hid],
                      "children": [x["id"] for x in reg.hypotheses(1000) if x["parent_id"] == hid]})

    @api.get("/research/memory")
    def memory():
        runs_ = reg.runs(limit=100000)
        fam: dict[str, dict] = {}
        for h in reg.hypotheses(100000):
            f = fam.setdefault(h["family"], {"tested": 0, "rejected": 0, "survived": 0})
            f["tested"] += 1
            f["survived" if h["status"] in ("paper_candidate", "advance_to_sealed") else "rejected"] += h["status"] != "proposed"
        return clean({"lessons": reg.lessons(300), "families": fam, "sealed_evaluations": reg.sealed_evaluations(),
                      "trial_count": reg.trial_count(), "runs_by_partition": pd.Series([r["partition"] for r in runs_]).value_counts().to_dict() if runs_ else {}})

    class ChatIn(BaseModel):
        question: str = Field(min_length=1, max_length=4000)
        run_id: str | None = None

    @api.post("/chat")
    def chat(body: ChatIn):
        return analyst.answer(reg, body.question, body.run_id)

    # ------------------------------------------------------------------ paper trading
    class PaperIn(BaseModel):
        strategy: str | None = None           # library name, or None for a manual-only session
        params: dict = Field(default_factory=dict)
        capital: float = Field(1_000_000, gt=0)
        manual_capital: float = Field(500_000, gt=0)
        dataset_id: str = FIXTURE_ID
        window: str = "sealed"                # sealed | validation | all
        label: str = ""

    @api.post("/paper/sessions")
    def paper_start(body: PaperIn):
        d = st.dataset(body.dataset_id)
        part = get_partition(reg, d)
        allocs = [Allocation(ManualBook(), "manual", body.manual_capital)]
        strat = None
        if body.strategy:
            if body.strategy.startswith("generated:"):
                raise HTTPException(400, "paper trading of generated code is NOT IMPLEMENTED: it would have to run inside the sandbox "
                                         "process per bar. Promote it by adding a reviewed copy to the strategy library.")
            try:
                strat = strategy_registry.create(body.strategy, body.params)
            except (KeyError, ValueError) as e:
                raise HTTPException(400, str(e)) from None
            allocs.insert(0, Allocation(strat, "algo", body.capital))
        from nseql.backtester.runner import default_symbols
        syms = default_symbols(d, [strat] if strat else [strategy_registry.create("buy_and_hold")], "1d")
        start = {"sealed": part.dev_start, "validation": part.dev_start, "all": None}[body.window]
        end = {"sealed": part.sealed_end, "validation": part.val_end, "all": None}[body.window]
        ps = PaperSession(ReplayFeed(d, syms, "1d", start, end), allocs, kill=st.kill, notify=st.notify, label=body.label or (body.strategy or "manual"))
        warm = {"sealed": part.sealed_start, "validation": part.val_start}.get(body.window)
        if warm:   # fast-forward through history so indicators are warm; trading in the warm-up is part of the record
            while ps.feed.remaining and pd.Timestamp(ps.feed._portal.timeline[ps.feed._k]) < pd.Timestamp(warm):
                if not ps.advance(1):
                    break
        sid = f"paper_{uuid.uuid4().hex[:8]}"
        st.sessions[sid] = ps
        reg.audit("user", "PAPER_SESSION_STARTED", session=sid, strategy=body.strategy, window=body.window)
        return {"session_id": sid, **clean(ps.state())}

    def _session(sid: str) -> PaperSession:
        if sid not in st.sessions:
            raise HTTPException(404, "unknown paper session (sessions are in-memory and end when the API restarts)")
        return st.sessions[sid]

    @api.get("/paper/sessions")
    def paper_list():
        return clean([{"session_id": k, **{x: v for x, v in s.state().items() if x in ("label", "created", "now", "steps", "remaining",
                                                                                       "halted", "equity", "capital", "feed", "feed_note")}}
                      for k, s in st.sessions.items()])

    @api.get("/paper/sessions/{sid}")
    def paper_state(sid: str):
        return {"session_id": sid, **clean(_session(sid).state())}

    class AdvanceIn(BaseModel):
        steps: int = Field(1, ge=1, le=500)

    @api.post("/paper/sessions/{sid}/advance")
    def paper_advance(sid: str, body: AdvanceIn):
        s = _session(sid)
        with st.lock:
            done = s.advance(body.steps)
        return {"session_id": sid, "advanced": done, **clean(s.state())}

    class PaperOrderIn(BaseModel):
        symbol: str
        qty: int
        type: str = "MARKET"
        limit: float | None = None
        stop: float | None = None
        tif: str = "DAY"

    @api.post("/paper/sessions/{sid}/orders")
    def paper_order(sid: str, body: PaperOrderIn):
        s = _session(sid)
        if body.qty == 0 or body.symbol not in s.portal.instruments:
            raise HTTPException(400, "unknown symbol or zero quantity")
        out = s.submit_manual(body.model_dump())
        reg.audit("user", "PAPER_ORDER", session=sid, **body.model_dump(), result=out["status"])
        return out

    # ------------------------------------------------------------------ brokers, risk, notifications
    @api.get("/brokers")
    def brokers():
        kite = ZerodhaKite(st.gate)
        return {"live_trading_enabled": st.settings.live_trading_enabled,
                "adapters": [{**kite.describe(), "credentials_present": kite.connected}, Upstox(st.gate).describe(), AngelOneSmartAPI(st.gate).describe()],
                "market_data": [{"name": "replay (stored dataset)", "status": "IMPLEMENTED"},
                                {"name": "Kite historical REST", "status": "UNVERIFIED", "credentials_present": st.settings.kite_credentials_present},
                                {"name": "KiteTicker WebSocket", "status": "NOT IMPLEMENTED"}],
                "gate_default": st.gate.check(None).to_dict(),
                "note": "order routing and market data are separate adapters; no order can be sent while the gate is closed"}

    @api.get("/execution/gate")
    def gate(sha256: str | None = None):
        return {"decision": st.gate.check(sha256).to_dict(), "approval_on_file": bool(sha256 and st.gate.approval(sha256)),
                "compliance": st.gate.compliance(),
                "how_to_approve": "a human runs scripts/approve_strategy.py in a terminal that holds HUMAN_APPROVAL_SECRET; there is no API for it"}

    @api.get("/risk")
    def risk():
        return clean({"kill_switch": {"engaged": st.kill.engaged, "reason": st.kill.reason, "history": st.kill.history},
                      "limits": RiskLimits().to_dict(), "live_trading_enabled": st.settings.live_trading_enabled,
                      "gate": st.gate.check(None).to_dict(),
                      "sessions": [{"session_id": k, "halted": s.halted_reason, "equity": s.state()["equity"], "capital": s.capital,
                                    "positions": len(s.state()["positions"]), "open_orders": len(s.state()["open_orders"])}
                                   for k, s in st.sessions.items()]})

    class KillIn(BaseModel):
        reason: str = Field(min_length=3, max_length=300)
        operator: str = Field(min_length=2, max_length=80)

    @api.post("/risk/kill")
    def kill(body: KillIn):
        st.kill.engage(f"manual: {body.reason} (by {body.operator})", datetime.now())
        for s in st.sessions.values():
            s.halt(st.kill.reason)
        reg.audit(body.operator, "KILL_SWITCH_ENGAGED", reason=body.reason)
        st.notify("kill_switch", f"Kill switch ENGAGED by {body.operator}: {body.reason}")
        return {"engaged": True, "reason": st.kill.reason}

    @api.post("/risk/reset")
    def reset(body: KillIn):
        try:
            st.kill.reset(body.operator, body.reason, datetime.now())
        except PermissionError as e:
            reg.audit(body.operator, "KILL_SWITCH_RESET_DENIED", reason=str(e))
            raise HTTPException(403, str(e)) from None
        reg.audit(body.operator, "KILL_SWITCH_RESET", reason=body.reason)
        return {"engaged": False, "note": "halted paper sessions stay halted; start a new session"}

    @api.get("/notifications")
    def notifications():
        return st.notifier.status()

    @api.post("/notifications/test")
    def notifications_test():
        ok = st.notifier.send("test", "NSE AI Quant Lab test notification")
        return {"delivered": ok, **st.notifier.status()}

    app.mount("/api", api)
    return app


def app_factory() -> FastAPI:
    return create_app()

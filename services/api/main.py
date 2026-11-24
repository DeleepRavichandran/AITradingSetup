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
import re
import os
import shutil
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from nseql import __version__
from nseql.agents import analyst, llm as llm_mod
from nseql.brain.service import BrainService
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
from nseql.paper.feed import KiteDailyFeed, ReplayFeed
from nseql.paper.scheduler import PaperScheduler
from nseql.paper.store import SessionStore
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
    ("Angel One SmartAPI: automatic login, history, 1-minute polling", "UNVERIFIED",
     "mock-tested only; real-service verification needs the operator's credentials (nseql angel-verify)"),
    ("TradingView chart-export import", "IMPLEMENTED", "manual exports only; no TradingView API exists"),
    ("AI research loop (9 agents, sandboxed code)", "IMPLEMENTED", "LLM optional; deterministic agents by default"),
    ("LLM-backed inventor / analyst", "UNVERIFIED", "needs ANTHROPIC_API_KEY; stub-tested"),
    ("Walk-forward, bootstrap, DSR, sealed out-of-sample", "IMPLEMENTED", ""),
    ("Portfolio simulation on one shared ledger", "IMPLEMENTED", "library strategies only"),
    ("Learning Brain: episodic/semantic memory, procedural and meta-learning, regime eligibility", "IMPLEMENTED",
     "benchmarked; memory has NOT yet shown a reliable out-of-sample improvement — see docs/LEARNING_BRAIN.md"),
    ("High-throughput screening of 49,728 configurations", "IMPLEMENTED", "local process pool; a distributed queue backend is not implemented"),
    ("Controlled self-modification with human promotion and automatic rollback", "IMPLEMENTED", "policy-level changes only; no code self-modification"),
    ("Paper trading on replayed data", "IMPLEMENTED", "replay, not a live market; sessions persist across restarts"),
    ("Unattended end-of-day paper trading (Kite daily candles + scheduler)", "UNVERIFIED", "mock-tested; needs Kite credentials and a daily login"),
    ("Authorised real-time streaming feed", "BLOCKED", "needs broker credentials and a data entitlement; WebSocket/tick adapter not written"),
    ("Live 1-minute market data", "UNVERIFIED", "Angel One completed-candle polling, mock-tested; no WebSocket/tick streaming adapter"),
    ("One-minute screening loop (intraday + swing, up to 50,000 configurations)", "IMPLEMENTED",
     "verified on replayed and synthetic data; realistic fills and one shared portfolio"),
    ("Command Deck (voice, camera, actions, news)", "IMPLEMENTED", "browser speech services; news from public RSS feeds"),
    ("Vega copilot: evidence-grounded answers on every page", "IMPLEMENTED", "deterministic queries of the lab's records with evidence links; no language model"),
    ("Strategy Universe: aggregation, paging and per-configuration dossiers", "IMPLEMENTED", "minute-loop configurations; historical backtest per configuration not linked yet"),
    ("Investment planning: SIP, step-up SIP, lump sum, SWP, STP, goals, XIRR", "IMPLEMENTED", "projections at user-assumed returns; historical SIP on imported NAVs"),
    ("Mutual-fund NAV import and analytics (AMFI files)", "UNVERIFIED", "parser tested on the documented layouts; no real AMFI file imported here"),
    ("Positional, long-term equity, ETF, futures and options workspaces", "NOT IMPLEMENTED", "backtester supports FUT/OPT on synthetic data; no real data, no workspaces"),
    ("Fundamentals, valuation, earnings, macro indicators", "NOT IMPLEMENTED", "no licensed data source"),
    ("Live order routing", "DISABLED", "gate closed by default; Kite adapter mock-tested only"),
    ("Upstox / Angel One order routing", "NOT IMPLEMENTED", "interface stubs; live trading is disabled anyway"),
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
        self.kill = KillSwitch(self.exp_dir / "kill_switch.json")      # survives restarts
        self.notifier = WhatsAppNotifier.from_env()
        self.gate = LiveGate(self.exp_dir / "approvals", self.exp_dir / "compliance.json", self.kill, self.settings)
        self.events = EventStore(self.exp_dir / "events.sqlite")
        self.jobs: dict[str, dict] = {}
        self.sessions: dict[str, PaperSession] = {}
        self.store = SessionStore(self.exp_dir / "paper")
        self.brain = BrainService(self.exp_dir, self.data_dir)
        self.session_meta: dict[str, dict] = {}
        self.restore_errors: dict[str, str] = {}
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
        ok, detail = sandbox.isolation_status()
        if not ok:
            raise HTTPException(409, "Research runs AI-written strategy code, which only runs inside the Linux sandbox, and that "
                                     f"is not available on this machine ({detail}). Backtests, screening, paper trading and the "
                                     "Command Deck all still work. To enable research on Windows, run the lab in Docker Desktop.")
        if any(j["kind"] == "research" and j["status"] == "running" for j in st.jobs.values()):
            raise HTTPException(409, "a research run is already in progress")

        def work(job):
            loop = ResearchLoop(st.data_dir, st.exp_dir, reg, dataset_id=body.dataset_id, seed=body.seed,
                                llm=llm_mod.default_client() if body.use_llm else None, notify=st.notify,
                                live_enabled=st.settings.live_trading_enabled, memory=st.brain.memory,
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
        window: str = "sealed"                # replay feed: sealed | validation | all
        label: str = ""
        feed: str = "replay"                  # replay | kite_daily
        symbols: list[str] = Field(default_factory=list, max_length=250)   # kite_daily only
        auto_bars: int = Field(0, ge=0, le=50)   # replay only: bars the scheduler advances per tick

    def _kite_api():
        from nseql.data.adapters.kite import KiteHistorical, load_session
        ses = load_session(st.exp_dir) or {}
        return KiteHistorical(os.environ.get("KITE_API_KEY") or ses.get("api_key"), os.environ.get("KITE_ACCESS_TOKEN") or ses.get("access_token"))

    _angel: dict = {}

    def _angel_api():
        from nseql.data.adapters.angelone import AngelHistorical
        if "api" not in _angel:
            _angel["api"] = AngelHistorical(session_dir=st.exp_dir)
        return _angel["api"]

    def _calendar():
        from nseql.instruments.calendar import TradingCalendar
        hol = st.exp_dir / "holidays.json"
        return TradingCalendar.from_file(hol) if hol.exists() else TradingCalendar()

    def _build_session(sid: str, meta: dict) -> tuple[PaperSession, int]:
        """Construct a session from its stored configuration. Used for new sessions and for restoring
        after a restart, so both go through exactly the same code. Returns (session, warm-up steps to
        run for a brand-new replay session)."""
        allocs = [Allocation(ManualBook(), "manual", meta["manual_capital"])]
        strat = None
        if meta.get("strategy"):
            strat = strategy_registry.create(meta["strategy"], meta.get("params") or {})
            allocs.insert(0, Allocation(strat, "algo", meta["capital"]))
        warm = 0
        if meta["feed"] in ("kite_daily", "angel_daily"):
            feed = KiteDailyFeed(meta["kite"], meta["live_from"], api_factory=_kite_api if meta["feed"] == "kite_daily" else _angel_api,
                                 calendar=_calendar())
            feed.kind = meta["feed"]
        else:
            from nseql.backtester.runner import default_symbols
            d = st.dataset(meta["dataset_id"])
            part = get_partition(reg, d)
            syms = default_symbols(d, [strat] if strat else [strategy_registry.create("buy_and_hold")], "1d")
            start = {"sealed": part.dev_start, "validation": part.dev_start, "all": None}[meta["window"]]
            end = {"sealed": part.sealed_end, "validation": part.val_end, "all": None}[meta["window"]]
            feed = ReplayFeed(d, syms, "1d", start, end)
            cut = {"sealed": part.sealed_start, "validation": part.val_start}.get(meta["window"])
            if cut:
                warm = int((feed._portal.timeline < pd.Timestamp(cut).value).sum())
        ps = PaperSession(feed, allocs, kill=st.kill, notify=st.notify, label=meta.get("label") or meta.get("strategy") or "manual",
                          journal=lambda rec, _sid=sid: st.store.append(_sid, rec))
        return ps, warm

    def _restore_sessions() -> None:
        """Rebuild every open session by replaying its journal. Exact, because the engine is deterministic."""
        for sid in st.store.sessions():
            try:
                meta = st.store.meta(sid)
                ps, _ = _build_session(sid, meta)
                recs = st.store.records(sid)
                ps.replaying = True
                for rec in recs:
                    ps.apply_record(rec)
                ps.replaying = False
                bars = [r for r in recs if r["t"] == "bar"]
                if meta["feed"] == "replay":
                    ps.feed.skip(len(bars))
                elif bars:
                    ps.feed.last_date = pd.Timestamp(bars[-1]["ts"]).date()
                st.sessions[sid], st.session_meta[sid] = ps, meta
            except Exception as e:      # a session that cannot be restored is reported, never silently dropped
                st.restore_errors[sid] = f"{type(e).__name__}: {e}"
                reg.audit("system", "PAPER_SESSION_RESTORE_FAILED", session=sid, error=st.restore_errors[sid])
        if st.sessions or st.restore_errors:
            reg.audit("system", "PAPER_SESSIONS_RESTORED", restored=sorted(st.sessions), failed=sorted(st.restore_errors))

    @api.post("/paper/sessions")
    def paper_start(body: PaperIn):
        if body.strategy and body.strategy.startswith("generated:"):
            raise HTTPException(400, "paper trading of generated code is NOT IMPLEMENTED: it would have to run inside the sandbox "
                                     "process per bar. Promote it by adding a reviewed copy to the strategy library.")
        if body.feed not in ("replay", "kite_daily", "angel_daily"):
            raise HTTPException(400, "feed must be 'replay', 'kite_daily' or 'angel_daily'")
        sid = f"paper_{uuid.uuid4().hex[:8]}"
        meta = {**body.model_dump(), "session_id": sid, "created": datetime.now().isoformat(timespec="seconds"), "closed": False}
        try:
            if body.strategy:
                cls = strategy_registry.get(body.strategy)
                cls(**body.params)
                if body.feed != "replay" and (cls.requires != "EQ" or "1d" not in cls.intervals):
                    raise ValueError(f"the {body.feed} feed supports daily cash-equity strategies only")
            if body.feed in ("kite_daily", "angel_daily"):
                if not body.symbols:
                    raise ValueError(f"{body.feed} needs a list of NSE symbols")
                if body.feed == "kite_daily":
                    from nseql.data.adapters.kite_import import resolve
                    nse = _kite_api().instruments("NSE")
                    found, missing = resolve(nse, nse[nse["segment"] == "INDICES"], [*body.symbols, "NIFTY 50"])
                else:
                    from nseql.data.adapters.angelone import resolve as angel_resolve
                    found, missing = angel_resolve(_angel_api().instruments(st.exp_dir), [*body.symbols, "NIFTY 50"])
                if missing:
                    raise ValueError(f"not found in Kite's NSE instrument list: {missing}")
                meta["kite"] = found
                meta["live_from"] = datetime.now().date().isoformat()
            else:
                st.dataset(body.dataset_id)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"{type(e).__name__}: {e}") from None
        st.store.create(sid, meta)
        ps, warm = _build_session(sid, meta)
        with st.lock:
            if warm:       # fast-forward through history so indicators are warm; trading in the warm-up is part of the record
                ps.advance(warm)
            st.sessions[sid], st.session_meta[sid] = ps, meta
        reg.audit("user", "PAPER_SESSION_STARTED", session=sid, strategy=body.strategy, feed=body.feed, window=body.window)
        if body.feed in ("kite_daily", "angel_daily"):
            scheduler.tick()      # load the warm-up history now rather than at the next timer tick
        return {"session_id": sid, "persisted": True, **clean(ps.state())}

    def _paper_provenance(ps) -> dict:
        from nseql.screening.provenance import classify
        syn = getattr(getattr(ps.feed, "ds", None), "synthetic", None)
        return classify(ps.feed, synthetic=syn, experiments_dir=st.exp_dir)

    def _session(sid: str) -> PaperSession:
        if sid not in st.sessions:
            raise HTTPException(404, "unknown paper session" + (f" (it could not be restored: {st.restore_errors[sid]})" if sid in st.restore_errors else ""))
        return st.sessions[sid]

    @api.get("/paper/sessions")
    def paper_list():
        return clean([{"session_id": k, **{x: v for x, v in s.state().items() if x in ("label", "created", "now", "steps", "remaining",
                                                                                       "halted", "equity", "capital", "feed", "feed_note")}}
                      for k, s in st.sessions.items()])

    @api.get("/paper/sessions/{sid}")
    def paper_state(sid: str):
        s_ = _session(sid)
        return {"session_id": sid, "persisted": True, "config": {k: v for k, v in st.session_meta[sid].items() if k != "kite"},
                "feed_status": getattr(s_.feed, "status", None), "feed_gaps": getattr(s_.feed, "gaps", [])[-20:],
                "data_source": _paper_provenance(s_), **clean(s_.state())}

    @api.post("/paper/sessions/{sid}/close")
    def paper_close(sid: str):
        """Stop tracking a session. Its journal is kept on disk; it is no longer restored or scheduled."""
        _session(sid)
        with st.lock:
            st.store.close(sid)
            st.sessions.pop(sid, None)
            st.session_meta.pop(sid, None)
        reg.audit("user", "PAPER_SESSION_CLOSED", session=sid)
        return {"closed": sid}

    @api.get("/paper/scheduler")
    def scheduler_status():
        return clean({**scheduler.status(), "restore_errors": st.restore_errors, "sessions": len(st.sessions),
                      "holiday_calendar": "supplied" if (st.exp_dir / "holidays.json").exists() else "NOT SUPPLIED (weekends only)"})

    @api.post("/paper/scheduler/tick")
    def scheduler_tick():
        """Run one scheduler pass now (the timer does this every minute when the scheduler is enabled)."""
        return clean({"done": scheduler.tick(), **scheduler.status()})

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

    # ------------------------------------------------------------------ learning brain
    brain = st.brain

    @api.get("/brain/summary")
    def brain_summary():
        return clean(brain.summary())

    @api.get("/brain/reports/{run_id}")
    def brain_report(run_id: str, curves: bool = True):
        try:
            r = brain.report(run_id)
        except KeyError:
            raise HTTPException(404, "unknown report") from None
        if not curves:
            for a in r["arms"].values():
                a.pop("curve", None)
        return clean(r)

    @api.get("/brain/verify")
    def brain_verify():
        return {"memory": brain.memory.verify(), "probes": brain.probes()}

    class BrainBenchIn(BaseModel):
        dataset_id: str = FIXTURE_ID
        negative_control: bool = False      # also run the zero-drift random-walk control
        label: str = ""

    @api.post("/brain/benchmark")
    def brain_benchmark(body: BrainBenchIn):
        d = st.dataset(body.dataset_id)
        part = get_partition(reg, d)
        if any(j["kind"] == "brain_benchmark" and j["status"] == "running" for j in st.jobs.values()):
            raise HTTPException(409, "a learning benchmark is already running")

        def work(job):
            out = {"main": brain.run_benchmark(d.id, end_date=part.val_end, label=body.label or f"{d.id} development+validation",
                                               progress=lambda i, n: job["progress"].append({"epoch": i, "of": n}))["run_id"]}
            if body.negative_control:
                from nseql.data.fixtures import generate_null_fixture
                if not (st.data_dir / "null_v1").exists():
                    generate_null_fixture(st.data_dir)
                out["negative_control"] = brain.run_benchmark("null_v1", end_date=part.val_end, label="negative control (random walk)")["run_id"]
            reg.audit("user", "BRAIN_BENCHMARK", **out)
            return out
        return st.job("brain_benchmark", work, dataset=d.id)

    # ------------------------------------------------------------------ one-minute screening (nseql.screening)
    screening: dict = {"sch": None, "runs": {}}

    def _run_id(meta: dict) -> str:
        return f"{meta.get('mode', 'replay')}:{Path(meta['root']).name}"

    def _live_active() -> bool:
        return (screening.get("live") or {}).get("state") in ("starting", "running")

    def _register(sch, meta: dict, activate: bool) -> str:
        """Every run has an explicit identity (review 2026-11-24, finding 3). The dashboard's active run is the
        live paper account while it starts or runs; a finished replay is registered beside it, never over it."""
        rid = _run_id(meta)
        screening["runs"][rid] = (sch, meta)
        if activate:
            screening["sch"], screening["meta"] = sch, meta
        return rid

    def _screening_open():
        """Reopen the most recent screening run after an API restart (its checkpoint is recovered)."""
        if screening["sch"] is not None:
            return screening["sch"]
        if (screening.get("live") or {}).get("state") in ("starting", "running"):
            return None                             # the live bot owns the account; a read-only copy would replace it on the dashboard
        meta = st.exp_dir / "screening" / "current.json"
        if not meta.exists():
            return None
        from nseql.screening.scheduler import MinuteScheduler
        m = json.loads(meta.read_text())
        from nseql.screening.scheduler import ReplayMinuteFeed
        feed = ReplayMinuteFeed({}, f"replay of {m.get('dataset')} (finished)", synthetic=bool(m.get("synthetic"))) if m["mode"] == "replay" else None
        from nseql.screening.execution import PortfolioRules
        try:                                        # a live account keeps its own starting capital
            screening["sch"] = MinuteScheduler(Path(m["root"]), m["symbols"], m["configs"], kill=st.kill, mode=m["mode"], feed=feed,
                                               experiments_dir=st.exp_dir,
                                               portfolio=(PortfolioRules.live(m.get("start_cash") or m["capital"]) if m.get("rules_version") else PortfolioRules(capital=m["capital"]))
                                               if m.get("capital") else True)
        except ValueError as e:                     # saved with another strategy list: report it, do not break every endpoint
            if "registry" not in str(e):
                raise
            screening["outdated"] = (f"The last screening run ({Path(m['root']).name}) was made with an older strategy list and cannot be "
                                     "resumed. Start a new replay, or restart with live screening for a fresh paper account.")
            return None
        screening["meta"] = m
        _register(screening["sch"], m, activate=True)
        return screening["sch"]

    class ScreenReplayIn(BaseModel):
        dataset_id: str = FIXTURE_ID
        sessions: int = Field(5, ge=1, le=60)
        configs: int | None = Field(None, ge=1, le=50_000)

    @api.post("/screening/replay")
    def screening_replay(body: ScreenReplayIn):
        """Run the one-minute scheduler over stored 1-minute bars (a REPLAY, not a live feed)."""
        from nseql.screening.scheduler import MinuteScheduler, ReplayMinuteFeed
        d = st.dataset(body.dataset_id)
        syms = d.symbols("1m")
        if not syms:
            raise HTTPException(400, f"dataset {d.id} has no 1-minute bars")
        if any(j["kind"] == "screening_replay" and j["status"] == "running" for j in st.jobs.values()):
            raise HTTPException(409, "a screening replay is already running")
        frames = {}
        for s in syms:
            df = d.bars(s, "1m")
            days = sorted(df["ts"].dt.normalize().unique())[: body.sessions]
            frames[s] = df[df["ts"].dt.normalize().isin(days)].reset_index(drop=True)
        root = st.exp_dir / "screening" / f"replay_{datetime.now():%Y%m%d_%H%M%S}"

        def work(job):
            feed = ReplayMinuteFeed(frames, f"replay of {d.id}" + (" (SYNTHETIC)" if d.synthetic else ""), synthetic=d.synthetic)
            sch = MinuteScheduler(root, syms, body.configs, feed=feed, kill=st.kill, mode="replay", experiments_dir=st.exp_dir)
            mins = feed.minutes()
            for k, m in enumerate(mins):
                sch.run_cycle(m + pd.Timedelta(seconds=6))
                if k % 50 == 0 or k == len(mins) - 1:
                    job["progress"].append({"minute": k + 1, "of": len(mins)})
            sch.checkpoint()
            meta = {"root": str(root), "symbols": syms, "configs": body.configs, "mode": "replay", "dataset": d.id, "synthetic": d.synthetic}
            activate = not _live_active()
            if activate:                                      # never point a restart at a replay while a live account runs
                (st.exp_dir / "screening" / "current.json").write_text(json.dumps(meta))
            _register(sch, meta, activate)
            reg.audit("user", "SCREENING_REPLAY", dataset=d.id, registered=len(sch.reg), minutes=len(mins))
            return {"registered": len(sch.reg), "minutes": len(mins), **sch.store.counts()}
        return st.job("screening_replay", work, dataset=d.id)

    def _watchlist() -> list[str]:
        from nseql.data.adapters.angelone import watchlist
        return watchlist(st.exp_dir)

    def _attach_challenger(sch, name: str, syms: list[str], log) -> None:
        """Forward contest: an isolated challenger paper account fed the incumbent's exact bars every minute."""
        from nseql.screening.challenger import CHALLENGER_VERSION, challenger_rules, factory
        from nseql.screening.scheduler import MinuteScheduler
        croot = st.exp_dir / "screening" / f"{name}_challenger_{CHALLENGER_VERSION.replace('.', '_')}"
        cf = croot / "live.json"
        now = datetime.now()
        if cf.exists():
            cm = json.loads(cf.read_text())
        else:
            first_day = now.date() if now.hour * 60 + now.minute < 9 * 60 + 16 else None
            cm = {"challenger": CHALLENGER_VERSION, "incumbent_account": name, "start_cash": round(sch.engine.x.equity() / 100, 2),
                  "live_from": now.strftime("%Y-%m-%dT%H:%M:00"),
                  "contest_from": (first_day or (pd.Timestamp(now.date()) + pd.offsets.BDay(1)).date()).isoformat(),
                  "symbols": syms, "note": "forward contest: sessions counted from contest_from (first full session)"}
        ch = MinuteScheduler(croot, syms, kill=st.kill, mode="live", calendar=_calendar(), live_from=cm["live_from"],
                             experiments_dir=st.exp_dir, portfolio=challenger_rules(cm["start_cash"]), engine_factory=factory())
        if not cf.exists():
            croot.mkdir(parents=True, exist_ok=True)
            cf.write_text(json.dumps(cm))
            today = now.strftime("%Y-%m-%dT09:15:00")             # today's bars so far build its opening range (no past trading)
            with sch.store._lock:
                rows = sch.store.db.execute("SELECT symbol, ts, open, high, low, close, volume FROM events WHERE ts > ? AND late = 0",
                                            (today,)).fetchall()
            if rows:
                ch.ingest([{"symbol": r[0], "ts": pd.Timestamp(r[1]), "open": r[2], "high": r[3], "low": r[4], "close": r[5], "volume": r[6]}
                           for r in rows], source="incumbent's stored bars (opening range)")
                ch.run_cycle(pd.Timestamp(now))
        sch.followers.append(ch)
        screening["challenger"] = (ch, cm)
        _register(ch, {"root": str(croot), "mode": "live", "account": croot.name, "challenger": CHALLENGER_VERSION}, activate=False)
        log(f"challenger {CHALLENGER_VERSION}: paper account {croot.name} (start cash {cm['start_cash']:,.2f}), contest from {cm['contest_from']}")

    def _review_busy() -> bool:
        """The daily review waits while a session is on (09:00-15:35 IST on a trading day): the live bot keeps the CPU."""
        now = datetime.now(views.IST).replace(tzinfo=None)
        return _calendar().is_session(now.date()) and 9 * 60 <= now.hour * 60 + now.minute < 15 * 60 + 35

    from nseql.screening.daily_review import ReviewQueue
    reviews = ReviewQueue(st.exp_dir, busy=_review_busy,
                          log=lambda m: print(f"[review] {datetime.now():%Y-%m-%d %H:%M:%S} {m}", flush=True))
    app.state.reviews = reviews

    def _start_live_screening() -> None:
        """Unattended: log in to Angel One, fetch daily history once, warm up, then screen every minute.
        Paper orders only from the live start (kept across restarts). Errors are visible in /screening/status."""
        from nseql.data.adapters.angelone import credentials_present, missing_credentials, resolve as angel_resolve
        from nseql.screening.feeds import BrokerMinutePoller, warm_up
        from nseql.screening.scheduler import MinuteScheduler
        screening["live"] = {"state": "starting", "broker": "angelone"}
        log = lambda msg: print(f"[live-screening] {datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)   # goes to experiments/logs/api.log
        log("starting: logging in to Angel One")
        try:
            if not credentials_present():
                raise RuntimeError(f"Angel One credentials missing in .env: {', '.join(missing_credentials())}")
            api = _angel_api()
            api.ensure_login()
            master = api.instruments(st.exp_dir)
            tokens, missing = angel_resolve(master, _watchlist())
            index = [t for t in tokens if t.get("kind") == "INDEX"]
            tokens = [t for t in tokens if t.get("kind") != "INDEX"]       # an index is context, never a tradable paper position
            if index:
                log(f"not traded (index, context only): {', '.join(t['symbol'] for t in index)}")
            if missing:
                from nseql.data.adapters.angelone import suggest
                for w in missing:
                    log(f"not found at Angel One: {w}; close matches: {', '.join(suggest(master, w)) or 'none'} (edit the watchlist)")
            if not tokens:
                raise RuntimeError(f"none of the watchlist symbols were found at Angel One: {missing}")
            from nseql.screening.execution import LIVE_RULES_VERSION, PortfolioRules
            capital = float(os.environ.get("NSEQL_PAPER_CAPITAL") or PortfolioRules.capital)
            if capital < 10_000:
                raise RuntimeError(f"NSEQL_PAPER_CAPITAL must be at least 10000 (got {capital:g})")
            # each starting capital is its own paper account: a new amount starts from scratch, it never inherits
            # another account's cash or positions; NSEQL_LIVE_RUN names a fresh account explicitly
            from nseql.screening.registry import REGISTRY_VERSION
            # a new strategy list (registry version) also opens a fresh account: old checkpoints cannot be resumed
            # ... and so does a new set of live trading rules (1.1: churn limits, index not traded)
            name = os.environ.get("NSEQL_LIVE_RUN") or ("live_angelone" + ("" if capital == PortfolioRules.capital else f"_{int(capital)}")
                                                        + ("" if REGISTRY_VERSION == 1 else f"_r{REGISTRY_VERSION}")
                                                        + f"_v{LIVE_RULES_VERSION.replace('.', '')}")
            if not name.replace("_", "").replace("-", "").isalnum():
                raise RuntimeError("NSEQL_LIVE_RUN may contain only letters, digits, '-' and '_'")
            root = st.exp_dir / "screening" / name
            root.mkdir(parents=True, exist_ok=True)
            lf = root / "live.json"
            first = not lf.exists()
            live_from = json.loads(lf.read_text())["live_from"] if not first else datetime.now().strftime("%Y-%m-%dT%H:%M:00")
            # a new rules version continues the same money and track record (operator: "continue with same money")
            carried = None
            if first and not os.environ.get("NSEQL_LIVE_RUN"):
                from nseql.screening.carry import CarryError, carry_state, find_previous
                prev = find_previous(st.exp_dir / "screening", name)
                if prev is not None:
                    try:
                        carried = carry_state(prev)
                    except CarryError as e:
                        raise RuntimeError(str(e)) from None
            old = {} if first else json.loads(lf.read_text())
            start_cash = float(carried["cash_inr"]) if carried else float(old.get("start_cash") or capital)
            original = float((carried or {}).get("original_capital") or old.get("original_capital") or capital)
            syms = [x["symbol"] for x in tokens]
            if not first and json.loads(lf.read_text()).get("capital", PortfolioRules.capital) != capital:
                raise RuntimeError(f"paper account '{name}' was started with a different capital; set NSEQL_LIVE_RUN to a new name to start fresh")
            sch = MinuteScheduler(root, syms, None, feed=BrokerMinutePoller(tokens, _angel_api), kill=st.kill, mode="live",
                                  calendar=_calendar(), live_from=live_from, experiments_dir=st.exp_dir, portfolio=PortfolioRules.live(start_cash))
            if first:
                lf.write_text(json.dumps({"live_from": live_from, "symbols": syms, "not_found": missing, "capital": capital, "account": name,
                                          "start_cash": start_cash, "original_capital": original,
                                          "carried_from": {k: v for k, v in (carried or {}).items() if k != "evidence"} or None}))
                if carried:
                    from nseql.screening.carry import apply_evidence, save_inherited
                    n_ev = apply_evidence(sch, carried["evidence"])
                    save_inherited(root, carried)
                    log(f"continuing from {carried['from_account']}: cash {start_cash:,.2f} (as of {carried['as_of']}), "
                        f"track record of {carried['round_trips']} round trips for {n_ev} strategies; original capital {original:,.0f}")
                    reg.audit("system", "LIVE_ACCOUNT_CONTINUED", account=name, from_account=carried["from_account"], cash_inr=start_cash,
                              round_trips=carried["round_trips"], strategies=n_ev)
                screening["live"]["warm_up"] = warm_up(sch, api, tokens, days=5)
            meta = {"root": str(root), "symbols": syms, "configs": None, "mode": "live", "broker": "angelone", "live_from": live_from,
                    "synthetic": False, "not_found": missing, "capital": capital, "account": name, "rules_version": LIVE_RULES_VERSION,
                    "start_cash": start_cash, "original_capital": original}
            (st.exp_dir / "screening" / "current.json").write_text(json.dumps(meta))
            sch.news_count = lambda sym, t: (st.news_watch.symbol_count(sym, t) if getattr(st, "news_watch", None) else None)   # journal context
            _register(sch, meta, activate=True)
            if os.environ.get("NSEQL_CHALLENGER", "on").lower() not in ("off", "false", "0"):
                try:
                    _attach_challenger(sch, name, syms, log)
                except Exception as e:      # noqa: BLE001 - the challenger never stops the incumbent
                    log(f"challenger not started: {type(e).__name__}: {e}")
            sch.on_session_close = lambda day, _n=name: reviews.submit(_n, day)     # automatic daily review after 15:30
            sch.start()
            try:
                q = reviews.catch_up()
                if q:
                    log(f"daily review: queued {', '.join(d for _, d in q)} (runs after the close, one at a time)")
            except Exception as e:      # noqa: BLE001
                log(f"daily review catch-up failed: {type(e).__name__}: {e}")
            screening["live"].update(state="running", symbols=len(syms), live_from=live_from, not_found=missing, capital=capital, account=name)
            log(f"running: {len(syms)} symbols, {len(sch.reg):,} configurations, paper account {name} (capital {capital:,.0f}, rules {LIVE_RULES_VERSION}), "
                f"paper orders from {live_from}" + (f"; not found: {', '.join(missing)}" if missing else ""))
            reg.audit("system", "LIVE_SCREENING_STARTED", broker="angelone", symbols=len(syms), live_from=live_from, account=name,
                      rules_version=LIVE_RULES_VERSION, rules=PortfolioRules.live(capital).to_dict())
        except Exception as e:                      # reported, never crashes the API
            screening["live"].update(state="error", error=f"{type(e).__name__}: {e}")
            log(f"ERROR: could not start: {type(e).__name__}: {e}")
            st.notify("feed", f"Live screening could not start: {type(e).__name__}: {e}")
            return
        hist = "angel_daily"                         # daily history for backtests, once; a failure here does not stop screening
        if not (st.data_dir / hist).exists():
            try:
                from datetime import date as _date

                from nseql.data.adapters.angel_import import import_angel
                screening["live"]["history"] = "downloading 2 years of daily candles"
                ds, _ = import_angel(api, [s for s in syms if s != "NIFTY50"], _date.today().replace(year=_date.today().year - 2), _date.today(),
                                     st.data_dir, hist, licence="Angel One SmartAPI, personal research", cache_dir=st.exp_dir,
                                     progress=lambda *_: None)
                screening["live"]["history"] = f"dataset {ds.id}: {len(ds.instruments)} instruments"
            except Exception as e:
                screening["live"]["history"] = f"download failed: {type(e).__name__}: {e} (will retry at the next start)"


    def _need_screening():
        sch = _screening_open()
        if sch is None:
            raise HTTPException(404, "no screening run yet: start a replay (POST /screening/replay), or set NSEQL_LIVE_SCREENING=angelone "
                                     "for the Angel One 1-minute polling feed (mock-tested, not yet verified against the real service)")
        return sch

    @api.get("/screening/runs")
    def screening_runs():
        """Every screening run this API knows, with its identity; exactly one is active on the dashboard."""
        _screening_open()
        act = screening.get("meta")
        return clean({"runs": [{"run_id": rid, "mode": m.get("mode"), "account": Path(m["root"]).name, "active": m is act,
                                "watermark": sch.watermark, "running": sch._thread is not None and sch._thread.is_alive(),
                                "synthetic": m.get("synthetic")} for rid, (sch, m) in screening["runs"].items()]})

    @api.get("/screening/status")
    def screening_status(run: str = ""):
        live = screening.get("live")
        if run:
            if run not in screening["runs"]:
                raise HTTPException(404, f"unknown run {run!r}; see /screening/runs")
            sch, meta = screening["runs"][run]
        else:
            sch, meta = _screening_open(), screening.get("meta")
        if sch is None:
            return clean({"started": False, "live": live or {"state": "off", "how": "set NSEQL_LIVE_SCREENING=angelone and the ANGEL_* keys in .env"},
                          "outdated_run": screening.get("outdated"), "live_trading": False})
        return clean({"started": True, **sch.status(), "run_id": _run_id(meta), "run": meta, "live": live})

    @api.get("/screening/summary")
    def screening_summary():
        sch = _need_screening()
        db = sch.store.db
        reg_ = sch.reg
        by_h = {}
        for h in ("intraday", "swing"):
            mask = reg_.intraday if h == "intraday" else ~reg_.intraday
            last = sch.engine.last_eval[mask]
            by_h[h] = {"registered": int(mask.sum()), "evaluated_ever": int((last >= 0).sum()),
                       "evaluated_last_minute": int((last == sch.engine.last_minute).sum()) if sch.engine.last_minute >= 0 else 0,
                       "open_positions": int(((sch.engine.pos == 1) & mask).sum()), "pending_orders": int(((sch.engine.pend != 0) & mask).sum())}
        pnl = {r[0]: {"filled": r[1], "realized_inr": (r[2] or 0) / 100, "charges_inr": (r[3] or 0) / 100}
               for r in db.execute("SELECT horizon, COUNT(*), SUM(realized_paise), SUM(charges_paise) FROM paper_orders WHERE status='filled' GROUP BY horizon")}
        lat = [r[0] for r in db.execute("SELECT latency_ms FROM cycles WHERE status='processed' ORDER BY minute DESC LIMIT 375")]
        return clean({"horizons": by_h, "paper": pnl, "decisions": dict(db.execute("SELECT decision, COUNT(*) FROM signals GROUP BY decision").fetchall()),
                      "buy_allocation": dict(db.execute("SELECT COALESCE(alloc_status, 'not recorded'), COUNT(*) FROM signals WHERE decision='BUY' "
                                                        "GROUP BY 1").fetchall()),
                      "latency_ms_recent": {"p50": float(np.percentile(lat, 50)), "p99": float(np.percentile(lat, 99)), "n": len(lat)} if lat else None,
                      "run": screening.get("meta"), "watermark": sch.watermark,
                      "note": "Forward PAPER ledger of the screening loop: simulated fills from the next minute "
                              + ("(volume-capped, with spread and impact) in one shared capital pot" if sch.engine.x.rules else "at the open, Rs 1,00,000 per configuration")
                              + ", dated charges. Not historical research results and not real orders."})

    _brain_cache: dict = {}

    def _brain_status() -> dict:
        """Learning Brain status from its own benchmark KPIs (cached 60 s). Storing results is not learning:
        'improving' is claimed only when the KPI the brain defines for out-of-sample lift is met."""
        if _brain_cache.get("at", 0) > time.time() - 60:
            return _brain_cache["v"]
        try:
            sm = brain.summary()
            k = {r["key"]: r["status"] for r in sm.get("kpis", [])}
            v = {"memory_episodes": sm["memory"]["episodes"], "champion": sm.get("champion"), "kpis": k,
                 "verdict": "out-of-sample improvement shown" if k.get("oos_lift") == "met" else
                            ("no reliable out-of-sample improvement yet" if k.get("oos_lift") == "not met" else "not benchmarked yet")}
        except Exception as e:  # noqa: BLE001 - the overview must render even if the brain store is unreadable
            v = {"error": f"{type(e).__name__}: {e}", "verdict": "unavailable"}
        _brain_cache.update(at=time.time(), v=v)
        return v

    def _readiness() -> list[dict]:
        """Each module's readiness from live runtime state. Nothing is assumed: unknown is reported as such."""
        from nseql.data.adapters.angelone import missing_credentials
        from nseql.screening.provenance import verification_status
        sch = _screening_open()
        rows = [{"module": "API", "status": "ready", "detail": f"up {round(time.time() - st.started)} s"}]
        if sch is None:
            live = screening.get("live") or {}
            rows.append({"module": "Screening loop", "status": "blocked" if live.get("state") == "error" else "idle",
                         "detail": live.get("error") or "no run yet (start a replay or the Angel One feed)"})
        else:
            lc = sch.last_cycle or {}
            lms = [v for v in (lc.get("latency_ms") or []) if v is not None]
            rows.append({"module": "Screening loop", "status": "degraded" if sch.overruns or lc.get("error") else "ready",
                         "detail": f"{len(sch.reg):,} configurations; last minute {sch.watermark or 'none'}; overruns {sch.overruns}"
                                   + (f"; slowest minute in last cycle {max(lms):.0f} ms" if lms else "")
                                   + (f"; last cycle failed: {lc['error']}" if lc.get("error") else "")})
            ds = sch.provenance()
            rows.append({"module": "Market data", "status": "degraded" if ds.get("stale") else ("ready" if ds.get("verified") or not ds.get("live") else "unverified"),
                         "detail": ds.get("label", "")})
            fh = sch.feed_health()
            if fh:
                rows.append({"module": "Feed health", "status": "degraded" if fh["degraded"] or fh["stale_symbols"] or fh["errors"] else "ready",
                             "detail": f"{fh['symbols']} symbols, capacity {fh['capacity_per_minute']}/min"
                                       + (f", rotation every {fh['rotation_minutes']} min" if fh["degraded"] else "")
                                       + (f", {len(fh['stale_symbols'])} stale" if fh["stale_symbols"] else "") + (f", {fh['errors']} errors" if fh["errors"] else "")})
        miss = missing_credentials()
        vs = verification_status(st.exp_dir)
        rows.append({"module": "Angel One adapter", "status": "ready" if vs["verified"] else ("unverified" if not miss else "not configured"),
                     "detail": vs["detail"] if not miss else f"missing {', '.join(miss)}"})
        ok, why = sandbox.isolation_status()
        rows.append({"module": "Research sandbox", "status": "ready" if ok else "blocked", "detail": why})
        rows.append({"module": "AI analyst (LLM)", "status": "ready" if llm_mod.credentials_present() else "not configured",
                     "detail": llm_mod.MODEL if llm_mod.credentials_present() else "rule-based analyst in use (no ANTHROPIC_API_KEY)"})
        b = _brain_status()
        rows.append({"module": "Learning Brain", "status": "unavailable" if "error" in b else "ready", "detail": b.get("error") or b["verdict"]})
        rows.append({"module": "Kill switch", "status": "engaged" if st.kill.engaged else "ready",
                     "detail": st.kill.reason or "clear" if st.kill.engaged else "clear; engage from the Risk page or the Deck"})
        rows.append({"module": "Live order routing", "status": "disabled", "detail": "LIVE_TRADING_ENABLED=false; no agent can change it"
                     if not st.settings.live_trading_enabled else "flag set in the environment; the live gate still requires human authorisation"})
        n = st.notifier.status()
        rows.append({"module": "WhatsApp notifications", "status": "unverified" if n["configured"] else "not configured", "detail": n["status"]})
        return rows

    @api.get("/readiness")
    def readiness():
        return clean({"modules": _readiness(), "note": "Derived from live runtime state at request time; 'unverified' = mock-tested only."})

    @api.get("/screening/overview")
    def screening_overview():
        """Everything the Strategy Monitor and the Deck show about the loop, from stored results only."""
        sch = _need_screening()
        db, e, x = sch.store.db, sch.engine, sch.engine.x
        q1 = lambda sql, *a: db.execute(sql, a).fetchone()[0]
        last = e.last_minute
        cyc = db.execute("SELECT status, COUNT(*) FROM cycles GROUP BY status").fetchall()
        lat = [r[0] for r in db.execute("SELECT latency_ms FROM cycles WHERE status='processed' ORDER BY minute DESC LIMIT 375")]
        lag = [r[0] for r in db.execute("SELECT lag_s FROM cycles WHERE status='processed' AND mode != 'warmup' ORDER BY minute DESC LIMIT 375") if r[0] is not None]
        snap = x.snapshot()
        alloc = snap["allocation"]
        eq = snap["equity_inr"]
        from nseql.screening.metrics import account_figures, reason_category, session_decisions
        meta_ = screening.get("meta") or {}
        today_ = datetime.now(views.IST).strftime("%Y-%m-%d")
        last_day = db.execute("SELECT MAX(substr(minute,1,10)) FROM cycles WHERE status='processed'").fetchone()[0] or today_
        fig = account_figures(sch, meta_, today_)
        start_cap = fig["starting_capital"]
        session = session_decisions(db, last_day, meta_.get("live_from"))
        by_cat: dict[str, int] = {}
        for why, n in db.execute("SELECT reason, SUM(n) FROM refusals GROUP BY reason").fetchall():
            c = reason_category(why)
            by_cat[c] = by_cat.get(c, 0) + int(n)
        refused = dict(sorted(by_cat.items(), key=lambda kv: -kv[1]))
        realised = db.execute("SELECT COALESCE(SUM(realized_paise), 0), COALESCE(SUM(charges_paise), 0) FROM paper_orders WHERE side='SELL'").fetchone()
        charges_all = q1("SELECT COALESCE(SUM(charges_paise), 0) FROM paper_orders")
        return clean({
            "data_source": sch.provenance(), "feed_health": sch.feed_health(),
            "configurations": {"registered": len(sch.reg), "evaluated_last_cycle": int((e.last_eval == last).sum()) if last >= 0 else 0,
                               "evaluated_ever": int((e.last_eval >= 0).sum()),
                               "qualified": alloc["qualified_configs"], "with_forward_evidence": alloc["configs_with_evidence"],
                               "unverified": len(sch.reg) - alloc["qualified_configs"],
                               "qualified_rule": f"at least {x.alloc.cfg.min_trades} forward round trips in this paper ledger and a positive shrunk mean net return"},
            "cycles": {"by_status": dict(cyc), "missed_minutes": sum(n for s_, n in cyc if s_ != "processed"), "overruns": sch.overruns,
                       "latency_ms": {"p50": float(np.percentile(lat, 50)), "p99": float(np.percentile(lat, 99)), "max": float(max(lat))} if lat else None,
                       "lag_s": {"p50": float(np.percentile(lag, 50)), "max": float(max(lag))} if lag else None},
            "signals": {"by_decision": dict(db.execute("SELECT decision, COUNT(*) FROM signals GROUP BY decision").fetchall()),
                        "buy_allocation": dict(db.execute("SELECT COALESCE(alloc_status, 'not recorded'), COUNT(*) FROM signals WHERE decision='BUY' GROUP BY 1").fetchall()),
                        "refusal_reasons": refused,
                        "refusal_reasons_note": "all-time totals by recorded reason category; 'warm-up history' = historical bars before the live "
                                                "start (never tradable)",
                        "latest_session": session},
            "capital": {"equity_inr": eq, "starting_capital_inr": start_cap, "cash_inr": snap["cash_inr"], "reserved_inr": snap["reserved_inr"],
                        "gross_exposure_inr": snap["gross_exposure_inr"],
                        "utilisation_pct": round(100 * (snap["gross_exposure_inr"] + snap["reserved_inr"]) / eq, 2) if eq else None,
                        "open_positions": snap["open_positions"], "halted_today": snap["halted_today"]},
            "paper_performance": {"equity_change_inr": fig["pnl_since_start"], "pnl_today_inr": fig["pnl_today"], "windows": fig["windows"],
                                  "realised_after_charges_inr": realised[0] / 100, "charges_paid_inr": charges_all / 100,
                                  "note": "simulated fills on " + sch.provenance().get("label", "unknown data") + "; not real trading results"},
            "allocation": alloc, "learning_brain": _brain_status(),
            "failures": {"feed_status": getattr(sch.feed, "status", None), "feed_errors": dict(list(getattr(sch.feed, "errors", {}).items())[:20]),
                         "event_conflicts": q1("SELECT COUNT(*) FROM event_conflicts"), "late_events": q1("SELECT COUNT(*) FROM events WHERE late=1"),
                         "portfolio_events": [dict(r) for r in db.execute("SELECT ts, event FROM portfolio_events ORDER BY ts DESC LIMIT 10")],
                         "live": screening.get("live")},
            "recovery": sch.recovery, "readiness": _readiness(),
        })

    # ------------------------------------------------------------------ V6: platform status, Command Center, Strategy Universe
    from nseql.analytics import planning as plan_mod
    from nseql.api import views
    from nseql.data.funds import FundStore
    from nseql.research import copilot as copilot_mod
    from nseql.screening import universe as uni

    @api.get("/platform/status")
    def platform_status(request: Request):
        sch = _screening_open()
        rd = _readiness()
        return clean({"data_source": sch.provenance() if sch is not None else {"kind": "none", "label": "No market data attached", "live": False},
                      "market": views.market_session(_calendar()), "kill_switch": {"engaged": st.kill.engaged, "reason": st.kill.reason},
                      "live_trading_enabled": st.settings.live_trading_enabled, "role": request.state.role, "auth_required": not insecure_local,
                      "modules": {"ready": sum(m["status"] == "ready" for m in rd), "total": len(rd),
                                  "attention": [m["module"] for m in rd if m["status"] in ("blocked", "degraded", "engaged", "unavailable")]},
                      "last_candle": sch.watermark if sch is not None else None, "run_id": _run_id(screening["meta"]) if screening.get("meta") else None,
                      "version": code_version()})

    @api.get("/command-center")
    def command_center():
        sch = _screening_open()
        try:
            bstate = brain.versions.state()
        except Exception:  # noqa: BLE001 - the page renders without the brain section
            bstate = {}
        return clean(views.command_center(st=st, sch=sch, reg=reg, brain_status=_brain_status(), brain_state=bstate, readiness=_readiness(),
                                          calendar=_calendar(), universe_summary=uni.summary(sch) if sch is not None else None,
                                          sandbox_status=sandbox.isolation_status(), meta=screening.get("meta")))

    @api.get("/universe/summary")
    def universe_summary():
        return clean(uni.summary(_need_screening(), reg))

    @api.get("/universe/configs")
    def universe_configs(family: str = "", timeframe: str = "", horizon: str = "", symbol: str = "", stage: str = "", q: str = "",
                         sort: str = "trades", desc: bool = True, limit: int = 100, offset: int = 0):
        if stage and stage not in uni.STAGES:
            raise HTTPException(400, f"stage must be one of {', '.join(uni.STAGES)}")
        return clean(uni.page(_need_screening(), family=family, timeframe=timeframe, horizon=horizon, symbol=symbol, stage=stage, q=q,
                              sort=sort, desc=desc, limit=min(max(limit, 1), 500), offset=max(offset, 0)))

    @api.get("/universe/configs/{config_id}")
    def universe_dossier(config_id: str):
        d = uni.dossier(_need_screening(), config_id, reg)
        if d is None:
            raise HTTPException(404, f"no configuration {config_id} in the current screening run")
        return clean(d)

    class _CopilotCtx:
        reg = st.registry
        news = staticmethod(lambda: getattr(st, "news_watch", None))
        sch = staticmethod(_screening_open)
        readiness = staticmethod(_readiness)
        brain_status = staticmethod(_brain_status)
        features = staticmethod(lambda: FEATURES)

    @api.get("/copilot/ask")
    def copilot_ask(q: str, page: str = ""):
        if not q.strip() or len(q) > 500:
            raise HTTPException(400, "ask a question of 1-500 characters")
        out = copilot_mod.answer(q, _CopilotCtx)
        return clean({**out, "question": q, "page": page})

    @api.get("/copilot/suggestions")
    def copilot_suggestions():
        return {"questions": [q for _, q in copilot_mod.SUPPORTED]}

    # ------------------------------------------------------------------ V6: investment planning (projections at user-assumed returns)
    def _pv(fn, *a, **k):
        try:
            return clean(fn(*a, **k))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @api.get("/planning/sip")
    def planning_sip(monthly: float, years: float, annual_return: float, step_up: float = 0.0):
        return _pv(plan_mod.project_sip, monthly, int(round(years * 12)), annual_return, step_up=step_up)

    @api.get("/planning/lumpsum")
    def planning_lumpsum(amount: float, years: float, annual_return: float):
        return _pv(plan_mod.project_lumpsum, amount, years, annual_return)

    @api.get("/planning/swp")
    def planning_swp(corpus: float, monthly: float, annual_return: float, step_up: float = 0.0, years: float = 50):
        return _pv(plan_mod.project_swp, corpus, monthly, annual_return, months=int(round(years * 12)), step_up=step_up)

    @api.get("/planning/stp")
    def planning_stp(corpus: float, monthly: float, source_return: float, target_return: float):
        return _pv(plan_mod.project_stp, corpus, monthly, source_return, target_return)

    @api.get("/planning/goal")
    def planning_goal(target_today: float, years: float, annual_return: float, inflation: float = 0.06, existing: float = 0.0, step_up: float = 0.0):
        return _pv(plan_mod.goal_plan, target_today, years, annual_return, inflation, existing=existing, step_up=step_up)

    # ------------------------------------------------------------------ V6: mutual funds (daily NAVs, AMFI files)
    funds = FundStore(st.data_dir)

    @api.get("/funds/status")
    def funds_status():
        return clean({**funds.status(), "source": "AMFI NAV files imported by the operator",
                      "how": "nseql import-amfi <NAVAll.txt or NAV history files>, or POST /api/funds/import with the file text"})

    @api.get("/funds/schemes")
    def funds_schemes(q: str = "", category: str = "", plan: str = "", limit: int = 50, offset: int = 0):
        return clean(funds.search(q, category, plan, min(max(limit, 1), 200), max(offset, 0)))

    @api.get("/funds/schemes/{code}")
    def funds_scheme(code: str, start: str | None = None, end: str | None = None):
        sc = funds.scheme(code)
        if sc is None:
            raise HTTPException(404, f"no scheme {code}")
        d, v = funds.series(code, start, end)
        step = max(1, len(d) // 600)
        return clean({"scheme": sc, "analytics": plan_mod.nav_analytics(d, v), "series": {"dates": [x.isoformat() for x in d[::step]], "nav": v[::step]},
                      "provenance": {"source": "AMFI published NAV", "points": len(d), "period": [d[0].isoformat(), d[-1].isoformat()] if d else None}})

    @api.get("/funds/schemes/{code}/sip")
    def funds_sip(code: str, monthly: float, start: str, end: str, day: int = 1, step_up: float = 0.0, exit_load: float = 0.0):
        d, v = funds.series(code)
        if not d:
            raise HTTPException(404, f"no NAVs for scheme {code}")
        return _pv(plan_mod.sip_on_nav, d, v, monthly, date.fromisoformat(start), date.fromisoformat(end), day=day, step_up=step_up, exit_load=exit_load)

    class FundImportIn(BaseModel):
        text: str = Field(max_length=60_000_000)
        file: str = "upload"

    @api.post("/funds/import")
    def funds_import(body: FundImportIn):
        try:
            out = funds.import_text(body.text, file=body.file)
        except ValueError as e:
            raise HTTPException(400, str(e))
        reg.audit("operator", "FUNDS_IMPORT", file=body.file, **{k: out[k] for k in ("schemes", "rows", "new_navs", "skipped")})
        return out

    def _page(table: str, order: str, where: dict, limit: int, offset: int):
        try:
            return clean(_need_screening().store.page(table, where=where, order=order, limit=min(max(limit, 1), 500), offset=max(offset, 0)))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @api.get("/screening/configs")
    def screening_configs(horizon: str = "", family: str = "", timeframe: str = "", symbol: str = "", limit: int = 50, offset: int = 0):
        return _page("configs", "idx", {"horizon": horizon, "family": family, "timeframe": timeframe, "symbol": symbol}, limit, offset)

    def _day(col: str, day: str) -> list:
        if not day:
            return []
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise HTTPException(400, "day must be YYYY-MM-DD")
        return [(f"substr({col},1,10) = ?", [day])]

    @api.get("/screening/signals")
    def screening_signals(horizon: str = "", symbol: str = "", decision: str = "", config_id: str = "", alloc_status: str = "",
                          day: str = "", reason: str = "", warmup: str = "", limit: int = 50, offset: int = 0):
        """day: IST session (YYYY-MM-DD); reason: a recorded-reason category (metrics.CATEGORIES); warmup: 'only' / 'exclude'."""
        from nseql.screening.metrics import category_sql
        extra = _day("bar_ts", day)
        if reason:
            try:
                extra.append(category_sql(reason))
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
        if warmup == "only":
            extra.append(("alloc_reason LIKE ?", ["warm-up%"]))
        elif warmup == "exclude":
            extra.append(("COALESCE(alloc_reason, '') NOT LIKE ?", ["warm-up%"]))
        try:
            return clean(_need_screening().store.page("signals", where={"horizon": horizon, "symbol": symbol, "decision": decision, "config_id": config_id,
                                                                       "alloc_status": alloc_status}, order="bar_ts DESC, config_id",
                                                      limit=min(max(limit, 1), 500), offset=max(offset, 0), extra=extra))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @api.get("/screening/orders")
    def screening_orders(horizon: str = "", status: str = "", symbol: str = "", side: str = "", config_id: str = "", day: str = "",
                         limit: int = 50, offset: int = 0):
        return clean(_need_screening().store.page("paper_orders", where={"horizon": horizon, "status": status, "symbol": symbol, "side": side,
                                                                          "config_id": config_id}, order="decided_at DESC, order_key",
                                                  limit=min(max(limit, 1), 500), offset=max(offset, 0), extra=_day("decided_at", day)))

    news_desk: dict = {}

    @api.get("/news")
    def news(limit: int = 30, topic: str = "", refresh: bool = False):
        """World and market headlines from public RSS feeds (fetched by this machine, cached 10 minutes)."""
        from nseql.data.news import NewsDesk
        if "desk" not in news_desk:
            news_desk["desk"] = NewsDesk(st.exp_dir)
        return clean(news_desk["desk"].headlines(min(max(limit, 1), 100), topic or None, refresh))

    def _desk():
        from nseql.data.news import NewsDesk
        if "desk" not in news_desk:
            news_desk["desk"] = NewsDesk(st.exp_dir)
        return news_desk["desk"]

    from nseql.data.news_watch import NewsWatch
    watch = NewsWatch(st.exp_dir, _desk(), symbols=lambda: (_screening_open().reg.symbols if _screening_open() is not None else []),
                      scheduler=_screening_open, market_open=lambda: _calendar().is_open(datetime.now(views.IST).replace(tzinfo=None)))
    st.news_watch = watch

    @api.get("/news/recent")
    def news_recent(limit: int = 50, symbol: str = "", theme: str = ""):
        """Headlines recorded by the watcher, newest first, with the time this lab first saw each one."""
        return clean({"items": watch.recent(min(max(limit, 1), 300), symbol, theme), "status": watch.status()})

    @api.get("/news/watch")
    def news_watch_status():
        today = datetime.now(views.IST).strftime("%Y-%m-%d")
        return clean({**watch.status(), "today": watch.counts(f"{today}T00:00:00")})

    @api.get("/market/pulse")
    def market_pulse(limit: int = 30):
        return clean({"pulses": watch.pulses(min(max(limit, 1), 200)), "every_s": watch.pulse_every})

    @api.get("/screening/fills")
    def screening_fills(symbol: str = "", side: str = "", config_id: str = "", order_key: str = "", day: str = "", limit: int = 50, offset: int = 0):
        return clean(_need_screening().store.page("paper_fills", where={"symbol": symbol, "side": side, "config_id": config_id, "order_key": order_key},
                                                  order="ts DESC, fill_key", limit=min(max(limit, 1), 500), offset=max(offset, 0),
                                                  extra=_day("ts", day)))

    @api.get("/screening/positions")
    def screening_positions():
        """Open paper positions now: one row per configuration holding shares, marked at the last close."""
        sch = _need_screening()
        e, r, x = sch.engine, sch.reg, sch.engine.x
        from nseql.screening.registry import FAMILIES, TF_NAMES
        from nseql.screening.universe import _rule_name
        rows = []
        for i in np.flatnonzero(e.pos == 1):
            i = int(i)
            last = float(e.fe.last_close[r.sym[i]])
            q, entry = int(e.qty[i]), float(e.entry_px[i])
            ch = int(e.entry_ch[i]) / 100
            pnl = (last - entry) * q - ch
            rows.append({"config_id": str(r.ids[i]), "symbol": r.symbols[r.sym[i]], "rule": _rule_name(FAMILIES[r.fam[i]], r.params[i]),
                         "timeframe": TF_NAMES[r.tf[i]], "horizon": "intraday" if r.intraday[i] else "swing", "qty": q, "entry_price": round(entry, 2),
                         "last_price": round(last, 2), "stop_price": round(float(e.stop_px[i]), 2), "value_inr": round(last * q, 2),
                         "unrealised_inr": round(pnl, 2), "unrealised_pct": round((last / entry - 1) * 100, 3) if entry else None,
                         "exit_pending": bool(e.pend[i] < 0)})
        rows.sort(key=lambda z: -z["value_inr"])
        return clean({"rows": rows, "total_value_inr": round(sum(z["value_inr"] for z in rows), 2),
                      "total_unrealised_inr": round(sum(z["unrealised_inr"] for z in rows), 2), "working_orders": int((e.pend != 0).sum()),
                      "as_of": sch.watermark, "data_source": sch.provenance(),
                      "note": "simulated positions; unrealised P&L at the last 1-minute close, after entry charges, before exit charges"})

    @api.get("/today")
    def today_view():
        """Plain-language summary for the simple 'My trading' page: money, today's result, what is held, recent trades,
        whether the bot is working. Simulated (paper) trading only."""
        sch = _screening_open()
        ds = sch.provenance() if sch is not None else {"kind": "none", "label": "No market data attached", "live": False}
        mkt = views.market_session(_calendar())
        out = {"market": mkt, "data_source": ds, "kill_switch": st.kill.engaged, "live_trading": False, "started": sch is not None}
        if sch is None:
            live = screening.get("live") or {}
            out["bot"] = {"state": "error" if live.get("state") == "error" else "off", "message": live.get("error") or "The bot is not running yet."}
            return clean(out)
        x, db = sch.engine.x, sch.store.db
        snap = x.snapshot()
        day = datetime.now(views.IST).strftime("%Y-%m-%d")
        with sch.store._lock:
            day_start = db.execute("SELECT equity_inr FROM cycles WHERE substr(minute,1,10) < ? AND equity_inr IS NOT NULL ORDER BY minute DESC LIMIT 1", (day,)).fetchone()
            first_today = db.execute("SELECT equity_inr FROM cycles WHERE substr(minute,1,10) = ? AND equity_inr IS NOT NULL ORDER BY minute LIMIT 1", (day,)).fetchone()
            fills = [dict(r) for r in db.execute("SELECT o.symbol, o.side, o.qty, o.price, o.fill_ts, o.charges_paise, o.realized_paise, o.config_id, o.horizon "
                                                 "FROM paper_orders o WHERE o.status IN ('filled','partial') ORDER BY o.fill_ts DESC LIMIT 25")]
            today_n = db.execute("SELECT COUNT(*), COALESCE(SUM(CASE WHEN side='SELL' THEN realized_paise END),0), COALESCE(SUM(charges_paise),0) "
                                 "FROM paper_orders WHERE status IN ('filled','partial') AND substr(fill_ts,1,10)=?", (day,)).fetchone()
            sig = dict(db.execute("SELECT COALESCE(alloc_status,'-'), COUNT(*) FROM signals WHERE decision='BUY' AND substr(bar_ts,1,10)=? GROUP BY 1", (day,)).fetchall())
            last = db.execute("SELECT minute, evaluated, latency_ms FROM cycles WHERE status='processed' ORDER BY minute DESC LIMIT 1").fetchone()
            curve_day = db.execute("SELECT MAX(substr(minute,1,10)) FROM cycles WHERE equity_inr IS NOT NULL").fetchone()[0]
            curve = db.execute("SELECT substr(minute,12,5), equity_inr FROM cycles WHERE substr(minute,1,10)=? AND equity_inr IS NOT NULL ORDER BY minute",
                               (curve_day,)).fetchall() if curve_day else []
            sells = db.execute("SELECT config_id, realized_paise, substr(fill_ts,12,2) FROM paper_orders WHERE side='SELL' AND status IN ('filled','partial') "
                               "AND substr(fill_ts,1,10)=?", (curve_day or day,)).fetchall()
            hours = db.execute("SELECT substr(fill_ts,12,2), side, COUNT(*) FROM paper_orders WHERE status IN ('filled','partial') AND substr(fill_ts,1,10)=? "
                               "GROUP BY 1, 2", (curve_day or day,)).fetchall()
            busy = db.execute("SELECT config_id, symbol, COUNT(*), COALESCE(SUM(CASE WHEN side='SELL' THEN realized_paise END),0) FROM paper_orders "
                              "WHERE status IN ('filled','partial') AND substr(fill_ts,1,10)=? GROUP BY 1, 2 ORDER BY 3 DESC, 4 LIMIT 5",
                              (curve_day or day,)).fetchall()
            refused = db.execute("SELECT reason, SUM(n) FROM refusals WHERE substr(minute,1,10)=? GROUP BY 1", (curve_day or day,)).fetchall()
        from nseql.screening.registry import FAMILIES
        from nseql.screening.universe import _rule_name
        idx = {str(i): k for k, i in enumerate(sch.reg.ids)}
        def rule(cid):
            k = idx.get(cid)
            return _rule_name(FAMILIES[sch.reg.fam[k]], sch.reg.params[k]) if k is not None else "strategy"
        base = (day_start or first_today or [snap["equity_inr"]])[0]
        fam_pnl: dict[str, float] = {}
        wins = losses = 0
        for cid, rp, _h in sells:
            k = idx.get(cid)
            f = FAMILIES[sch.reg.fam[k]] if k is not None else "other"
            fam_pnl[f] = fam_pnl.get(f, 0.0) + (rp or 0) / 100
            wins += (rp or 0) > 0
            losses += (rp or 0) < 0
        per_hour: dict[str, dict] = {}
        for h, side, n in hours:
            per_hour.setdefault(h, {"hour": f"{h}:00", "buys": 0, "sells": 0})["buys" if side == "BUY" else "sells"] = n
        step = max(1, len(curve) // 120)
        holdings_rows = screening_positions()["rows"]
        alloc = {}
        for h in holdings_rows:
            alloc[h["symbol"]] = alloc.get(h["symbol"], 0.0) + h["value_inr"]
        charts = {"session": curve_day, "equity_curve": [{"t": t, "v": v} for t, v in curve[::step]] + ([{"t": curve[-1][0], "v": curve[-1][1]}] if curve and (len(curve) - 1) % step else []),
                  "day_open_value": base, "allocation": [{"label": k, "value": round(v, 2)} for k, v in sorted(alloc.items(), key=lambda z: -z[1])]
                  + [{"label": "Free cash", "value": round(snap["cash_inr"] - snap["reserved_inr"], 2)}],
                  "pnl_by_family": [{"label": k, "value": round(v, 2)} for k, v in sorted(fam_pnl.items(), key=lambda z: z[1])],
                  "wins": int(wins), "losses": int(losses), "trades_by_hour": [per_hour[h] for h in sorted(per_hour)]}
        LIMITS = (("cool-down", "cool-down after a sale"), ("daily cap", "daily cap reached"), ("one at a time", "already holding that stock"),
                  ("strategy type losing", "strategy type losing after costs"), ("daily loss limit", "today's loss limit"))
        limited = {label: int(sum(n for why, n in refused if str(why).startswith(key))) for key, label in LIMITS}
        charts["most_active"] = [{"strategy": rule(cid), "symbol": sym, "trades": int(n), "profit": rp / 100} for cid, sym, n, rp in busy]
        charts["limited"] = [{"label": k, "value": v} for k, v in limited.items() if v]
        charts["rules_version"] = (screening.get("meta") or {}).get("rules_version") or "1.0"
        from nseql.screening import rca as _rca
        try:
            with sch.store._lock:
                rc = _rca.analyse(sch.store.db, curve_day or day, write=False) if curve_day or day else []
            sm = _rca.summary(rc)
            charts["rca"] = {**sm, "latest": [{"time": str(t["exit_ts"])[11:16], "symbol": t["symbol"], "outcome": t["outcome"],
                                               "summary": t["summary"], "causes": t["causes"], "net_inr": round(t["net_inr"], 2)}
                                              for t in sorted(rc, key=lambda t: str(t["exit_ts"]), reverse=True)[:12]]}
        except Exception as e:      # noqa: BLE001 - shown, never breaks the page
            charts["rca"] = {"error": f"{type(e).__name__}: {e}"}
        from nseql.screening.metrics import account_figures, session_decisions
        meta_ = screening.get("meta") or {}
        fig = account_figures(sch, meta_, day)
        with sch.store._lock:
            session = session_decisions(db, curve_day or day, meta_.get("live_from"))
        age = None
        if last:
            age = (datetime.now(views.IST).replace(tzinfo=None) - pd.Timestamp(last[0]).to_pydatetime()).total_seconds()
        working = sch._thread is not None and sch._thread.is_alive()
        if st.kill.engaged:
            bot = {"state": "stopped", "message": "Paused by the kill switch: no new paper trades. Reset it on the Risk Control page."}
        elif not working:
            bot = {"state": "off", "message": "The bot is not running (replay finished or not started)."}
        elif not mkt["open"]:
            bot = {"state": "waiting", "message": f"Market {mkt['state']}. The bot starts checking again when the market opens."}
        elif age is not None and age > 180:
            bot = {"state": "stale", "message": f"No new prices for {int(age // 60)} minutes. Check that this laptop is online and awake."}
        else:
            bot = {"state": "ok", "message": f"Working: checked {int(last[1] or 0):,} strategies at {str(last[0])[11:16]}." if last else "Working: waiting for the first candle."}
        out.update({
            "bot": bot,
            "account": {"starting_capital": fig["starting_capital"], "value_now": fig["equity_now"], "cash_free": fig["cash_free"],
                        "invested": fig["invested"], "since_start": fig["pnl_since_start"], "return_since_start": fig["return_since_start"],
                        "exposure": fig["exposure"], "day_open_equity": fig["day_open_equity"],
                        "today": fig["pnl_today"], "today_trades": today_n[0], "today_realised": today_n[1] / 100,
                        "today_charges": today_n[2] / 100, "halted_today": snap["halted_today"], "windows": fig["windows"]},
            "session": session,
            "holdings": holdings_rows,
            "charts": charts,
            "recent_trades": [{"time": str(f["fill_ts"]).replace("T", " ")[:16], "action": "Bought" if f["side"] == "BUY" else "Sold", "symbol": f["symbol"],
                               "qty": f["qty"], "price": f["price"], "strategy": rule(f["config_id"]), "horizon": f["horizon"],
                               "profit": (f["realized_paise"] or 0) / 100 if f["side"] == "SELL" else None, "charges": (f["charges_paise"] or 0) / 100,
                               "config_id": f["config_id"]} for f in fills],
            "signals_today": {"accepted": session["accepted"], "eligible": session["eligible"], "warm_up": session["warm_up"],
                              "deferred": session["deferred"], "rejected": session["rejected"]},
        })
        return clean(out)

    @api.get("/review")
    def review_list():
        """Every closed session of the live paper accounts, with its daily-review state (latest first)."""
        from nseql.screening.daily_review import closed_sessions, live_accounts
        rows = []
        for acc in live_accounts(st.exp_dir):
            for day in closed_sessions(acc):
                rows.append({"account": acc.name, "day": day, "state": reviews.state(acc.name, day)})
        rows.sort(key=lambda r: (r["day"], r["account"]), reverse=True)
        return clean({"sessions": rows, "running": list(reviews.current) if reviews.current else None, "queued": [list(k) for k in reviews.pending],
                      "schedule": "after the 15:30 candle of each trading day, once the session is over (holidays have no session)"})

    @api.get("/review/{account}/{day}")
    def review_get(account: str, day: str):
        from nseql.screening.daily_review import review_dir, status as _status
        if "/" in account or "\\" in account or ".." in account or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise HTTPException(400, "bad account or day")
        f = review_dir(st.exp_dir, account, day) / "review.json"
        rev = json.loads(f.read_text()) if f.exists() else None
        return clean({"account": account, "day": day, "state": reviews.state(account, day), "status": _status(st.exp_dir, account, day), "review": rev})

    class ReviewRun(BaseModel):
        account: str
        day: str
        force: bool = False

    @api.post("/review/run")
    def review_run(body: ReviewRun):
        """Queue the review of one closed session (analysis only: it never trades or changes rules)."""
        from nseql.screening.daily_review import closed_sessions
        acc = st.exp_dir / "screening" / body.account
        if "/" in body.account or "\\" in body.account or ".." in body.account or not (acc / "live.json").exists():
            raise HTTPException(404, "unknown paper account")
        if body.day not in closed_sessions(acc):
            raise HTTPException(400, f"{body.account} has no closed session on {body.day}")
        r = reviews.submit(body.account, body.day, force=body.force)
        return {"account": body.account, "day": body.day, "result": r,
                "note": "waits until the market closes if a session is on" if _review_busy() else "starts now"}

    @api.get("/screening/rca")
    def screening_rca(day: str = ""):
        """Root cause for every completed round trip of a day (winners and losers). Documentation, not a trading input."""
        from nseql.screening import rca as _rca
        sch = _need_screening()
        d = day or datetime.now(views.IST).strftime("%Y-%m-%d")
        with sch.store._lock:
            rc = _rca.analyse(sch.store.db, d, write=False)
        return clean({"day": d, **_rca.summary(rc), "trades": rc,
                      "note": "'recovered after the sale' uses prices after the exit (hindsight): documentation only."})

    @api.get("/contest")
    def contest_view():
        """Incumbent vs challenger: the forward paper contest (if the challenger runs) and the latest replay contests.
        Synthetic, historical replay and forward results are labelled separately; only forward results can decide."""
        from nseql.screening.contest import forward_report
        out: dict = {"forward": None, "replays": []}
        _screening_open()
        if screening.get("challenger") and screening.get("sch") is not None:
            ch, cm = screening["challenger"]
            try:
                out["forward"] = forward_report(screening["sch"], ch, cm)
            except Exception as e:      # noqa: BLE001 - shown, never breaks the page
                out["forward"] = {"error": f"{type(e).__name__}: {e}"}
        base = st.exp_dir / "contest"
        if base.exists():
            reps = sorted(base.glob("*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for p in reps[:5]:
                try:
                    out["replays"].append({"folder": p.parent.name, **json.loads(p.read_text())})
                except (OSError, ValueError):
                    continue
        rb = st.exp_dir / "robustness"
        out["robustness"] = None
        if rb.exists():
            reps = sorted(rb.glob("*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if reps:
                try:
                    r = json.loads(reps[0].read_text())
                    out["robustness"] = {"folder": reps[0].parent.name, **{k: r[k] for k in ("kind", "kind_label", "sessions", "symbols", "challenger",
                                                                                            "monte_carlo", "walk_forward", "sensitivity") if k in r}}
                except (OSError, ValueError):
                    pass
        out["how"] = ("python -m nseql.cli contest --fixture  (synthetic, from a clean checkout); --dataset <id> for a historical replay; "
                      "python -m nseql.cli robustness --fixture for the robustness checks")
        return clean(out)

    @api.get("/screening/portfolio")
    def screening_portfolio():
        sch = _need_screening()
        db = sch.store.db
        with sch.store._lock:
            refused = dict(db.execute("SELECT reason, SUM(n) FROM refusals GROUP BY reason ORDER BY 2 DESC").fetchall())
            events = [json.loads(r[0]) for r in db.execute("SELECT detail FROM portfolio_events ORDER BY ts DESC LIMIT 50")]
            curve = [{"minute": r[0], "equity_inr": r[1], "cash_inr": r[2]} for r in
                     db.execute("SELECT minute, equity_inr, cash_inr FROM cycles WHERE equity_inr IS NOT NULL AND substr(minute, 12) = '15:30:00' ORDER BY minute")]
        return clean({**sch.engine.x.snapshot(), "refused_signals": refused, "events": events, "daily_close_equity": curve,
                      "note": "One shared simulated capital pot for every configuration. Signals the portfolio refused are counted "
                              "by reason; they remain in the decisions list as what the strategy wanted."})

    @api.get("/screening/cycles")
    def screening_cycles(status: str = "", limit: int = 100, offset: int = 0):
        return _page("cycles", "minute DESC", {"status": status}, limit, offset)

    class ProposeIn(BaseModel):
        changes: dict | None = None        # None = let the meta-optimiser propose a variation
        rationale: str = ""

    @api.post("/brain/versions/propose")
    def brain_propose(body: ProposeIn):
        out = brain.propose(body.changes, body.rationale)
        reg.audit("user", "BRAIN_VERSION_PROPOSED", **{k: v for k, v in out.items() if k != "changes"}, changes=out["changes"])
        return out

    class FrozenIn(BaseModel):
        dataset_id: str = FIXTURE_ID

    @api.post("/brain/versions/{vid}/frozen-benchmark")
    def brain_frozen(vid: str, body: FrozenIn):
        st.dataset(body.dataset_id)
        return st.job("brain_frozen_benchmark", lambda job: brain.frozen_benchmark(vid, body.dataset_id), version=vid)

    class ApproveIn(BaseModel):
        approver: str = Field(min_length=2, max_length=80)
        reason: str = Field(min_length=5, max_length=500)

    @api.post("/brain/versions/{vid}/promote")      # human-only promotion of a RESEARCH policy; unrelated to live trading
    def brain_approve(vid: str, body: ApproveIn):
        from nseql.brain.versions import LifecycleError
        try:
            out = brain.approve(vid, body.approver, body.reason)
        except PermissionError as e:
            reg.audit(body.approver, "BRAIN_APPROVAL_DENIED", version=vid, reason=str(e))
            raise HTTPException(403, str(e)) from None
        except LifecycleError as e:
            raise HTTPException(409, str(e)) from None
        reg.audit(body.approver, "BRAIN_VERSION_PROMOTED", version=vid, reason=body.reason)
        return out

    # ------------------------------------------------------------------ brokers, risk, notifications
    @api.get("/brokers")
    def brokers():
        kite = ZerodhaKite(st.gate)
        return {"live_trading_enabled": st.settings.live_trading_enabled,
                "adapters": [{**kite.describe(), "credentials_present": kite.connected}, Upstox(st.gate).describe(), AngelOneSmartAPI(st.gate).describe()],
                "market_data": [{"name": "replay (stored dataset)", "status": "IMPLEMENTED"},
                                {"name": "Kite historical REST", "status": "UNVERIFIED", "credentials_present": st.settings.kite_credentials_present},
                                {"name": "Angel One SmartAPI historical REST", "status": "UNVERIFIED"},
                                {"name": "Angel One 1-minute polling (screening loop)", "status": "UNVERIFIED"},
                                {"name": "TradingView chart exports (manual files)", "status": "IMPLEMENTED"},
                                {"name": "KiteTicker WebSocket", "status": "NOT IMPLEMENTED"},
                                {"name": "Angel One SmartStream WebSocket", "status": "NOT IMPLEMENTED"}],
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

    scheduler = PaperScheduler(lambda: st.sessions, notify=st.notify, lock=st.lock, meta=lambda sid: st.session_meta.get(sid, {}),
                               calendar=_calendar())
    app.state.scheduler = scheduler
    _restore_sessions()
    if os.environ.get("NSEQL_LIVE_SCREENING", "").lower() == "angelone":
        screening["live"] = {"state": "starting", "broker": "angelone"}     # before any request can reopen the account read-only
        threading.Thread(target=_start_live_screening, name="live-screening-bootstrap", daemon=True).start()
    if os.environ.get("NSEQL_NEWS_WATCH", "").lower() == "true" or (
            os.environ.get("NSEQL_LIVE_SCREENING", "").lower() == "angelone" and os.environ.get("NSEQL_NEWS_WATCH", "").lower() != "false"):
        st.news_watch.start()                       # news every 5 min, market pulse every 15 min while open
    if os.environ.get("NSEQL_SCHEDULER", "").lower() == "true":
        scheduler.start(float(os.environ.get("NSEQL_SCHEDULER_TICK_SECONDS", "60")))
    app.mount("/api", api)
    return app


def app_factory() -> FastAPI:
    return create_app()

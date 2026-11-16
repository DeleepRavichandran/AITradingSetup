"""Configuration -> backtest -> auditable run artefacts."""
from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from nseql import __version__
from nseql.analytics.metrics import compute_metrics
from nseql.backtester.engine import Allocation, BacktestEngine, EngineResult
from nseql.backtester.execution import ExecutionConfig
from nseql.data.portal import DataPortal
from nseql.data.store import Dataset
from nseql.instruments.rules import BrokerageProfile, ChargeSchedule, MarginRules
from nseql.risk.limits import RiskLimits
from nseql.strategies import registry
from nseql.strategies.api import Strategy


class StrategyRef(BaseModel):
    name: str                                # registry name, or "generated:<code_id>"
    params: dict = Field(default_factory=dict)
    capital: float = 1_000_000.0
    book: str | None = None


class BacktestConfig(BaseModel):
    dataset_id: str = "synthetic_v1"
    interval: str = "1d"
    start: str | None = None
    end: str | None = None
    symbols: list[str] | None = None         # None = derive from what the strategies require
    strategies: list[StrategyRef]
    execution: dict = Field(default_factory=dict)
    brokerage: str = "discount"              # "discount" | "zero"
    cost_multiplier: float = 1.0
    margin: dict = Field(default_factory=dict)
    risk: dict = Field(default_factory=dict)
    allow_rule_extrapolation: bool = False
    liquidate_on_margin_breach: bool = True
    label: str = ""

    def fingerprint_payload(self) -> dict:
        d = self.model_dump()
        d.pop("label", None)
        return d


def code_version() -> str:
    try:
        root = Path(__file__).resolve().parents[2]
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "services", "packages", "agents"], cwd=root,
                               capture_output=True, text=True, timeout=5)
        if sha.returncode == 0:
            return f"{__version__}+{sha.stdout.strip()}{'-dirty' if dirty.stdout.strip() else ''}"
    except Exception:
        pass
    return __version__


def _source_hash(obj) -> str:
    src = getattr(obj, "__nseql_source__", None)
    if src is None:
        try:
            src = inspect.getsource(type(obj))
        except (OSError, TypeError):
            src = type(obj).__qualname__
    return hashlib.sha256(src.encode()).hexdigest()[:16]


def default_symbols(ds: Dataset, strategies: list[Strategy], interval: str) -> list[str]:
    need = {s.requires for s in strategies}
    out: set[str] = set()
    und = {s.params.get("underlying") for s in strategies if s.params.get("underlying")}
    for sym, inst in ds.instruments.items():
        k = inst.kind.value
        if ("EQ" in need and k in ("EQ", "ETF")) or (k == "INDEX"):
            out.add(sym)
        if k in need and (not und or inst.underlying in und):
            out.add(sym)
    return sorted(s for s in out if ds.has_bars(s, interval))


@dataclass
class RunResult:
    run_id: str
    config: BacktestConfig
    summary: dict
    engine: EngineResult
    strategies: list[Strategy] = field(default_factory=list)

    @property
    def metrics(self) -> dict:
        return self.summary["metrics"]

    def trades(self) -> list[dict]:
        return [t.to_dict() for t in self.engine.account.trades]

    def fills(self) -> list[dict]:
        return [f.to_dict() for f in self.engine.account.fills]

    def equity_frame(self) -> pd.DataFrame:
        e = self.engine
        df = pd.DataFrame({"ts": e.ts.astype("datetime64[ns]"), "equity": e.equity / 100, "cash": e.cash / 100,
                           "gross_exposure": e.gross_exposure / 100, "margin_used": e.margin_used / 100})
        for b, v in e.book_equity.items():
            df[f"book:{b}"] = v / 100
        return df

    def save(self, run_dir: Path) -> Path:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        e = self.engine
        (run_dir / "config.json").write_text(self.config.model_dump_json(indent=1))
        (run_dir / "summary.json").write_text(json.dumps(self.summary, indent=1, default=str))
        self.equity_frame().to_parquet(run_dir / "equity.parquet", index=False)
        (run_dir / "fills.json").write_text(json.dumps(self.fills()))
        (run_dir / "trades.json").write_text(json.dumps(self.trades()))
        (run_dir / "orders.json").write_text(json.dumps([o.to_dict() for o in e.orders]))
        pd.DataFrame([l.to_dict() for l in e.account.ledger]).to_parquet(run_dir / "ledger.parquet", index=False)
        with open(run_dir / "events.jsonl", "w") as f:
            for ev in e.events:
                f.write(json.dumps(ev, default=str) + "\n")
        return run_dir


def result_hash(e: EngineResult) -> str:
    """Hash of everything that matters numerically: ledger, fills and the equity curve."""
    h = hashlib.sha256()
    for l in e.account.ledger:
        h.update(f"{l.seq}|{l.ts.value}|{l.book}|{l.type}|{l.symbol}|{l.amount}|{l.cash_after}\n".encode())
    for f in e.account.fills:
        h.update(f"{f.id}|{f.ts.value}|{f.symbol}|{f.side}|{f.qty}|{f.price}|{f.charges.get('total', 0)}\n".encode())
    h.update(np.ascontiguousarray(e.equity).tobytes())
    return h.hexdigest()


def run_backtest(cfg: BacktestConfig, data_dir: Path, *, portal: DataPortal | None = None,
                 strategy_factory: Callable[[StrategyRef], Strategy] | None = None,
                 dataset: Dataset | None = None) -> RunResult:
    t_start = time.perf_counter()
    ds = dataset or Dataset(Path(data_dir) / cfg.dataset_id)
    factory = strategy_factory or (lambda ref: registry.create(ref.name, ref.params))
    strategies = [factory(ref) for ref in cfg.strategies]
    books = [ref.book or f"{ref.name}#{i + 1}" for i, ref in enumerate(cfg.strategies)]
    if portal is None:
        symbols = cfg.symbols or default_symbols(ds, strategies, cfg.interval)
        portal = DataPortal.from_dataset(ds, cfg.interval, symbols, cfg.start, cfg.end)
    charges = ChargeSchedule(brokerage=BrokerageProfile.zero() if cfg.brokerage == "zero" else BrokerageProfile(),
                             allow_extrapolation=cfg.allow_rule_extrapolation, multiplier=cfg.cost_multiplier)
    engine = BacktestEngine(portal, [Allocation(s, b, ref.capital) for s, b, ref in zip(strategies, books, cfg.strategies)],
                            exec_cfg=ExecutionConfig(**cfg.execution), charges=charges,
                            margin_rules=MarginRules(**cfg.margin), risk=RiskLimits(**cfg.risk),
                            liquidate_on_margin_breach=cfg.liquidate_on_margin_breach)
    res = engine.run()
    acct = res.account
    capital = sum(r.capital for r in cfg.strategies)
    trades = [t.to_dict() for t in acct.trades]
    total_ch = sum(b.charges for b in acct.books.values()) / 100
    traded = sum(f.qty * f.price for f in acct.fills if f.reason != "expiry") / 100
    parts = [ev["participation"] for ev in res.events if ev["type"] == "FILL" and ev.get("participation")]
    metrics = compute_metrics(res.ts, res.equity, trades, initial_capital=capital, total_charges=total_ch,
                              traded_value=traded, gross_exposure=res.gross_exposure, participations=parts,
                              max_participation=engine.cfg.max_participation)
    per_book = {}
    for b, ref in zip(books, cfg.strategies):
        bt = [t for t in trades if t["book"] == b]
        per_book[b] = compute_metrics(res.ts, res.book_equity[b], bt, initial_capital=ref.capital,
                                      total_charges=acct.books[b].charges / 100)
    charge_break: dict[str, float] = {}
    for f in acct.fills:
        for k, v in f.charges.items():
            if k != "total":
                charge_break[k] = charge_break.get(k, 0) + v / 100
    recon = acct.reconcile()
    flags = list(res.flags)
    if ds.synthetic:
        flags.insert(0, "SYNTHETIC DATA — results say nothing about real markets")
    payload = cfg.fingerprint_payload()
    src = {b: _source_hash(s) for b, s in zip(books, strategies)}
    fp = hashlib.sha256(json.dumps({"cfg": payload, "dataset": ds.content_hash, "src": src}, sort_keys=True).encode()).hexdigest()
    status = "error" if res.error else ("ok" if recon["ok"] else "reconciliation_failed")
    open_positions = [{"book": b.id, "symbol": p.symbol, "qty": p.qty, "avg_price": p.avg_price,
                       "mark": acct.marks.get(p.symbol, 0) / 100, "unrealized": acct.unrealized(p) / 100}
                      for b in acct.books.values() for p in b.positions.values()]
    counts: dict[str, int] = {}
    for o in res.orders:
        counts[o.status.value] = counts.get(o.status.value, 0) + 1
    summary = {
        "status": status, "error": res.error, "label": cfg.label, "fingerprint": fp, "result_hash": result_hash(res),
        "code_version": code_version(), "strategy_source_hashes": src,
        "dataset": {"id": ds.id, "content_hash": ds.content_hash, "synthetic": ds.synthetic,
                    "source": ds.manifest.source, "licence": ds.manifest.licence},
        "period": {"start": str(pd.Timestamp(res.ts[0])) if len(res.ts) else None,
                   "end": str(pd.Timestamp(res.ts[-1])) if len(res.ts) else None, "bars": int(len(res.ts)),
                   "interval": cfg.interval, "symbols": len(portal.symbols)},
        "metrics": metrics, "per_book": per_book, "charges_breakdown": charge_break, "reconciliation": recon,
        "orders": counts, "fills": len(acct.fills), "open_positions": open_positions, "flags": flags,
        "strategies": [{"book": b, "name": ref.name, "params": s.params, "capital": ref.capital,
                        "hypothesis": s.hypothesis, "family": s.family}
                       for b, ref, s in zip(books, cfg.strategies, strategies)],
        "execution": engine.cfg.to_dict(), "runtime_seconds": round(time.perf_counter() - t_start, 3),
    }
    run_id = f"{fp[:10]}-{int(time.time() * 1000) % 10**9:09d}"
    return RunResult(run_id, cfg, summary, res, strategies)

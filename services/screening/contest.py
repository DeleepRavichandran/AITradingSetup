"""Auditable contest: corrected incumbent screening trader vs the challenger, in isolated paper accounts.

Fairness
  * Two separate accounts (own ledger, cash, orders and positions), identical starting capital, the same
    watchlist (indices excluded), the same execution model and charges, the same portfolio risk ceilings.
  * Both receive EXACTLY the same market events at the same timestamps every minute, whether or not they trade.
  * Cash is reported as a third, zero-return reference, so it is plain when both traders lose money.

Protocol (`run_fixture` / `run_dataset`)
  1. Development sessions (chronologically first) are run to check the implementation; nothing is tuned on them
     (all parameters are fixed in code before any run).
  2. A manifest is FROZEN before the holdout: code version, strategy versions and parameters, execution costs,
     data fingerprint, development and evaluation dates, the primary measure and the verdict rules. Its SHA-256
     is stored with every result; a change to any of them is a new evaluation.
  3. The holdout sessions are run once and reported.

Primary measure (declared before any result): the paired daily difference of after-cost portfolio returns,
challenger minus incumbent, per session, including sessions where either trader made no trade (return 0).
Interval: 95% circular block bootstrap of the mean difference (block length and seed fixed below).

Verdict (forward paper only; synthetic and historical replays are reported but never decide promotion):
  * at least MIN_SESSIONS completed paper sessions AND MIN_TRADES closed challenger trades, else "inconclusive";
  * "challenger ahead" only if the interval's lower bound > 0, the challenger's cumulative net P&L > 0 and its
    maximum drawdown is no worse than the incumbent's;
  * "incumbent ahead" if the interval's upper bound < 0; otherwise "inconclusive".
Promotion is the operator's decision. A challenger rule change is a new version and a new forward evaluation.
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from nseql.screening.challenger import CHALLENGER_CONFIG, CHALLENGER_VERSION, challenger_rules, factory
from nseql.screening.execution import LIVE_RULES_VERSION, ExecConfig, PortfolioRules
from nseql.screening.registry import REGISTRY_VERSION
from nseql.screening.scheduler import MinuteScheduler

CONTEST_VERSION = "1.0"
MIN_SESSIONS = 60
MIN_TRADES = 100
BOOTSTRAP = {"method": "circular block bootstrap of the mean paired daily difference", "confidence": 0.95, "block_length": 5,
             "resamples": 10_000, "seed": 20261124}
KINDS = {"synthetic": "SYNTHETIC replay (made-up prices; says nothing about real markets)",
         "historical": "historical real-data replay",
         "forward": "forward paper trading (live prices, simulated orders)"}


def incumbent_label() -> str:
    return f"screening rules {LIVE_RULES_VERSION} (registry v{REGISTRY_VERSION})"


def code_version(repo: Path | None = None) -> dict:
    repo = repo or Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo, capture_output=True, text=True,
                                    timeout=10).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        commit, dirty = "", None
    return {"commit": commit or "unknown", "uncommitted_changes": dirty}


def fingerprint(stream: list[tuple]) -> str:
    h = hashlib.sha256()
    for t, bars in stream:
        for b in sorted(bars, key=lambda x: x["symbol"]):
            h.update(f"{b['symbol']}|{pd.Timestamp(b['ts']).isoformat()}|{b['open']:.4f}|{b['high']:.4f}|{b['low']:.4f}|{b['close']:.4f}|{b['volume']:.0f}\n".encode())
    return h.hexdigest()


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------------- running
@dataclass
class Pair:
    incumbent: MinuteScheduler
    challenger: MinuteScheduler


def open_pair(root: Path, symbols: list[str], capital: float, exec_cfg: ExecConfig | None = None) -> Pair:
    root = Path(root)
    inc = MinuteScheduler(root / "incumbent", symbols, portfolio=PortfolioRules.live(capital), exec_cfg=exec_cfg, mode="replay")
    ch = MinuteScheduler(root / "challenger", symbols, portfolio=challenger_rules(capital), exec_cfg=exec_cfg, mode="replay",
                         engine_factory=factory())
    inc.verbose = ch.verbose = False
    return Pair(inc, ch)


def feed(pair: Pair, stream: list[tuple]) -> None:
    """Identical events to both accounts: same bars, same processing timestamp, every minute."""
    for t, bars in stream:
        for s in (pair.incumbent, pair.challenger):
            s.ingest(bars)
            s.run_cycle(pd.Timestamp(t) + pd.Timedelta(seconds=6))


# ---------------------------------------------------------------------------------------------------------- measuring
def account_metrics(sch: MinuteScheduler, capital: float, sessions: list[str]) -> dict:
    """After-cost results for one account over `sessions` (YYYY-MM-DD), read from its own ledger."""
    db = sch.store.db
    cyc = pd.read_sql_query("SELECT minute, equity_inr, cash_inr FROM cycles WHERE equity_inr IS NOT NULL ORDER BY minute", db)
    cyc["day"] = cyc.minute.str[:10]
    cyc = cyc[cyc.day.isin(sessions)]
    eq_close = cyc.groupby("day").equity_inr.last().reindex(sessions)
    eq_close = eq_close.ffill().fillna(capital)                    # a session with no processed minute keeps the prior equity
    prev = eq_close.shift(1).fillna(capital)
    daily_ret = (eq_close / prev - 1).fillna(0.0)
    curve = cyc.equity_inr.to_numpy() if len(cyc) else np.array([capital])
    peak = np.maximum.accumulate(np.concatenate([[capital], curve]))
    dd = (np.concatenate([[capital], curve]) / peak - 1)
    o = pd.read_sql_query("SELECT side, status, qty, price, charges_paise, requested_qty, fill_ts, symbol, config_id, product FROM paper_orders", db)
    o = o[o.fill_ts.fillna("").str[:10].isin(sessions) | o.status.isin(["rejected", "cancelled"])]
    filled = o[o.status.isin(["filled", "partial"])]
    value = float((filled.qty * filled.price).sum())
    from nseql.screening.metrics import session_decisions
    dec = {"warm_up": 0, "eligible": 0, "accepted": 0, "deferred": 0, "rejected": 0, "rejected_by_category": {}}
    for d in sessions:                                     # same sessions as every other measure; recorded reasons only
        x = session_decisions(db, d, sch.live_from)
        for k in ("warm_up", "eligible", "accepted", "deferred", "rejected"):
            dec[k] += x[k]
        for k, v in x["rejected_by_category"].items():
            dec["rejected_by_category"][k] = dec["rejected_by_category"].get(k, 0) + v
    sells = filled[filled.side == "SELL"]
    exposure = float(((cyc.equity_inr - cyc.cash_inr) / cyc.equity_inr).clip(lower=0).mean()) if len(cyc) else 0.0
    # positions an intraday strategy still held after the session (liquidity prevented the square-off)
    resid = []
    for d in sessions:
        b = filled[(filled.fill_ts.str[:10] == d) & (filled["product"] == "MIS")]      # intraday only; swing holds overnight by design
        net = (b.assign(q=np.where(b.side == "BUY", b.qty, -b.qty)).groupby(["config_id", "symbol"]).q.sum())
        resid += [{"day": d, "symbol": s, "qty": int(q)} for (_, s), q in net.items() if q > 0]
    end = float(eq_close.iloc[-1]) if len(eq_close) else capital
    start = float(prev.iloc[0]) if len(prev) else capital
    return {"start_equity": round(start, 2), "end_equity": round(end, 2), "net_pnl_inr": round(end - start, 2),
            "daily": [{"day": d, "equity": round(float(e), 2), "return": float(r)} for d, e, r in zip(sessions, eq_close, daily_ret)],
            "curve": [{"t": m, "equity": float(e)} for m, e in zip(cyc.minute, cyc.equity_inr)][:: max(1, len(cyc) // 400)],
            "max_drawdown": round(float(dd.min()), 6), "worst_day": round(float(daily_ret.min()), 6) if len(daily_ret) else 0.0,
            "turnover": round(value / max(start, 1), 4), "charges_inr": round(float(filled.charges_paise.sum()) / 100, 2),
            "trades_closed": int(len(sells)), "fills": int(len(filled)), "exposure_avg": round(exposure, 4),
            "orders_partial": int((o.status == "partial").sum()), "orders_cancelled_or_rejected": int(o.status.isin(["cancelled", "rejected"]).sum()),
            "buy_decisions": dec, "buys_refused_by_limits": dec["rejected"], "residual_positions": resid}


def block_bootstrap(d: np.ndarray, cfg: dict = BOOTSTRAP) -> dict:
    n = len(d)
    if n == 0:
        return {"mean": None, "low": None, "high": None, "n": 0}
    rng = np.random.default_rng(cfg["seed"])
    L = max(1, min(cfg["block_length"], n))
    k = math.ceil(n / L)
    starts = rng.integers(0, n, size=(cfg["resamples"], k))
    idx = (starts[:, :, None] + np.arange(L)[None, None, :]).reshape(cfg["resamples"], -1)[:, :n] % n
    means = d[idx].mean(axis=1)
    a = (1 - cfg["confidence"]) / 2
    return {"mean": float(d.mean()), "low": float(np.quantile(means, a)), "high": float(np.quantile(means, 1 - a)), "n": n}


def verdict(kind: str, inc: dict, ch: dict, ci: dict, sessions: int) -> dict:
    rules = {"min_sessions": MIN_SESSIONS, "min_challenger_trades": MIN_TRADES, "interval": "95% block bootstrap, lower bound > 0",
             "also": "challenger cumulative net P&L > 0 and max drawdown no worse than the incumbent's"}
    if kind != "forward":
        return {"status": "not a forward verdict", "kind": kind, "rules": rules,
                "reason": f"{KINDS[kind]}: reported for verification only; only forward paper sessions can decide."}
    if sessions < MIN_SESSIONS or ch["trades_closed"] < MIN_TRADES:
        return {"status": "inconclusive", "kind": kind, "rules": rules,
                "reason": f"not enough evidence yet: {sessions}/{MIN_SESSIONS} sessions, {ch['trades_closed']}/{MIN_TRADES} challenger trades"}
    if ci["low"] > 0 and ch["net_pnl_inr"] > 0 and ch["max_drawdown"] >= inc["max_drawdown"]:
        return {"status": "challenger ahead", "kind": kind, "rules": rules,
                "reason": "paired daily advantage interval above zero, positive net P&L, drawdown no worse"}
    if ci["high"] < 0:
        return {"status": "incumbent ahead", "kind": kind, "rules": rules, "reason": "paired daily advantage interval below zero"}
    return {"status": "inconclusive", "kind": kind, "rules": rules, "reason": "the paired daily advantage interval includes zero, "
            "or the challenger's P&L or drawdown condition is not met"}


def compare(pair: Pair, sessions: list[str], capital: float, kind: str, manifest: dict | None = None) -> dict:
    inc = account_metrics(pair.incumbent, capital, sessions)
    ch = account_metrics(pair.challenger, capital, sessions)
    d = np.array([c["return"] - i["return"] for c, i in zip(ch["daily"], inc["daily"])])
    ci = block_bootstrap(d)
    cash = {"net_pnl_inr": 0.0, "max_drawdown": 0.0, "note": "cash earns 0% here: the reference to beat"}
    both_lost = inc["net_pnl_inr"] < 0 and ch["net_pnl_inr"] < 0
    return {"contest_version": CONTEST_VERSION, "kind": kind, "kind_label": KINDS[kind], "sessions": sessions, "capital": capital,
            "incumbent": {"version": incumbent_label(), **inc}, "challenger": {"version": CHALLENGER_VERSION, "config": CHALLENGER_CONFIG.to_dict(), **ch},
            "cash": cash, "both_lost_money": both_lost,
            "primary": {"measure": "paired daily after-cost return difference, challenger minus incumbent (incl. zero-trade days)",
                        "daily_differences": [float(x) for x in d], **ci, "bootstrap": BOOTSTRAP},
            "verdict": verdict(kind, inc, ch, ci, len(sessions)),
            "manifest": manifest, "manifest_sha256": sha(manifest) if manifest else None}


# ---------------------------------------------------------------------------------------------------------- protocols
def freeze(path: Path, **fields) -> dict:
    """Write the evaluation manifest BEFORE the holdout run. Refuses to overwrite a different frozen manifest."""
    m = {"contest_version": CONTEST_VERSION, "code": code_version(), "incumbent": incumbent_label(),
         "incumbent_rules": PortfolioRules.live(fields["capital"]).to_dict(), "challenger": CHALLENGER_VERSION,
         "challenger_config": CHALLENGER_CONFIG.to_dict(), "challenger_rules": challenger_rules(fields["capital"]).to_dict(),
         "execution": (fields.get("exec_cfg") or ExecConfig()).to_dict(), "charges": "dated NSE charge schedule (nseql.instruments.rules)",
         "primary_measure": "paired daily after-cost return difference, challenger minus incumbent",
         "bootstrap": BOOTSTRAP, "verdict_rules": {"min_sessions": MIN_SESSIONS, "min_challenger_trades": MIN_TRADES},
         **{k: v for k, v in fields.items() if k != "exec_cfg"}}
    path = Path(path)
    if path.exists():
        old = json.loads(path.read_text())
        if sha(old) != sha(m):
            raise RuntimeError(f"a different manifest is already frozen at {path}; a changed evaluation needs a new folder")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(m, indent=2, default=str))
    return m


def run_protocol(root: Path, stream: list[tuple], symbols: list[str], dev_days: list[str], eval_days: list[str], capital: float,
                 kind: str, data: dict) -> dict:
    """Development run, freeze, then one holdout run. Each phase uses fresh, isolated accounts."""
    root = Path(root)
    days = lambda s: [x for x in stream if pd.Timestamp(x[0]).strftime("%Y-%m-%d") in s]
    dev_stream, ev_stream = days(set(dev_days)), days(set(eval_days))
    dev = open_pair(root / "development", symbols, capital)
    feed(dev, dev_stream)
    dev_rep = compare(dev, dev_days, capital, kind)
    manifest = freeze(root / "manifest.json", capital=capital, symbols=symbols, development_days=dev_days, evaluation_days=eval_days,
                      data={**data, "fingerprint_evaluation": fingerprint(ev_stream)}, data_kind=kind)
    ev = open_pair(root / "evaluation", symbols, capital)
    # the evaluation accounts also see the development period first (warm indicators), but trade and are measured on
    # the evaluation sessions only: paper orders start at the first evaluation minute
    for s in (ev.incumbent, ev.challenger):
        s.live_from = pd.Timestamp(f"{eval_days[0]}T09:16:00").isoformat()
    feed(ev, dev_stream + ev_stream)
    rep = compare(ev, eval_days, capital, kind, manifest)
    rep["development"] = {k: dev_rep[k] for k in ("sessions", "incumbent", "challenger", "primary")}
    (root / "report.json").write_text(json.dumps(rep, indent=2, default=str))
    return rep


def run_fixture(root: Path, symbols_n: int = 10, dev_n: int = 5, eval_n: int = 15, seed: int = 7, capital: float = 100_000) -> dict:
    """Deterministic SYNTHETIC contest from a clean checkout (no data or credentials needed)."""
    from nseql.screening.synthetic import minute_stream, symbols
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=dev_n + eval_n)]
    syms = symbols(symbols_n)
    stream = minute_stream(symbols_n, days, seed=seed)
    return run_protocol(root, stream, syms, days[:dev_n], days[dev_n:], capital, "synthetic",
                        {"source": "nseql.screening.synthetic.minute_stream", "seed": seed, "symbols": symbols_n})


def stream_from_frames(frames: dict[str, pd.DataFrame]) -> list[tuple]:
    """{symbol: 1-minute bars with ts/open/high/low/close/volume} -> [(minute, [bars])] in time order (session bars only)."""
    from nseql.screening.scheduler import _in_session
    by: dict[pd.Timestamp, list[dict]] = {}
    for s, df in frames.items():
        for r in df.itertuples():
            t = pd.Timestamp(r.ts)
            if _in_session(t):
                by.setdefault(t, []).append({"symbol": s, "ts": t, "open": float(r.open), "high": float(r.high), "low": float(r.low),
                                             "close": float(r.close), "volume": float(r.volume)})
    return sorted(by.items(), key=lambda x: x[0])


def forward_report(incumbent: MinuteScheduler, challenger: MinuteScheduler, meta: dict) -> dict:
    """Forward paper contest from the two live ledgers (sessions from contest_from with processed minutes)."""
    with incumbent.store._lock, challenger.store._lock:
        days = [r[0] for r in challenger.store.db.execute(
            "SELECT DISTINCT substr(minute,1,10) FROM cycles WHERE status='processed' AND mode != 'warmup' AND substr(minute,1,10) >= ? ORDER BY 1",
            (meta["contest_from"],))]
        rep = compare(Pair(incumbent, challenger), days, float(meta["start_cash"]), "forward",
                      {"contest_from": meta["contest_from"], "challenger": meta.get("challenger"), "incumbent_account": meta.get("incumbent_account"),
                       "start_cash": meta["start_cash"], "code": code_version(), "verdict_rules": {"min_sessions": MIN_SESSIONS,
                                                                                                    "min_challenger_trades": MIN_TRADES}})
    return rep

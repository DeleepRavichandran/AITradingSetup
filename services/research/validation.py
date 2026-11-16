"""Validation toolkit: partitions, walk-forward, resampling tests, sensitivity, multiple-testing control.

Everything here consumes backtests through an `Evaluator`, which runs built-in strategies
in-process and AI-generated code only inside the sandbox. All randomness is seeded.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from nseql.analytics.metrics import TRADING_DAYS, drawdown_stats, sharpe
from nseql.backtester.runner import BacktestConfig, run_backtest
from nseql.data.store import Dataset
from nseql.research import sandbox


# ----------------------------------------------------------------------------- partitions
@dataclass(frozen=True)
class Partition:
    """Chronological split. `sealed` is touched only by the final, once-only evaluation."""
    dev_start: str
    dev_end: str
    val_start: str
    val_end: str
    sealed_start: str
    sealed_end: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    def range(self, name: str) -> tuple[str, str]:
        return {"dev": (self.dev_start, self.dev_end), "validation": (self.val_start, self.val_end),
                "sealed": (self.sealed_start, self.sealed_end), "dev+validation": (self.dev_start, self.val_end)}[name]


def make_partition(ds: Dataset, interval: str = "1d", dev: float = 0.6, val: float = 0.2, reference: str | None = None) -> Partition:
    sym = reference or next(s for s in ds.symbols(interval) if ds.instruments[s].kind.value in ("INDEX", "EQ"))
    dates = sorted(set(ds.bars(sym, interval)["ts"].dt.date))
    n = len(dates)
    if n < 100:
        raise ValueError("not enough history to partition")
    a, b = int(n * dev), int(n * (dev + val))
    s = lambda d: d.isoformat()
    return Partition(s(dates[0]), s(dates[a - 1]), s(dates[a]), s(dates[b - 1]), s(dates[b]), s(dates[-1]))


def purged_kfold(n: int, k: int = 5, label_horizon: int = 1, embargo: int = 0) -> list[tuple[np.ndarray, np.ndarray]]:
    """Index folds for ML: training rows whose label window overlaps the test block are purged,
    and `embargo` rows after each test block are dropped."""
    out = []
    edges = np.linspace(0, n, k + 1, dtype=int)
    idx = np.arange(n)
    for a, b in zip(edges[:-1], edges[1:]):
        train = (idx < a - label_horizon) | (idx >= b + embargo + label_horizon)
        out.append((idx[train], idx[a:b]))
    return out


# ----------------------------------------------------------------------------- evaluator
class Evaluator:
    """Runs variants of one strategy. `source` set => generated code => sandbox only."""

    def __init__(self, data_dir: Path, strategy: str, *, source: str | None = None, base: dict | None = None,
                 capital: float = 1_000_000.0, out_dir: Path | None = None, limits: sandbox.SandboxLimits | None = None):
        self.data_dir, self.strategy, self.source = Path(data_dir), strategy, source
        self.base = base or {}
        self.capital = capital
        self.out_dir = out_dir
        self.limits = limits
        self.calls = 0

    def _cfg(self, v: dict) -> dict:
        cfg = {**self.base, **{k: x for k, x in v.items() if k not in ("params", "save", "strategy")}}
        cfg["strategies"] = [{"name": v.get("strategy", self.strategy), "params": v.get("params", {}), "capital": self.capital}]
        return cfg

    def run_many(self, variants: list[dict]) -> list[dict]:
        self.calls += len(variants)
        if self.source is not None and not all(v.get("strategy") for v in variants):
            own = [(i, v) for i, v in enumerate(variants) if not v.get("strategy")]
            jobs = [{"id": i, "config": self._cfg(v), "save": bool(v.get("save"))} for i, v in own]
            res = sandbox.run_jobs(self.source, jobs, self.data_dir, out_dir=self.out_dir, limits=self.limits)
            out: list[dict | None] = [None] * len(variants)
            if not res["results"]:
                for i, _ in own:
                    out[i] = {"ok": False, "error": res["error"]}
            for r in res["results"]:
                out[r["id"]] = self._norm(r)
            rest = [(i, v) for i, v in enumerate(variants) if v.get("strategy")]
            for (i, v), r in zip(rest, self._inproc([v for _, v in rest])):
                out[i] = r
            return out  # type: ignore[return-value]
        return self._inproc(variants)

    def _inproc(self, variants: list[dict]) -> list[dict]:
        out = []
        for v in variants:
            try:
                r = run_backtest(BacktestConfig(**self._cfg(v)), self.data_dir)
                path = str(r.save(self.out_dir / r.run_id)) if (self.out_dir and v.get("save")) else None
                eq = r.equity_frame()
                daily = eq.groupby(eq["ts"].dt.normalize())["equity"].last()
                out.append({"ok": r.summary["status"] == "ok", "summary": r.summary, "run_id": r.run_id, "path": path,
                            "daily": daily, "error": r.summary["error"],
                            "trades": [{"net": t["net"], "exit_ts": t["exit_ts"], "symbol": t["symbol"]} for t in r.trades()]})
            except Exception as ex:
                out.append({"ok": False, "error": f"{type(ex).__name__}: {ex}"})
        return out

    @staticmethod
    def _norm(r: dict) -> dict:
        if not r.get("summary"):
            return {"ok": False, "error": r.get("error", "no result")}
        d = r["daily_equity"]
        return {"ok": r["ok"], "summary": r["summary"], "run_id": r["run_id"], "path": r.get("path"),
                "daily": pd.Series(d["values"], index=pd.to_datetime(d["dates"])), "trades": r["trades"],
                "error": r["summary"].get("error")}

    def run(self, **variant) -> dict:
        return self.run_many([variant])[0]


def objective(summary: dict, min_trades: int = 10) -> float:
    """Selection objective: Sharpe, but a variant with too few trades scores -inf (no evidence)."""
    m = summary["metrics"]
    if m.get("trades", 0) < min_trades or m.get("sharpe") is None:
        return float("-inf")
    return float(m["sharpe"])


def grid_points(grid: dict, max_points: int = 24, seed: int = 0) -> list[dict]:
    keys = sorted(grid)
    pts = [dict(zip(keys, c)) for c in itertools.product(*(grid[k] for k in keys))]
    if len(pts) > max_points:                      # deterministic subsample keeps the compute budget bounded
        rng = np.random.default_rng(seed)
        pts = [pts[i] for i in sorted(rng.choice(len(pts), max_points, replace=False))]
    return pts


# ----------------------------------------------------------------------------- walk-forward
def walk_forward(ev: Evaluator, grid: dict, *, start: str, end: str, train_days: int = 250, test_days: int = 60,
                 mode: str = "rolling", max_points: int = 16, min_trades: int = 5, dataset: Dataset | None = None,
                 base_params: dict | None = None) -> dict:
    """Walk-forward optimisation.

    For each fold: every grid point is backtested on the training window only; the best is then
    run from the training start through the test window with those parameters frozen, and ONLY
    the test-window daily returns are kept. Stitched together they form the out-of-sample curve.
    """
    ds = dataset or Dataset(ev.data_dir / ev.base.get("dataset_id", "synthetic_v1"))
    interval = ev.base.get("interval", "1d")
    ref = next(s for s in ds.symbols(interval) if ds.instruments[s].kind.value in ("INDEX", "EQ"))
    dates = [d for d in sorted(set(ds.bars(ref, interval, start, end)["ts"].dt.date))]
    pts = grid_points(grid, max_points) or [{}]
    base_params = base_params or {}
    folds = []
    oos_parts = []
    i = train_days
    while i + 5 <= len(dates):
        tr0 = dates[0] if mode == "expanding" else dates[i - train_days]
        tr1 = dates[i - 1]
        te0, te1 = dates[i], dates[min(i + test_days, len(dates)) - 1]
        ins = ev.run_many([{"params": {**base_params, **p}, "start": tr0.isoformat(), "end": tr1.isoformat()} for p in pts])
        scores = [objective(r["summary"], min_trades) if r["ok"] else float("-inf") for r in ins]
        best = int(np.argmax(scores))
        fold = {"fold": len(folds) + 1, "train": [tr0.isoformat(), tr1.isoformat()], "test": [te0.isoformat(), te1.isoformat()],
                "candidates": len(pts), "chosen_params": pts[best], "in_sample_sharpe": None if scores[best] == float("-inf") else scores[best]}
        if scores[best] == float("-inf"):
            fold.update(skipped="no parameter set produced enough trades in training", oos_return=0.0, oos_sharpe=None)
            oos = pd.Series(0.0, index=pd.to_datetime([d for d in dates if te0 <= d <= te1]))
        else:
            r = ev.run(params={**base_params, **pts[best]}, start=tr0.isoformat(), end=te1.isoformat(), save=True)
            if not r["ok"]:
                fold.update(skipped=f"out-of-sample run failed: {r.get('error')}", oos_return=0.0, oos_sharpe=None)
                oos = pd.Series(0.0, index=pd.to_datetime([d for d in dates if te0 <= d <= te1]))
            else:
                rets = r["daily"].pct_change()
                oos = rets[rets.index >= pd.Timestamp(te0)].fillna(0.0)
                fold.update(oos_return=float((1 + oos).prod() - 1), oos_sharpe=sharpe(oos.to_numpy()), run_id=r["run_id"],
                            oos_trades_total_run=r["summary"]["metrics"]["trades"])
        folds.append(fold)
        oos_parts.append(oos)
        i += test_days
    if not folds:
        return {"error": "window too short for the requested train/test lengths", "folds": []}
    oos_all = pd.concat(oos_parts)
    curve = (1 + oos_all).cumprod()
    is_s = [f["in_sample_sharpe"] for f in folds if f["in_sample_sharpe"] is not None]
    oos_s = [f["oos_sharpe"] for f in folds if f.get("oos_sharpe") is not None]
    return {
        "mode": mode, "train_days": train_days, "test_days": test_days, "grid": grid, "grid_points": len(pts),
        "folds": folds, "backtests_run": ev.calls,
        "oos": {"total_return": float(curve.iloc[-1] - 1), "sharpe": sharpe(oos_all.to_numpy()),
                "max_drawdown": drawdown_stats(curve)["max_drawdown"], "sessions": int(len(oos_all)),
                "positive_folds": int(sum(f["oos_return"] > 0 for f in folds)), "folds": len(folds)},
        "mean_in_sample_sharpe": float(np.mean(is_s)) if is_s else None,
        "mean_oos_sharpe": float(np.mean(oos_s)) if oos_s else None,
        "walk_forward_efficiency": (float(np.mean(oos_s) / np.mean(is_s)) if is_s and oos_s and np.mean(is_s) > 0 else None),
        "param_drift": len({tuple(sorted(f["chosen_params"].items())) for f in folds}),
        "curve": {"dates": [str(d.date()) for d in curve.index], "values": [float(x) for x in curve]},
    }


# ----------------------------------------------------------------------------- resampling
def monte_carlo_trades(trade_nets: list[float], capital: float, n: int = 2000, seed: int = 11) -> dict:
    """Reshuffle the order of realised trades (path risk) and bootstrap them (outcome risk)."""
    x = np.asarray(trade_nets, dtype=float)
    if len(x) < 5:
        return {"error": "fewer than 5 trades", "trades": int(len(x))}
    rng = np.random.default_rng(seed)
    dds = np.empty(n)
    for i in range(n):
        eq = capital + np.cumsum(rng.permutation(x))
        peak = np.maximum.accumulate(np.concatenate([[capital], eq]))[1:]
        dds[i] = ((eq - peak) / peak).min()
    boot = rng.choice(x, size=(n, len(x)), replace=True).sum(axis=1)
    return {"trades": int(len(x)), "simulations": n, "seed": seed,
            "max_drawdown_p50": float(np.quantile(dds, 0.5)), "max_drawdown_p95": float(np.quantile(dds, 0.05)),
            "max_drawdown_worst": float(dds.min()), "bootstrap_pnl_p05": float(np.quantile(boot, 0.05)),
            "bootstrap_pnl_p50": float(np.quantile(boot, 0.5)), "bootstrap_pnl_p95": float(np.quantile(boot, 0.95)),
            "prob_loss": float((boot <= 0).mean())}


def block_bootstrap_sharpe(returns, block: int = 10, n: int = 2000, seed: int = 13) -> dict:
    """Circular block bootstrap of daily returns (preserves short-range dependence).
    p_value = share of resamples with Sharpe <= 0."""
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    T = len(r)
    if T < 3 * block:
        return {"error": "series too short", "sessions": int(T)}
    rng = np.random.default_rng(seed)
    k = math.ceil(T / block)
    out = np.empty(n)
    for i in range(n):
        starts = rng.integers(0, T, k)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[:T] % T
        s = r[idx]
        sd = s.std(ddof=1)
        out[i] = s.mean() / sd * math.sqrt(TRADING_DAYS) if sd > 0 else 0.0
    return {"sessions": int(T), "block": block, "simulations": n, "seed": seed, "sharpe": sharpe(r),
            "ci_05": float(np.quantile(out, 0.05)), "ci_95": float(np.quantile(out, 0.95)),
            "p_value": float((out <= 0).mean())}


# ----------------------------------------------------------------------------- multiple testing
def deflated_sharpe(returns, n_trials: int, trial_sharpe_var: float | None = None) -> dict:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014).

    Probability that the true Sharpe exceeds the maximum Sharpe expected from `n_trials`
    unskilled attempts, adjusting for sample length, skewness and kurtosis. All in per-period units.
    """
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    T = len(r)
    if T < 30 or r.std(ddof=1) == 0:
        return {"error": "insufficient data", "dsr": None}
    sr = r.mean() / r.std(ddof=1)
    skew, kurt = float(stats.skew(r)), float(stats.kurtosis(r, fisher=False))
    n = max(int(n_trials), 1)
    var = trial_sharpe_var if trial_sharpe_var and trial_sharpe_var > 0 else 1.0 / T   # null: SR estimator variance
    if n > 1:
        g = 0.5772156649
        sr0 = math.sqrt(var) * ((1 - g) * stats.norm.ppf(1 - 1 / n) + g * stats.norm.ppf(1 - 1 / (n * math.e)))
    else:
        sr0 = 0.0
    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr * sr, 1e-12))
    z = (sr - sr0) * math.sqrt(T - 1) / denom
    return {"dsr": float(stats.norm.cdf(z)), "sharpe_annual": float(sr * math.sqrt(TRADING_DAYS)),
            "expected_max_sharpe_annual": float(sr0 * math.sqrt(TRADING_DAYS)), "n_trials": n, "sessions": int(T),
            "skew": skew, "kurtosis": kurt}


def benjamini_hochberg(pvalues: list[float], alpha: float = 0.10) -> list[bool]:
    """False-discovery-rate control across a family of hypotheses. Returns reject flags."""
    m = len(pvalues)
    if m == 0:
        return []
    order = np.argsort(pvalues)
    thresh = alpha * (np.arange(1, m + 1) / m)
    passed = np.asarray(pvalues)[order] <= thresh
    k = np.max(np.nonzero(passed)[0]) + 1 if passed.any() else 0
    out = np.zeros(m, dtype=bool)
    out[order[:k]] = True
    return out.tolist()


# ----------------------------------------------------------------------------- sensitivity
def cost_sensitivity(ev: Evaluator, params: dict, start: str, end: str, multipliers=(0.0, 1.0, 2.0, 3.0)) -> dict:
    res = ev.run_many([{"params": params, "start": start, "end": end, "cost_multiplier": m,
                        "execution": {**ev.base.get("execution", {}), "slippage_multiplier": max(m, 0.0)}} for m in multipliers])
    rows = [{"multiplier": m, "sharpe": r["summary"]["metrics"]["sharpe"], "total_return": r["summary"]["metrics"]["total_return"],
             "total_costs": r["summary"]["metrics"]["total_costs"]} if r["ok"] else {"multiplier": m, "error": r.get("error")}
            for m, r in zip(multipliers, res)]
    ok = [x for x in rows if "error" not in x]
    be = next((x["multiplier"] for x in ok if (x["total_return"] or 0) <= 0), None)
    return {"rows": rows, "first_unprofitable_multiplier": be,
            "note": "multiplier scales statutory charges, brokerage and slippage together"}


def stress_tests(ev: Evaluator, params: dict, start: str, end: str) -> dict:
    base_exec = ev.base.get("execution", {})
    scenarios = {"baseline": {}, "one_bar_latency": {"latency_bars": 1}, "triple_spread": {"slippage_multiplier": 3.0},
                 "thin_liquidity_2pct": {"max_participation": 0.02}, "limit_on_touch_optimistic": {"limit_fill_on_touch": True}}
    res = ev.run_many([{"params": params, "start": start, "end": end, "execution": {**base_exec, **x}} for x in scenarios.values()])
    return {name: ({"sharpe": r["summary"]["metrics"]["sharpe"], "total_return": r["summary"]["metrics"]["total_return"],
                    "max_drawdown": r["summary"]["metrics"]["max_drawdown"], "trades": r["summary"]["metrics"]["trades"]}
                   if r["ok"] else {"error": r.get("error")}) for name, r in zip(scenarios, res)}


def parameter_stability(ev: Evaluator, params: dict, grid: dict, start: str, end: str) -> dict:
    """Move each parameter one grid step either side of the chosen value. A real effect degrades
    gracefully; an overfit one collapses."""
    variants = []
    for k, vals in sorted(grid.items()):
        vals = sorted(set(vals) | {params.get(k)}) if params.get(k) is not None else sorted(vals)
        if params.get(k) is None:
            continue
        i = vals.index(params[k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                variants.append({**params, k: vals[j]})
    if not variants:
        return {"neighbours": 0, "note": "no parameter grid declared"}
    res = ev.run_many([{"params": params, "start": start, "end": end}] + [{"params": v, "start": start, "end": end} for v in variants])
    base = res[0]["summary"]["metrics"]["sharpe"] if res[0]["ok"] else None
    rows = [{"params": v, "sharpe": r["summary"]["metrics"]["sharpe"] if r["ok"] else None} for v, r in zip(variants, res[1:])]
    sh = [x["sharpe"] for x in rows if x["sharpe"] is not None]
    return {"base_sharpe": base, "neighbours": len(rows), "rows": rows,
            "share_positive": float(np.mean([s > 0 for s in sh])) if sh else None,
            "median_neighbour_sharpe": float(np.median(sh)) if sh else None,
            "worst_neighbour_sharpe": float(np.min(sh)) if sh else None}


def regime_breakdown(daily_equity: pd.Series, benchmark_close: pd.Series, vol_window: int = 20, trend_window: int = 100) -> dict:
    """Strategy performance split by the benchmark's volatility tercile and trend state.

    Regime labels for day t use benchmark data through t-1 only. Tercile cut-offs are computed on
    the whole sample, so this is a descriptive diagnostic, not a tradable signal."""
    r = daily_equity.pct_change().dropna()
    b = benchmark_close.copy()
    b.index = pd.to_datetime(b.index).normalize()
    vol = b.pct_change().rolling(vol_window).std().shift(1)
    trend = (b > b.rolling(trend_window).mean()).shift(1)
    df = pd.DataFrame({"r": r, "vol": vol.reindex(r.index), "trend": trend.reindex(r.index)}).dropna()
    if len(df) < 60:
        return {"error": "not enough overlapping history"}
    q1, q2 = df["vol"].quantile([1 / 3, 2 / 3])
    df["vol_regime"] = np.where(df["vol"] <= q1, "low_vol", np.where(df["vol"] <= q2, "mid_vol", "high_vol"))
    df["trend_regime"] = np.where(df["trend"].astype(bool), "uptrend", "downtrend")
    out = {}
    for col in ("vol_regime", "trend_regime"):
        for name, g in df.groupby(col):
            out[name] = {"sessions": int(len(g)), "mean_daily_return": float(g["r"].mean()), "sharpe": sharpe(g["r"].to_numpy()),
                         "total_return": float((1 + g["r"]).prod() - 1), "share_of_sessions": float(len(g) / len(df))}
    return out


def baseline_comparison(ev: Evaluator, params: dict, start: str, end: str) -> dict:
    res = ev.run_many([{"params": params, "start": start, "end": end},
                       {"strategy": "buy_and_hold", "params": {}, "start": start, "end": end},
                       {"strategy": "random_entry", "params": {}, "start": start, "end": end}])
    pick = lambda r: ({k: r["summary"]["metrics"].get(k) for k in ("sharpe", "total_return", "max_drawdown", "trades")}
                      if r["ok"] else {"error": r.get("error")})
    out = {"strategy": pick(res[0]), "buy_and_hold": pick(res[1]), "random_entry": pick(res[2])}
    s, b = out["strategy"].get("sharpe"), out["buy_and_hold"].get("sharpe")
    out["sharpe_excess_vs_buy_and_hold"] = (s - b) if s is not None and b is not None else None
    return out


def window_metrics(daily: pd.Series, trades: list[dict], start: str, end: str) -> dict:
    """Metrics of one calendar window cut from a longer run (positions and indicator state carry
    in from before the window, exactly as they would in live operation)."""
    rets = daily.pct_change()
    a, b = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)
    w = rets[(rets.index >= a) & (rets.index < b)].dropna()
    tr = [t for t in trades if a <= pd.Timestamp(t["exit_ts"]) < b]
    if len(w) == 0:
        return {"sessions": 0, "trades": len(tr), "sharpe": None, "total_return": None, "max_drawdown": None}
    curve = (1 + w).cumprod()
    nets = [t["net"] for t in tr]
    return {"sessions": int(len(w)), "trades": len(tr), "sharpe": sharpe(w.to_numpy()),
            "total_return": float(curve.iloc[-1] - 1), "max_drawdown": drawdown_stats(pd.concat([pd.Series([1.0]), curve]).reset_index(drop=True).set_axis(pd.date_range("2000-01-01", periods=len(curve) + 1)))["max_drawdown"],
            "win_rate": float(np.mean([n > 0 for n in nets])) if nets else None, "net_trade_pnl": float(sum(nets)),
            "window": [start, end]}


def lookahead_audit(ds: Dataset, cfg: BacktestConfig, factory, cuts=None) -> dict:
    """Look-ahead trap test. Run the strategy once on the real data and once on a GuardedPortal, in
    which every value in the future of the engine's clock is noise until the clock reaches it.
    A causal strategy submits exactly the same orders in both runs. Any difference proves that a
    decision used information that did not yet exist."""
    from nseql.backtester.engine import Allocation, BacktestEngine
    from nseql.backtester.runner import default_symbols
    from nseql.data.portal import DataPortal, GuardedPortal
    keep = ("book", "symbol", "side", "qty", "type", "limit_price", "stop_price", "tag", "submitted_ts")
    ref = cfg.strategies[0]
    syms = cfg.symbols or default_symbols(ds, [factory(ref)], cfg.interval)
    clean = DataPortal.from_dataset(ds, cfg.interval, syms, cfg.start, cfg.end)
    runs = []
    for portal in (clean, GuardedPortal(DataPortal.from_dataset(ds, cfg.interval, syms, cfg.start, cfg.end))):
        r = BacktestEngine(portal, [Allocation(factory(ref), "b", ref.capital)]).run()
        runs.append(([{k: d[k] for k in keep} for o in r.orders if not o.forced for d in [o.to_dict()]], r.error))
    (a, ea), (b, eb) = runs
    first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), None)
    if first is None and len(a) != len(b):
        first = min(len(a), len(b))
    identical = first is None and ea == eb
    return {"passed": identical and not ea, "identical": identical, "orders_checked": len(a), "orders_on_guarded_data": len(b),
            "first_difference": None if first is None else {"index": first, "clean": a[first] if first < len(a) else None,
                                                            "guarded": b[first] if first < len(b) else None},
            "error": ea or eb, "method": "future values replaced by noise until the clock reaches them"}

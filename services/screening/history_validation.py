"""Historical validation on real NSE minute data (docs/HISTORY.md): is the current methodology any good on months of
history it was not designed on?

Runs on the laptop (the Angel One login lives there), unattended, after the close; the results are readable through the API.
Reuses the existing pieces: Angel One import (angel_import.py), the minute replay engine (MinuteScheduler, the exact live
rules and execution model), contest metrics and bootstrap, integrity statistics, the concept study and the feature study.

Protocol (registered once in the evidence ledger, then only extended forward, never re-split):
  warm-up        the first WARMUP sessions: indicators build up, no orders
  development    up to 50% of the sessions at registration
  validation     50-75%
  holdout        from the registered cut-off onwards (new sessions join it as data grows)
  contaminated   sessions on or after the first live paper day: the live rules (1.3, 1.4) were shaped on them, so they are
                 reported separately and never counted as holdout evidence
No parameter is fitted on this data; the segments exist so that later research keeps a clean holdout.

For each segment: the incumbent (live rules) and the challenger after all costs, against cash, NIFTY 50 and buy-and-hold of
the watchlist; mean daily return with a block-bootstrap interval; drawdown; trades; charges. On the holdout round trips:
random-entry placebo and cost and delay stress. Then the concept study (ICT/SMC/price action) and the feature study
(heatmap features) on the same data. Results go to experiments/history/<run>/report.json, the evidence ledger and knowledge.
"""
from __future__ import annotations

import json
import math
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from nseql.screening import integrity as I

VERSION = "hv-1.0"
DATASET = "angel_minute"
WARMUP = 5
FIRST_LIVE_DAY = "2026-11-23"            # the live paper account began; rules 1.1-1.4 were shaped on sessions from here
SEGMENTS = ("development", "validation", "holdout", "contaminated")
# (label, variant, reference): does each brain component add value, and is it more than trading less or smaller?
ABLATIONS = (("B vs A: uncertainty-awareness", "B uncertainty-aware", "incumbent"),
             ("B vs B0: selection quality, not just fewer or smaller trades", "B uncertainty-aware", "B0 matched control"),
             ("B0 vs A: effect of merely trading less or smaller", "B0 matched control", "incumbent"))


def history_root(exp_dir: Path) -> Path:
    p = Path(exp_dir) / "history"
    p.mkdir(parents=True, exist_ok=True)
    return p


def status(exp_dir: Path) -> dict:
    f = history_root(exp_dir) / "status.json"
    return json.loads(f.read_text()) if f.exists() else {"state": "never run"}


def _status(exp_dir: Path, **kw) -> None:
    f = history_root(exp_dir) / "status.json"
    cur = status(exp_dir)
    cur.update(kw, updated_at=datetime.now().isoformat(timespec="seconds"))
    f.write_text(json.dumps(cur, indent=2, default=str))


# ================================================================================================ 1. data
def import_history(api, data_dir: Path, exp_dir: Path, symbols: list[str], months: int = 12, end: date | None = None) -> dict:
    """1-minute candles for the watchlist and NIFTY 50 from Angel One, in 30-day requests. Whatever Angel One returns is
    what is used; coverage is reported per symbol, nothing is filled in."""
    from nseql.data.adapters.angel_import import import_angel
    end = end or date.today()
    start = end - timedelta(days=int(months * 30.5))
    _status(exp_dir, state="importing", phase=f"Angel One 1-minute candles {start} -> {end}")
    ds, missing = import_angel(api, symbols, start, end, data_dir, DATASET, licence="Angel One SmartAPI, personal research",
                               interval="1m", cache_dir=exp_dir, overwrite=True, progress=lambda m: _status(exp_dir, detail=m))
    return coverage(ds) | {"not_imported": missing, "requested": [str(start), str(end)]}


def coverage(ds) -> dict:
    rows = []
    for s in ds.symbols("1m"):
        b = ds.bars(s, "1m")
        days = pd.to_datetime(b.ts).dt.normalize().nunique() if len(b) else 0
        rows.append({"symbol": s, "bars": int(len(b)), "sessions": int(days), "first": str(b.ts.iloc[0])[:10] if len(b) else None,
                     "last": str(b.ts.iloc[-1])[:10] if len(b) else None})
    return {"dataset": ds.id, "content_hash": ds.content_hash, "synthetic": ds.synthetic, "symbols": rows}


def _frames(ds) -> dict[str, pd.DataFrame]:
    out = {}
    for s in ds.symbols("1m"):
        b = ds.bars(s, "1m")[["ts", "open", "high", "low", "close", "volume"]].copy()
        b["ts"] = pd.to_datetime(b.ts)
        out[s] = b
    return out


def _day_stream(frames: dict[str, pd.DataFrame], day: str) -> list[tuple]:
    from nseql.screening.scheduler import _in_session
    by: dict = {}
    lo, hi = pd.Timestamp(day), pd.Timestamp(day) + pd.Timedelta(days=1)
    for s, df in frames.items():
        d = df[(df.ts >= lo) & (df.ts < hi)]
        for r in d.itertuples():
            if _in_session(r.ts):
                by.setdefault(r.ts, []).append({"symbol": s, "ts": r.ts, "open": float(r.open), "high": float(r.high), "low": float(r.low),
                                                "close": float(r.close), "volume": float(r.volume)})
    return sorted(by.items(), key=lambda x: x[0])


# ================================================================================================ 2. protocol
def protocol(exp_dir: Path, dataset_id: str, sessions: list[str]) -> dict:
    """Registered once per dataset: the holdout cut-off never moves back (new data only extends the holdout)."""
    from nseql.screening.evidence_guard import GuardedLedger
    L = GuardedLedger(I.Ledger(exp_dir), "evaluation")
    reg = [e for e in L.entries("protocol") if e["subject"] == f"HIST {dataset_id}"]
    if reg:
        return reg[0]["body"]
    usable = sessions[WARMUP:]
    pre = [d for d in usable if d < FIRST_LIVE_DAY]
    if len(pre) < 8:
        raise ValueError(f"only {len(pre)} usable sessions before {FIRST_LIVE_DAY}; need at least 8 for a protocol")
    body = {"dataset": dataset_id, "registered_at": datetime.now().isoformat(timespec="seconds"), "warmup_sessions": WARMUP,
            "development_until": pre[int(len(pre) * 0.5) - 1], "validation_until": pre[int(len(pre) * 0.75) - 1],
            "holdout_from": pre[int(len(pre) * 0.75)], "contaminated_from": FIRST_LIVE_DAY, "version": VERSION,
            "rule": "the holdout only grows forward; it is never re-split or tuned on"}
    L.append("protocol", f"HIST {dataset_id}", body)
    return body


def segment_of(day: str, p: dict) -> str | None:
    if day >= p["contaminated_from"]:
        return "contaminated"
    if day >= p["holdout_from"]:
        return "holdout"
    if day > p["validation_until"]:
        return None                                            # the gap is impossible by construction; kept for safety
    return "development" if day <= p["development_until"] else "validation"


# ================================================================================================ 3. measuring
def _benchmarks(frames: dict[str, pd.DataFrame], days: list[str]) -> dict:
    cl = {s: df.groupby(df.ts.dt.normalize()).close.last() for s, df in frames.items()}
    out = {}
    if "NIFTY50" in cl or "NIFTY 50" in cl:
        n = cl.get("NIFTY50", cl.get("NIFTY 50"))
        r = n.pct_change()
        out["NIFTY 50"] = [float(r.get(pd.Timestamp(d), np.nan)) for d in days]
    stocks = [s for s in cl if not s.upper().startswith("NIFTY")]
    if stocks:
        m = pd.DataFrame({s: cl[s].pct_change() for s in stocks}).mean(axis=1)
        out["buy and hold watchlist"] = [float(m.get(pd.Timestamp(d), np.nan)) for d in days]
    out["cash"] = [0.0] * len(days)
    return out


def _series_stats(r: list[float]) -> dict:
    from nseql.screening.contest import block_bootstrap
    x = np.array([v for v in r if v is not None and math.isfinite(v)], dtype=float)
    if not len(x):
        return {"sessions": 0}
    eq = np.cumprod(1 + x)
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))
    t = I.t_test(x)
    ci = block_bootstrap(x)
    sd = float(x.std(ddof=1)) if len(x) > 1 else 0.0
    return {"sessions": int(len(x)), "total_return": float(eq[-1] - 1), "mean_daily": float(x.mean()), "ci_low": ci["low"], "ci_high": ci["high"],
            "p_mean_above_zero": t["p"], "sharpe": float(x.mean() / sd * math.sqrt(252)) if sd > 0 else None,
            "max_drawdown": float((np.concatenate([[1.0], eq]) / peak - 1).min()), "worst_day": float(x.min()), "best_day": float(x.max())}


MIN_READING_SESSIONS = I.MIN_SESSIONS     # 20: below this a paired comparison gets numbers but no reading


def _excess(a: list[float], b: list[float]) -> dict:
    """Paired daily difference a - b with a block-bootstrap interval. No reading below MIN_READING_SESSIONS: with as many
    sessions as the bootstrap block length or fewer, every resample is the whole series and the 'interval' is a point, so a
    single good day would read as 'ahead'."""
    from nseql.screening.contest import block_bootstrap
    d = np.array([x - y for x, y in zip(a, b) if x is not None and y is not None and math.isfinite(x) and math.isfinite(y)], dtype=float)
    if len(d) < 2:
        return {"sessions": int(len(d))}
    ci = block_bootstrap(d)
    t = I.t_test(d)
    out = {"sessions": int(len(d)), "mean_daily_excess": float(d.mean()), "ci_low": ci["low"], "ci_high": ci["high"], "p": t["p"]}
    if len(d) < MIN_READING_SESSIONS:
        return {**out, "reading": f"too few sessions ({len(d)} of {MIN_READING_SESSIONS})"}
    return {**out, "reading": "ahead (interval above zero)" if ci["low"] > 0 else "behind (interval below zero)" if ci["high"] < 0 else "indistinguishable"}


def plain(seg: dict, who: str) -> str:
    s = seg.get(who, {})
    if not s.get("sessions"):
        return f"{who}: no sessions in this segment."
    vs = seg.get("vs", {}).get(who, {})
    bits = [f"{who} {s['total_return']:+.2%} over {s['sessions']} sessions after costs (max drawdown {s['max_drawdown']:.1%})"]
    for k, v in vs.items():
        if v.get("reading"):
            bits.append(f"vs {k}: {v['reading']}")
    return "; ".join(bits) + "."


# ================================================================================================ 4. run
def market_session_on(now: datetime | None = None) -> bool:
    """09:00-15:35 IST on a weekday: the live bot has the machine."""
    from zoneinfo import ZoneInfo
    now = now or datetime.now(ZoneInfo("Asia/Kolkata"))
    return now.weekday() < 5 and 9 * 60 <= now.hour * 60 + now.minute < 15 * 60 + 35


def validate(data_dir: Path, exp_dir: Path, dataset_id: str = DATASET, capital: float = 100_000, out: Path | None = None,
             studies: bool = True, max_sessions: int | None = None, yield_to_market: bool = False) -> dict:
    """yield_to_market: pause between replayed days while the live session is on (laptop runs)."""
    from nseql.data.store import Dataset
    from nseql.screening import contest as C
    ds = Dataset(Path(data_dir) / dataset_id)
    frames = _frames(ds)
    stocks = [s for s in frames if not s.upper().startswith("NIFTY")]
    sessions = sorted({str(d)[:10] for f in frames.values() for d in f.ts.dt.normalize().unique()})
    if max_sessions:
        sessions = sessions[-max_sessions:]
    p = protocol(exp_dir, dataset_id, sessions)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = Path(out) if out else history_root(exp_dir) / run_id
    if root.exists():
        shutil.rmtree(root)
    from nseql.screening import brain_comparison as BC
    from nseql.screening.scheduler import MinuteScheduler
    pair = C.open_pair(root / "replay", stocks, capital)
    live_from = pd.Timestamp(f"{sessions[min(WARMUP, len(sessions) - 1)]}T09:16:00").isoformat()
    variant_b = MinuteScheduler(root / "replay" / "variant_B", stocks, portfolio=BC.rules("B", capital), mode="replay")
    variant_b.verbose = False
    for s_ in (pair.incumbent, pair.challenger, variant_b):
        s_.live_from = live_from
    _status(exp_dir, state="replaying", run=run_id, sessions=len(sessions), done=0, phase="minute replay: incumbent, challenger, variant B")
    sub = {s: frames[s] for s in stocks}
    import time as _time

    def _feed(scheds, day):
        for t, bars in _day_stream(sub, day):
            for s_ in scheds:
                s_.ingest(bars)
                s_.run_cycle(pd.Timestamp(t) + pd.Timedelta(seconds=6))

    for k, day in enumerate(sessions):
        while yield_to_market and market_session_on():
            _status(exp_dir, phase=f"paused for the market session (resumes after 15:35); next {day}")
            _time.sleep(120)
        _feed([pair.incumbent, pair.challenger, variant_b], day)
        if k % 5 == 0:
            _status(exp_dir, done=k + 1, phase=f"minute replay {day}")
    # second pass: the matched control. A's own ranking, limited to B's average number of new trades a day and scaled to B's
    # average ticket, so "B is better" cannot mean "B trades less or smaller"
    counted0 = sessions[WARMUP:]
    b_per_day = BC.entries_per_day(variant_b, counted0)
    b_ratio = BC.size_ratio(variant_b, pair.incumbent, counted0)
    variant_b0 = MinuteScheduler(root / "replay" / "variant_B0", stocks, portfolio=BC.rules("B0", capital, max(1, round(b_per_day)), b_ratio), mode="replay")
    variant_b0.verbose = False
    variant_b0.live_from = live_from
    _status(exp_dir, phase="minute replay: matched control (B0)", done=0)
    for k, day in enumerate(sessions):
        while yield_to_market and market_session_on():
            _status(exp_dir, phase=f"paused for the market session (resumes after 15:35); next {day}")
            _time.sleep(120)
        _feed([variant_b0], day)
        if k % 5 == 0:
            _status(exp_dir, done=k + 1, phase=f"control replay {day}")
    for s_ in (pair.incumbent, pair.challenger, variant_b, variant_b0):
        s_.store.db.commit()
    counted = sessions[WARMUP:]
    bench = _benchmarks(frames, counted)
    inc_all = C.account_metrics(pair.incumbent, capital, counted)
    ch_all = C.account_metrics(pair.challenger, capital, counted)
    b_all = C.account_metrics(variant_b, capital, counted)
    b0_all = C.account_metrics(variant_b0, capital, counted)
    series = {"incumbent": [d["return"] for d in inc_all["daily"]], "challenger": [d["return"] for d in ch_all["daily"]],
              "B uncertainty-aware": [d["return"] for d in b_all["daily"]], "B0 matched control": [d["return"] for d in b0_all["daily"]], **bench}
    segs = {}
    for name in SEGMENTS:
        idx = [i for i, d in enumerate(counted) if segment_of(d, p) == name]
        if not idx:
            segs[name] = {"sessions": []}
            continue
        seg = {"sessions": [counted[i] for i in idx]}
        for who, r in series.items():
            seg[who] = _series_stats([r[i] for i in idx])
        seg["vs"] = {who: {b: _excess([series[who][i] for i in idx], [series[b][i] for i in idx]) for b in bench}
                     for who in ("incumbent", "challenger")}
        seg["challenger_vs_incumbent"] = _excess([series["challenger"][i] for i in idx], [series["incumbent"][i] for i in idx])
        seg["ablation"] = {name: _excess([series[a][i] for i in idx], [series[b][i] for i in idx]) for name, a, b in ABLATIONS}
        seg["plain"] = [plain(seg, "incumbent"), plain(seg, "challenger")]
        segs[name] = seg
    # trade-level checks on the holdout round trips
    inc_dir = root / "replay" / "incumbent"
    rt = I.round_trips(inc_dir)
    hold = set(segs["holdout"]["sessions"])
    rt_h = rt[rt.exit_ts.str[:10].isin(hold)] if len(rt) else rt
    trade = {"round_trips_all": int(len(rt)), "round_trips_holdout": int(len(rt_h)),
             "placebo_holdout": I.placebo(inc_dir, rt_h) if len(rt_h) >= 2 else {"p": None},
             "stress_holdout": I.stress(inc_dir, rt_h) if len(rt_h) else {"n": 0}}
    quality = {name: BC.trade_quality(root / "replay" / folder) for name, folder in
               (("incumbent", "incumbent"), ("challenger", "challenger"), ("B uncertainty-aware", "variant_B"), ("B0 matched control", "variant_B0"))}
    rep = {"version": VERSION, "run": run_id, "variants": {"B_entries_per_day": round(b_per_day, 2), "trade_quality": quality,
                                                            "B_size_ratio": None if b_ratio is None else round(b_ratio, 3),
                                                            "rules": {"B": BC.rules("B", capital).to_dict(), "B0": BC.rules("B0", capital, max(1, round(b_per_day)), b_ratio).to_dict()}}, "generated_at": datetime.now().isoformat(timespec="seconds"), "dataset": coverage(ds),
           "protocol": p, "capital": capital, "kind": "SYNTHETIC replay" if ds.synthetic else "historical replay of real NSE 1-minute data",
           "incumbent_rules": inc_all.get("version") or C.incumbent_label(), "challenger": C.CHALLENGER_VERSION,
           "segments": segs, "trades": trade,
           "curves": {"days": counted, **{k: [None if v is None or not math.isfinite(v) else round(float(v), 6) for v in r] for k, r in series.items()}},
           "limits": ["Candle-based fills at the first price after the decision (execution 2.1: the open two minutes after the decision candle) "
                      "with the modelled spread, impact and dated charges; real fills can be worse.",
                      "Current watchlist only: stocks that left the large caps are absent (survivorship).",
                      "No corporate-action adjustment is applied to the minute data.",
                      "Angel One coverage decides the period; gaps are reported, never filled."]}
    rep["variants"]["warnings"] = [(f"{k} made no round trips: comparisons with it measure staying in cash, not selection"
                                    if not (v.get("round_trips") or 0) else
                                    f"{k} made {v['round_trips']} round trips (fewer than {I.MIN_TRADES}): too few to judge its selection")
                                   for k, v in quality.items() if k.startswith("B") and (v.get("round_trips") or 0) < I.MIN_TRADES]
    if studies:
        _status(exp_dir, state="studies", phase="concept and feature studies on the same data")
        from nseql.screening import concept_study as CS, feature_study as FS
        from nseql.screening.price_action import candles
        cs = CS.run(None, exp_dir, frames=[(s, candles(frames[s], 5), frames[s], True) for s in stocks], source_label=f"{dataset_id} 5-minute")
        fs = FS.run(inc_dir, exp_dir, source_label=f"{dataset_id} replay")
        rep["concepts"] = {"run": cs["run"], "candidates": [r["concept"] for r in cs["results"] if r["verdict"] == "holdout-supported finding" and r["horizon"] == CS.HORIZONS[0]],
                           "validation_candidates": [r["concept"] for r in cs["results"] if r["verdict"] == "validation candidate" and r["horizon"] == CS.HORIZONS[0]],
                           "verdicts": {v: sum(1 for r in cs["results"] if r["verdict"] == v and r["horizon"] == CS.HORIZONS[0]) for v in {r["verdict"] for r in cs["results"]}}}
        rep["features"] = {"run": fs["run"], "verdicts": {r["feature"]: r["verdict"] for r in fs["results"]}}
        adds = BC.supported_additions({"results": cs["results"]}, {"results": fs["results"]})
        rep["variants"]["C_market_context"] = ({"status": "not run", "why": "no concept or feature reached holdout-supported status, so C would be "
                                                "identical to A and D to B"} if not adds else {"status": "pending", "supported_additions": adds,
                                                "why": "supported additions exist; wiring them as entry filters needs the operator's go-ahead"})
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.json").write_text(json.dumps(rep, indent=1, default=str))
    _record(exp_dir, rep)
    _status(exp_dir, state="done", run=run_id, phase="finished", finished_at=datetime.now().isoformat(timespec="seconds"))
    return rep


def _record(exp_dir: Path, rep: dict) -> None:
    from nseql.screening.brain import Knowledge
    from nseql.screening.evidence_guard import GuardedLedger
    L = GuardedLedger(I.Ledger(exp_dir), "research")
    K = Knowledge(L)
    h = rep["segments"]["holdout"]
    for who in ("incumbent", "challenger"):
        s = h.get(who, {})
        vs = h.get("vs", {}).get(who, {})
        body = {"run": rep["run"], "dataset": rep["dataset"]["dataset"], "holdout_sessions": s.get("sessions", 0),
                "total_return": s.get("total_return"), "vs": {k: v.get("reading") for k, v in vs.items()}, "version": VERSION}
        L.append("result", f"HIST-{who}", body)
        behind_cash = vs.get("cash", {}).get("reading") == "behind (interval below zero)"
        ahead_all = all(v.get("reading") == "ahead (interval above zero)" for v in vs.values()) and vs
        st = "contradicted finding" if behind_cash else "experimental finding" if ahead_all else "observed fact"
        K.write(f"HIST-{who}", st, f"On held-out historical sessions the {who} " + (
            "lost money against cash with an interval below zero" if behind_cash else
            "beat cash, NIFTY 50 and buy and hold" if ahead_all else "was not distinguishable from its benchmarks") + " after all costs.",
                "ledger", evidence=body, limitations="historical replay with modelled fills; current watchlist only (survivorship)",
                revalidate="every historical validation run", reason=f"run {rep['run']}")


def latest(exp_dir: Path) -> dict:
    root = history_root(exp_dir)
    reps = sorted(root.glob("*/report.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    out = {"status": status(exp_dir), "report": None}
    if reps:
        out["report"] = json.loads(reps[0].read_text())
    return out

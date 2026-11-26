"""Does any market-structure concept have value? (docs/MARKET_STRUCTURE.md). Research only: results go to the research store
and the evidence ledger; no trading module reads them (test-enforced).

For every concept in market_structure.CONCEPTS, on every stock and session available:
  1. Event at the candle where the concept first became observable (market_structure.detect, point in time).
  2. Outcome: direction x (close h candles later / open of the NEXT candle - 1) minus a round-trip cost; intraday outcomes
     never cross the session close (no overnight). Labelled hindsight: it exists only here.
  3. Compared with (a) zero after costs, (b) the simpler rule of the same kind (CONCEPTS[...][1], e.g. a BOS against a plain
     20-candle breakout), (c) random timing in the same stock and session (seeded placebo).
  4. Benjamini-Hochberg across all concepts; stability across a chronological split (first 60% of sessions = development,
     last 40% = holdout); consistency across stocks; by session window (NSE "kill-zone" adaptation), by the previous day's
     volatility regime (known at the open), and in combination with trend alignment and premium/discount.
  5. Verdict (never "validated": that needs forward evidence and the operator):
       insufficient sample         fewer than MIN_EVENTS events
       no value after costs        mean not above zero after correction, or the holdout disagrees
       no edge over the simpler rule
       no edge over random timing
       candidate                   all of the above passed; next: forward confirmatory sessions
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from nseql.screening import integrity as I
from nseql.screening import market_structure as M

VERSION = "cs-1.0"
COST = 0.0015
HORIZONS = (6, 12)              # candles (30 and 60 minutes on 5-minute candles; 6 and 12 days on daily)
MIN_EVENTS = 30
ALPHA = 0.05
PLACEBO_DRAWS = 300
SEED = 20261125
SCHEMA = """
CREATE TABLE IF NOT EXISTS concept_results (run TEXT, source TEXT, concept TEXT, family TEXT, horizon INTEGER, n INTEGER, mean_net REAL,
  p REAL, p_bh REAL, baseline TEXT, baseline_mean REAL, p_vs_baseline REAL, placebo_mean REAL, p_vs_placebo REAL, dev_mean REAL,
  holdout_mean REAL, stock_share_positive REAL, verdict TEXT, detail TEXT, PRIMARY KEY (run, source, concept, horizon));
CREATE TABLE IF NOT EXISTS concept_runs (run TEXT PRIMARY KEY, at TEXT, source TEXT, sessions INTEGER, symbols INTEGER, events INTEGER,
  params_hash TEXT, version TEXT, note TEXT);
"""


def _outcomes(c: pd.DataFrame, idx: np.ndarray, direction: np.ndarray, h: int, intraday: bool) -> np.ndarray:
    O, C = c.open.to_numpy(float), c.close.to_numpy(float)
    day = pd.to_datetime(c.end).dt.normalize().to_numpy()
    out = np.full(len(idx), np.nan)
    for k, (i, d) in enumerate(zip(idx, direction)):
        e, x = i + 1, i + h
        if x >= len(c) or (intraday and day[x] != day[i]):
            continue
        out[k] = d * (C[x] / O[e] - 1) - COST
    return out


def _sources(account: Path | None, data_dir: Path | None, tf: int = 5) -> list[tuple[str, list[tuple[str, pd.DataFrame, pd.DataFrame | None, bool]]]]:
    """(source label, [(symbol, candles, m1 or None, intraday)]) from the account's stored 1-minute bars and, when present,
    the two-year daily history."""
    from nseql.screening.price_action import candles
    out = []
    if account is not None and (Path(account) / "screening.sqlite").exists():
        con = sqlite3.connect(f"file:{Path(account) / 'screening.sqlite'}?mode=ro", uri=True)
        try:
            syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM events WHERE late=0")]
            frames = []
            for s in syms:
                m1 = pd.read_sql_query("SELECT ts, open, high, low, close, volume FROM events WHERE symbol=? AND late=0 ORDER BY ts", con, params=(s,))
                m1["ts"] = pd.to_datetime(m1.ts)
                frames.append((s, candles(m1, tf), m1, True))
        finally:
            con.close()
        out.append((f"{Path(account).name} {tf}-minute", frames))
    if data_dir is not None and (Path(data_dir) / "angel_daily").exists():
        from nseql.data.store import Dataset
        ds = Dataset(Path(data_dir) / "angel_daily")
        frames = []
        for s in ds.symbols("1d"):
            b = ds.bars(s, "1d").copy()
            b["end"] = pd.to_datetime(b.ts)
            frames.append((s, b[["open", "high", "low", "close", "volume", "end"]].reset_index(drop=True), None, False))
        out.append(("angel_daily 1-day", frames))
    return out


def run(account: Path | None, exp_dir: Path, data_dir: Path | None = None, tf: int = 5, record: bool = True, frames: list | None = None,
        source_label: str = "supplied frames") -> dict:
    """`frames` (symbol, candles, m1 or None, intraday) replaces the stored sources (used by tests and replays)."""
    rng = np.random.default_rng(SEED)
    exp_dir = Path(exp_dir)
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    results, runs = [], []
    for source, frames in ([(source_label, frames)] if frames is not None else _sources(account, data_dir, tf)):
        ev_rows, base_rows, plac_rows = [], [], []
        sessions = set()
        nifty = next((f for f in frames if f[0] == "NIFTY50"), None)
        for sym, c, m1, intraday in frames:
            if len(c) < 30 or sym in ("NIFTY50", "NIFTYBANK", "INDIAVIX"):
                continue
            daily = M.daily_from_m1(m1) if m1 is not None else None
            d = M.detect(c, m1=m1, daily=daily, intraday=intraday)
            evs = d["events"] + (M.smt(c, nifty[1], "NIFTY 50") if nifty is not None and sym != "NIFTY50" else [])
            days = pd.to_datetime(c.end).dt.normalize()
            sessions |= set(days.astype(str))
            # previous-day volatility regime (known at the open)
            dr = c.assign(d=days).groupby("d").agg(h=("high", "max"), l=("low", "min"), cl=("close", "last"))
            prev_vol = ((dr.h - dr.l) / dr.cl).shift(1)
            for e in evs:
                if e["type"] not in M.CONCEPTS:
                    continue
                ev_rows.append({"symbol": sym, "type": e["type"], "i": e["i"], "dir": e["direction"], "day": str(days.iloc[e["i"]])[:10],
                                "session": e.get("session"), "trend": e.get("trend"), "pd": e.get("premium_discount"),
                                "prev_vol": prev_vol.get(days.iloc[e["i"]]), "intraday": intraday})
            for b in d["baselines"]:
                base_rows.append({"symbol": sym, "type": b["type"], "i": b["i"], "dir": b["direction"], "day": str(days.iloc[b["i"]])[:10],
                                  "intraday": intraday})
            for h in HORIZONS:
                for rows in (ev_rows, base_rows):
                    sel = [r for r in rows if r["symbol"] == sym and f"h{h}" not in r]
                    if sel:
                        vals = _outcomes(c, np.array([r["i"] for r in sel]), np.array([r["dir"] for r in sel]), h, intraday)
                        for r, v in zip(sel, vals):
                            r[f"h{h}"] = v
                # placebo: random candle in the same session, per event (seeded)
                day_idx = {d_: np.flatnonzero(days.to_numpy() == np.datetime64(d_)) for d_ in {r["day"] for r in ev_rows if r["symbol"] == sym}}
                for r in [r for r in ev_rows if r["symbol"] == sym]:
                    pool = day_idx.get(r["day"])
                    if pool is None or not len(pool):
                        continue
                    draws = rng.choice(pool, size=8)
                    v = _outcomes(c, draws, np.full(8, r["dir"]), h, intraday)
                    plac_rows.append({"type": r["type"], "h": h, "vals": v})
        E, Bdf = pd.DataFrame(ev_rows), pd.DataFrame(base_rows)
        if E.empty:
            runs.append({"source": source, "sessions": len(sessions), "events": 0})
            continue
        all_days = sorted(sessions)
        cut = all_days[int(len(all_days) * 0.6)] if len(all_days) >= 5 else None
        for h in HORIZONS:
            col = f"h{h}"
            rows_h = []
            for concept, g in E.groupby("type"):
                x = g[col].dropna()
                fam, base_name = M.CONCEPTS[concept]
                t = I.t_test(x)
                b = Bdf[(Bdf.type == base_name)][col].dropna() if base_name and len(Bdf) else pd.Series(dtype=float)
                pv = [p for p in plac_rows if p["type"] == concept and p["h"] == h]
                plac = np.concatenate([p["vals"] for p in pv]) if pv else np.array([])
                plac = plac[np.isfinite(plac)]
                from scipy import stats as _s
                p_b = float(_s.ttest_ind(x, b, equal_var=False, alternative="greater").pvalue) if len(x) >= 2 and len(b) >= 2 else None
                # placebo p: share of resampled placebo means (same size as x) at least the concept's mean
                p_pl = None
                if len(plac) >= 10 and len(x) >= 2:
                    sims = rng.choice(plac, size=(PLACEBO_DRAWS, len(x)), replace=True).mean(axis=1)
                    p_pl = float((np.sum(sims >= x.mean()) + 1) / (PLACEBO_DRAWS + 1))
                dev = g[g.day < cut][col].dropna() if cut else x
                hold = g[g.day >= cut][col].dropna() if cut else pd.Series(dtype=float)
                st = g.groupby("symbol")[col].agg(["mean", "count"])
                st = st[st["count"] >= 3]
                share = float((st["mean"] > 0).mean()) if len(st) else None
                aligned = g[((g.trend == "up") & (g.dir > 0)) | ((g.trend == "down") & (g.dir < 0))][col].dropna()
                pdz = g[((g.pd == "discount") & (g.dir > 0)) | ((g.pd == "premium") & (g.dir < 0))][col].dropna()
                vol_t = g.dropna(subset=["prev_vol"])
                if len(vol_t) >= 9:
                    q = vol_t.prev_vol.quantile([1 / 3, 2 / 3]).to_numpy()
                    reg = {name: float(vol_t[m][col].mean()) if m.sum() else None for name, m in
                           (("quiet days", vol_t.prev_vol <= q[0]), ("normal days", (vol_t.prev_vol > q[0]) & (vol_t.prev_vol <= q[1])),
                            ("volatile days", vol_t.prev_vol > q[1]))}
                else:
                    reg = {}
                rows_h.append({"source": source, "concept": concept, "family": fam, "horizon": h, "n": int(len(x)),
                               "mean_net": None if x.empty else float(x.mean()), "p": t["p"], "baseline": base_name,
                               "baseline_mean": None if b.empty else float(b.mean()), "p_vs_baseline": p_b,
                               "placebo_mean": None if not len(plac) else float(plac.mean()), "p_vs_placebo": p_pl,
                               "dev_mean": None if dev.empty else float(dev.mean()), "holdout_mean": None if hold.empty else float(hold.mean()),
                               "stock_share_positive": share,
                               "detail": {"sessions": {k: float(v) for k, v in g.groupby("session")[col].mean().dropna().items()},
                                          "regimes": reg, "trend_aligned": {"n": int(len(aligned)), "mean": None if aligned.empty else float(aligned.mean())},
                                          "premium_discount_aligned": {"n": int(len(pdz)), "mean": None if pdz.empty else float(pdz.mean())},
                                          "holdout_n": int(len(hold))}})
            keep = I.bh([r["p"] if r["n"] >= MIN_EVENTS else None for r in rows_h], ALPHA)
            keep_b = I.bh([r["p_vs_baseline"] if r["n"] >= MIN_EVENTS else None for r in rows_h], ALPHA)
            ps = sorted([r["p"] for r in rows_h if r["p"] is not None and r["n"] >= MIN_EVENTS])
            for r, k, kb in zip(rows_h, keep, keep_b):
                r["p_bh_pass"] = bool(k)
                if r["n"] < MIN_EVENTS:
                    v = "insufficient sample"
                elif not k or (r["holdout_mean"] is not None and r["holdout_mean"] <= 0):
                    v = "no value after costs"
                elif r["baseline"] and not kb:
                    v = "no edge over the simpler rule"
                elif r["p_vs_placebo"] is not None and r["p_vs_placebo"] >= ALPHA:
                    v = "no edge over random timing"
                else:
                    v = "candidate"
                r["verdict"] = v
            results += rows_h
        runs.append({"source": source, "sessions": len(sessions), "symbols": len(frames), "events": int(len(E)),
                     "development_until": cut, "baseline_events": int(len(Bdf))})
    out = {"run": run_id, "version": VERSION, "params_hash": M.params_hash(), "generated_at": datetime.now().isoformat(timespec="seconds"),
           "runs": runs, "results": results, "cost_round_trip": COST, "horizons": HORIZONS, "min_events": MIN_EVENTS,
           "unavailable": M.UNAVAILABLE,
           "note": ("Historical, exploratory-then-holdout evidence on existing data with thresholds fixed before the run. A 'candidate' "
                    "still needs forward confirmatory sessions and the operator's approval before any rule may use it.")}
    if record:
        _record(out, exp_dir)
    return out


def _record(out: dict, exp_dir: Path) -> None:
    from nseql.screening.opportunities import research_db
    db = sqlite3.connect(str(research_db(exp_dir)))
    try:
        db.executescript(SCHEMA)
        for r in out["runs"]:
            db.execute("INSERT OR REPLACE INTO concept_runs VALUES (?,?,?,?,?,?,?,?,?)",
                       (out["run"], out["generated_at"], r["source"], r.get("sessions"), r.get("symbols"), r.get("events"), out["params_hash"],
                        out["version"], out["note"]))
        db.executemany("INSERT OR REPLACE INTO concept_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       [(out["run"], r["source"], r["concept"], r["family"], r["horizon"], r["n"], r["mean_net"], r["p"], 1.0 if not r["p_bh_pass"] else 0.0,
                         r["baseline"], r["baseline_mean"], r["p_vs_baseline"], r["placebo_mean"], r["p_vs_placebo"], r["dev_mean"],
                         r["holdout_mean"], r["stock_share_positive"], r["verdict"], json.dumps(r["detail"])) for r in out["results"]])
        db.commit()
    finally:
        db.close()
    # evidence ledger: definitions registered once (hash), each verdict recorded when it changes; knowledge follows
    L = I.Ledger(exp_dir)
    if not any(e["body"].get("params_hash") == out["params_hash"] for e in L.entries("concept")):
        L.append("concept", "MS-definitions", {"params_hash": out["params_hash"], "version": M.VERSION, "params": M.PARAMS,
                                               "concepts": {k: {"family": v[0], "baseline": v[1]} for k, v in M.CONCEPTS.items()}})
    last = {e["subject"]: e["body"] for e in L.entries("result") if e["subject"].startswith("MS-")}
    from nseql.screening.brain import Knowledge
    K = Knowledge(L)
    for r in out["results"]:
        if r["horizon"] != HORIZONS[0]:
            continue
        sub = f"MS-{r['concept']} ({r['source']})"
        body = {"verdict": r["verdict"], "n": r["n"], "mean_net": r["mean_net"], "p": r["p"], "status": r["verdict"], "version": VERSION}
        if last.get(sub, {}).get("verdict") != r["verdict"] or last.get(sub, {}).get("n") != r["n"]:
            L.append("result", sub, body)
        st = {"candidate": "experimental finding", "insufficient sample": "unverified hypothesis"}.get(r["verdict"], "contradicted finding")
        prev = K.current().get(sub)
        if st == "contradicted finding" and prev and prev["status"] == "experimental finding":
            st = "invalidated finding"
        K.write(sub, st, f"{r['concept'].replace('_', ' ')} ({r['family']}) has value after costs beyond {r['baseline'] or 'random timing'}",
                "concept_study", evidence={"n": r["n"], "mean_net": r["mean_net"], "p": r["p"], "vs_simpler": r["p_vs_baseline"],
                                           "vs_random": r["p_vs_placebo"], "holdout_mean": r["holdout_mean"]},
                contradicting=[f"verdict: {r['verdict']}"] if r["verdict"] != "candidate" else [],
                limitations="candle-based pattern, inferred; no order-book data; historical evidence until forward sessions confirm",
                revalidate="every concept study run", reason=r["verdict"])


def latest(exp_dir: Path) -> dict:
    from nseql.screening.opportunities import research_db
    f = research_db(exp_dir)
    if not f.exists():
        return {"run": None, "results": []}
    db = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        try:
            r = db.execute("SELECT run FROM concept_runs ORDER BY at DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            return {"run": None, "results": []}
        if not r:
            return {"run": None, "results": []}
        runs = [dict(x) for x in db.execute("SELECT * FROM concept_runs WHERE run=?", (r["run"],))]
        res = []
        for x in db.execute("SELECT * FROM concept_results WHERE run=? ORDER BY source, concept, horizon", (r["run"],)):
            d = dict(x)
            d["detail"] = json.loads(d["detail"] or "{}")
            d["p_bh_pass"] = d.pop("p_bh") == 0.0
            res.append(d)
        return {"run": r["run"], "runs": runs, "results": res, "unavailable": M.UNAVAILABLE}
    finally:
        db.close()

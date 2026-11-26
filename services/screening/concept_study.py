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
  5. Status (validation_protocol.py; never "validated"): selection uses development sessions only, with session-clustered
     tests corrected across all concepts and horizons; only development survivors are evaluated once on the sealed holdout.
     insufficient evidence / rejected / exploratory finding / validation candidate / holdout-supported finding.
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

VERSION = "cs-2.0"
# evidence status -> knowledge status (validation_protocol.STATUSES; never "validated" from an event study)
STATUS_TO_KNOWLEDGE = {"holdout-supported finding": "experimental finding", "validation candidate": "unverified hypothesis",
                       "exploratory finding": "unverified hypothesis", "insufficient evidence": "unverified hypothesis",
                       "rejected": "contradicted finding"}
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
                    vv = v[np.isfinite(v)]
                    if len(vv):
                        plac_rows.append({"type": r["type"], "h": h, "day": r["day"], "v": float(vv.mean())})
        E, Bdf = pd.DataFrame(ev_rows), pd.DataFrame(base_rows)
        if E.empty:
            runs.append({"source": source, "sessions": len(sessions), "events": 0})
            continue
        all_days = sorted(sessions)
        # ---- staged validation (validation_protocol.py): development selects, the sealed holdout is read once per
        # definition and only for development survivors; session-clustered inference; correction across everything
        from nseql.screening import validation_protocol as V
        from nseql.screening.evidence_guard import GuardedLedger
        EV = GuardedLedger(I.Ledger(exp_dir), "evaluation")
        data_fp = V.fingerprint({"source": source, "sessions": all_days, "symbols": sorted(f[0] for f in frames)})
        defs = V.fingerprint({"params": M.params_hash(), "study": VERSION, "horizons": HORIZONS, "cost": COST})
        key = f"concepts {source}"
        part = V.register_partition(EV, key, all_days, data_fp, defs)
        dev_days, emb_days, hold_days = V.split(all_days, part)
        P = pd.DataFrame(plac_rows) if plac_rows else pd.DataFrame(columns=["type", "h", "day", "v"])
        tests = []
        for h in HORIZONS:
            col = f"h{h}"
            for concept, g in E.groupby("type"):
                fam, base_name = M.CONCEPTS[concept]
                gd = g[g.day.isin(dev_days)]
                xd = gd[[col, "day"]].dropna().rename(columns={col: "v", "day": "c"})
                dev = V.cluster_test(xd.v, xd.c)
                bd = Bdf[(Bdf.type == base_name) & Bdf.day.isin(dev_days)][[col, "day"]].dropna().rename(columns={col: "v", "day": "c"}) \
                    if base_name and len(Bdf) else pd.DataFrame(columns=["v", "c"])
                pdv = P[(P.type == concept) & (P.h == h) & P.day.isin(dev_days)][["v", "day"]].rename(columns={"day": "c"})
                vs_b = V.paired_cluster_test(xd, bd) if len(bd) and len(xd) else None
                vs_p = V.paired_cluster_test(xd, pdv) if len(pdv) and len(xd) else None
                st = gd.groupby("symbol")[col].agg(["mean", "count"])
                st = st[st["count"] >= 3]
                aligned = gd[((gd.trend == "up") & (gd.dir > 0)) | ((gd.trend == "down") & (gd.dir < 0))][col].dropna()
                pdz = gd[((gd.pd == "discount") & (gd.dir > 0)) | ((gd.pd == "premium") & (gd.dir < 0))][col].dropna()
                vol_t = gd.dropna(subset=["prev_vol"])
                reg = {}
                if len(vol_t) >= 9:
                    q = vol_t.prev_vol.quantile([1 / 3, 2 / 3]).to_numpy()
                    reg = {name: float(vol_t[m][col].mean()) if m.sum() else None for name, m in
                           (("quiet days", vol_t.prev_vol <= q[0]), ("normal days", (vol_t.prev_vol > q[0]) & (vol_t.prev_vol <= q[1])),
                            ("volatile days", vol_t.prev_vol > q[1]))}
                tests.append({"source": source, "concept": concept, "family": fam, "horizon": h, "baseline": base_name, "g": g, "col": col,
                              "dev": dev, "vs_b": vs_b, "vs_p": vs_p, "baseline_mean": None if bd.empty else float(bd.v.mean()),
                              "placebo_mean": None if pdv.empty else float(pdv.v.mean()),
                              "stock_share_positive": float((st["mean"] > 0).mean()) if len(st) else None,
                              "detail": {"sessions": {k: float(v) for k, v in gd.groupby("session")[col].mean().dropna().items()}, "regimes": reg,
                                         "trend_aligned": {"n": int(len(aligned)), "mean": None if aligned.empty else float(aligned.mean())},
                                         "premium_discount_aligned": {"n": int(len(pdz)), "mean": None if pdz.empty else float(pdz.mean())},
                                         "inference": "session-clustered (session means, then across sessions)"}})
        # one correction across concepts x horizons, for each comparison
        k0 = V.bh([t["dev"]["p"] for t in tests])
        kb = V.bh([t["vs_b"]["p"] if t["vs_b"] else None for t in tests])
        kp = V.bh([t["vs_p"]["p"] if t["vs_p"] else None for t in tests])
        for t, a0, ab, ap in zip(tests, k0, kb, kp):
            t["fail"] = [] if a0 and (t["dev"]["mean"] or 0) > 0 else ["no value after costs in development"]
            if t["vs_b"] is not None and not ab:
                t["fail"].append(f"no edge over the simpler rule ({t['baseline']}) in development")
            if t["vs_p"] is not None and not ap:
                t["fail"].append("no edge over random timing in development")
            t["dev_pass"] = not t["fail"]
            t["p_bh_pass"] = bool(a0)
        cands = [t for t in tests if t["dev_pass"] and t["dev"]["clusters"] >= V.MIN_DEV_SESSIONS and t["dev"]["n"] >= V.MIN_EVENTS]
        access = V.holdout_access(EV, key, defs, data_fp) if cands and hold_days else None
        for t in cands:
            gh = t["g"][t["g"].day.isin(hold_days)][[t["col"], "day"]].dropna().rename(columns={t["col"]: "v", "day": "c"})
            t["hold"] = V.cluster_test(gh.v, gh.c)
            bh_ = Bdf[(Bdf.type == t["baseline"]) & Bdf.day.isin(hold_days)][[t["col"], "day"]].dropna().rename(columns={t["col"]: "v", "day": "c"}) \
                if t["baseline"] and len(Bdf) else pd.DataFrame(columns=["v", "c"])
            t["hold_vs_b"] = V.paired_cluster_test(gh, bh_) if len(bh_) and len(gh) else None
        kh = V.bh([t["hold"]["p"] for t in cands])
        for t, ok in zip(cands, kh):
            t["hold_pass"] = bool(ok and (t["hold"]["mean"] or 0) > 0 and (t["hold_vs_b"] is None or (t["hold_vs_b"]["p"] or 1) < V.ALPHA))
        for t in tests:
            status, reason = V.decide(t["dev"], t["dev_pass"], t.get("hold"), t.get("hold_pass", False), bool(access and access["reused"]))
            if status == "rejected" and not t["dev_pass"]:
                reason = "; ".join(t["fail"])
                if not t["p_bh_pass"] and (t["dev"]["p"] or 1) < V.ALPHA and (t["dev"]["mean"] or 0) > 0:
                    status, reason = "exploratory finding", "raw development signal that does not survive correction: " + reason
            h_ = t.get("hold") or {}
            results.append({"source": t["source"], "concept": t["concept"], "family": t["family"], "horizon": t["horizon"],
                            "n": int(t["dev"]["n"] + h_.get("n", 0)), "dev_sessions": t["dev"]["clusters"], "holdout_sessions": h_.get("clusters", 0),
                            "mean_net": t["dev"]["mean"], "p": t["dev"]["p"], "ci": [t["dev"]["lo"], t["dev"]["hi"]], "baseline": t["baseline"],
                            "baseline_mean": t["baseline_mean"], "p_vs_baseline": (t["vs_b"] or {}).get("p"), "placebo_mean": t["placebo_mean"],
                            "p_vs_placebo": (t["vs_p"] or {}).get("p"), "dev_mean": t["dev"]["mean"], "holdout_mean": h_.get("mean"),
                            "holdout_p": h_.get("p"), "stock_share_positive": t["stock_share_positive"], "p_bh_pass": t["p_bh_pass"],
                            "verdict": status, "reason": reason, "detail": t["detail"]})
        cut = part.get("holdout_from")
        runs.append({"source": source, "sessions": len(sessions), "symbols": len(frames), "events": int(len(E)),
                     "development_until": cut, "baseline_events": int(len(Bdf))})
    out = {"run": run_id, "version": VERSION, "params_hash": M.params_hash(), "generated_at": datetime.now().isoformat(timespec="seconds"),
           "runs": runs, "results": results, "cost_round_trip": COST, "horizons": HORIZONS, "min_events": MIN_EVENTS,
           "unavailable": M.UNAVAILABLE,
           "statuses": list(__import__("nseql.screening.validation_protocol", fromlist=["STATUSES"]).STATUSES),
           "note": ("Development selects; the sealed holdout is read once per definition, only for development survivors; "
                    "session-clustered inference. Even a holdout-supported finding needs forward shadow evidence and the operator's "
                    "approval before any rule may use it.")}
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
    from nseql.screening.evidence_guard import GuardedLedger
    L = GuardedLedger(I.Ledger(exp_dir), "research")
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
        st = STATUS_TO_KNOWLEDGE.get(r["verdict"], "unverified hypothesis")
        prev = K.current().get(sub)
        if st == "contradicted finding" and prev and prev["status"] == "experimental finding":
            st = "invalidated finding"
        K.write(sub, st, f"{r['concept'].replace('_', ' ')} ({r['family']}) has value after costs beyond {r['baseline'] or 'random timing'}",
                "concept_study", evidence={"n": r["n"], "mean_net": r["mean_net"], "p": r["p"], "vs_simpler": r["p_vs_baseline"],
                                           "vs_random": r["p_vs_placebo"], "holdout_mean": r["holdout_mean"]},
                contradicting=[f"verdict: {r['verdict']} ({r.get('reason', '')})"] if r["verdict"] in ("rejected", "insufficient evidence") else [],
                limitations="candle-based pattern, inferred; no order-book data; historical evidence until forward sessions confirm",
                revalidate="every concept study run", reason=r["verdict"])


def latest(exp_dir: Path, as_of=None) -> dict:
    """The newest concept study; with `as_of`, the newest one COMPLETED by then (playback)."""
    from nseql.screening.opportunities import research_db
    f = research_db(exp_dir)
    if not f.exists():
        return {"run": None, "results": []}
    db = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        try:
            r = (db.execute("SELECT run FROM concept_runs WHERE at <= ? ORDER BY at DESC LIMIT 1", (pd.Timestamp(as_of).isoformat(),)).fetchone()
                 if as_of is not None else db.execute("SELECT run FROM concept_runs ORDER BY at DESC LIMIT 1").fetchone())
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


def consistency(exp_dir: Path) -> dict:
    """The research store is a convenience copy; the evidence ledger is authoritative. Every verdict in the latest run must
    match the ledger's latest result for that concept (first horizon). A mismatch means the research record was edited."""
    lat = latest(exp_dir)
    if not lat.get("run"):
        return {"checked": 0, "mismatches": []}
    L = I.Ledger(exp_dir)
    led = {}
    for e in L.entries("result"):
        if e["subject"].startswith("MS-"):
            led[e["subject"]] = e["body"]
    bad, n = [], 0
    for r in lat["results"]:
        if r["horizon"] != HORIZONS[0]:
            continue
        sub = f"MS-{r['concept']} ({r['source']})"
        if sub in led:
            n += 1
            if led[sub].get("verdict") != r["verdict"]:
                bad.append({"concept": sub, "research_store": r["verdict"], "ledger": led[sub].get("verdict")})
    return {"checked": n, "mismatches": bad}

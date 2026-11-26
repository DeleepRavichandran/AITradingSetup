"""Do heatmap features predict anything? (docs/HEATMAPS.md). Research only; results go to the research store, the evidence
ledger and the knowledge store. No trading module reads them (test-enforced).

Panel: every watchlist stock at fixed decision times (every 30 minutes, 10:15-14:45, non-overlapping with the 30-minute
outcome, so observations do not share outcomes). Each feature is computed only from data at or before that time:
  mom_60        the stock's own last-hour return (the simple control every other feature must beat)
  rs_60         last-hour return minus the watchlist average (relative strength)
  rs_day        return since the previous close minus the watchlist average
  sector_rs_60  the stock's sector last-hour return minus the watchlist average (sector rotation)
  rel_volume    last 30 minutes' volume over the same half-hour's average on earlier sessions
  vol_change    last hour's realised volatility over the previous session's
  liq_above     distance to the nearest chart-inferred buy-side level above (market_structure levels active at that time)
  liq_below     distance to the nearest chart-inferred sell-side level below
Outcome: return from the next minute's open to 30 minutes later (same session).

Tests, per feature: cross-sectional rank correlation (information coefficient) at each time, its mean across times with a
t-test; Benjamini-Hochberg across features; the same on the feature residualised on mom_60 (incremental value); the
later 40% of sessions as a holdout; and whether buying the top third beats doing nothing after a round-trip cost.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from nseql.screening import heatmaps as HM
from nseql.screening import integrity as I

VERSION = "fs-2.0"
FEATURES = ("mom_60", "rs_60", "rs_day", "sector_rs_60", "rel_volume", "vol_change", "liq_above", "liq_below")
TIMES = [f"{h:02d}:{m:02d}" for h, m in [(10, 15), (10, 45), (11, 15), (11, 45), (12, 15), (12, 45), (13, 15), (13, 45), (14, 15), (14, 45)]]
HORIZON_MIN = 30
COST = 0.0015
MIN_TIMES = 30
MIN_STOCKS = 5
ALPHA = 0.05


def panel(account: Path, with_liquidity: bool = True) -> pd.DataFrame:
    """Feature rows (time, symbol, features..., fwd) built point in time from the account's stored 1-minute bars."""
    con = sqlite3.connect(f"file:{Path(account) / 'screening.sqlite'}?mode=ro", uri=True)
    try:
        b = pd.read_sql_query("SELECT symbol, ts, open, high, low, close, volume FROM events WHERE late=0 ORDER BY ts", con)
    finally:
        con.close()
    b = b[~b.symbol.isin(HM.INDEX)]
    if b.empty:
        return pd.DataFrame()
    b["ts"] = pd.to_datetime(b.ts)
    cl, op, vo = (b.pivot_table(index="ts", columns="symbol", values=k).sort_index() for k in ("close", "open", "volume"))
    syms = list(cl.columns)
    days = sorted(cl.index.normalize().unique())
    levels = {}
    if with_liquidity:
        from nseql.screening import market_structure as M
        from nseql.screening.price_action import candles
        for s in syms:
            m1 = b[b.symbol == s][["ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
            d = M.detect(candles(m1, 5), m1=m1, daily=M.daily_from_m1(m1))
            levels[s] = [(pd.Timestamp(x["observable_at"]), pd.Timestamp(x["taken_at"]) if x["taken_at"] else None, x["price"], x["side"]) for x in d["levels"]]
    # half-hour volume per day for relative volume (prior sessions only)
    bucket = ((cl.index.hour * 60 + cl.index.minute - 556) // 30)
    vb = vo.groupby([cl.index.normalize(), bucket]).sum()
    rows = []
    for di, day in enumerate(days):
        dcl = cl[cl.index.normalize() == day]
        if dcl.empty:
            continue
        prev = cl[cl.index.normalize() < day]
        prev_close = prev.ffill().iloc[-1] if len(prev) else None
        prev_day = prev[prev.index.normalize() == prev.index.normalize().max()] if len(prev) else None
        prev_vol = np.log(prev_day).diff().std() if prev_day is not None and len(prev_day) > 30 else None
        for hm in TIMES:
            t = day + pd.Timedelta(hours=int(hm[:2]), minutes=int(hm[3:]))
            past = dcl[dcl.index <= t]
            if len(past) < 60:
                continue
            last = past.ffill().iloc[-1]
            ago = past[past.index <= t - pd.Timedelta(minutes=60)]
            if ago.empty:
                continue
            hour_ago = ago.ffill().iloc[-1]
            mom = last / hour_ago - 1
            avg = mom.mean()
            rs_day = (last / prev_close - 1) - (last / prev_close - 1).mean() if prev_close is not None else pd.Series(np.nan, index=syms)
            sec = pd.Series({s: HM.sector(s) for s in syms})
            sec_mom = mom.groupby(sec).transform("mean")
            k = (t.hour * 60 + t.minute - 556) // 30
            k30 = vo[(vo.index > t - pd.Timedelta(minutes=30)) & (vo.index <= t)].sum()
            hist = [vb.loc[(d, k)] for d in days[:di] if (d, k) in vb.index]
            relv = k30 / pd.concat(hist, axis=1).mean(axis=1) if hist else pd.Series(np.nan, index=syms)
            hv = np.log(past[past.index > t - pd.Timedelta(minutes=60)]).diff().std()
            volc = hv / prev_vol if prev_vol is not None else pd.Series(np.nan, index=syms)
            # outcome: next minute's open to 30 minutes later
            nxt = op[(op.index > t)].iloc[:1]
            end = cl[(cl.index > t) & (cl.index <= t + pd.Timedelta(minutes=HORIZON_MIN))]
            if nxt.empty or end.empty or end.index[-1].normalize() != day:
                continue
            fwd = end.ffill().iloc[-1] / nxt.iloc[0] - 1
            for s in syms:
                la = lb = np.nan
                if with_liquidity and s in levels and pd.notna(last[s]):
                    act = [(p, side) for (ob, tk, p, side) in levels[s] if ob <= t and (tk is None or tk > t)]
                    up = [p / last[s] - 1 for p, side in act if side in ("buy-side", "both") and p > last[s]]
                    dn = [1 - p / last[s] for p, side in act if side in ("sell-side", "both") and p < last[s]]
                    la, lb = (min(up) if up else np.nan), (min(dn) if dn else np.nan)
                rows.append({"time": t, "day": str(day)[:10], "symbol": s, "mom_60": mom[s], "rs_60": mom[s] - avg, "rs_day": rs_day.get(s, np.nan),
                             "sector_rs_60": (sec_mom[s] - avg) if sec.nunique() > 1 else np.nan, "rel_volume": relv.get(s, np.nan),
                             "vol_change": volc.get(s, np.nan), "liq_above": la, "liq_below": lb, "fwd": fwd.get(s, np.nan)})
    return pd.DataFrame(rows)


def _ic(df: pd.DataFrame, col: str) -> pd.Series:
    out = {}
    for t, g in df.groupby("time"):
        g = g[[col, "fwd"]].dropna()
        if len(g) >= MIN_STOCKS and g[col].nunique() > 1:
            out[t] = g[col].rank().corr(g.fwd.rank())
    return pd.Series(out, dtype=float)


def _resid(df: pd.DataFrame, col: str) -> pd.Series:
    """Feature minus its cross-sectional linear fit on own momentum (mom_60) at each time."""
    out = pd.Series(np.nan, index=df.index)
    for _, g in df.groupby("time"):
        g = g[[col, "mom_60"]].dropna()
        if len(g) >= MIN_STOCKS and g.mom_60.std() > 0:
            beta = np.polyfit(g.mom_60, g[col], 1)
            out.loc[g.index] = g[col] - np.polyval(beta, g.mom_60)
    return out


def _top_net(df: pd.DataFrame, f: str) -> pd.Series:
    """Per decision time: mean outcome of the top third by the feature, minus the round-trip cost (long-only use)."""
    out = {}
    for t, g in df.groupby("time"):
        g = g[[f, "fwd"]].dropna()
        if len(g) >= MIN_STOCKS and g[f].nunique() > 1:
            out[t] = float(g[g[f] >= g[f].quantile(2 / 3)].fwd.mean()) - COST
    return pd.Series(out, dtype=float)


def _by_day(s: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    return s.to_numpy(), np.array([str(t)[:10] for t in s.index])


def study(df: pd.DataFrame, part: dict | None = None, access=None) -> list[dict]:
    """Staged validation (validation_protocol.py): development selects (day-clustered information coefficients, correction
    across features, incremental value over own momentum, after costs); the sealed holdout is read only for development
    survivors, through `access()` (which logs the access and says whether the holdout was already used by other
    definitions). Missing or small holdout evidence never qualifies."""
    from nseql.screening import validation_protocol as V
    if df.empty:
        return []
    days = sorted(df.day.unique())
    if part is None:
        k = max(1, int(len(days) * V.DEV_SHARE))
        part = {"dev_until": days[k - 1], "embargo": days[k:k + V.EMBARGO_SESSIONS],
                "holdout_from": days[k + V.EMBARGO_SESSIONS] if len(days) > k + V.EMBARGO_SESSIONS else None}
    dev_days, _, hold_days = V.split(days, part)
    dev_df, hold_df = df[df.day.isin(dev_days)], df[df.day.isin(hold_days)]
    rows = []
    for f in FEATURES:
        ic = _ic(dev_df, f)
        dev = V.cluster_test(*_by_day(ic))
        inc = None
        if f != "mom_60":
            ic_r = _ic(dev_df.assign(_r=_resid(dev_df, f)), "_r")
            inc = V.cluster_test(*_by_day(ic_r))
        top = V.cluster_test(*_by_day(_top_net(dev_df, f)))
        rows.append({"feature": f, "dev": dev, "inc": inc, "top": top})
    k0 = V.bh([r["dev"]["p"] for r in rows])
    ki = V.bh([r["inc"]["p"] if r["inc"] else None for r in rows])
    for r, a0, ai in zip(rows, k0, ki):
        r["fail"] = [] if a0 and (r["dev"]["mean"] or 0) > 0 else ["no predictive value in development"]
        if r["inc"] is not None and not ai:
            r["fail"].append("no incremental value over own momentum in development")
        if (r["top"]["mean"] or 0) <= 0:
            r["fail"].append("buying the top third does not beat doing nothing after costs in development")
        r["dev_pass"] = not r["fail"]
    cands = [r for r in rows if r["dev_pass"] and r["dev"]["clusters"] >= V.MIN_DEV_SESSIONS and r["dev"]["n"] >= V.MIN_EVENTS]
    acc = access() if (cands and hold_days and access) else ({"reused": False} if cands and hold_days else None)
    for r in cands:
        r["hold"] = V.cluster_test(*_by_day(_ic(hold_df, r["feature"])))
        r["hold_top"] = V.cluster_test(*_by_day(_top_net(hold_df, r["feature"])))
    kh = V.bh([r["hold"]["p"] for r in cands])
    for r, ok in zip(cands, kh):
        r["hold_pass"] = bool(ok and (r["hold"]["mean"] or 0) > 0 and (r["hold_top"]["mean"] or 0) > 0)
    res = []
    for r in rows:
        status, reason = V.decide(r["dev"], r["dev_pass"], r.get("hold"), r.get("hold_pass", False), bool(acc and acc.get("reused")))
        if status == "rejected" and not r["dev_pass"]:
            reason = "; ".join(r["fail"])
            if r["fail"][0].startswith("no predictive value") and (r["dev"]["p"] or 1) < V.ALPHA and (r["dev"]["mean"] or 0) > 0:
                status, reason = "exploratory finding", "raw development signal that does not survive correction: " + reason
        h = r.get("hold") or {}
        res.append({"feature": r["feature"], "times": int(r["dev"]["n"] + h.get("n", 0)), "dev_days": r["dev"]["clusters"],
                    "holdout_days": h.get("clusters", 0), "mean_ic": r["dev"]["mean"], "p": r["dev"]["p"], "ci": [r["dev"]["lo"], r["dev"]["hi"]],
                    "incremental_mean_ic": (r["inc"] or {}).get("mean"), "p_incremental": (r["inc"] or {}).get("p"),
                    "holdout_mean_ic": h.get("mean"), "holdout_p": h.get("p"), "top_third_net": r["top"]["mean"],
                    "holdout_top_third_net": (r.get("hold_top") or {}).get("mean"), "verdict": status, "reason": reason})
    return res


SCHEMA = """CREATE TABLE IF NOT EXISTS feature_results (run TEXT, at TEXT, source TEXT, feature TEXT, times INTEGER, mean_ic REAL, p REAL,
  incremental_mean_ic REAL, p_incremental REAL, holdout_mean_ic REAL, top_third_net REAL, verdict TEXT, PRIMARY KEY (run, feature))"""


def run(account: Path, exp_dir: Path, df: pd.DataFrame | None = None, record: bool = True, source_label: str | None = None) -> dict:
    df = panel(account) if df is None else df
    from nseql.screening import validation_protocol as V
    from nseql.screening.evidence_guard import GuardedLedger
    label = source_label or (Path(account).name if account is not None else "supplied panel")
    part, access = None, None
    if record and len(df):
        EV = GuardedLedger(I.Ledger(exp_dir), "evaluation")
        days = sorted(df.day.unique())
        data_fp = V.fingerprint({"source": label, "days": days, "symbols": sorted(df.symbol.unique())})
        defs = V.fingerprint({"study": VERSION, "features": FEATURES, "times": TIMES, "horizon": HORIZON_MIN, "cost": COST})
        part = V.register_partition(EV, f"features {label}", days, data_fp, defs)
        access = lambda: V.holdout_access(EV, f"features {label}", defs, data_fp)
    res = study(df, part, access)
    out = {"run": datetime.now().strftime("%Y%m%dT%H%M%S"), "version": VERSION, "source": source_label or (Path(account).name if account is not None else "supplied panel"),
           "observations": int(len(df)), "times": int(df.time.nunique()) if len(df) else 0, "results": res, "features": list(FEATURES),
           "note": "information coefficients from point-in-time features; a 'candidate' still needs forward sessions and the operator's approval"}
    if record and res:
        from nseql.screening.opportunities import research_db
        db = sqlite3.connect(str(research_db(exp_dir)))
        try:
            db.execute(SCHEMA)
            db.executemany("INSERT OR REPLACE INTO feature_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           [(out["run"], datetime.now().isoformat(timespec="seconds"), out["source"], r["feature"], r["times"], r["mean_ic"], r["p"],
                             r["incremental_mean_ic"], r["p_incremental"], r["holdout_mean_ic"], r["top_third_net"], r["verdict"]) for r in res])
            db.commit()
        finally:
            db.close()
        L = GuardedLedger(I.Ledger(exp_dir), "research")
        last = {e["subject"]: e["body"] for e in L.entries("result") if e["subject"].startswith("HM-")}
        from nseql.screening.brain import Knowledge
        K = Knowledge(L)
        for r in res:
            sub = f"HM-{r['feature']}"
            if last.get(sub, {}).get("verdict") != r["verdict"] or last.get(sub, {}).get("times") != r["times"]:
                L.append("result", sub, {"verdict": r["verdict"], "status": r["verdict"], "times": r["times"], "mean_ic": r["mean_ic"], "p": r["p"]})
            from nseql.screening.concept_study import STATUS_TO_KNOWLEDGE
            st = STATUS_TO_KNOWLEDGE.get(r["verdict"], "unverified hypothesis")
            prev = K.current().get(sub)
            if st == "contradicted finding" and prev and prev["status"] == "experimental finding":
                st = "invalidated finding"
            K.write(sub, st, f"Heatmap feature '{r['feature']}' predicts the next 30 minutes' cross-section beyond own momentum, after costs",
                    "concept_study", evidence={k: r[k] for k in ("times", "mean_ic", "p", "incremental_mean_ic", "holdout_mean_ic", "top_third_net")},
                    contradicting=[f"verdict: {r['verdict']} ({r['reason']})"] if r["verdict"] in ("rejected", "insufficient evidence") else [],
                    limitations="watchlist cross-section of about 20 stocks; candle data only", revalidate="every daily review", reason=r["verdict"])
    return out


def latest(exp_dir: Path) -> dict:
    from nseql.screening.opportunities import research_db
    f = research_db(exp_dir)
    if not f.exists():
        return {"run": None, "results": []}
    db = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        try:
            r = db.execute("SELECT run FROM feature_results ORDER BY at DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            return {"run": None, "results": []}
        return {"run": r["run"] if r else None,
                "results": [dict(x) for x in db.execute("SELECT * FROM feature_results WHERE run=? ORDER BY feature", (r["run"],))] if r else []}
    finally:
        db.close()



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

VERSION = "fs-1.0"
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


def study(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    days = sorted(df.day.unique())
    cut = days[int(len(days) * 0.6)] if len(days) >= 5 else None
    res = []
    for f in FEATURES:
        ic = _ic(df, f)
        t = I.t_test(ic.to_numpy())
        inc = None
        if f != "mom_60":
            r = df.assign(_r=_resid(df, f))
            ic_r = _ic(r, "_r")
            inc = I.t_test(ic_r.to_numpy())
        hold = ic[[str(x)[:10] >= cut for x in ic.index]] if cut else pd.Series(dtype=float)
        # long-only use: buy the top third by the feature at each time
        tops = []
        for _, g in df.groupby("time"):
            g = g[[f, "fwd"]].dropna()
            if len(g) >= MIN_STOCKS:
                tops.append(float(g[g[f] >= g[f].quantile(2 / 3)].fwd.mean()) - COST)
        res.append({"feature": f, "times": int(len(ic)), "mean_ic": None if ic.empty else float(ic.mean()), "p": t["p"],
                    "incremental_mean_ic": None if inc is None else inc["mean"], "p_incremental": None if inc is None else inc["p"],
                    "holdout_mean_ic": None if hold.empty else float(hold.mean()), "top_third_net": float(np.mean(tops)) if tops else None})
    keep = I.bh([r["p"] if r["times"] >= MIN_TIMES else None for r in res], ALPHA)
    keep_i = I.bh([r["p_incremental"] if r["times"] >= MIN_TIMES else None for r in res], ALPHA)
    for r, k, ki in zip(res, keep, keep_i):
        r["p_bh_pass"] = bool(k)
        if r["times"] < MIN_TIMES:
            v = "insufficient sample"
        elif not k or (r["holdout_mean_ic"] is not None and r["holdout_mean_ic"] <= 0):
            v = "no predictive value"
        elif r["feature"] != "mom_60" and not ki:
            v = "no incremental value over momentum"
        elif r["top_third_net"] is None or r["top_third_net"] <= 0:
            v = "no value after costs"
        else:
            v = "candidate"
        r["verdict"] = v
    return res


SCHEMA = """CREATE TABLE IF NOT EXISTS feature_results (run TEXT, at TEXT, source TEXT, feature TEXT, times INTEGER, mean_ic REAL, p REAL,
  incremental_mean_ic REAL, p_incremental REAL, holdout_mean_ic REAL, top_third_net REAL, verdict TEXT, PRIMARY KEY (run, feature))"""


def run(account: Path, exp_dir: Path, df: pd.DataFrame | None = None, record: bool = True) -> dict:
    df = panel(account) if df is None else df
    res = study(df)
    out = {"run": datetime.now().strftime("%Y%m%dT%H%M%S"), "version": VERSION, "source": Path(account).name if account is not None else "supplied panel",
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
        L = I.Ledger(exp_dir)
        last = {e["subject"]: e["body"] for e in L.entries("result") if e["subject"].startswith("HM-")}
        from nseql.screening.brain import Knowledge
        K = Knowledge(L)
        for r in res:
            sub = f"HM-{r['feature']}"
            if last.get(sub, {}).get("verdict") != r["verdict"] or last.get(sub, {}).get("times") != r["times"]:
                L.append("result", sub, {"verdict": r["verdict"], "status": r["verdict"], "times": r["times"], "mean_ic": r["mean_ic"], "p": r["p"]})
            st = {"candidate": "experimental finding", "insufficient sample": "unverified hypothesis"}.get(r["verdict"], "contradicted finding")
            prev = K.current().get(sub)
            if st == "contradicted finding" and prev and prev["status"] == "experimental finding":
                st = "invalidated finding"
            K.write(sub, st, f"Heatmap feature '{r['feature']}' predicts the next 30 minutes' cross-section beyond own momentum, after costs",
                    "concept_study", evidence={k: r[k] for k in ("times", "mean_ic", "p", "incremental_mean_ic", "holdout_mean_ic", "top_third_net")},
                    contradicting=[] if r["verdict"] == "candidate" else [f"verdict: {r['verdict']}"],
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



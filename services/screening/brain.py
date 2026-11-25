"""VEGA brain: uncertainty, decision quality, knowledge, counterfactuals, decay and self-measurement (docs/VEGA_BRAIN.md).

OBSERVATION ONLY. Nothing here places, blocks or sizes an order, and no trading module imports it (test-enforced). Its
outputs become evidence for pre-registered hypotheses (integrity.py), and a change to trading rules still needs the
experiment protocol and the operator's approval. It extends, not duplicates: the ledger, journal, integrity ledger,
research store and allocator scores are reused.

1. Uncertainty gates, evaluated for every entry with ONLY what was known at its decision minute (point-in-time SQL,
   `ts <= decision`): data fresh, data complete, positive expected edge after costs, familiar market conditions (an
   out-of-distribution test against earlier sessions at the same minute), and contradicting chart evidence. "Would have
   abstained" is recorded per entry; whether abstaining helps is hypothesis H-ABSTAIN, decided by integrity.py.
2. Four separate grades per completed trade: decision quality (information at decision time), outcome (what happened
   later; labelled hindsight), execution (delay, slippage against the decision candle, charges) and learning value
   (one trade is an observation, never a finding).
3. Knowledge: versioned entries on the hash-chained evidence ledger with a status (observed fact, unverified hypothesis,
   experimental finding, validated finding, contradicted, superseded, invalidated). Corrections are new versions; history
   is never deleted. Only computed sources may write knowledge (external text is refused: poisoning defence).
4. Counterfactual shadow outcomes for each real trade under a FIXED, predeclared set of alternatives (later or earlier
   exit, later entry, no trade). Stored in the research store, labelled hypothetical, never mixed with the paper ledger,
   and they ignore capital competition between trades (stated limitation).
5. Strategy decay per strategy type (Page-Hinkley) and shared risk (concurrent positions, correlated daily results).
6. Measures of the brain itself, each next to a reference baseline. There is no combined "intelligence score".
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from nseql.screening import integrity as I

VERSION = "brain-1.0"
FRESH_LAG_S = 55            # same bound as the scheduler's catch-up rule
COMPLETE_SHARE = 0.9
OOD_MIN_SESSIONS = 10
OOD_BAND = (2.5, 97.5)
ALTERNATIVES = (            # predeclared; never chosen after seeing outcomes
    ("exit 15 min after entry", 0, 15), ("exit 30 min after entry", 0, 30), ("exit 60 min after entry", 0, 60),
    ("exit at 15:15", 0, None), ("enter 1 min later, same exit", 1, "same"), ("enter 5 min later, same exit", 5, "same"),
)
STATUSES = ("observed fact", "unverified hypothesis", "experimental finding", "validated finding", "contradicted finding",
            "superseded finding", "invalidated finding")
KNOWLEDGE_SOURCES = ("ledger", "integrity", "daily_review", "operator")
LEVELS = ("observed fact", "heuristic interpretation", "statistically supported finding")


# ================================================================================================ 1. uncertainty gates
def _market_vol(db: sqlite3.Connection, day: str) -> pd.Series:
    """Median absolute 1-minute return across the watchlist, averaged from the open up to each minute (point in time)."""
    b = pd.read_sql_query("SELECT symbol, ts, close FROM events WHERE late=0 AND ts >= ? AND ts < ? ORDER BY symbol, ts",
                          db, params=(day, I._next(day)))
    if b.empty:
        return pd.Series(dtype=float)
    p = b.pivot_table(index="ts", columns="symbol", values="close").sort_index()
    r = np.log(p).diff().abs().median(axis=1)
    v = r.expanding().mean()
    v.index = pd.to_datetime(v.index).hour * 60 + pd.to_datetime(v.index).minute
    return v[~v.index.duplicated(keep="last")]


def gate_table(account: Path, rt: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per entry (completed round trips), with every gate computed from data at or before its decision minute."""
    account = Path(account)
    rt = I.round_trips(account) if rt is None else rt
    cols = ["config_id", "symbol", "decided_at", "exit_ts", "ret", "net", "fresh", "complete", "evidence", "familiar", "contradiction",
            "abstain", "reasons"]
    if rt.empty:
        return pd.DataFrame(columns=cols)
    con = I._ro(account)
    try:
        cyc = {m: (lag, have, exp, mode) for m, lag, have, exp, mode in
               con.execute("SELECT minute, lag_s, symbols_with_data, symbols_expected, mode FROM cycles WHERE status='processed'")}
        try:
            pa = {(c, t): j for c, t, j in con.execute("SELECT config_id, ts, price_action FROM journal WHERE side='BUY'")}
        except sqlite3.Error:
            pa = {}
        days = sorted({str(d)[:10] for d in rt.decided_at})
        all_days = [r[0] for r in con.execute("SELECT DISTINCT substr(ts,1,10) FROM events WHERE late=0 ORDER BY 1")]
        vols = {d: _market_vol(con, d) for d in all_days if d <= max(days)}
    finally:
        con.close()
    from nseql.screening.price_action import labels
    rows = []
    for r in rt.itertuples(index=False):
        dec = str(r.decided_at)
        lag, have, exp, mode = cyc.get(dec, (None, None, None, None))
        fresh = None if lag is None else bool(lag <= FRESH_LAG_S and mode != "catch-up")
        complete = None if not exp else bool(have / exp >= COMPLETE_SHARE)
        evidence = None if r.score is None or pd.isna(r.score) else bool(r.score > 0)
        # out-of-distribution: today's volatility so far versus earlier sessions at the same minute
        d, t = dec[:10], pd.Timestamp(dec)
        m = t.hour * 60 + t.minute
        now_v = vols.get(d, pd.Series(dtype=float))
        now_v = now_v[now_v.index <= m]
        prior = [v[v.index <= m].iloc[-1] for k, v in vols.items() if k < d and len(v[v.index <= m])]
        if len(now_v) == 0 or len(prior) < OOD_MIN_SESSIONS:
            familiar = None
        else:
            lo, hi = np.percentile(prior, OOD_BAND)
            familiar = bool(lo <= now_v.iloc[-1] <= hi)
        lab = {}
        j = pa.get((r.config_id, r.entry_ts))
        if j:
            try:
                lab = labels(json.loads(j))
            except (ValueError, TypeError):
                lab = {}
        contra = [x for x in (lab.get("ch_trend") == "5m and 15m both down" and "5m and 15m trends both down",
                              lab.get("ch_rsi") == "RSI >= 70 (overbought)" and "RSI overbought",
                              lab.get("ch_room") == "resistance within 0.5% (little room)" and "resistance within 0.5%") if x]
        reasons = [n for n, v in (("stale data", fresh), ("incomplete data", complete), ("no positive expected edge", evidence),
                                  ("unfamiliar market conditions", familiar)) if v is False]
        rows.append({"config_id": r.config_id, "symbol": r.symbol, "decided_at": dec, "exit_ts": r.exit_ts, "ret": r.ret, "net": r.net,
                     "fresh": fresh, "complete": complete, "evidence": evidence, "familiar": familiar,
                     "contradiction": "; ".join(contra) if contra else (None if not lab else ""),
                     "abstain": bool(reasons), "reasons": "; ".join(reasons)})
    return pd.DataFrame(rows, columns=cols)


def gates_now(sch, now: datetime | None = None) -> list[dict]:
    """The same gates for the next decision, from the live engine (no outcome involved). For the dashboard."""
    db = sch.store.reader() if hasattr(sch.store, "reader") else sch.store.db
    r = db.execute("SELECT minute, lag_s, symbols_with_data, symbols_expected, mode FROM cycles WHERE status='processed' ORDER BY minute DESC LIMIT 1").fetchone()
    out = []
    if r is None:
        return [{"gate": "data", "ok": None, "detail": "no processed minute yet"}]
    m, lag, have, exp, mode = r
    age = None if now is None else (pd.Timestamp(now) - pd.Timestamp(m)).total_seconds() - 60
    out.append({"gate": "Data fresh", "ok": bool(lag is not None and lag <= FRESH_LAG_S and mode != "catch-up" and (age is None or age < 180)),
                "detail": f"last minute {str(m)[11:16]} decided {lag:.0f} s after it closed" + (f"; {int(age)} s ago" if age is not None else "")
                if lag is not None else "unknown"})
    out.append({"gate": "Data complete", "ok": bool(exp and have / exp >= COMPLETE_SHARE), "detail": f"{have} of {exp} stocks had a bar"})
    return out


# ================================================================================================ 2. decision / outcome / execution / learning
def trade_quality(account: Path, gates: pd.DataFrame | None = None) -> dict:
    account = Path(account)
    rt = I.round_trips(account)
    if rt.empty:
        return {"trades": [], "matrix": {}, "n": 0}
    g = gate_table(account, rt) if gates is None else gates
    con = I._ro(account)
    try:
        dec_close = {(s, t): c for s, t, c in con.execute("SELECT symbol, ts, close FROM events WHERE late=0")}
    finally:
        con.close()
    out = []
    for r, gg in zip(rt.itertuples(index=False), g.itertuples(index=False)):
        known = [v for v in (gg.fresh, gg.complete, gg.evidence, gg.familiar) if v is not None]
        decision = "unknown" if not known else "unsound" if gg.abstain else "sound"
        ref = dec_close.get((r.symbol, str(r.decided_at)))
        slip = None if not ref else (r.entry / ref - 1)
        delay = (pd.Timestamp(r.entry_ts) - pd.Timestamp(r.decided_at)).total_seconds() / 60
        gross = r.net + r.charges
        out.append({"symbol": r.symbol, "decided_at": r.decided_at, "exit_ts": r.exit_ts,
                    "decision": {"grade": decision, "basis": "information at the decision minute only",
                                 "failed_gates": gg.reasons or None, "contradicting_chart": gg.contradiction or None,
                                 "expected_edge_bps": None if r.score is None or pd.isna(r.score) else round(float(r.score), 2)},
                    "outcome": {"net_inr": round(r.net, 2), "ret": round(r.ret, 5), "won": bool(r.net > 0), "basis": "later prices (hindsight)"},
                    "execution": {"delay_min": round(delay, 1), "entry_vs_decision_close": None if slip is None else round(slip, 5),
                                  "charges_inr": round(r.charges, 2), "charges_share_of_gross": None if gross <= 0 else round(r.charges / gross, 3)},
                    "learning": {"level": "observed fact", "note": "one trade is an observation; it supports no rule change by itself"}})
    m: dict[str, int] = {}
    for t in out:
        k = f"{t['decision']['grade']} decision, {'won' if t['outcome']['won'] else 'lost'}"
        m[k] = m.get(k, 0) + 1
    return {"trades": out[-200:], "matrix": m, "n": len(out),
            "note": "A sound decision can lose and an unsound one can win; outcomes alone never grade a decision."}


# ================================================================================================ 3. knowledge
class Knowledge:
    """Versioned knowledge on the evidence ledger (append-only). Status changes and corrections are new versions."""

    def __init__(self, ledger: I.Ledger):
        self.L = ledger

    def write(self, kid: str, status: str, statement: str, source: str, evidence: dict | None = None, contradicting: list | None = None,
              conditions: dict | None = None, limitations: str = "", revalidate: str = "", reason: str = "") -> str | None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        if source not in KNOWLEDGE_SOURCES:
            raise PermissionError(f"knowledge may only come from {KNOWLEDGE_SOURCES}, not {source!r} (external text never becomes knowledge)")
        cur = self.current().get(kid)
        body = {"status": status, "statement": statement, "source": source, "evidence": evidence or {}, "contradicting": contradicting or [],
                "conditions": conditions or {}, "limitations": limitations, "revalidate": revalidate, "reason": reason,
                "version": (cur["version"] + 1) if cur else 1, "brain": VERSION}
        if cur and all(cur.get(k) == body[k] for k in ("status", "statement", "evidence", "contradicting")):
            return None                                          # nothing new: no version
        return self.L.append("knowledge", kid, body)

    def history(self, kid: str) -> list[dict]:
        return [{"at": e["at"], **e["body"]} for e in self.L.entries("knowledge", kid)]

    def current(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for e in self.L.entries("knowledge"):
            out[e["subject"]] = {"id": e["subject"], "at": e["at"], **e["body"]}
        return out

    def corrections(self) -> list[dict]:
        """Entries whose status moved from supported towards contradicted or invalidated (self-correction)."""
        rank = {"experimental finding": 2, "validated finding": 3, "unverified hypothesis": 1}
        out = []
        for kid in self.current():
            h = self.history(kid)
            for a, b in zip(h, h[1:]):
                if rank.get(a["status"], 0) >= 1 and b["status"] in ("contradicted finding", "invalidated finding", "superseded finding"):
                    out.append({"id": kid, "from": a["status"], "to": b["status"], "at": b["at"], "reason": b.get("reason")})
        return out


def sync_knowledge(K: Knowledge, rep: dict, trade_report: dict | None = None) -> int:
    """Turn computed evidence into knowledge. Daily 'next time' patterns become UNVERIFIED hypotheses (never rules);
    integrity results move them; a falsified claim that was once supported becomes invalidated, with its history kept."""
    n = 0
    v = rep.get("validation", {})
    n += bool(K.write("K-ACCOUNT", "observed fact", f"Over {v.get('sessions', 0)} session(s) and {v.get('round_trips', 0)} round trips: "
                      + (v.get("benchmarks") or "no benchmark yet"), "ledger", evidence={"sessions": v.get("sessions"), "round_trips": v.get("round_trips")},
                      limitations="paper trading with simulated fills; one account series"))
    for h in rep.get("hypotheses", []):
        cur = K.current().get(h["id"])
        st = {"survived": "experimental finding", "falsified": "contradicted finding"}.get(h["status"], "unverified hypothesis")
        if h["status"] == "falsified" and cur and cur["status"] in ("experimental finding", "validated finding"):
            st = "invalidated finding"
        n += bool(K.write(h["id"], st, h["statement"], "integrity",
                          evidence={"p": h.get("p"), "confirmatory": (h.get("confirmatory") or {}).get("sessions", (h.get("confirmatory") or {}).get("round_trips"))},
                          contradicting=[c["detail"] for c in h.get("contradicting", [])],
                          limitations="survival is not proof; validated only by the operator after forward evidence",
                          revalidate="every daily review", reason=h.get("reason") or ""))
    for p in (trade_report or {}).get("next_time", []):
        kid = f"K-PATTERN-{p['pattern']}"
        prev = K.current().get(kid)
        days = (prev or {}).get("evidence", {}).get("days", 0) + 1
        trades = (prev or {}).get("evidence", {}).get("trades", 0) + p["trades"]
        n += bool(K.write(kid, "unverified hypothesis", f"{p['meaning']} Proposed test: {p['next_time']}", "daily_review",
                          evidence={"days": days, "trades": trades, "lost_inr": round((prev or {}).get("evidence", {}).get("lost_inr", 0) + p.get("lost_inr", 0), 2)},
                          limitations="a heuristic reading of individual trades; not a finding until tested on data it was not drawn from",
                          revalidate=p.get("decided_by") or "experiment"))
    return n


# ================================================================================================ 4. counterfactual shadow outcomes
SHADOW_SCHEMA = """CREATE TABLE IF NOT EXISTS shadow_alternatives (account TEXT, entry_ts TEXT, config_id TEXT, symbol TEXT, alternative TEXT,
  ret_net REAL, actual_ret REAL, kind TEXT DEFAULT 'hypothetical shadow trade', PRIMARY KEY (account, entry_ts, config_id, alternative))"""


def shadow(account: Path, exp_dir: Path | None = None) -> dict:
    """Each real round trip replayed under the predeclared ALTERNATIVES with its own cost fraction. Hypothetical: it ignores
    capital competition between trades and assumes the same fill quality."""
    account = Path(account)
    rt = I.round_trips(account)
    if rt.empty:
        return {"n": 0, "alternatives": []}
    bars = I._bars(account, sorted(rt.symbol.unique()), sorted(rt.entry_ts.str[:10].unique()))
    res = []
    for r in rt.itertuples(index=False):
        k = bars.get((r.symbol, str(r.entry_ts)[:10]))
        if k is None:
            continue
        ts, op, cl = k
        e0 = int(np.searchsorted(ts, np.datetime64(pd.Timestamp(r.entry_ts)), side="left"))
        x0 = int(np.searchsorted(ts, np.datetime64(pd.Timestamp(r.exit_ts)), side="left"))
        cost = r.charges / (r.entry * r.qty)
        sq = int(np.searchsorted(ts, np.datetime64(pd.Timestamp(str(r.entry_ts)[:10]) + pd.Timedelta(hours=15, minutes=15)), side="right")) - 1
        for name, delay, hold in ALTERNATIVES:
            e = min(e0 + delay, len(ts) - 1)
            x = x0 if hold == "same" else sq if hold is None else e0 + int(hold)
            x = min(max(x, e), len(ts) - 1)
            px_in = op[e] if delay else r.entry
            res.append({"entry_ts": r.entry_ts, "config_id": r.config_id, "symbol": r.symbol, "alternative": name,
                        "ret_net": float(cl[x] / px_in - 1 - cost), "actual_ret": float(r.ret)})
    df = pd.DataFrame(res)
    if exp_dir is not None and len(df):
        from nseql.screening.opportunities import research_db
        db = sqlite3.connect(str(research_db(exp_dir)))
        try:
            db.execute(SHADOW_SCHEMA)
            db.executemany("INSERT OR REPLACE INTO shadow_alternatives (account, entry_ts, config_id, symbol, alternative, ret_net, actual_ret) "
                           "VALUES (?,?,?,?,?,?,?)", [(account.name, *x.values()) for x in res])
            db.commit()
        finally:
            db.close()
    summ = []
    if len(df):
        act = float(rt.ret.mean())
        summ.append({"alternative": "actual trades", "mean_ret": round(act, 5), "n": len(rt)})
        summ.append({"alternative": "no trade", "mean_ret": 0.0, "n": len(rt)})
        for name, g in df.groupby("alternative", sort=False):
            summ.append({"alternative": name, "mean_ret": round(float(g.ret_net.mean()), 5), "n": len(g)})
    return {"n": len(rt), "alternatives": summ, "kind": "hypothetical shadow trades (research only)",
            "limitations": "ignores capital competition between trades and assumes the same fill quality; alternatives fixed in advance"}


# ================================================================================================ 5. decay and shared risk
def decay(account: Path) -> list[dict]:
    rt = I.round_trips(account)
    if rt.empty:
        return []
    con = I._ro(account)
    try:
        fam = dict(con.execute("SELECT id, family || ' ' || timeframe FROM configs").fetchall())
    finally:
        con.close()
    rt = rt.assign(group=rt.config_id.map(fam).fillna("unknown"))
    out = []
    for g, x in rt.groupby("group"):
        ph = I.page_hinkley(x.net.to_numpy())
        half = len(x) // 2
        out.append({"strategy_type": g, "round_trips": len(x), "net_inr": round(float(x.net.sum()), 2),
                    "first_half_mean": round(float(x.ret.iloc[:half].mean()), 5) if half else None,
                    "second_half_mean": round(float(x.ret.iloc[half:].mean()), 5) if half else None,
                    "deterioration": "needs 30 round trips" if not ph["decided"] else ("alarm" if ph["alarm"] else "no alarm")})
    return sorted(out, key=lambda r: -r["round_trips"])


def shared_risk(account: Path) -> dict:
    """How independent the bets really are: the most positions open together, and correlation of daily results between
    strategy types (correlated strategies share one risk, whatever their count)."""
    rt = I.round_trips(account)
    if rt.empty:
        return {"max_concurrent": 0}
    ev = sorted([(t, 1) for t in rt.entry_ts] + [(t, -1) for t in rt.exit_ts])
    cur = mx = 0
    for _, d in ev:
        cur += d
        mx = max(mx, cur)
    con = I._ro(account)
    try:
        fam = dict(con.execute("SELECT id, family FROM configs").fetchall())
    finally:
        con.close()
    daily = rt.assign(day=rt.exit_ts.str[:10], f=rt.config_id.map(fam)).pivot_table(index="day", columns="f", values="net", aggfunc="sum").fillna(0)
    high = []
    if daily.shape[0] >= 5 and daily.shape[1] >= 2:
        c = daily.corr()
        for i, a in enumerate(c.columns):
            for b in c.columns[i + 1:]:
                if c.loc[a, b] >= 0.7:
                    high.append({"pair": f"{a} / {b}", "correlation": round(float(c.loc[a, b]), 2)})
    same_sym = int(rt.groupby([rt.symbol, rt.entry_ts.str[:10]]).config_id.nunique().gt(1).sum())
    return {"max_concurrent": mx, "highly_correlated_types": high, "days": int(daily.shape[0]),
            "same_stock_same_day_by_several_strategies": same_sym,
            "note": "correlation needs at least 5 sessions; strategies with correlation >= 0.7 count as one bet"}


# ================================================================================================ 6. the brain's own measures
NOT_IMPLEMENTED = [
    "swing, positional and long-term books (daily data exists on the laptop; no validated setups yet)",
    "futures and options (no licensed chain or margin data)",
    "ETF, mutual fund and SIP decisions inside the portfolio (planner and NAV module exist separately)",
    "news and sentiment as trading inputs (observed and counted only; not validated)",
    "institutional flow data (no reliable source connected)",
    "reliability diagrams for probability forecasts (the allocator produces a score, not a probability)",
]


def metrics(rep: dict, gates: pd.DataFrame, quality: dict, K: Knowledge, data_quality: dict) -> list[dict]:
    hy = rep.get("hypotheses", [])
    raw = sum(1 for h in hy if h.get("p") is not None and h["p"] < I.ALPHA)
    kept = sum(1 for h in hy if h["status"] == "survived")
    crit = rep.get("critic", [])
    n = len(gates)
    return [
        {"measure": "Hypotheses pre-registered / decided", "value": f"{len(hy)} / {sum(h['status'] in ('survived', 'falsified') for h in hy)}",
         "baseline": "0 decided until confirmatory evidence exists", "evidence": "evidence ledger"},
        {"measure": "False positives removed by correction", "value": f"{raw} raw p<{I.ALPHA} vs {kept} after Benjamini-Hochberg",
         "baseline": "uncorrected testing", "evidence": "integrity report"},
        {"measure": "Contradictions found", "value": f"{sum(not c['survives'] for c in crit)} of {len(crit)} checks contradict the incumbent",
         "baseline": "a system that only confirms finds 0", "evidence": "critic"},
        {"measure": "Safe abstention", "value": f"{int(gates.abstain.sum()) if n else 0} of {n} entries failed a mandatory gate",
         "baseline": "0 failures expected if gates were enforced", "evidence": "point-in-time gate table"},
        {"measure": "Decision vs outcome", "value": ", ".join(f"{k}: {v}" for k, v in quality.get("matrix", {}).items()) or "no trades",
         "baseline": "outcome alone (win rate)", "evidence": "trade quality"},
        {"measure": "Calibration", "value": next((b["evidence"] for b in rep.get("biases", []) if b["bias"].startswith("Overconfidence")), "—"),
         "baseline": "rank correlation 0 (no information)", "evidence": "allocator score vs outcome"},
        {"measure": "Data-quality detection", "value": f"{data_quality.get('quarantined', 0)} quarantined, {data_quality.get('conflicts', 0)} conflicting, "
                                                        f"{data_quality.get('late', 0)} late bars", "baseline": "accept everything", "evidence": "ledger"},
        {"measure": "Self-corrections", "value": f"{len(K.corrections())} knowledge entries corrected (history kept)",
         "baseline": "0 (a system that never corrects)", "evidence": "knowledge versions"},
        {"measure": "Reproducibility", "value": "identical report for identical ledgers (seeded)", "baseline": "unseeded resampling",
         "evidence": "test_vega_brain::test_reproducible"},
        {"measure": "Out-of-sample improvement", "value": next((h["status"] for h in hy if h["id"] == "H-CHALLENGER"), "—"),
         "baseline": "the incumbent", "evidence": "H-CHALLENGER"},
    ]


def data_quality(account: Path) -> dict:
    con = I._ro(account)
    try:
        q = lambda sql: (con.execute(sql).fetchone() or [0])[0]
        try:
            quarantined = q("SELECT COUNT(*) FROM event_quarantine")
        except sqlite3.Error:
            quarantined = 0
        return {"quarantined": quarantined, "conflicts": q("SELECT COUNT(*) FROM event_conflicts"), "late": q("SELECT COUNT(*) FROM events WHERE late=1")}
    finally:
        con.close()


def report(exp_dir: Path, rep: dict | None = None, data_dir: Path | None = None) -> dict:
    exp_dir = Path(exp_dir)
    rep = rep or I.report(exp_dir, data_dir)
    acc = I.accounts(exp_dir)
    cur = acc["incumbent"][-1] if acc["incumbent"] else None
    K = Knowledge(I.Ledger(exp_dir))
    if cur is None:
        return {"version": VERSION, "account": None, "knowledge": list(K.current().values()), "not_implemented": NOT_IMPLEMENTED,
                "note": "no live paper account yet"}
    g = gate_table(cur)
    q = trade_quality(cur, g)
    dq = data_quality(cur)
    by_gate = {}
    for k in ("fresh", "complete", "evidence", "familiar"):
        s = g[k] if len(g) else pd.Series(dtype=object)
        by_gate[k] = {"pass": int((s == True).sum()), "fail": int((s == False).sum()), "unknown": int(s.isna().sum())}  # noqa: E712
    return {"version": VERSION, "account": cur.name, "generated_at": datetime.now().isoformat(timespec="seconds"),
            "uncertainty": {"entries": len(g), "would_abstain": int(g.abstain.sum()) if len(g) else 0, "by_gate": by_gate,
                            "with_contradiction": int((g.contradiction.fillna("") != "").sum()) if len(g) else 0,
                            "recent": g.tail(30).replace({np.nan: None}).to_dict("records"),
                            "note": "computed from data at each decision minute; observation only (H-ABSTAIN tests whether abstaining would help)"},
            "quality": q, "shadow": shadow(cur, exp_dir), "decay": decay(cur), "shared_risk": shared_risk(cur), "data_quality": dq,
            "knowledge": sorted(K.current().values(), key=lambda x: x["at"], reverse=True), "corrections": K.corrections(),
            "metrics": metrics(rep, g, q, K, dq), "not_implemented": NOT_IMPLEMENTED,
            "principles": "No combined intelligence score. Profit is not decision quality. AI agreement is not evidence."}

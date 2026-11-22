"""Vega copilot: answers questions about the lab from its own records, with links to the evidence.

Deterministic intent routing; no language model is involved. Every answer says which records it read and
what it did NOT look at, so it never implies live market access or reasoning it did not perform. Unknown
questions get the list of supported questions rather than a guess.

The `ctx` object supplies read-only accessors (see services/api/main.py):
  sch() -> MinuteScheduler | None        reg -> research Registry
  readiness() -> list[dict]              brain_status() -> dict
  features() -> list[(feature, status, note)]
"""
from __future__ import annotations

import re

import numpy as np

CONFIG_ID = re.compile(r"\b[0-9a-f]{16}\b")
HYP_ID = re.compile(r"\bh[0-9a-z]{6,}\b")

SUPPORTED = [
    ("market", "What changed in the market?"),
    ("strongest", "Which strategies have the strongest validated evidence?"),
    ("why_rejected", "Why was this strategy rejected? (include a configuration or hypothesis id)"),
    ("correlated", "Which strategies are correlated?"),
    ("loss", "What caused today's simulated portfolio loss?"),
    ("failed", "Which research experiments failed?"),
    ("brain", "What did the Learning Brain discover?"),
    ("next", "What should the research system investigate next?"),
    ("stale", "Is the current data feed stale?"),
    ("incomplete", "Which market or investment modules are incomplete?"),
]

ROUTES = [
    ("why_rejected", r"\bwhy\b.*\b(reject|refus|defer|gat|fail)"),
    ("stale", r"\b(stale|fresh|delay|feed|data source|live data)\b"),
    ("incomplete", r"\b(incomplete|missing|not (yet )?(built|implemented)|modules?|blocked|unverified)\b"),
    ("loss", r"\b(loss|lost|losing|drawdown|p ?& ?l|pnl|down today)\b"),
    ("correlat", r"\b(correlat|overlap|redundan|similar)"),
    ("failed", r"\b(fail|rejected|didn'?t work|errors?)\b.*\b(experiment|hypothes|research)|\b(experiment|hypothes|research)\b.*\b(fail|rejected)"),
    ("brain", r"\b(learning brain|brain|learn(ed|ing)?|discover)"),
    ("next", r"\b(next|investigate|should .*research|what to (try|test))\b"),
    ("strongest", r"\b(strong|best|validated|evidence|top|qualified|winning)\b"),
    ("market", r"\b(market|moved|movers?|changed|happen)"),
]


def route(question: str) -> str | None:
    q = question.lower()
    for name, pat in ROUTES:
        if re.search(pat, q):
            return "correlated" if name == "correlat" else name
    return None


def _ans(text: str, evidence: list[dict], read: list[str], not_read: list[str], intent: str) -> dict:
    return {"intent": intent, "answer": text, "evidence": evidence, "grounding": {"read": read, "did_not_read": not_read},
            "method": "deterministic query of the lab's own records (no language model)"}


def _cfg_link(cid: str, label: str | None = None) -> dict:
    return {"label": label or cid, "href": f"/universe?config={cid}", "kind": "configuration"}


def answer(question: str, ctx) -> dict:
    intent = route(question)
    if intent is None:
        return {"intent": None, "answer": "I can only answer questions I can ground in the lab's records. Try one of these:",
                "suggestions": [q for _, q in SUPPORTED], "evidence": [], "grounding": {"read": [], "did_not_read": []},
                "method": "no matching question type; nothing was looked up"}
    return globals()[f"_{intent}"](question, ctx)


# ---------------------------------------------------------------------------------------------- intents
def _stale(q, ctx):
    sch = ctx.sch()
    if sch is None:
        return _ans("No market data feed is attached: there is no screening run yet, so there is nothing to be stale.", [],
                    ["screening scheduler state"], ["broker APIs (not contacted)"], "stale")
    ds, fh = sch.provenance(), sch.feed_health()
    parts = [f"Data source: {ds['label']}."]
    if ds.get("freshness_seconds") is not None:
        parts.append(f"The last processed bar closed {round(ds['freshness_seconds'])} s ago.")
    parts.append("It is STALE." if ds.get("stale") else ("It is not stale." if ds.get("live") else "Staleness applies only to live feeds; this is not a live feed."))
    if fh:
        parts.append(f"{fh['symbols']} symbols polled, capacity {fh['capacity_per_minute']} a minute"
                     + (f", {len(fh['stale_symbols'])} symbol(s) without a bar for over 3 minutes" if fh["stale_symbols"] else "")
                     + (f", {fh['errors']} erroring" if fh["errors"] else "") + ".")
    return _ans(" ".join(parts), [{"label": "Strategy Monitor: loop overview", "href": "/screening", "kind": "page"}],
                ["screening provenance", "feed health"], ["the broker's own status page"], "stale")


def _incomplete(q, ctx):
    feats = [f for f in ctx.features() if f[1] != "IMPLEMENTED"]
    mods = [m for m in ctx.readiness() if m["status"] not in ("ready", "disabled")]
    lines = [f"{f[0]}: {f[1].lower()}" + (f" ({f[2]})" if f[2] else "") for f in feats]
    text = (f"{len(feats)} capabilities are not fully implemented and verified, and {len(mods)} runtime modules are not ready. "
            + " ".join(f"• {l}." for l in lines[:12]))
    if mods:
        text += " Not ready now: " + ", ".join(f"{m['module']} ({m['status']})" for m in mods) + "."
    return _ans(text, [{"label": "Capability matrix (docs/AUDIT_V6.md)", "href": "/system", "kind": "page"},
                       {"label": "Module readiness", "href": "/", "kind": "page"}],
                ["backend capability list", "runtime readiness"], [], "incomplete")


def _strongest(q, ctx):
    from nseql.screening import universe
    ev, out = [], []
    sch = ctx.sch()
    if sch is not None:
        df = universe.frame(sch)
        qual = df[df.stage == "qualified"].sort_values(["shrunk_bps", "trades"], ascending=False).head(5)
        traded = df[df.trades > 0]
        if len(qual):
            out.append(f"{len(qual)} configuration(s) are qualified in the forward paper ledger (≥{sch.engine.x.alloc.cfg.min_trades} trades, positive shrunk mean): "
                       + "; ".join(f"{r.symbol} {r.rule} {r.timeframe} ({int(r.trades)} trades, {r.mean_net_bps:.1f} bps/trade)" for r in qual.itertuples()) + ".")
            ev += [_cfg_link(r.id, f"{r.symbol} {r.rule} {r.timeframe}") for r in qual.itertuples()]
        else:
            out.append(f"No screening configuration is qualified yet: {len(traded)} of {len(df):,} have completed a forward trade, "
                       f"none with at least {sch.engine.x.alloc.cfg.min_trades} trades and a positive shrunk mean.")
        out.append(f"Data: {sch.provenance()['label']}.")
    cands = [h for h in ctx.reg.hypotheses(1000) if h["status"] in ("paper_candidate", "promoted_to_paper_candidate")]
    if cands:
        out.append(f"Research: {len(cands)} hypothesis(es) passed the audit and the sealed check: " + ", ".join(h["id"] for h in cands[:5]) + ".")
        ev += [{"label": f"hypothesis {h['id']}", "href": f"/factory?h={h['id']}", "kind": "hypothesis"} for h in cands[:5]]
    else:
        out.append("Research: no hypothesis has passed the sealed out-of-sample check.")
    out.append("Evidence of past performance is not a forecast of future profit.")
    return _ans(" ".join(out), ev, ["screening forward ledger", "research hypotheses"], ["live market prices"], "strongest")


def _why_rejected(q, ctx):
    from nseql.screening import universe
    m = CONFIG_ID.search(q.lower())
    sch = ctx.sch()
    if m and sch is not None:
        d = universe.dossier(sch, m.group(0), ctx.reg)
        if d:
            sig = [s for s in d["recent_signals"] if s["decision"] == "BUY" and s["alloc_status"] in ("rejected", "deferred")][:3]
            text = (f"Configuration {m.group(0)} ({d['config']['rule']}, {d['config']['symbol']} {d['config']['timeframe']}) is at stage "
                    f"'{d['stage']}'. " + " ".join(d["weaknesses"]))
            if sig:
                text += " Recent refused signals: " + "; ".join(f"{s['bar_ts']}: {s['alloc_status']} — {s['alloc_reason']}" for s in sig) + "."
            return _ans(text, [_cfg_link(m.group(0), "Open the dossier")], ["configuration dossier", "signal allocation log"], [], "why_rejected")
    h = HYP_ID.search(q.lower())
    if h:
        hyp = ctx.reg.hypothesis(h.group(0))
        if hyp:
            ls = [l for l in ctx.reg.lessons(200) if l.get("hypothesis_id") == hyp["id"]]
            text = f"Hypothesis {hyp['id']} ({hyp.get('family')}) has status '{hyp['status']}': {hyp.get('status_reason') or 'no reason recorded'}."
            if ls:
                text += " Lesson recorded: " + ls[0]["text"] + (f" Next: {ls[0]['next_step']}" if ls[0].get("next_step") else "")
            return _ans(text, [{"label": f"hypothesis {hyp['id']}", "href": f"/factory?h={hyp['id']}", "kind": "hypothesis"}],
                        ["research registry", "lessons"], [], "why_rejected")
    return _ans("Give me a 16-character configuration id or a hypothesis id (for example from the Strategy Universe or the Strategy Factory) "
                "and I will show the recorded reasons.", [{"label": "Strategy Universe", "href": "/universe", "kind": "page"}], [], [], "why_rejected")


def _correlated(q, ctx):
    sch = ctx.sch()
    if sch is None:
        return _ans("No screening run, so there are no return series to correlate.", [], [], [], "correlated")
    a = sch.engine.x.alloc
    n = a.buf_n
    if n < 10:
        return _ans(f"Only {n} one-minute returns are buffered; at least 10 are needed to measure correlation.", [], ["allocator return buffer"], [], "correlated")
    R = a.ret_buf[:, :n] if n < a.ret_buf.shape[1] else a.ret_buf
    sym = sch.reg.symbols
    ok = R.std(axis=1) > 0
    idx = np.flatnonzero(ok)
    if len(idx) < 2:
        return _ans("Fewer than two symbols moved in the buffered window.", [], ["allocator return buffer"], [], "correlated")
    C = np.corrcoef(R[idx])
    iu = np.triu_indices(len(idx), 1)
    order = np.argsort(-C[iu])[:5]
    pairs = [(sym[idx[iu[0][k]]], sym[idx[iu[1][k]]], C[iu][k]) for k in order]
    text = (f"Over the last {n} one-minute returns ({sch.provenance()['label']}), the most correlated symbol pairs are: "
            + "; ".join(f"{x}–{y} {c:.2f}" for x, y, c in pairs)
            + ". Configurations on correlated symbols are penalised by the ranked allocator. Within one symbol, all configurations of the same rule "
              "family trade the same price series and are highly redundant.")
    return _ans(text, [{"label": "Strategy Universe", "href": "/universe", "kind": "page"}], ["allocator return buffer (60 minutes)"],
                ["daily returns", "fundamentals"], "correlated")


def _loss(q, ctx):
    sch = ctx.sch()
    if sch is None:
        return _ans("There is no simulated portfolio yet.", [], [], [], "loss")
    db = sch.store.db
    with sch.store._lock:
        day = db.execute("SELECT MAX(substr(fill_ts,1,10)) FROM paper_orders WHERE fill_ts IS NOT NULL").fetchone()[0]
        if not day:
            return _ans("No paper order has filled yet.", [], ["paper ledger"], [], "loss")
        rows = db.execute("SELECT symbol, config_id, SUM(realized_paise), SUM(charges_paise), COUNT(*) FROM paper_orders WHERE substr(fill_ts,1,10)=? "
                          "GROUP BY symbol, config_id", (day,)).fetchall()
        eq = db.execute("SELECT equity_inr FROM cycles WHERE substr(minute,1,10)=? AND equity_inr IS NOT NULL ORDER BY minute", (day,)).fetchall()
    real = sum((r[2] or 0) for r in rows) / 100
    chg = sum((r[3] or 0) for r in rows) / 100
    worst = sorted(rows, key=lambda r: (r[2] or 0))[:3]
    change = (eq[-1][0] - eq[0][0]) if len(eq) >= 2 else None
    text = (f"On {day} ({sch.provenance()['label']}): equity changed by ₹{change:,.0f} during the session. " if change is not None else f"On {day}: ")
    text += f"Realised P&L of exits ₹{real:,.0f} after charges; charges paid ₹{chg:,.0f} across {sum(r[4] for r in rows)} orders. "
    if worst and (worst[0][2] or 0) < 0:
        text += "Largest realised losses: " + "; ".join(f"{r[0]} config {r[1]} ₹{(r[2] or 0) / 100:,.0f}" for r in worst if (r[2] or 0) < 0) + ". "
    text += "The rest of any equity change is unrealised marking of open positions. These are simulated fills."
    return _ans(text, [_cfg_link(r[1], f"{r[0]} {r[1]}") for r in worst if (r[2] or 0) < 0],
                ["paper ledger", "cycle equity series"], ["real broker statements"], "loss")


def _failed(q, ctx):
    hyps = [h for h in ctx.reg.hypotheses(1000) if str(h["status"]).startswith("rejected") or h["status"] in ("failed", "exhausted")]
    exps = [e for e in ctx.reg.experiments(50) if e.get("status") not in ("done", "finished", "running")]
    text = f"{len(hyps)} hypothesis(es) were rejected" + (": " + "; ".join(f"{h['id']} ({h.get('family')}): {h.get('status_reason') or h['status']}" for h in hyps[:5]) if hyps else "") + "."
    if exps:
        text += f" {len(exps)} research run(s) did not finish normally: " + ", ".join(f"{e['id']} ({e.get('status')})" for e in exps[:5]) + "."
    return _ans(text, [{"label": f"hypothesis {h['id']}", "href": f"/factory?h={h['id']}", "kind": "hypothesis"} for h in hyps[:5]],
                ["research registry"], [], "failed")


def _brain(q, ctx):
    b = ctx.brain_status()
    ls = ctx.reg.lessons(5)
    text = (f"Learning Brain verdict: {b.get('verdict', 'unavailable')}. It holds {b.get('memory_episodes', '—')} memory episodes. "
            + ("KPI status: " + ", ".join(f"{k} {v}" for k, v in (b.get("kpis") or {}).items()) + ". " if b.get("kpis") else "")
            + ("Most recent lessons: " + " ".join(f"[{l['family']}] {l['text']}" for l in ls[:3]) if ls else "No lessons recorded yet.")
            + " Storing results is not learning; an improvement is only claimed when the out-of-sample KPI is met.")
    return _ans(text, [{"label": "AI Research Brain", "href": "/learning", "kind": "page"}], ["Learning Brain KPIs", "lessons"], [], "brain")


def _next(q, ctx):
    ls = ctx.reg.lessons(30)
    steps: dict[str, int] = {}
    for l in ls:
        if l.get("next_step"):
            steps[l["next_step"]] = steps.get(l["next_step"], 0) + 1
    gaps = [f[0] for f in ctx.features() if f[1] in ("NOT IMPLEMENTED", "BLOCKED")][:4]
    text = ("Next steps recorded by the research agents: " + "; ".join(f"{k} (×{v})" for k, v in sorted(steps.items(), key=lambda x: -x[1])[:4]) + ". "
            if steps else "No next steps have been recorded yet. ")
    sch = ctx.sch()
    if sch is not None:
        from nseql.screening import universe
        s = universe.summary(sch)
        cells = [c for c in s["map"] if c["with_evidence"]]
        if cells:
            best = max(cells, key=lambda c: (c["mean_net_bps"] or -1e9))
            text += (f"In the forward ledger the least-bad cell is {best['family']} on {best['timeframe']} ({best['mean_net_bps']:.1f} bps/trade over {best['trades']} trades); "
                     "more forward trades there would sharpen the evidence. ")
    if gaps:
        text += "Platform gaps that limit research: " + ", ".join(gaps) + ". "
    text += "These are suggestions from recorded evidence, not predictions."
    return _ans(text, [{"label": "Experiment Memory", "href": "/memory", "kind": "page"}], ["lessons", "forward ledger", "capability list"], [], "next")


def _market(q, ctx):
    sch = ctx.sch()
    if sch is None:
        return _ans("No market data is attached, so I cannot say what changed. Headlines on the Command Deck are for reading only; I do not analyse them.",
                    [], [], ["news headlines", "broker APIs"], "market")
    db = sch.store.db
    with sch.store._lock:
        last = db.execute("SELECT MAX(substr(ts,1,10)) FROM events WHERE late=0").fetchone()[0]
        rows = db.execute("SELECT symbol, MIN(ts), MAX(ts) FROM events WHERE substr(ts,1,10)=? AND late=0 GROUP BY symbol", (last,)).fetchall() if last else []
        moves = []
        for s, t0, t1 in rows:
            o = db.execute("SELECT open FROM events WHERE symbol=? AND ts=?", (s, t0)).fetchone()[0]
            c = db.execute("SELECT close FROM events WHERE symbol=? AND ts=?", (s, t1)).fetchone()[0]
            if o:
                moves.append((s, (c / o - 1) * 100))
    if not moves:
        return _ans("No bars have been received yet.", [], ["event log"], [], "market")
    moves.sort(key=lambda x: x[1])
    up = [m for m in moves[::-1] if m[1] > 0][:3]
    dn = [m for m in moves if m[1] < 0][:3]
    breadth = sum(m[1] > 0 for m in moves)
    text = (f"In the bars this lab received for {last} ({sch.provenance()['label']}): {breadth} of {len(moves)} symbols rose from first open to last close. "
            + ("Biggest gains: " + ", ".join(f"{s} {r:+.2f}%" for s, r in up) + ". " if up else "")
            + ("Biggest falls: " + ", ".join(f"{s} {r:+.2f}%" for s, r in dn) + ". " if dn else "")
            + "This covers only the watched symbols; I did not read news, indices outside the watchlist, or live exchange data.")
    return _ans(text, [{"label": "Market Intelligence", "href": "/market", "kind": "page"}], ["screening event log"], ["news", "exchange-wide data"], "market")

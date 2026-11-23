"""Strategy Universe: aggregations, paging and per-configuration dossiers over the screening registry.

Everything is computed from the scheduler's in-memory registry and engine state plus the screening store.
Nothing is estimated in the browser and nothing is invented: a configuration that has never traded has
no return, and fields that do not exist for it (historical backtest, walk-forward) are reported as
not run.

Validation stages (forward, in this paper ledger only):
  registered        never evaluated yet
  evaluated         evaluated at least once, no completed round trip
  forward_evidence  at least one completed round trip, fewer than the allocator's min_trades
  qualified         >= min_trades round trips and a positive shrunk mean net return
  gated             >= min_trades round trips and a non-positive shrunk mean (no capital under ranking)

A "configuration" is rule family x parameters x timeframe x stop x symbol. The rules (36 in 12 families)
are the independent strategies; parameter, timeframe, stop and symbol variants are not counted separately.
"""
from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd
from nseql.screening.registry import FAMILIES, HORIZON, TF_NAMES

STAGES = ("registered", "evaluated", "forward_evidence", "qualified", "gated")
SORTS = {"mean_net": "mean_net_bps", "trades": "trades", "realised": "realised_inr", "evaluations": "evaluations", "id": "id",
         "confidence": "confidence"}
_cache: dict = {}


def _rule_name(fam: str, p: dict) -> str:
    return {
        "cross": lambda: f"SMA {p['fast']}/{p['slow']} cross",
        "breakout": lambda: f"Channel breakout {p['entry']}/{p['exit']}",
        "rsi": lambda: f"RSI{p['n']} {p['buy']}/{p['sell']}",
        "zscore": lambda: f"Z-score reversion {p['z_entry']:+g}",
        "bollinger": lambda: f"Bollinger breakout {p['n']}/{p['k']}σ",
        "macd": lambda: f"MACD {p['fast']}-{p['slow']}-{p['signal']}",
        "momentum": lambda: f"Momentum {p['n']} bars > {p['min_return'] * 100:g}%",
        "volume": lambda: f"Volume breakout {p['n']} ({p['volume_x']}x)",
        "vwap": lambda: f"VWAP reversion {p['below_vwap'] * 100:+g}%",
        "orb": lambda: f"Opening range breakout {p['minutes']} min",
        "gap": lambda: f"Gap {'and go' if p['kind'] == 'gap-and-go' else 'fill'} {(p.get('min_gap') or p.get('max_gap')) * 100:+g}%",
        "prevday": lambda: "Previous-day high breakout",
    }[fam]()


_static_cache: dict = {}


def _static(r) -> dict:
    """Columns that never change for a registry (ids, names, families): built once, not every minute."""
    if _static_cache.get("key") != id(r):
        fam = np.array(FAMILIES, dtype=object)[r.fam]
        tf = np.array(TF_NAMES, dtype=object)[r.tf]
        hz = np.array([HORIZON[t] for t in TF_NAMES], dtype=object)[r.tf]
        _static_cache.update(key=id(r), cols={
            "idx": np.arange(len(r)), "id": np.array([str(x) for x in r.ids], dtype=object),
            "symbol": np.array(r.symbols, dtype=object)[r.sym], "family": fam, "timeframe": tf, "horizon": hz,
            "rule": np.array([_rule_name(f, p) for f, p in zip(fam, r.params)], dtype=object), "stop_pct": r.stop})
    return _static_cache["cols"]


def _lookup(ids: np.ndarray, d: dict, div: float = 1.0) -> np.ndarray:
    out = np.zeros(len(ids))
    if d:
        pos = pd.Index(ids).get_indexer(list(d))
        ok = pos >= 0
        out[pos[ok]] = np.array([v or 0 for v in d.values()], dtype=float)[ok] / div
    return out


def frame(sch) -> pd.DataFrame:
    """One row per registered configuration. Cached until the next processed minute."""
    key = (id(sch), sch.watermark, int(sch.engine.x.alloc.ev_n.sum()))
    if _cache.get("key") == key:
        return _cache["df"]
    r, e, a = sch.reg, sch.engine, sch.engine.x.alloc
    est = a.estimates(sch.engine.x.group)
    db = sch.store.db
    with sch.store._lock:
        real = dict(db.execute("SELECT config_id, SUM(realized_paise) FROM paper_orders WHERE side='SELL' GROUP BY config_id").fetchall())
        chg = dict(db.execute("SELECT config_id, SUM(charges_paise) FROM paper_orders GROUP BY config_id").fetchall())
        buys = dict(db.execute("SELECT config_id, COUNT(*) FROM signals WHERE decision='BUY' GROUP BY config_id").fetchall())
        acc = dict(db.execute("SELECT config_id, COUNT(*) FROM signals WHERE decision='BUY' AND alloc_status='accepted' GROUP BY config_id").fetchall())
    static = _static(r)
    ids = static["id"]
    n = a.ev_n
    mean = np.divide(a.ev_sum, n, out=np.full(len(n), np.nan), where=n > 0)
    stage = np.where(e.evals == 0, 0, np.where(n == 0, 1, np.where(n < a.cfg.min_trades, 2, np.where(est["mu"] > 0, 3, 4))))
    df = pd.DataFrame({
        **static,
        "evaluations": e.evals, "trades": n.astype(int), "wins": a.ev_w.astype(int),
        "mean_net_bps": mean * 1e4, "shrunk_bps": est["mu"] * 1e4, "confidence": est["confidence"],
        "stage": [STAGES[k] for k in stage], "position": np.where(e.pos == 1, "open", np.where(e.pend != 0, "working", "flat")),
        "realised_inr": _lookup(ids, real, 100), "charges_inr": _lookup(ids, chg, 100),
        "buy_signals": _lookup(ids, buys), "accepted": _lookup(ids, acc),
    })
    _cache.update(key=key, df=df)
    return df


def _clean(v):
    if isinstance(v, (np.floating, float)):
        return None if not np.isfinite(v) else round(float(v), 4)
    if isinstance(v, np.integer):
        return int(v)
    return v


def summary(sch, research_registry=None) -> dict:
    t0 = time.perf_counter()
    df = frame(sch)
    traded = df[df.trades > 0]
    g = df.groupby(["family", "timeframe"])
    cells = []
    for (f, t), x in g:
        tr = x[x.trades > 0]
        cells.append({"family": f, "timeframe": t, "horizon": HORIZON[t], "configs": len(x), "evaluated": int((x.evaluations > 0).sum()),
                      "with_evidence": len(tr), "qualified": int((x.stage == "qualified").sum()), "gated": int((x.stage == "gated").sum()),
                      "trades": int(x.trades.sum()),
                      "mean_net_bps": _clean((tr.mean_net_bps * tr.trades).sum() / tr.trades.sum()) if len(tr) else None,
                      "realised_inr": round(float(x.realised_inr.sum()), 2)})
    hist = None
    if len(traded) >= 5:
        v = traded.mean_net_bps.clip(-500, 500)
        counts, edges = np.histogram(v, bins=20, range=(-500, 500))
        hist = {"edges_bps": edges.tolist(), "counts": counts.tolist(), "clipped_at_bps": 500, "configs": len(traded)}
    fam_rules = df.groupby("family").rule.nunique().to_dict()
    research = None
    if research_registry is not None:
        hs = research_registry.hypotheses(100000)
        st: dict[str, int] = {}
        for h in hs:
            if h.get("origin") != "system":
                st[h["status"]] = st.get(h["status"], 0) + 1
        research = {"hypotheses_by_status": st, "backtests": len(research_registry.runs(limit=100000))}
    return {
        "registered": len(df), "symbols": int(df.symbol.nunique()), "families": len(fam_rules), "rules": int(sum(fam_rules.values())),
        "rules_by_family": fam_rules,
        "note": f"{sum(fam_rules.values())} rules in {len(fam_rules)} families are the independent strategies; the {len(df):,} configurations "
                "are their parameter, timeframe, stop and symbol variants.",
        "by_stage": {s: int((df.stage == s).sum()) for s in STAGES},
        "by_horizon": df.horizon.value_counts().to_dict(), "by_timeframe": df.timeframe.value_counts().to_dict(),
        "map": cells, "return_distribution": hist, "research": research,
        "funnel": [{"stage": "registered", "n": len(df)}, {"stage": "evaluated", "n": int((df.evaluations > 0).sum())},
                   {"stage": "signalled", "n": int((df.buy_signals > 0).sum())}, {"stage": "allocated capital", "n": int((df.accepted > 0).sum())},
                   {"stage": "completed a trade", "n": len(traded)}, {"stage": "qualified", "n": int((df.stage == "qualified").sum())}],
        "stages_help": __doc__.split("Validation stages")[1].split("A \"configuration\"")[0].strip(),
        "compute_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def page(sch, *, family="", timeframe="", horizon="", symbol="", stage="", q="", sort="trades", desc=True, limit=100, offset=0) -> dict:
    df = frame(sch)
    m = np.ones(len(df), dtype=bool)
    for col, v in (("family", family), ("timeframe", timeframe), ("horizon", horizon), ("symbol", symbol), ("stage", stage)):
        if v:
            m &= (df[col] == v).to_numpy()
    if q:
        ql = q.lower()
        m &= (df.id.str.startswith(ql) | df.symbol.str.lower().str.contains(ql, regex=False) | df.rule.str.lower().str.contains(ql, regex=False)).to_numpy()
    x = df[m]
    col = SORTS.get(sort, "trades")
    x = x.sort_values([col, "id"], ascending=[not desc, True], na_position="last")
    rows = [{k: _clean(v) for k, v in rec.items()} for rec in x.iloc[offset:offset + limit].to_dict("records")]
    return {"total": int(m.sum()), "limit": limit, "offset": offset, "sort": sort, "desc": desc, "rows": rows}


def dossier(sch, config_id: str, research_registry=None) -> dict | None:
    df = frame(sch)
    hit = df.index[df.id == config_id]
    if not len(hit):
        return None
    row = df.loc[hit[0]]
    i = int(row.idx)
    r, x, a = sch.reg, sch.engine.x, sch.engine.x.alloc
    d = r.describe(i)
    db = sch.store.db
    with sch.store._lock:
        orders = [dict(o) for o in db.execute("SELECT * FROM paper_orders WHERE config_id=? ORDER BY decided_at DESC LIMIT 60", (config_id,))]
        sigs = [dict(s) for s in db.execute("SELECT bar_ts, decision, price, reason, alloc_status, alloc_reason, alloc_score FROM signals "
                                            "WHERE config_id=? ORDER BY bar_ts DESC LIMIT 60", (config_id,))]
        alloc_counts = dict(db.execute("SELECT COALESCE(alloc_status,'not recorded'), COUNT(*) FROM signals WHERE config_id=? AND decision='BUY' GROUP BY 1",
                                       (config_id,)).fetchall())
    prov = sch.provenance()
    sells = [o for o in orders if o["side"] == "SELL" and o["status"] in ("filled", "partial")]
    gross = sum((o["realized_paise"] or 0) + (o["charges_paise"] or 0) for o in sells) / 100
    weak = []
    if prov.get("synthetic"):
        weak.append("All evidence comes from SYNTHETIC data; it says nothing about real markets.")
    elif not prov.get("live"):
        weak.append("Evidence comes from replayed historical data, not live forward trading.")
    if row.trades < a.cfg.min_trades:
        weak.append(f"Only {int(row.trades)} completed forward trade(s); at least {a.cfg.min_trades} are needed before the allocator treats the evidence as meaningful.")
    if row.trades and row.mean_net_bps is not None and row.mean_net_bps < 0:
        weak.append(f"Mean forward return after costs is negative ({row.mean_net_bps:.1f} bps per trade).")
    if row.charges_inr and gross > 0 and row.charges_inr > 0.5 * gross:
        weak.append(f"Charges (₹{row.charges_inr:,.0f}) consume more than half of gross profit.")
    refused = sum(v for k, v in alloc_counts.items() if k != "accepted")
    if refused and refused >= alloc_counts.get("accepted", 0):
        weak.append(f"{refused} of its BUY signals were not given capital (rejected or deferred by the portfolio).")
    if d["horizon"] == "intraday":
        weak.append("Intraday on 1-minute bars: fills are modelled from bar opens and volume, not from the order book.")
    same_rule = df[(df.family == row.family) & (df.timeframe == row.timeframe) & (df.rule == row.rule) & (df.id != config_id)]
    same_sym = df[(df.symbol == row.symbol) & (df.id != config_id)]
    pick = lambda z: [{k: _clean(rec[k]) for k in ("id", "symbol", "rule", "timeframe", "stage", "trades", "mean_net_bps")}
                      for rec in z.sort_values(["trades", "id"], ascending=[False, True]).head(6).to_dict("records")]
    notes = []
    if research_registry is not None:
        notes = [{"outcome": l["outcome"], "text": l["text"], "next_step": l.get("next_step"), "at": l.get("created_at")}
                 for l in research_registry.lessons(50, family=row.family)][:8]
    est = a.estimates(x.group)
    return {
        "config": {**d, "rule": row.rule, "definition": _definition(d)},
        "stage": row.stage, "position": row.position,
        "data_provenance": prov,
        "forward_evidence": {"trades": int(row.trades), "wins": int(row.wins), "mean_net_bps": _clean(row.mean_net_bps),
                             "shrunk_mean_bps": _clean(est["mu"][i] * 1e4), "win_probability": _clean(est["p_win"][i]),
                             "confidence": _clean(est["confidence"][i]), "qualified": bool(est["qualified"][i]),
                             "realised_after_charges_inr": _clean(row.realised_inr), "charges_inr": _clean(row.charges_inr),
                             "evaluations": int(row.evaluations), "buy_signals_by_allocation": alloc_counts},
        "historical_backtest": {"status": "not run", "how": f"Backtesting Studio: strategy screened_config with config id {config_id}"},
        "walk_forward": {"status": "not run"}, "out_of_sample": {"status": "not run",
                                                                "note": "forward paper evidence above is out-of-sample by construction (decided before each bar)"},
        "costs": {"product": "MIS (intraday)" if d["horizon"] == "intraday" else "CNC (delivery)", "schedule": "dated NSE charge schedule",
                  "charges_paid_inr": _clean(row.charges_inr)},
        "execution": {"model": x.mode, "settings": x.cfg.to_dict() if x.cfg else None, "portfolio_rules": x.rules.to_dict() if x.rules else None,
                      "allocation": x.rules.allocation if x.rules else None},
        "paper_orders": orders, "recent_signals": sigs,
        "related": {"same_rule_other_symbols": pick(same_rule), "same_symbol_other_rules": pick(same_sym)},
        "weaknesses": weak, "ai_research_notes": notes,
        "version_history": [{"version": 1, "id": config_id, "note": "content-addressed id (hash of family, parameters, timeframe, symbol, stop); "
                             "the definition cannot change without the id changing", "registered_for_run": (sch.store.path.parent.name)}],
    }


def _definition(d: dict) -> str:
    p, f = d["params"], d["family"]
    txt = {
        "cross": lambda: f"Buy when SMA{p['fast']} closes above SMA{p['slow']}; exit when it is no longer above.",
        "breakout": lambda: f"Buy when the close exceeds the prior {p['entry']}-bar high; exit when it falls below the prior {p['exit']}-bar low.",
        "rsi": lambda: f"Buy when RSI{p['n']} falls below {p['buy']}; exit when it rises above {p['sell']}.",
        "zscore": lambda: f"Buy when the close is more than {abs(p['z_entry']):g} standard deviations below its 20-bar mean; exit when it is back above the mean.",
        "bollinger": lambda: "Buy when the close breaks above the upper Bollinger band (20 bars, 2 standard deviations); exit below the 20-bar mean.",
        "macd": lambda: f"Buy when MACD ({p['fast']}-{p['slow']}) crosses above its {p['signal']}-bar signal line; exit when it falls below it.",
        "momentum": lambda: f"Buy when the {p['n']}-bar return exceeds {p['min_return'] * 100:g}%; exit when it turns negative.",
        "volume": lambda: f"Buy on a new {p['n']}-bar closing high with volume above {p['volume_x']}x the 20-bar average; exit below the 10-bar low.",
        "vwap": lambda: f"Buy when the close is more than {abs(p['below_vwap']) * 100:g}% below the session VWAP; exit when it is back above VWAP.",
        "orb": lambda: f"Buy when the close breaks above the high of the first {p['minutes']} minutes; exit below the middle of that range.",
        "gap": lambda: (f"Buy after a gap up of more than {p['min_gap'] * 100:g}% while trading above the day's open; exit below the open."
                        if p["kind"] == "gap-and-go" else
                        f"After a gap down of more than {abs(p['max_gap']) * 100:g}%, buy while still that far below yesterday's close; exit when the gap is filled."),
        "prevday": lambda: f"Buy when the close breaks above yesterday's high; exit {abs(p['exit_below']) * 100:g}% back below it.",
    }[f]()
    return (f"{txt} Evaluated on {d['timeframe']} bars of {d['symbol']} ({d['horizon']}), long only, stop {d['stop_pct'] * 100:.1f}% below the fill"
            + ("; intraday positions square off from 15:15, no new entries after 15:00." if d["horizon"] == "intraday" else "."))


def as_json(obj) -> str:
    return json.dumps(obj, default=str)

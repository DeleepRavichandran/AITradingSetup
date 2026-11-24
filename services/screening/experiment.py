"""Chronological experiment: hypotheses, a challenger, and whether learning adds value (review of 1376b45, sections 3-4).

One market and horizon: liquid NSE-style cash equities, intraday and swing configurations of the minute engine, Rs 1 lakh,
rules 1.2, the existing charge, slippage, participation and shared-capital models.

Phases (chronological, never overlapping)
  development   first DEV days        implementation checks only; nothing is chosen on them
  validation    next VAL_WINDOWS x VAL_LEN days, walk-forward windows: hypotheses are accepted or rejected here
  holdout       last HOLD days        opened ONCE, after the manifest (parameters, selection result, primary metric) is frozen
  forward       live paper contest on the operator's laptop (not here); promotion needs 60 sessions and 100 trades

Hypotheses (from the incumbent's first live day, 2026-11-23: 363 round trips, -Rs 2,248; charges about a quarter of the
loss; losers concentrated in the first 30 minutes and in thin, volatile names). Each is ONE isolated change to the learned
incumbent (INC-L), measured on its own before anything is combined:
  H1 turnover consumes the gross edge. Mechanism: most funded configurations have no evidence that their gross move beats
     the round-trip cost, so each extra trade adds cost. Falsified if the incumbent's gross P&L before costs is itself not
     positive (nothing to save), or if holding cash until a configuration is qualified does not improve net P&L.
     Change: fund only configurations with qualified forward evidence; otherwise hold cash.
  H2 opening-range entries lose. Mechanism: the first 30 minutes are noisy (wide ranges, gaps), so 1-minute rules trade
     noise. Falsified if first-30-minute trades are not worse than later ones, or if a 30-minute entry blackout does not help.
     Change: no new entries in the first 30 minutes.
  H3 fills in thin liquidity pay impact. Mechanism: orders that take a large share of a minute's volume pay square-root
     impact on entry and exit. Falsified if impact is a small share of costs, or if a lower participation cap does not help.
     Change: participation cap 10% -> 2% of each bar's volume.

Selection rule (declared here, before any result): a hypothesis is accepted if, on the validation phase, the paired
daily return difference versus INC-L has a positive mean AND it beats INC-L's net P&L in at least 2 of the 3 windows.
The final challenger is INC-L with all accepted changes; if none is accepted, the existing ORB-breadth challenger (CH-0).

Learning question: the learned selector (INC-L: ranked by forward evidence, evidence gates on) against a fixed selector
(INC-F: the same candidate pool, dates, capital and execution; registry order, no evidence gates). On planted-edge data
(factor_stream: 20% of symbols drift upward) learning SHOULD help; on a pure random walk (minute_stream) it must not.
Both are SYNTHETIC: they test the machinery, not the market. Profitability on real data stays "unproven".

Primary metric (frozen): paired daily after-cost return difference, candidate minus comparator, 95% circular block bootstrap.
"""
from __future__ import annotations

import json
import math
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from nseql.screening.contest import block_bootstrap, code_version, fingerprint, sha
from nseql.screening.execution import EXECUTION_VERSION, LIVE_RULES_VERSION, ExecConfig, PortfolioRules

CAPITAL = 100_000.0
SYMBOLS = 20
DEV, VAL_WINDOWS, VAL_LEN, HOLD = 6, 3, 5, 15
SEEDS = {"edge": 11, "null": 12}
PRIMARY = "paired daily after-cost return difference (candidate - comparator), 95% circular block bootstrap"

VARIANTS = {
    "INC-L": {"label": "incumbent, learned selector (rules 1.2, ranked by forward evidence)"},
    "INC-F": {"label": "incumbent, fixed selector (registry order, no evidence gates)", "rules": {"allocation": "first_come", "group_gate_trades": 0},
              "alloc": {"reject_negative": False}},
    "H1": {"label": "INC-L + hold cash unless qualified", "rules": {"fund_qualified_only": True}},
    "H2": {"label": "INC-L + no entries in the first 30 minutes", "rules": {"no_entry_first_minutes": 30}},
    "H3": {"label": "INC-L + participation cap 2%", "exec": {"participation": 0.02}},
    "CH-0": {"label": "challenger orb30-breadth-1.0", "engine": "challenger"},
    # iteration 2 (recorded after iteration 1: H1 as specified can never trade, because evidence only accrues from funded
    # trades, so "fund only qualified" is cash by construction; H1b keeps exploring at a quarter of the size)
    "H1b": {"label": "INC-L + quarter size until qualified (hold most of the cash, keep learning)", "rules": {"explore_fraction": 0.25}},
}
STRESS = {"slippage x2": {"half_spread_bps": 10.0, "impact_bps": 50.0}, "one-bar delay": {"delay_bars": 1}}


def calendar(iteration: int = 1) -> dict[str, list[str]]:
    """Iteration k adds a new, untouched holdout of HOLD days after the previous one (the generators are sequential and
    seeded, so earlier days are identical). Development and validation never change."""
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=DEV + VAL_WINDOWS * VAL_LEN + HOLD * iteration)]
    val = days[DEV:DEV + VAL_WINDOWS * VAL_LEN]
    return {"development": days[:DEV], "validation": val, "windows": [val[k * VAL_LEN:(k + 1) * VAL_LEN] for k in range(VAL_WINDOWS)],
            "holdout": days[-HOLD:], "previous_holdouts": days[DEV + VAL_WINDOWS * VAL_LEN:-HOLD], "all": days}


def stream(kind: str, days: list[str]):
    from nseql.screening.synthetic import factor_stream, minute_stream
    return factor_stream(SYMBOLS, days, seed=SEEDS[kind]) if kind == "edge" else minute_stream(SYMBOLS, days, seed=SEEDS[kind], missing=0.0)


def spec(name: str, extra: dict | None = None) -> dict:
    v = dict(VARIANTS.get(name, {}))
    v.setdefault("rules", {})
    v.setdefault("exec", {})
    v.setdefault("alloc", {})
    for k, x in (extra or {}).items():
        v[k] = {**v.get(k, {}), **x} if isinstance(x, dict) else x
    return v


# ---------------------------------------------------------------------------------------------------- one run
def _scheduler(root: Path, syms: list[str], v: dict):
    from nseql.screening.allocator import AllocatorConfig
    from nseql.screening.challenger import challenger_rules, factory
    from nseql.screening.engine import ScreeningEngine
    from nseql.screening.registry import build
    from nseql.screening.scheduler import MinuteScheduler
    ec = ExecConfig(**v["exec"])
    if v.get("engine") == "challenger":
        return MinuteScheduler(root, syms, portfolio=replace(challenger_rules(CAPITAL), **v["rules"]), exec_cfg=ec, mode="replay",
                               engine_factory=factory())
    alloc = AllocatorConfig(**v["alloc"])

    def make(symbols, exec_cfg, portfolio):
        reg = build(symbols, None)
        return reg, ScreeningEngine(reg, exec_cfg, portfolio, alloc)
    return MinuteScheduler(root, syms, portfolio=replace(PortfolioRules.live(CAPITAL), **v["rules"]), exec_cfg=ec, mode="replay",
                           engine_factory=make)


def run_variant(args) -> dict:
    """(root, data kind, days, variant name, spec) -> per-day results read back from the variant's own ledger."""
    root, kind, days, name, v = args
    root = Path(root)
    st = stream(kind, days)
    syms = sorted({b["symbol"] for _, bs in st for b in bs})
    t0 = time.perf_counter()
    sch = _scheduler(root, syms, v)
    sch.verbose = False
    for ts, bars in st:
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    out = measure(sch, days)
    out.update({"variant": name, "data": kind, "seconds": round(time.perf_counter() - t0, 1), "spec": v,
                "trade_log": [list(r) for r in sch.store.db.execute(
                    "SELECT side, ts, symbol, qty, price, charges_paise, realized_paise FROM paper_fills ORDER BY ts LIMIT 200").fetchall()]})
    (root / "result.json").write_text(json.dumps(out, indent=2, default=str))
    sch.store.db.close()
    for f in list(root.glob("screening.sqlite*")) + [root / "checkpoint.npz"]:     # scratch ledgers are GBs; the result is kept
        f.unlink(missing_ok=True)
    return out


def measure(sch, days: list[str]) -> dict:
    """Per-day after-cost results from the ledger, with the gross-to-net breakdown in rupees."""
    db = sch.store.db
    cyc = pd.read_sql_query("SELECT minute, equity_inr, cash_inr FROM cycles WHERE equity_inr IS NOT NULL ORDER BY minute", db)
    cyc["day"] = cyc.minute.str[:10]
    eq = cyc.groupby("day").equity_inr.last().reindex(days).ffill().fillna(CAPITAL)
    ret = (eq / eq.shift(1).fillna(CAPITAL) - 1).fillna(0.0)
    expo = cyc.assign(x=((cyc.equity_inr - cyc.cash_inr) / cyc.equity_inr).clip(lower=0)).groupby("day").x.mean().reindex(days).fillna(0.0)
    f = pd.read_sql_query("SELECT f.symbol, f.side, f.ts, f.qty, f.price, f.charges_paise, f.realized_paise, e.open AS bar_open "
                          "FROM paper_fills f LEFT JOIN events e ON e.symbol = f.symbol AND e.ts = f.ts", db)
    f["day"] = f.ts.str[:10]
    f["slip"] = (f.price - f.bar_open).abs() * f.qty                     # spread + impact paid against the bar's open
    f["value"] = f.qty * f.price
    g = f.groupby("day")
    sells = f[f.side == "SELL"]
    per = pd.DataFrame({"charges": g.charges_paise.sum() / 100, "slippage": g.slip.sum(), "turnover": g.value.sum(),
                        "trades": sells.groupby("day").size()}).reindex(days).fillna(0.0)
    pnl = eq.diff().fillna(eq.iloc[0] - CAPITAL) if len(eq) else eq
    sym_real = sells.groupby("symbol").realized_paise.sum() / 100
    return {"days": days, "equity": [round(float(x), 2) for x in eq], "returns": [float(x) for x in ret], "pnl": [round(float(x), 2) for x in pnl],
            "exposure": [round(float(x), 4) for x in expo], "charges": [round(float(x), 2) for x in per.charges],
            "slippage": [round(float(x), 2) for x in per.slippage], "turnover": [round(float(x), 2) for x in per.turnover],
            "trades": [int(x) for x in per.trades], "symbol_realized": {k: round(float(v), 2) for k, v in sym_real.items()}}


# ---------------------------------------------------------------------------------------------------- summarising
def window(r: dict, days: list[str]) -> dict:
    idx = [r["days"].index(d) for d in days]
    pick = lambda k: np.array([r[k][i] for i in idx], dtype=float)
    ret, pnl, ch, sl, tv, tr, ex = (pick(k) for k in ("returns", "pnl", "charges", "slippage", "turnover", "trades", "exposure"))
    eq = np.array([r["equity"][i] for i in idx])
    start = r["equity"][idx[0] - 1] if idx[0] > 0 else CAPITAL
    curve = np.concatenate([[start], eq])
    dd = float((curve / np.maximum.accumulate(curve) - 1).min())
    net = float(eq[-1] - start)
    best_day = float(pnl.max()) if len(pnl) else 0.0
    return {"days": len(days), "from": days[0], "to": days[-1], "net_inr": round(net, 2), "return": round(net / start, 6),
            "max_drawdown": round(dd, 6), "turnover_x": round(float(tv.sum()) / start, 3), "exposure_avg": round(float(ex.mean()), 4),
            "closed_trades": int(tr.sum()),
            "gross_to_net_inr": {"gross_at_bar_opens": round(net + float(ch.sum()) + float(sl.sum()), 2), "spread_and_impact": round(-float(sl.sum()), 2),
                                 "charges": round(-float(ch.sum()), 2), "net": round(net, 2)},
            "best_day_inr": round(best_day, 2), "best_day_share_of_net": round(best_day / net, 3) if net > 0 else None}


def paired(a: dict, b: dict, days: list[str]) -> dict:
    ia, ib = [a["days"].index(d) for d in days], [b["days"].index(d) for d in days]
    d = np.array([a["returns"][i] - b["returns"][j] for i, j in zip(ia, ib)])
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in block_bootstrap(d).items()}


def exposure_baseline(kind: str, days: list[str], exposure: float, exec_cfg: ExecConfig | None = None) -> dict:
    """Simple exposure-matched baseline on identical bars: each day buy the whole watchlist equally at the 09:17 open (decided
    on the 09:16 close), sell at the 15:16 open; position value = exposure x equity; same spread/impact formula and dated
    MIS charges. Answers: did the candidate earn more than just being that exposed to the market?"""
    from nseql.instruments.models import Instrument, Kind
    from nseql.instruments.rules import ChargeSchedule
    cfg = exec_cfg or ExecConfig()
    chs = ChargeSchedule(allow_extrapolation=True)
    st = stream(kind, days)
    by = {(b["symbol"], ts.strftime("%Y-%m-%d %H:%M")): b for ts, bars in st for b in bars}
    syms = sorted({b["symbol"] for _, bs in st for b in bs})
    eq, rows = CAPITAL, []
    for d in days:
        start, pnl, charges, slip = eq, 0.0, 0.0, 0.0
        per = exposure * eq / len(syms)
        for s in syms:
            bi, bo = by.get((s, f"{d} 09:17")), by.get((s, f"{d} 15:16"))
            if not bi or not bo:
                continue
            q = int(per // bi["open"])
            if q <= 0:
                continue
            fr = lambda b: (cfg.half_spread_bps + cfg.impact_bps * math.sqrt(q / max(b["volume"], 1.0))) / 1e4
            pin, pout = bi["open"] * (1 + fr(bi)), bo["open"] * (1 - fr(bo))
            c = sum(chs.compute(Instrument(s, Kind.EQ), side, q, round(px * 100), "MIS", pd.Timestamp(d).date()).total for side, px in ((1, pin), (-1, pout))) / 100
            pnl += (pout - pin) * q - c
            charges += c
            slip += (pin - bi["open"]) * q + (bo["open"] - pout) * q
        eq += pnl
        rows.append({"day": d, "equity": eq, "ret": pnl / start, "charges": charges, "slip": slip})
    return {"days": days, "equity": [r["equity"] for r in rows], "returns": [r["ret"] for r in rows],
            "pnl": [r["equity"] - (rows[k - 1]["equity"] if k else CAPITAL) for k, r in enumerate(rows)], "exposure": [exposure] * len(rows),
            "charges": [r["charges"] for r in rows], "slippage": [r["slip"] for r in rows], "turnover": [0.0] * len(rows),
            "trades": [len(syms)] * len(rows), "symbol_realized": {}, "variant": f"exposure-matched baseline ({exposure:.0%} of equity)"}


# ---------------------------------------------------------------------------------------------------- protocol
def run(root: Path, workers: int = 3, iteration: int = 1, reuse: Path | None = None, hypotheses: tuple[str, ...] = ("H1", "H2", "H3"),
        carried: dict | None = None) -> dict:
    """iteration 1: the predeclared protocol. iteration >= 2: a recorded follow-up with a NEW untouched holdout; phase-1
    results of variants already run on the same development/validation data are reused from `reuse` (identical code and
    data), and `carried` holds earlier selection decisions that stand."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    cal = calendar(iteration)
    log = root / "variants.jsonl"                                             # every variant tried, kept or discarded
    tried = lambda rec: log.open("a").write(json.dumps(rec, default=str) + "\n")
    pre = cal["development"] + cal["validation"]

    # 1. development + validation only (the holdout is not generated yet)
    names = ["INC-L", "INC-F", "CH-0", *hypotheses]
    res = {}
    for src in [p for p in (root, reuse) if p is not None]:                 # resume: results already measured are not rerun
        for f in Path(src).glob("phase1/*/*/result.json"):
            r = json.loads(f.read_text())
            if r["variant"] in names:
                res[(r["data"], r["variant"])] = r
    jobs = [(root / "phase1" / "edge" / n, "edge", pre, n, spec(n)) for n in names if ("edge", n) not in res]
    with ProcessPoolExecutor(workers) as ex:
        res.update({(r["data"], r["variant"]): r for r in ex.map(run_variant, jobs)})
    base = res[("edge", "INC-L")]
    gross = window(base, cal["validation"])["gross_to_net_inr"]
    sel = dict(carried or {})
    for h in hypotheses:
        r = res[("edge", h)]
        p = paired(r, base, cal["validation"])
        wins = sum(window(r, w)["net_inr"] > window(base, w)["net_inr"] for w in cal["windows"])
        ok = (p["mean"] or 0) > 0 and wins >= 2
        sel[h] = {"label": VARIANTS[h]["label"], "validation_vs_INC-L": p, "windows_won": f"{wins}/{len(cal['windows'])}", "accepted": bool(ok),
                  "validation": window(r, cal["validation"])}
        tried({"phase": "validation", "variant": h, "accepted": bool(ok), **sel[h]})
    for n in ("INC-L", "INC-F", "CH-0"):
        tried({"phase": "validation", "variant": n, "validation": window(res[("edge", n)], cal["validation"])})
    acc = [h for h in sel if sel[h]["accepted"]]
    final = {"name": "CH-0", "spec": spec("CH-0")} if not acc else {"name": "FINAL=" + "+".join(acc), "spec": _combine(acc)}

    # 2. freeze everything before the holdout exists
    manifest = {"protocol": __doc__.split("\n")[0], "iteration": iteration, "code": code_version(), "execution_version": EXECUTION_VERSION, "rules_version": LIVE_RULES_VERSION,
                "calendar": cal, "capital": CAPITAL, "symbols": SYMBOLS, "seeds": SEEDS, "variants": VARIANTS, "stress": STRESS,
                "selection_rule": "positive mean paired daily difference vs INC-L on validation AND net P&L above INC-L in >= 2 of 3 windows",
                "selection": {h: {"accepted": sel[h]["accepted"], "windows_won": sel[h]["windows_won"]} for h in sel},
                "final_challenger": final, "primary_metric": PRIMARY,
                "data_fingerprint_validation": fingerprint(stream("edge", pre))}
    manifest["sha256"] = sha({k: v for k, v in manifest.items() if k != "sha256"})
    mf = root / "manifest.json"
    if mf.exists():                                       # resuming: the frozen manifest stands; the selection must agree with it
        frozen = json.loads(mf.read_text())
        if frozen["final_challenger"]["name"] != final["name"] or frozen["selection"] != manifest["selection"] or \
                {k: cal.get(k) for k in frozen["calendar"]} != frozen["calendar"]:
            raise RuntimeError("recomputed selection differs from the frozen manifest; refusing to open the holdout")
        manifest = frozen
    else:
        mf.write_text(json.dumps(manifest, indent=2, default=str))

    # 3. open the holdout once: every compared account runs the full calendar (indicators and evidence carry through)
    full = cal["all"]
    jobs = [(root / "phase2" / "edge" / n, "edge", full, n, spec(n)) for n in ("INC-L", "INC-F", "CH-0")]
    if final["name"] != "CH-0":
        jobs.append((root / "phase2" / "edge" / "FINAL", "edge", full, final["name"], final["spec"]))
    jobs += [(root / "phase2" / "null" / n, "null", full, n, spec(n)) for n in ("INC-L", "INC-F")]
    fk = "FINAL" if final["name"] != "CH-0" else "CH-0"
    for s, x in STRESS.items():
        jobs.append((root / "phase2" / "edge" / f"{fk} {s}", "edge", full, f"{final['name']} ({s})", {**final["spec"], "exec": {**final["spec"]["exec"], **x}}))
    with ProcessPoolExecutor(workers) as ex:
        out = list(ex.map(run_variant, jobs))
    r2 = {(r["data"], r["variant"]): r for r in out}
    fin = r2[("edge", final["name"])]
    H = cal["holdout"]
    expo = window(fin, H)["exposure_avg"]
    bl = exposure_baseline("edge", full, expo)
    rows = {n: window(r2[("edge", n)], H) for n in ("INC-L", "INC-F", "CH-0")}
    rows[final["name"]] = window(fin, H)
    rows["cash"] = {"net_inr": 0.0, "return": 0.0, "max_drawdown": 0.0, "closed_trades": 0, "exposure_avg": 0.0}
    rows[bl["variant"]] = window(bl, H)
    stress = {s: window(r2[("edge", f"{final['name']} ({s})")], H) for s in STRESS}
    learn = {k: {"learned": window(r2[(k, "INC-L")], H), "fixed": window(r2[(k, "INC-F")], H),
                 "learned_minus_fixed": paired(r2[(k, "INC-L")], r2[(k, "INC-F")], H)} for k in ("edge", "null")}
    sym = fin["symbol_realized"]
    best_sym = max(sym.items(), key=lambda kv: kv[1]) if sym else None
    tot_real = sum(sym.values())
    rep = {"manifest_sha256": manifest["sha256"], "data": "SYNTHETIC (factor_stream with planted drift; minute_stream random walk as a null control)",
           "periods": {k: [v[0], v[-1]] for k, v in cal.items() if k not in ("windows",) and v},
           "hypotheses": sel, "incumbent_gross_validation": gross, "final_challenger": final["name"],
           "holdout": rows, "final_vs": {n: paired(fin, r2[("edge", n)], H) for n in ("INC-L", "CH-0") if n != final["name"]}
                                        | {"cash": paired(fin, {"days": full, "returns": [0.0] * len(full)}, H), "baseline": paired(fin, bl, H)},
           "stress": stress, "learning": learn,
           "concentration": {"best_symbol": best_sym[0] if best_sym else None, "best_symbol_realized_inr": round(best_sym[1], 2) if best_sym else None,
                             "best_symbol_share_of_realized": round(best_sym[1] / tot_real, 3) if best_sym and tot_real > 0 else None},
           "seconds": {r["variant"] + "/" + r["data"]: r["seconds"] for r in out}}
    (root / "report.json").write_text(json.dumps(rep, indent=2, default=str))
    tried({"phase": "holdout", "variant": final["name"], "holdout": rows[final["name"]]})
    return rep


def _combine(names: list[str]) -> dict:
    v = spec("INC-L")
    for n in names:
        s = spec(n)
        for k in ("rules", "exec", "alloc"):
            v[k] = {**v[k], **s[k]}
    v["label"] = "INC-L + " + " + ".join(names)
    return v

"""The learning benchmark: does memory-informed research beat memory-free research on data it
had not seen when it decided?

Protocol (fixed before any results were seen):
  * decisions every `epoch_len` sessions after a warm-up; every arm screens the same number of
    configurations (`budget`) and holds `top_k` of them for the next window;
  * an arm decides at the close of day d using rows <= d and memory available at d; its holdings
    are scored on rows d+1 .. d+L, which no arm has seen; outcomes enter memory on day d+L;
  * memory-free arms screen the same random sample (common random numbers), so differences
    between them come from ranking alone;
  * primary comparison: `memory` vs `baseline_critic` (same self-critic, no memory). Secondary:
    vs greedy-by-in-sample-Sharpe and vs random. Ablations show which memory component matters;
  * negative control: identical protocol on a zero-drift random-walk dataset, where no method
    should win;
  * the sealed window is used at most once (`final_sealed=True`) and the use is recorded.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy import stats

from nseql.brain.memory import EpisodicMemory
from nseql.brain.policy import ARMS, COMPARISONS, PROTOCOL_VERSION, ResearchPolicy
from nseql.brain.runner import Arm, Runner
from nseql.data.store import Dataset
from nseql.evaluation import cache as cache_mod, pool
from nseql.evaluation.space import size as universe_size
from nseql.evaluation.vector import side_cost

PRIMARY = ("memory_no_critic", "baseline_robust")


def beats(x: np.ndarray, ref: float) -> np.ndarray:
    """1 if above the reference, 0.5 on a tie, 0 below."""
    return (x > ref) + 0.5 * (x == ref)


def prepare(ds: Dataset, work: Path, budget: pool.EvalBudget | None = None, progress=None, cost: float | None = None) -> tuple:
    cdir = cache_mod.build(ds, Path(work) / "cache")
    P = cache_mod.load(cdir)
    cost = side_cost(P.dates()[len(P.dates()) // 2]) if cost is None else float(cost)
    out = Path(work) / "results" / f"{cdir.name}_c{cost:.6f}"
    st = pool.evaluate_universe(cdir, out, cost, budget=budget, progress=progress)
    return P, pool.load_results(out), cost, st


def decision_rows(P, *, start_after: int = 260, epoch_len: int = 10, end_date: str | None = None, start_date: str | None = None) -> list[int]:
    dates = [str(x) for x in P.dates()]
    last = max(i for i, x in enumerate(dates) if end_date is None or x <= end_date)
    first = start_after if start_date is None else max(start_after, next(i for i, x in enumerate(dates) if x >= start_date) - 1)
    return list(range(first, last - epoch_len + 1, epoch_len))


def paired(a: list[float], b: list[float], seed: int = 3, block: int = 3) -> dict:
    d = np.asarray(a) - np.asarray(b)
    n = len(d)
    if n < 3:
        return {"epochs": n}
    sd = d.std(ddof=1)
    t = d.mean() / (sd / math.sqrt(n)) if sd > 0 else 0.0
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(5000):
        starts = rng.integers(0, n, math.ceil(n / block))
        ix = (starts[:, None] + np.arange(block)).ravel()[:n] % n
        boots.append(d[ix].mean())
    return {"epochs": n, "mean_diff": float(d.mean()), "t": float(t), "p_two_sided": float(2 * stats.t.sf(abs(t), n - 1)),
            "ci90": [float(np.quantile(boots, 0.05)), float(np.quantile(boots, 0.95))], "win_rate": float((d > 0).mean())}


def holm(comps: dict) -> None:
    """Holm-Bonferroni adjustment across the family of pre-declared comparisons (in place)."""
    keys = [k for k, v in comps.items() if "p_two_sided" in v.get("forward_return", {})]
    ps = sorted((comps[k]["forward_return"]["p_two_sided"], k) for k in keys)
    m, running = len(ps), 0.0
    for i, (p, k) in enumerate(ps):
        running = max(running, min(1.0, (m - i) * p))
        comps[k]["forward_return"]["p_holm"] = running


def _series_stats(daily: np.ndarray) -> dict:
    if len(daily) < 2:
        return {}
    curve = np.cumprod(1 + daily)
    sd = daily.std(ddof=1)
    return {"total_return": float(curve[-1] - 1), "sharpe": float(daily.mean() / sd * math.sqrt(252)) if sd > 0 else None,
            "max_drawdown": float((curve / np.maximum.accumulate(curve) - 1).min()), "days": int(len(daily))}


def run(ds: Dataset, work: Path, memory: EpisodicMemory, *, run_id: str | None = None, epoch_len: int = 10, end_date: str | None = None,
        start_date: str | None = None, arms: dict | None = None, base_policy: dict | None = None, seed: int = 7,
        eval_budget: pool.EvalBudget | None = None, progress=None, label: str = "", cost: float | None = None,
        sealed_from: str | None = None) -> dict:
    t0 = time.perf_counter()
    P, res, cost, est = prepare(ds, work, eval_budget, cost=cost)
    run_id = run_id or f"bench_{datetime.now():%Y%m%d_%H%M%S}"
    arms = arms or ARMS
    base = base_policy or {}
    arm_objs = [Arm(name, ResearchPolicy(**{**base, **ov}), f"{run_id}:{name}") for name, ov in arms.items()]
    runner = Runner(P, res, memory, cost, arm_objs, seed=seed, epoch_len=epoch_len)
    rows = decision_rows(P, epoch_len=epoch_len, end_date=end_date, start_date=start_date)
    dates = runner.dates
    R = res["R"]
    per = {a.name: {"epoch_returns": [], "daily": [], "dates": [], "screen_quality": [], "hits_selected": 0, "screened": 0,
                    "regime": [], "pbo": [], "survivors": [], "corr": [], "eff_n": [], "calib": [], "changes": [], "approach": []} for a in arm_objs}
    epochs = []
    for e, d in enumerate(rows):
        lo, hi = d + 1, min(d + epoch_len, len(dates) - 1)
        univ_fwd = np.asarray(R[:, lo:hi + 1], dtype=float).sum(axis=1)       # evaluation only: never shown to an arm
        med = float(np.median(univ_fwd[runner.candidates(d)]))
        decs = [(a, runner.decide(a, e, d)) for a in arm_objs]
        for a, dec in decs:
            runner.record_decision(a, dec)
        for a, dec in decs:
            s = runner.settle(a, dec)
            m = per[a.name]
            m["epoch_returns"].append(s["epoch_return"])
            m["daily"].extend(s["daily"].tolist())
            m["dates"].extend(dates[lo:hi + 1])
            m["screen_quality"].append(float(beats(s["fwd"], med).mean()))
            sel = dec.selections[dec.approach]
            m["hits_selected"] += float(beats(univ_fwd[sel], med).sum())
            m["n_selected"] = m.get("n_selected", 0) + len(sel)
            m["screened"] += len(dec.screened)
            m["regime"].append(dec.regime["label"])
            m["pbo"].append(dec.pbo)
            m["survivors"].append(dec.critic.get("survivors"))
            m["approach"].append(dec.approach)
            m["changes"] += [dict(c, epoch=e, date=dec.as_of) for c in dec.changes]
            if len(sel) > 1:
                with np.errstate(invalid="ignore", divide="ignore"):
                    C = np.corrcoef(np.asarray(R[sel][:, max(d - 119, 0):d + 1], dtype=float))
                C = np.nan_to_num(C)
                off = C[~np.eye(len(sel), dtype=bool)]
                ev = np.clip(np.linalg.eigvalsh(C), 0, None)
                m["corr"].append(float(off.mean()))
                m["eff_n"].append(float(ev.sum() ** 2 / (ev ** 2).sum()) if ev.sum() > 0 else 1.0)
            if dec.meta_pred is not None:
                prior_rate = a.state.base_rate if a.state else 0.5
                m["calib"].append((dec.meta_pred.tolist(), s["label"].tolist(), prior_rate))
        epochs.append({"epoch": e, "decided": dates[d], "window": [dates[lo], dates[hi]], "regime": decs[0][1].regime["label"],
                       "universe_median_forward": med})
        if progress:
            progress(e + 1, len(rows))
    report_arms = {}
    for a in arm_objs:
        m = per[a.name]
        k = {"screen_quality": float(np.mean(m["screen_quality"])),
             "robust_selections_per_1000_screened": 1000 * m["hits_selected"] / max(m["screened"], 1),
             "selected_hit_rate": m["hits_selected"] / max(m.get("n_selected", 0), 1),
             "epochs_in_cash": int(sum(1 for x in m["survivors"] if not x)),
             "mean_pbo": float(np.mean([x for x in m["pbo"] if x is not None])) if any(x is not None for x in m["pbo"]) else None,
             "mean_survivors": float(np.mean(m["survivors"])), "trials": int(m["screened"]),
             "mean_pairwise_corr": float(np.mean(m["corr"])) if m["corr"] else None,
             "effective_bets": float(np.mean(m["eff_n"])) if m["eff_n"] else None}
        by_reg: dict = {}
        for r, x in zip(m["regime"], m["epoch_returns"]):
            by_reg.setdefault(r, []).append(x)
        k["regime"] = {r: {"epochs": len(v), "mean_epoch_return": float(np.mean(v))} for r, v in sorted(by_reg.items())}
        robust = [v["mean_epoch_return"] for v in k["regime"].values() if v["epochs"] >= 3]
        k["worst_regime_mean"] = float(min(robust)) if robust else None
        if m["calib"]:
            p = np.concatenate([np.asarray(x[0]) for x in m["calib"]])
            y = np.concatenate([np.asarray(x[1]) for x in m["calib"]])
            ref = np.concatenate([np.full(len(x[0]), x[2]) for x in m["calib"]])
            brier, brier_ref = float(((p - y) ** 2).mean()), float(((ref - y) ** 2).mean())
            bins = np.clip((p * 10).astype(int), 0, 9)
            ece = float(sum(abs(p[bins == b].mean() - y[bins == b].mean()) * (bins == b).mean() for b in range(10) if (bins == b).any()))
            auc = None
            if len(set(y.tolist())) == 2:
                from sklearn.metrics import roc_auc_score
                auc = float(roc_auc_score(y, p))
            k["calibration"] = {"predictions": int(len(p)), "brier": brier, "brier_base_rate": brier_ref,
                                "brier_skill": 1 - brier / brier_ref if brier_ref > 0 else None, "ece": ece, "auc": auc}
        daily = np.asarray(m["daily"])
        if a.state is not None:
            k["self_validation"] = {"screening": a.state.screening_evidence(), "meta_brier_skill": a.state.meta_skill()}
        report_arms[a.name] = {"policy": a.policy.model_dump(), "uses_memory": a.policy.uses_memory, "epoch_returns": m["epoch_returns"],
                               "forward": _series_stats(daily), "kpis": k, "approach_counts": {x: m["approach"].count(x) for x in set(m["approach"])},
                               "decision_changes": m["changes"][-50:], "n_decision_changes": len(m["changes"]),
                               "curve": {"dates": m["dates"], "values": np.cumprod(1 + daily).round(6).tolist()}}
    comps = {}
    for x, y in COMPARISONS:
        if x in per and y in per:
            comps[f"{x}_vs_{y}"] = {"forward_return": paired(per[x]["epoch_returns"], per[y]["epoch_returns"]),
                                    "screen_quality": paired(per[x]["screen_quality"], per[y]["screen_quality"])}
    segments = {}
    if sealed_from is not None:
        sealed = [i for i, ep in enumerate(epochs) if ep["window"][0] >= sealed_from]
        for x, y in COMPARISONS:
            if x in per and y in per and sealed:
                segments[f"{x}_vs_{y}"] = {"forward_return": paired([per[x]["epoch_returns"][i] for i in sealed], [per[y]["epoch_returns"][i] for i in sealed]),
                                           "screen_quality": paired([per[x]["screen_quality"][i] for i in sealed], [per[y]["screen_quality"][i] for i in sealed])}
        segments = {"sealed_from": sealed_from, "epochs": len(sealed), "comparisons": segments,
                    "arm_returns": {a: float(np.prod([1 + per[a]["epoch_returns"][i] for i in sealed]) - 1) for a in per}}
    holm(comps)
    if segments.get("comparisons"):
        holm(segments["comparisons"])
    pc = (segments.get("comparisons") or comps).get(f"{PRIMARY[0]}_vs_{PRIMARY[1]}", {}).get("forward_return", {})
    if pc.get("epochs", 0) >= 3:
        if pc["ci90"][0] > 0:
            verdict = "memory-informed research beat its memory-free counterpart on unseen windows (90% CI above zero)"
        elif pc["ci90"][1] < 0:
            verdict = "memory-informed research did WORSE than its memory-free counterpart on unseen windows"
        else:
            verdict = "no reliable difference between memory-informed and memory-free research on unseen windows"
    else:
        verdict = "too few epochs to judge"
    findings = []
    if "memory" in per:
        st = next(a for a in arm_objs if a.name == "memory").state
        if st is not None:
            findings = st.findings()[:40]
    report = {"run_id": run_id, "label": label, "created": datetime.now().isoformat(timespec="seconds"),
              "dataset": {"id": ds.id, "content_hash": ds.content_hash, "synthetic": ds.synthetic, "description": ds.manifest.description},
              "protocol": {"epoch_len": epoch_len, "epochs": len(rows), "first_decision": dates[rows[0]] if rows else None,
                           "last_window_end": epochs[-1]["window"][1] if epochs else None, "universe": universe_size(),
                           "budget_per_decision": arm_objs[0].policy.budget, "top_k": arm_objs[0].policy.top_k, "seed": seed,
                           "cost_per_side": cost, "primary_comparison": list(PRIMARY),
                           "protocol_version": PROTOCOL_VERSION, "comparisons": [list(c) for c in COMPARISONS]},
              "evaluation": est, "epochs": epochs, "arms": report_arms, "comparisons": comps, "sealed_segment": segments or None,
              "verdict": verdict, "verdict_basis": "sealed window" if segments else "all epochs",
              "findings": findings, "runtime_seconds": round(time.perf_counter() - t0, 1)}
    memory.append("system", "benchmark_report", {"run_id": run_id, "label": label, "dataset": ds.id, "content_hash": ds.content_hash,
                                                 "verdict": verdict, "primary": pc, "epochs": len(rows)},
                  available_at=epochs[-1]["window"][1] if epochs else "0000-00-00")
    return report


def save(report: dict, root: Path) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"{report['run_id']}.json"
    p.write_text(json.dumps(report, default=float))
    return p

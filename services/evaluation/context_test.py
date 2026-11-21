"""Does a market-context filter actually help? A paired test against a placebo.

For every context state (e.g. "only trade while India VIX is calm") we take a sample of strategy
configurations and compare each one WITH the filter against its identical twin WITHOUT it, on the
same days. A filter also cuts exposure, which changes returns on its own, so the comparison that
matters is against a PLACEBO: the same on/off pattern shifted in time (a circular shift keeps how
often and for how long the filter is on, but breaks its link to the market). If the real filter
does no better than the shifted ones, the context carries no usable information for these rules.

Only development and validation rows are used; the sealed segment is never touched here.
"""
from __future__ import annotations

import math

import numpy as np
from nseql.evaluation.cache import Panel, from_arrays
from nseql.evaluation.space import CONTEXT, twins, universe
from nseql.evaluation.vector import evaluate


def _sharpe(r: np.ndarray) -> float:
    s = r.std()
    return float(r.mean() / s * math.sqrt(252)) if s > 0 else 0.0


def _with_state(P: Panel, state: str, arr: np.ndarray) -> Panel:
    q = from_arrays(dict(P), P.meta)
    q[state] = arr
    return q


def run(P: Panel, cost: float, *, rows: tuple[int, int], pairs: int = 150, placebos: int = 20, seed: int = 0,
        states: tuple[str, ...] = CONTEXT, progress=None) -> dict:
    """rows = [start, end) of the panel rows to score (development + validation only)."""
    if not P.meta.get("context"):
        raise ValueError("this cache has no market context; build it with a context dataset")
    lo, hi = rows
    if hi - lo < 120:
        raise ValueError("need at least 120 scored rows")
    rng = np.random.default_rng(seed)
    cfgs, tw = universe(True), twins()
    by_state = {s: [i for i, c in enumerate(cfgs) if c.regime == s] for s in states}
    base_cache: dict[int, np.ndarray] = {}

    def score(cfg, panel) -> tuple[float, float]:
        _, r, _ = evaluate(cfg, panel, cost)
        r = r[lo:hi].astype(float)
        return float(r.mean() * 252), _sharpe(r)

    def base(i):
        if i not in base_cache:
            base_cache[i] = np.array(score(cfgs[i], P))
        return base_cache[i]

    out = {}
    for k, st in enumerate(states):
        sample = rng.choice(by_state[st], size=min(pairs, len(by_state[st])), replace=False)
        diffs = np.array([np.array(score(cfgs[i], P)) - base(tw[i]) for i in sample])      # [pairs, (ret, sharpe)]
        real = np.median(diffs[:, 1])
        arr = np.asarray(P[st]).copy()
        T = arr.shape[0]
        if arr.min() == 1:                                 # no data behind this state: nothing to test
            out[st] = {"pairs": 0, "on_fraction_scored": 1.0, "median_sharpe_change": 0.0, "median_return_change": 0.0, "share_improved": 0.0,
                       "placebo_median_sharpe_change": 0.0, "placebo_95th": 0.0, "z_vs_placebo": 0.0, "p_vs_placebo": 1.0, "p_empirical": 1.0,
                       "no_data": True}
            continue
        shifts = rng.integers(60, max(61, T - 60), size=placebos)
        plac = []
        for sh in shifts:
            Q = _with_state(P, st, np.roll(arr, int(sh)))
            d = np.array([score(cfgs[i], Q)[1] - base(tw[i])[1] for i in sample])
            plac.append(float(np.median(d)))
        plac = np.array(plac)
        p_emp = (1 + int((plac >= real).sum())) / (1 + len(plac))
        # a handful of placebos cannot give a small empirical p, so the decision uses a one-sided z-score
        # against the placebo distribution (and separately requires beating its 95th percentile)
        sd = float(plac.std(ddof=1)) if len(plac) > 1 else 0.0
        z = (real - float(plac.mean())) / sd if sd > 0 else (np.inf if real > plac.mean() else 0.0)
        p = float(0.5 * math.erfc(z / math.sqrt(2)))
        out[st] = {"pairs": len(sample), "on_fraction_scored": round(float(arr[lo:hi].mean()), 3),
                   "median_sharpe_change": round(float(real), 3), "median_return_change": round(float(np.median(diffs[:, 0])), 4),
                   "share_improved": round(float((diffs[:, 1] > 0).mean()), 3),
                   "placebo_median_sharpe_change": round(float(np.median(plac)), 3),
                   "placebo_95th": round(float(np.quantile(plac, 0.95)), 3), "z_vs_placebo": round(float(z), 2),
                   "p_vs_placebo": round(p, 4), "p_empirical": round(p_emp, 3)}
        if progress:
            progress(k + 1, len(states))
    tested = [s for s in out if not out[s].get("no_data")]
    ranked = sorted(tested, key=lambda s: out[s]["p_vs_placebo"])
    prev = 0.0
    for r, s in enumerate(ranked):                      # Holm step-down across the states actually tested
        prev = max(prev, min(1.0, out[s]["p_vs_placebo"] * (len(ranked) - r)))
        x = out[s]
        x["p_holm"] = round(prev, 4)
        x["verdict"] = ("helps beyond chance" if x["p_holm"] < 0.05 and x["median_sharpe_change"] > max(0.0, x["placebo_95th"])
                        else "no better than a placebo filter")
    for s in out:
        if out[s].get("no_data"):
            out[s].update(p_holm=1.0, verdict="not tested: no data for this state")
    return {"states": out, "rows": [lo, hi], "cost_per_side": cost, "pairs_per_state": pairs, "placebos": placebos,
            "context": P.meta.get("context"), "synthetic": bool(P.meta.get("synthetic") or (P.meta.get("context") or {}).get("synthetic")),
            "method": "median paired Sharpe change (filter vs identical unfiltered twin) compared with the same filter "
                      "circularly shifted in time (one-sided z-score against the placebo runs, Holm-adjusted across states; "
                      "must also beat the placebo 95th percentile)"}

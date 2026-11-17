"""Self-criticism applied to every batch of screened configurations before anything is selected.

* multiple testing: Benjamini-Hochberg on the in-sample p-values of the whole screened batch
  (every screened configuration counts as a trial, not only the survivors);
* cost fragility: the in-sample mean must stay positive when costs are multiplied;
* leakage: arms only see a HistoryView that refuses rows after the decision date, and the screener
  is checked for causality by tests; nothing is inferred here from future data;
* redundancy: near-duplicate return streams are not selected twice;
* overfitting: probability of backtest overfitting (CSCV, Bailey et al. 2017) is measured on every
  batch and reported.
"""
from __future__ import annotations

from itertools import combinations

import numpy as np

from nseql.brain.diagnostics import IS_WINDOW, HistoryView


def benjamini_hochberg(p: np.ndarray, alpha: float) -> np.ndarray:
    m = len(p)
    if m == 0:
        return np.zeros(0, dtype=bool)
    order = np.argsort(p)
    ok = p[order] <= alpha * np.arange(1, m + 1) / m
    k = (np.nonzero(ok)[0].max() + 1) if ok.any() else 0
    out = np.zeros(m, dtype=bool)
    out[order[:k]] = True
    return out


def survivors(view: HistoryView, idx: np.ndarray, X: np.ndarray, p: np.ndarray, *, alpha: float, stress: float) -> tuple[np.ndarray, dict]:
    fdr = benjamini_hochberg(p, alpha)
    R = view.window(idx, IS_WINDOW)
    G = view.window(idx, IS_WINDOW, "G")
    robust_cost = (R - (stress - 1.0) * (G - R)).mean(axis=1) > 0
    active = X[:, 7] > 0.05                                # exposure: it actually traded
    keep = fdr & robust_cost & active
    return keep, {"screened": int(len(idx)), "pass_fdr": int(fdr.sum()), "pass_cost_stress": int((fdr & robust_cost).sum()),
                  "survivors": int(keep.sum())}


def select(view: HistoryView, idx_ranked: np.ndarray, k: int, max_corr: float | None) -> np.ndarray:
    """Take the k best in rank order, skipping anything correlated above max_corr with a pick."""
    if max_corr is None or len(idx_ranked) <= 1:
        return idx_ranked[:k]
    cand = idx_ranked[: min(len(idx_ranked), 20 * k)]
    R = view.window(cand, 120)
    sd = R.std(axis=1)
    picked: list[int] = []
    for i in range(len(cand)):
        if sd[i] <= 1e-12:
            continue
        if picked:
            with np.errstate(invalid="ignore", divide="ignore"):
                c = np.corrcoef(np.vstack([R[i], R[picked]]))[0, 1:]
            if np.nanmax(c) > max_corr:
                continue
        picked.append(i)
        if len(picked) == k:
            break
    return cand[picked]


def pbo(view: HistoryView, idx: np.ndarray, blocks: int = 16, combos: int = 120, seed: int = 0) -> float | None:
    """Probability of backtest overfitting by combinatorially symmetric cross-validation on the
    in-sample window: how often the in-sample winner ranks below the median out of sample."""
    if len(idx) < 10:
        return None
    R = view.window(idx, IS_WINDOW)
    n = R.shape[1] // blocks * blocks
    if n < blocks * 4:
        return None
    M = R[:, -n:].reshape(len(idx), blocks, -1).mean(axis=2)          # [configs, blocks]
    rng = np.random.default_rng(seed)
    all_c = list(combinations(range(blocks), blocks // 2))
    pick = rng.choice(len(all_c), size=min(combos, len(all_c)), replace=False)
    below = 0
    for j in pick:
        tr = np.zeros(blocks, dtype=bool)
        tr[list(all_c[j])] = True
        best = np.argmax(M[:, tr].mean(axis=1))
        oos = M[:, ~tr].mean(axis=1)
        rank = (oos < oos[best]).mean()
        below += rank < 0.5
    return float(below / len(pick))

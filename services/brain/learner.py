"""What the Brain knows, rebuilt point-in-time from episodic memory.

Learning signal: for every screened configuration, whether its realised forward return beat the
median forward return of the RANDOM part of that decision's screen (an unbiased estimate of the
universe median, so the label measures selection skill, not market direction).

  semantic memory  — success rates per facet (family, horizon, overlays, sizing), overall and per
                     regime, shrunk toward the global rate; findings are facets whose rate differs
                     from the global rate after Benjamini-Hochberg across all facets;
  procedural       — Thompson sampling over facets decides WHAT to screen next;
  meta-learning    — (a) a calibrated logistic model predicts forward success from in-sample
                     diagnostics + facets + regime, (b) a bandit over ranking approaches learns
                     HOW to rank, from each approach's realised forward performance;
  regime adaptation— families whose success rate in the current regime is reliably poor are
                     benched for screening (research only; no paper/live allocation is changed).
"""
from __future__ import annotations

import math

import numpy as np
from scipy.stats import beta as beta_dist, binomtest

from nseql.brain.memory import EpisodicMemory
from nseql.brain.regime import TREND, VOL
from nseql.evaluation.space import FACETS, facet_table


class BrainState:
    def __init__(self, memory: EpisodicMemory, namespace: str):
        self.memory, self.ns = memory, namespace
        values, codes = facet_table()
        self.values, self.codes = values, codes
        self.n_reg = len(VOL) * len(TREND)
        self.succ = {f: np.zeros((self.n_reg + 1, len(values[f]))) for f in FACETS}   # row n_reg = all regimes
        self.tot = {f: np.zeros((self.n_reg + 1, len(values[f]))) for f in FACETS}
        self.X: list[np.ndarray] = []
        self.y: list[np.ndarray] = []
        self.approach: dict[str, list[float]] = {}
        # prequential evidence that each learned component helps on data it had not seen
        self.meta_brier = self.ref_brier = 0.0
        self.meta_n = 0
        self.hits = {"explore": [0.0, 0], "exploit": [0.0, 0]}
        self.exploit_epochs = 0
        self._last_seq = 0
        self._pending: list = []
        self.as_of: str | None = None
        self._model = None
        self._model_n = -1

    # ------------------------------------------------------------------ folding memory
    @staticmethod
    def regime_code(label: str) -> int:
        v, t = label.split("_")
        return VOL.index(v) * len(TREND) + TREND.index(t)

    def advance(self, as_of: str) -> None:
        """Absorb every episode available at `as_of` (and nothing that was not yet available)."""
        if self.as_of is not None and as_of < self.as_of:
            raise ValueError("BrainState only moves forward in time")
        self.as_of = as_of
        new = self.memory.read(self.ns, ("outcome", "approach_outcome"), after_seq=self._last_seq)
        if new:
            self._last_seq = new[-1].seq
        rows, keep = self._pending + new, []
        for ep in sorted(rows, key=lambda e: e.seq):
            if ep.available_at > as_of:
                keep.append(ep)
                continue
            if ep.kind == "outcome":
                a = ep.arrays
                idx, lab = a["idx"].astype(int), a["label"].astype(float)
                rate_before = self.base_rate
                if "meta_pred" in a:
                    self.meta_brier += float(((a["meta_pred"] - lab) ** 2).sum())
                    self.ref_brier += float(((rate_before - lab) ** 2).sum())
                    self.meta_n += len(lab)
                if "explore" in a:
                    ex = a["explore"].astype(bool)
                    self.hits["explore"][0] += float(lab[ex].sum()); self.hits["explore"][1] += int(ex.sum())
                    if (~ex).any():
                        self.hits["exploit"][0] += float(lab[~ex].sum()); self.hits["exploit"][1] += int((~ex).sum())
                        self.exploit_epochs += 1
                rc = self.regime_code(ep.summary["regime"])
                for f in FACETS:
                    c = self.codes[f][idx]
                    for r in (rc, self.n_reg):
                        np.add.at(self.succ[f][r], c, lab)
                        np.add.at(self.tot[f][r], c, 1.0)
                self.X.append(self.features(idx, a["diag"], ep.summary["regime"]))
                self.y.append(lab)
            else:
                for name, v in ep.summary["returns"].items():
                    self.approach.setdefault(name, []).append(float(v))
        self._pending = keep

    @property
    def n_outcomes(self) -> int:
        return int(self.tot[FACETS[0]][self.n_reg].sum())

    @property
    def base_rate(self) -> float:
        n = self.n_outcomes
        return float(self.succ[FACETS[0]][self.n_reg].sum() / n) if n else 0.5

    # ------------------------------------------------------------------ features / meta-model
    def features(self, idx: np.ndarray, diag: np.ndarray, regime: str) -> np.ndarray:
        cols = [np.clip(diag, -50, 50)]
        for f in FACETS:
            oh = np.zeros((len(idx), len(self.values[f])))
            oh[np.arange(len(idx)), self.codes[f][idx]] = 1
            cols.append(oh)
        v, t = regime.split("_")
        reg = np.zeros((len(idx), len(VOL) + len(TREND)))
        reg[:, VOL.index(v)] = 1
        reg[:, len(VOL) + TREND.index(t)] = 1
        cols.append(reg)
        fam = np.zeros((len(idx), len(self.values["family"]) * len(VOL)))
        fam[np.arange(len(idx)), self.codes["family"][idx] * len(VOL) + VOL.index(v)] = 1
        cols.append(fam)
        return np.hstack(cols)

    def model(self, c: float, min_evidence: int):
        n = self.n_outcomes
        if n < max(min_evidence, 50):
            return None
        if self._model is not None and self._model_n == n:
            return self._model
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        X, y = np.vstack(self.X)[-100_000:], np.concatenate(self.y)[-100_000:]
        if len(set(y.tolist())) < 2:
            return None
        self._model = make_pipeline(StandardScaler(), LogisticRegression(C=c, max_iter=300)).fit(X, y)
        self._model_n = n
        return self._model

    # ------------------------------------------------------------------ self-validation gates
    def meta_skill(self) -> float | None:
        return 1 - self.meta_brier / self.ref_brier if self.meta_n >= 2000 and self.ref_brier > 0 else None

    def meta_trusted(self) -> bool:
        """The meta-model is used for ranking only after it has beaten the base rate on predictions it
        made before the outcomes were known."""
        sk = self.meta_skill()
        return sk is not None and sk > 0

    def screening_evidence(self) -> dict:
        (he, ne), (hx, nx) = self.hits["explore"], self.hits["exploit"]
        pe, px = (he / ne if ne else None), (hx / nx if nx else None)
        se = math.sqrt(max(pe * (1 - pe), 1e-9) / ne + max(px * (1 - px), 1e-9) / nx) if ne and nx else None
        return {"explore_hit": pe, "exploit_hit": px, "lift": (px - pe) if pe is not None and px is not None else None,
                "z": ((px - pe) / se) if se else None, "epochs": self.exploit_epochs}

    def screening_trusted(self, trial_epochs: int = 3) -> bool:
        """Learned screening keeps its budget only while the configurations it chose beat the random
        explore sample screened alongside them (same epochs, same label)."""
        ev = self.screening_evidence()
        return self.exploit_epochs < trial_epochs or (ev["lift"] is not None and ev["lift"] > 0)

    # ------------------------------------------------------------------ procedural: what to screen
    def facet_posterior(self, regime: str, prior: float) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        """Beta(a, b) per facet value, conditional on the regime and shrunk toward the facet's
        all-regime rate, which is itself shrunk toward the global rate."""
        g = self.base_rate
        rc = self.regime_code(regime)
        out = {}
        for f in FACETS:
            s_all, n_all = self.succ[f][self.n_reg], self.tot[f][self.n_reg]
            p_all = (s_all + prior * g) / (n_all + prior)
            s, n = self.succ[f][rc], self.tot[f][rc]
            out[f] = (s + prior * p_all, (n - s) + prior * (1 - p_all))
        return out

    def thompson_scores(self, regime: str, prior: float, rng: np.random.Generator) -> np.ndarray:
        post = self.facet_posterior(regime, prior)
        score = None
        for f in FACETS:
            draw = rng.beta(post[f][0], post[f][1])
            v = draw[self.codes[f]]
            score = v if score is None else score + v
        return score / len(FACETS)

    def eligibility(self, regime: str, prior: float, floor: float, min_n: int) -> dict[str, dict]:
        a, b = self.facet_posterior(regime, prior)["family"]
        n = self.tot["family"][self.regime_code(regime)]
        mean = a / (a + b)
        fams = self.values["family"]
        out = {f: {"eligible": True, "posterior": float(mean[i]), "n": int(n[i])} for i, f in enumerate(fams)}
        bench = [i for i in np.argsort(mean) if n[i] >= min_n and mean[i] < floor]
        for i in bench[: max(len(fams) - 2, 0)]:          # always keep at least two families in play
            out[fams[i]]["eligible"] = False
        return out

    # ------------------------------------------------------------------ meta: how to rank
    def choose_approach(self, approaches: tuple[str, ...], rng: np.random.Generator, has_model: bool) -> tuple[str, dict]:
        stats = {}
        for a in approaches:
            v = np.array(self.approach.get(a, []))
            stats[a] = {"n": int(len(v)), "mean": float(v.mean()) if len(v) else None}
        usable = [a for a in approaches if stats[a]["n"] >= 3 and (a != "meta" or has_model)]
        if not usable:
            return ("robust" if "robust" in approaches else approaches[0]), stats
        allv = np.concatenate([np.array(self.approach[a]) for a in usable])
        sd = float(allv.std()) or 1e-3
        draws = {a: rng.normal(stats[a]["mean"] * stats[a]["n"] / (stats[a]["n"] + 2), sd / math.sqrt(stats[a]["n"] + 2)) for a in usable}
        return max(draws, key=draws.get), stats

    # ------------------------------------------------------------------ semantic memory
    def findings(self, prior: float = 20.0, alpha: float = 0.10, min_n: int = 200) -> list[dict]:
        g, out = self.base_rate, []
        regimes = [(self.n_reg, "all")] + [(VOL.index(v) * len(TREND) + TREND.index(t), f"{v}_{t}") for v in VOL for t in TREND]
        for f in FACETS:
            for rc, rlabel in regimes:
                for i, val in enumerate(self.values[f]):
                    n, s = int(self.tot[f][rc][i]), float(self.succ[f][rc][i])
                    if n < min_n:
                        continue
                    a, b = s + prior * g, n - s + prior * (1 - g)
                    lo, hi = beta_dist.ppf([0.05, 0.95], a, b)
                    p = binomtest(int(round(s)), n, g).pvalue
                    out.append({"facet": f, "value": val, "regime": rlabel, "n": n, "success_rate": s / n, "posterior": a / (a + b),
                                "ci90": [float(lo), float(hi)], "global_rate": g, "lift": a / (a + b) - g, "p_value": float(p)})
        if not out:
            return []
        from nseql.brain.critic import benjamini_hochberg
        keep = benjamini_hochberg(np.array([o["p_value"] for o in out]), alpha)
        res = [dict(o, significant=bool(k)) for o, k in zip(out, keep)]
        return sorted([r for r in res if r["significant"]], key=lambda r: -abs(r["lift"]))

    def recall(self, idx: int, regime: str | None = None, prior: float = 20.0) -> dict:
        """Everything memory holds that is relevant to one configuration."""
        rc = self.regime_code(regime) if regime else self.n_reg
        g = self.base_rate
        facets = {}
        for f in FACETS:
            c = int(self.codes[f][idx])
            n, s = float(self.tot[f][rc][c]), float(self.succ[f][rc][c])
            facets[f] = {"value": self.values[f][c], "n": int(n), "posterior": (s + prior * g) / (n + prior)}
        return {"facets": facets, "global_rate": g, "outcomes_in_memory": self.n_outcomes}

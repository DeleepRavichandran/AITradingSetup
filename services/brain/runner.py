"""Drive research arms through time: decide at row d using only what was known at d, then settle
outcomes once the forward window has passed. Used by the benchmark (historical replay) and by the
continuous step that runs as new data arrives.

Information flow for an arm at decision row d (as_of = date[d]):
  * market data and screener results: through HistoryView, rows <= d only;
  * memory: BrainState.advance(as_of) — outcomes become visible on their available_at date;
  * forward returns for rows d+1 .. d+L are read ONLY by `settle`, after the decision is recorded.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field

import numpy as np

from nseql.brain.critic import pbo, select, survivors
from nseql.brain.diagnostics import HistoryView, diagnostics
from nseql.brain.learner import BrainState
from nseql.brain.memory import EpisodicMemory
from nseql.brain.policy import ResearchPolicy
from nseql.brain.regime import regime_at
from nseql.evaluation.space import universe


@dataclass
class Arm:
    name: str
    policy: ResearchPolicy
    namespace: str
    state: BrainState | None = None
    last: dict = field(default_factory=dict)


@dataclass
class Decision:
    arm: str
    epoch: int
    d: int
    as_of: str
    regime: dict
    screened: np.ndarray
    explore: np.ndarray
    diag: np.ndarray
    pvals: np.ndarray
    meta_pred: np.ndarray | None
    approach: str
    selections: dict
    critic: dict
    pbo: float | None
    eligibility: dict | None
    n_outcomes: int
    changes: list


def _seed(*parts) -> int:
    return zlib.crc32("|".join(map(str, parts)).encode())


class Runner:
    def __init__(self, P, results: dict, memory: EpisodicMemory, cost: float, arms: list[Arm], *, seed: int = 7,
                 epoch_len: int = 10, min_history: int = 30):
        self.P, self.R, self.G, self.E = P, results["R"], results["G"], results["E"]
        self.memory, self.cost, self.arms, self.seed, self.L = memory, cost, arms, seed, epoch_len
        self.dates = [str(x) for x in P.dates()]
        self.lookback = np.array([c.lookback for c in universe()])
        self.min_history = min_history
        for a in arms:
            if a.policy.uses_memory and a.state is None:
                a.state = BrainState(memory, a.namespace)

    # ------------------------------------------------------------------ decide
    def candidates(self, d: int) -> np.ndarray:
        return np.nonzero(self.lookback <= d - self.min_history)[0]

    def decide(self, arm: Arm, epoch: int, d: int) -> Decision:
        pol, st = arm.policy, arm.state
        as_of = self.dates[d]
        regime = regime_at(self.P, d)
        if st is not None:
            st.advance(as_of)
        rng_common = np.random.default_rng(_seed(self.seed, epoch, "screen"))
        rng_arm = np.random.default_rng(_seed(self.seed, epoch, arm.name))
        cand = self.candidates(d)
        changes = []
        elig = None
        if pol.use_eligibility and st is not None and st.n_outcomes >= pol.min_evidence and regime["vol"] != "unknown":
            elig = st.eligibility(regime["label"], pol.prior_strength, pol.eligibility_floor, pol.eligibility_min_n)
            benched = sorted(f for f, v in elig.items() if not v["eligible"])
            if benched != arm.last.get("benched", []):
                changes.append({"type": "eligibility", "regime": regime["label"], "from": arm.last.get("benched", []), "to": benched,
                                "evidence": {f: elig[f] for f in benched}})
            arm.last["benched"] = benched
            fam = np.array([universe()[i].family for i in cand])
            cand = cand[~np.isin(fam, benched)]
        B = min(pol.budget, len(cand))
        learned_screen = pol.use_procedural and st is not None and st.n_outcomes >= pol.min_evidence
        if learned_screen:
            trusted = st.screening_trusted()
            if trusted != arm.last.get("screening_trusted"):
                changes.append({"type": "component", "component": "learned_screening", "enabled": trusted, "evidence": st.screening_evidence()})
            arm.last["screening_trusted"] = trusted
            frac = pol.explore_frac if trusted else 1 - pol.explore_frac      # keep a small exploit trial running when distrusted
            n_exp = max(int(round(frac * B)), min(B, 50))
            explore = rng_common.choice(cand, n_exp, replace=False)
            score = st.thompson_scores(regime["label"], pol.prior_strength, rng_arm)[cand]
            score[np.isin(cand, explore)] = -np.inf
            exploit = cand[np.argsort(-score, kind="stable")[: B - n_exp]]
            screened = np.concatenate([explore, exploit])
            exp_mask = np.r_[np.ones(len(explore), bool), np.zeros(len(exploit), bool)]
        else:
            screened = rng_common.choice(cand, B, replace=False)
            exp_mask = np.ones(B, bool)
        view = HistoryView(self.R, self.G, self.E, d, self.cost)
        X, p = diagnostics(view, screened)
        if pol.use_critic:
            keep, cstats = survivors(view, screened, X, p, alpha=pol.fdr_alpha, stress=pol.cost_stress)
        else:
            keep, cstats = np.ones(len(screened), bool), {"screened": int(len(screened)), "survivors": int(len(screened)), "critic": "off"}
        model = st.model(pol.meta_c, pol.min_evidence) if (pol.use_ranking and st is not None) else None
        meta_pred = model.predict_proba(st.features(screened, X, regime["label"]))[:, 1] if model is not None else None
        if pol.use_ranking and st is not None:
            trusted = st.meta_trusted()
            if model is not None and trusted != arm.last.get("meta_trusted"):
                changes.append({"type": "component", "component": "meta_model_ranking", "enabled": trusted,
                                "evidence": {"brier_skill": st.meta_skill(), "predictions": st.meta_n}})
            arm.last["meta_trusted"] = trusted
            approach, astats = st.choose_approach(pol.approaches, rng_arm, model is not None and trusted)
            shadow = [a for a in pol.approaches if a != "meta" or model is not None]
            if approach != arm.last.get("approach"):
                changes.append({"type": "ranking_approach", "from": arm.last.get("approach"), "to": approach, "evidence": astats})
            arm.last["approach"] = approach
        else:
            approach, shadow = pol.fixed_approach, [pol.fixed_approach]
        sels = {}
        for a in dict.fromkeys([approach, *shadow]):
            sel_idx = np.nonzero(keep)[0]
            if a == "random":
                pick = rng_arm.choice(sel_idx, min(pol.top_k, len(sel_idx)), replace=False) if len(sel_idx) else sel_idx
                sels[a] = screened[pick]
                continue
            s = {"is_sharpe": X[:, 0], "robust": X[:, :3].min(axis=1), "meta": meta_pred}[a][sel_idx]
            ranked = screened[sel_idx[np.argsort(-s, kind="stable")]]
            sels[a] = select(view, ranked, pol.top_k, pol.max_corr)      # de-duplication is portfolio construction, applied to every ranked arm
        return Decision(arm.name, epoch, d, as_of, regime, screened, exp_mask, X, p, meta_pred, approach, sels, cstats,
                        pbo(view, screened, seed=epoch), elig, st.n_outcomes if st else 0, changes)

    def record_decision(self, arm: Arm, dec: Decision) -> None:
        cfgs = universe()
        self.memory.append(arm.namespace, "decision", {
            "arm": arm.name, "epoch": dec.epoch, "regime": dec.regime, "approach": dec.approach, "critic": dec.critic, "pbo": dec.pbo,
            "selected": [cfgs[i].key for i in dec.selections[dec.approach]], "eligibility": dec.eligibility, "n_outcomes": dec.n_outcomes,
            "changes": dec.changes, "policy": arm.policy.model_dump()},
            available_at=dec.as_of, decided_at=dec.as_of, epoch=dec.epoch,
            arrays={"screened": dec.screened.astype(np.int32), "explore": dec.explore, "diag": dec.diag.astype(np.float32),
                    "p": dec.pvals.astype(np.float32), **({"meta_pred": dec.meta_pred.astype(np.float32)} if dec.meta_pred is not None else {}),
                    **{f"sel_{a}": v.astype(np.int32) for a, v in dec.selections.items()}})

    # ------------------------------------------------------------------ settle
    def settle(self, arm: Arm, dec: Decision) -> dict:
        lo, hi = dec.d + 1, min(dec.d + self.L, len(self.dates) - 1)
        rows = slice(lo, hi + 1)
        out = {}
        for a, sel in dec.selections.items():
            daily = np.asarray(self.R[sel][:, rows], dtype=float).mean(axis=0) if len(sel) else np.zeros(hi - lo + 1)
            out[a] = daily
        fwd = np.asarray(self.R[dec.screened][:, rows], dtype=float).sum(axis=1)
        ref = float(np.median(fwd[dec.explore]))
        # ties (typically configurations that were flat, fwd == 0 == ref) are decided by a fair coin, so
        # "being in cash" is not mechanically labelled a failure (protocol v3; found by the negative control)
        tie = np.random.default_rng(_seed(self.seed, dec.epoch, arm.name, "tie")).random(len(fwd)) < 0.5
        label = ((fwd > ref) | ((fwd == ref) & tie)).astype(np.int8)
        avail = self.dates[hi]
        self.memory.append(arm.namespace, "outcome", {"arm": arm.name, "epoch": dec.epoch, "regime": dec.regime["label"], "reference": ref,
                                                      "window": [self.dates[lo], avail]},
                           available_at=avail, decided_at=dec.as_of, epoch=dec.epoch,
                           arrays={"idx": dec.screened.astype(np.int32), "diag": dec.diag.astype(np.float32), "fwd": fwd.astype(np.float32),
                                   "label": label, "explore": dec.explore, **({"meta_pred": dec.meta_pred.astype(np.float32)} if dec.meta_pred is not None else {})})
        if len(dec.selections) > 1:
            self.memory.append(arm.namespace, "approach_outcome", {"arm": arm.name, "epoch": dec.epoch, "regime": dec.regime["label"],
                                                                   "returns": {a: float(np.prod(1 + v) - 1) for a, v in out.items()}},
                               available_at=avail, decided_at=dec.as_of, epoch=dec.epoch)
        return {"daily": out[dec.approach], "epoch_return": float(np.prod(1 + out[dec.approach]) - 1), "rows": [lo, hi],
                "fwd": fwd, "label": label, "reference": ref, "approach_returns": {a: float(np.prod(1 + v) - 1) for a, v in out.items()}}

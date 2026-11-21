"""Controlled self-modification of the research system.

A *system version* is a ResearchPolicy. Its whole life is a sequence of immutable events in the
episodic memory (namespace "system"); the current state is always derived by folding them.

    proposed ─► frozen-benchmark ─► forward shadow evaluation ─► ready ─► HUMAN approval ─► champion
        │              │ fail                │ fail                                         │
        └─ rejected ◄──┴─────────────────────┘                     automatic rollback ◄─────┘

* Proposals that touch broker credentials, execution permissions, risk limits or safety
  mechanisms are rejected before validation (policy.check_protected); the schema forbids unknown
  fields. There is no code path from here to those subsystems.
* Frozen benchmark: a fixed list of historical decision dates on a fixed dataset hash, replayed for
  challenger and champion in fresh memories. The challenger must not be worse.
* Forward evaluation: challenger and champion run side by side on decision dates AFTER the
  proposal was made (data the proposal could not have been tuned on).
* Promotion needs a named human approver. Agents, "system" and the scheduler cannot approve.
* Rollback is automatic: a failed correctness probe, or a promoted champion that underperforms the
  version it replaced on later forward windows, reverts to the previous champion.
"""
from __future__ import annotations

import math
import uuid

import numpy as np

from nseql.brain.benchmark import paired
from nseql.brain.memory import EpisodicMemory
from nseql.brain.policy import ProtectedSettingError, ResearchPolicy, check_protected
from nseql.brain.runner import Arm, Runner

NS = "system"
TUNABLE = {"budget": (500, 5000, int), "top_k": (5, 30, int), "explore_frac": (0.10, 0.60, float), "prior_strength": (5.0, 100.0, float),
           "meta_c": (0.05, 5.0, float), "fdr_alpha": (0.05, 0.40, float), "max_corr": (0.70, 0.98, float),
           "min_evidence": (500, 5000, int), "eligibility_floor": (0.30, 0.48, float)}
AGENT_NAMES = ("agent", "ai", "system", "scheduler", "brain", "bot", "claude", "auto")


class LifecycleError(RuntimeError):
    pass


class SystemVersions:
    def __init__(self, memory: EpisodicMemory, *, min_forward_epochs: int = 6, tolerance: float = 0.0005, rollback_t: float = -1.5):
        self.memory, self.min_forward, self.tol, self.rollback_t = memory, min_forward_epochs, tolerance, rollback_t

    # ------------------------------------------------------------------ state
    def _emit(self, kind: str, summary: dict, as_of: str) -> int:
        return self.memory.append(NS, kind, summary, available_at=as_of, decided_at=as_of)

    def state(self) -> dict:
        vs: dict[str, dict] = {}
        champion, history = None, []
        for ep in self.memory.read(NS):
            s, k = ep.summary, ep.kind
            vid = s.get("version")
            if k == "version_proposed":
                vs[vid] = {"id": vid, "policy": s["policy"], "parent": s.get("parent"), "origin": s["origin"], "rationale": s.get("rationale", ""),
                           "changes": s.get("changes") or {},
                           "proposed_at": ep.available_at, "status": "proposed", "benchmark": None, "forward": [], "events": []}
            if vid in vs:
                vs[vid]["events"].append({"kind": k, "at": ep.available_at, "seq": ep.seq})
            if k == "version_rejected" and vid in vs:
                vs[vid]["status"], vs[vid]["reason"] = "rejected", s["reason"]
            elif k == "version_benchmarked":
                vs[vid]["benchmark"] = s["result"]
                vs[vid]["status"] = "forward_evaluation" if s["passed"] else "rejected"
                if not s["passed"]:
                    vs[vid]["reason"] = "did not match the champion on the frozen benchmark"
            elif k == "version_forward":
                vs[vid]["forward"].append({"epoch": s["epoch"], "challenger": s["challenger"], "champion": s["champion"], "at": ep.available_at})
            elif k == "version_ready":
                vs[vid]["status"] = "awaiting_approval"
                vs[vid]["readiness"] = s
            elif k == "version_approved":
                vs[vid]["approved_by"], vs[vid]["approval_reason"] = s["approver"], s["reason"]
            elif k == "version_promoted":
                if champion and champion in vs:
                    vs[champion]["status"] = "retired"
                vs[vid]["status"], vs[vid]["previous_champion"] = "champion", s.get("previous")
                champion = vid
                history.append({"event": "promoted", "version": vid, "at": ep.available_at, "previous": s.get("previous")})
            elif k == "version_rolled_back":
                vs[vid]["status"], vs[vid]["reason"] = "rolled_back", s["reason"]
                champion = s["restored"]
                vs[champion]["status"] = "champion"
                history.append({"event": "rolled_back", "version": vid, "restored": champion, "at": ep.available_at, "reason": s["reason"]})
            elif k == "champion_vs_previous" and vid in vs:
                vs[vid].setdefault("since_promotion", []).append({"champion": s["champion"], "previous": s["previous"], "at": ep.available_at})
        return {"versions": vs, "champion": champion, "history": history}

    def champion(self) -> tuple[str | None, ResearchPolicy | None]:
        st = self.state()
        c = st["champion"]
        return (c, ResearchPolicy(**st["versions"][c]["policy"])) if c else (None, None)

    # ------------------------------------------------------------------ lifecycle
    def seed(self, policy: ResearchPolicy, as_of: str) -> str:
        if self.state()["champion"]:
            raise LifecycleError("a champion already exists")
        vid = "v1"
        self._emit("version_proposed", {"version": vid, "policy": policy.model_dump(), "origin": "seed", "parent": None,
                                        "rationale": "initial research policy"}, as_of)
        self._emit("version_promoted", {"version": vid, "previous": None, "note": "initial champion; no approval needed for the seed"}, as_of)
        return vid

    def propose(self, changes: dict, *, origin: str, as_of: str, rationale: str = "") -> tuple[str, str | None]:
        """Returns (version id, rejection reason or None)."""
        cid, cpol = self.champion()
        vid = f"v{uuid.uuid4().hex[:6]}"
        try:
            check_protected(changes)
            pol = ResearchPolicy(**{**(cpol.model_dump() if cpol else {}), **changes})
        except ProtectedSettingError as e:
            self._emit("version_proposed", {"version": vid, "policy": {}, "origin": origin, "parent": cid, "rationale": rationale,
                                            "requested_changes": sorted(changes)}, as_of)
            self._emit("version_rejected", {"version": vid, "reason": str(e)}, as_of)
            return vid, str(e)
        except ValueError as e:
            self._emit("version_proposed", {"version": vid, "policy": {}, "origin": origin, "parent": cid, "rationale": rationale,
                                            "requested_changes": sorted(changes)}, as_of)
            self._emit("version_rejected", {"version": vid, "reason": f"invalid policy: {str(e)[:300]}"}, as_of)
            return vid, "invalid policy"
        self._emit("version_proposed", {"version": vid, "policy": pol.model_dump(), "origin": origin, "parent": cid, "rationale": rationale,
                                        "changes": changes}, as_of)
        return vid, None

    def frozen_benchmark(self, vid: str, P, results: dict, cost: float, rows: list[int], *, dataset_hash: str, as_of: str, seed: int = 11) -> dict:
        st = self.state()
        v = st["versions"][vid]
        if v["status"] != "proposed":
            raise LifecycleError(f"{vid} is {v['status']}, not proposed")
        champ = ResearchPolicy(**st["versions"][st["champion"]]["policy"])
        chal = ResearchPolicy(**v["policy"])
        mem = EpisodicMemory(":memory:")                                    # isolated: cannot touch production memory
        arms = [Arm("champion", champ, "frozen:champion"), Arm("challenger", chal, "frozen:challenger")]
        runner = Runner(P, results, mem, cost, arms, seed=seed)
        rets = {"champion": [], "challenger": []}
        for e, d in enumerate(rows):
            decs = [(a, runner.decide(a, e, d)) for a in arms]
            for a, dec in decs:
                rets[a.name].append(runner.settle(a, dec)["epoch_return"])
        cmp = paired(rets["challenger"], rets["champion"])
        lo = cmp.get("ci90", [-1, -1])[0]
        passed = bool(cmp.get("epochs", 0) >= 3 and cmp["mean_diff"] >= 0 and lo > -self.tol * 4)
        result = {"dataset_hash": dataset_hash, "rows": rows, "comparison": cmp, "rule": "mean lift >= 0 and 90% CI lower bound > -4 x tolerance"}
        self._emit("version_benchmarked", {"version": vid, "passed": passed, "result": result}, as_of)
        return {"passed": passed, **result}

    def record_forward(self, vid: str, epoch: int, challenger_ret: float, champion_ret: float, as_of: str) -> None:
        self._emit("version_forward", {"version": vid, "epoch": epoch, "challenger": challenger_ret, "champion": champion_ret}, as_of)

    def evaluate_readiness(self, vid: str, as_of: str, probes: dict) -> dict:
        v = self.state()["versions"][vid]
        if v["status"] != "forward_evaluation":
            return {"ready": False, "reason": f"status is {v['status']}"}
        f = [x for x in v["forward"] if x["at"] > v["proposed_at"]]           # strictly after the proposal
        if len(f) < self.min_forward:
            return {"ready": False, "reason": f"{len(f)} of {self.min_forward} forward windows"}
        cmp = paired([x["challenger"] for x in f], [x["champion"] for x in f])
        if "mean_diff" not in cmp:
            return {"ready": False, "reason": "at least 3 forward windows are needed"}
        try:
            check_protected(v.get("changes") or {})
            clean = True
        except ProtectedSettingError:
            clean = False
        checks = {"forward_lift_positive": cmp["mean_diff"] > 0, "not_clearly_worse": cmp["ci90"][0] > -self.tol * 4,
                  "probes_pass": all(probes.values()), "no_protected_changes": clean}
        if all(checks.values()):
            self._emit("version_ready", {"version": vid, "forward": cmp, "checks": checks, "probes": probes}, as_of)
            return {"ready": True, "forward": cmp, "checks": checks}
        if not checks["not_clearly_worse"] or not checks["probes_pass"]:
            self._emit("version_rejected", {"version": vid, "reason": f"forward evaluation failed: {checks}"}, as_of)
        return {"ready": False, "forward": cmp, "checks": checks}

    def approve(self, vid: str, approver: str, reason: str, as_of: str) -> dict:
        """Promotion by a named human. The API also requires the operator token for this call."""
        name = (approver or "").strip()
        if len(name) < 2 or name.lower().startswith(AGENT_NAMES):
            raise PermissionError("promotion needs a named human approver")
        if len((reason or "").strip()) < 5:
            raise PermissionError("an approval reason is required")
        st = self.state()
        v = st["versions"].get(vid)
        if not v or v["status"] != "awaiting_approval":
            raise LifecycleError(f"{vid} is not awaiting approval")
        self._emit("version_approved", {"version": vid, "approver": name, "reason": reason}, as_of)
        self._emit("version_promoted", {"version": vid, "previous": st["champion"]}, as_of)
        return {"champion": vid, "previous": st["champion"]}

    def record_champion_vs_previous(self, champion_ret: float, previous_ret: float, as_of: str) -> None:
        c = self.state()["champion"]
        self._emit("champion_vs_previous", {"version": c, "champion": champion_ret, "previous": previous_ret}, as_of)

    def monitor(self, as_of: str, probes: dict) -> dict:
        """Automatic rollback. Correctness failures revert immediately; performance regressions revert
        once the promoted champion is reliably behind the version it replaced."""
        st = self.state()
        c = st["champion"]
        v = st["versions"].get(c) if c else None
        prev = v.get("previous_champion") if v else None
        if not v or not prev:
            return {"action": "none", "reason": "no promoted challenger to monitor"}
        if not all(probes.values()):
            reason = f"correctness probe failed: {[k for k, ok in probes.items() if not ok]}"
        else:
            h = v.get("since_promotion", [])
            if len(h) < self.min_forward:
                return {"action": "none", "reason": f"{len(h)} of {self.min_forward} windows since promotion"}
            d = np.array([x["champion"] - x["previous"] for x in h])
            t = d.mean() / (d.std(ddof=1) / math.sqrt(len(d))) if d.std(ddof=1) > 0 else 0.0
            if not (d.mean() < -self.tol and t < self.rollback_t):
                return {"action": "none", "mean_diff": float(d.mean()), "t": float(t)}
            reason = f"forward regression vs previous champion: mean {d.mean():+.4%} per window, t {t:.2f} over {len(d)} windows"
        self._emit("version_rolled_back", {"version": c, "restored": prev, "reason": reason}, as_of)
        return {"action": "rolled_back", "version": c, "restored": prev, "reason": reason}


def propose_variation(policy: ResearchPolicy, rng: np.random.Generator) -> tuple[dict, str]:
    """Meta-optimiser: change one or two tunable knobs within bounds."""
    keys = list(rng.choice(sorted(TUNABLE), size=int(rng.integers(1, 3)), replace=False))
    out = {}
    for k in keys:
        lo, hi, typ = TUNABLE[k]
        cur = getattr(policy, k)
        new = float(np.clip(cur * math.exp(rng.normal(0, 0.35)), lo, hi))
        out[k] = int(round(new)) if typ is int else round(new, 4)
    return out, "meta-optimiser variation of " + ", ".join(f"{k}: {getattr(policy, k)} -> {v}" for k, v in out.items())


def probes(memory: EpisodicMemory, P=None, n_configs: int = 3, seed: int = 0) -> dict:
    """Cheap correctness checks used by readiness and rollback."""
    out = {"memory_chain_intact": memory.verify()["ok"]}
    if P is not None:
        from nseql.evaluation.space import universe
        from nseql.evaluation.vector import weights
        rng = np.random.default_rng(seed)
        T = int(P["ts"].shape[0])
        cut = int(T * 0.6)
        ok = True
        for i in rng.choice(len(universe()), n_configs, replace=False):
            full = weights(universe()[i], P)
            part = weights(universe()[i], P.truncated(cut + 1))
            ok &= bool(np.allclose(full[: cut + 1], part, equal_nan=True))
        out["screener_causal"] = ok
    return out

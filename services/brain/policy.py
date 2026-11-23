"""The research policy: every tunable knob of the Learning Brain, and the guard that keeps
self-modification away from anything safety-related.

A system version is a ResearchPolicy. The schema forbids unknown fields, and before validation
any proposal is scanned for keys that look like broker credentials, execution permissions, risk
limits or safety mechanisms; such a proposal is rejected outright. The Brain package also has no
import path to those subsystems (enforced by a test).
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROTECTED = re.compile(r"(risk|limit|notional|leverage|broker|kite|token|secret|credential|password|api_?key|live|"
                       r"execution|order|route|kill|halt|approv|compliance|margin|capital|sandbox|permission|auth|"
                       r"loss|drawdown|position|exposure|quantity|qty|stop|circuit|paper|session)", re.I)


class ProtectedSettingError(PermissionError):
    pass


def check_protected(proposal: dict, path: str = "") -> None:
    for k, v in proposal.items():
        here = f"{path}.{k}" if path else str(k)
        if PROTECTED.search(str(k)):
            raise ProtectedSettingError(f"'{here}' is a protected setting; the research system may not change it")
        if isinstance(v, dict):
            check_protected(v, here)


class ResearchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    budget: int = Field(2000, ge=50, le=50_000)              # configurations screened per decision
    top_k: int = Field(10, ge=1, le=100)                     # configurations held in the selected portfolio
    explore_frac: float = Field(0.25, ge=0.05, le=1.0)       # share of the budget screened at random
    prior_strength: float = Field(20.0, ge=1.0, le=500.0)    # shrinkage toward the global rate
    meta_c: float = Field(0.5, gt=0.0, le=100.0)             # inverse L2 strength of the meta-model
    fdr_alpha: float = Field(0.20, gt=0.0, lt=1.0)           # Benjamini-Hochberg level on in-sample p-values
    cost_stress: float = Field(2.0, ge=1.0, le=5.0)          # survivors must stay profitable at this cost multiple
    max_corr: float = Field(0.90, gt=0.0, le=1.0)            # de-duplicate near-identical selections
    min_evidence: int = Field(1000, ge=0)                    # outcomes needed before learned components switch on
    eligibility_floor: float = Field(0.40, ge=0.0, le=0.5)   # family success rate below which it is benched in a regime
    eligibility_min_n: int = Field(400, ge=10)
    use_procedural: bool = True                              # learn WHAT to screen
    use_ranking: bool = True                                 # learn HOW to rank (approach choice + meta-model)
    use_eligibility: bool = True                             # regime-conditional benching of families
    use_critic: bool = True                                  # FDR / cost-stress / de-duplication
    fixed_approach: Literal["is_sharpe", "robust", "meta", "random"] = "robust"   # used when use_ranking is off
    approaches: tuple[str, ...] = ("is_sharpe", "robust", "meta")

    @property
    def uses_memory(self) -> bool:
        return self.use_procedural or self.use_ranking or self.use_eligibility

    @staticmethod
    def from_proposal(d: dict) -> "ResearchPolicy":
        check_protected(d)
        return ResearchPolicy(**d)


PROTOCOL_VERSION = 3
# v1 (first development run) compared only critic-gated arms; the critic left ~14 of 2,000 survivors,
# so selection had no room and every critic arm mostly held cash. v2 adds the same comparisons with
# the critic off, and makes every learned component prove forward value before it is used.
ARMS: dict[str, dict] = {
    # critic ON (FDR + cost stress + de-duplication)
    "memory": {},
    "baseline_critic": {"use_procedural": False, "use_ranking": False, "use_eligibility": False},
    # critic OFF — selection has room, so this is where memory can make a difference
    "memory_no_critic": {"use_critic": False},
    "memory_no_critic_screening_only": {"use_critic": False, "use_ranking": False},
    "memory_no_critic_ranking_only": {"use_critic": False, "use_procedural": False, "use_eligibility": False},
    "baseline_greedy": {"use_procedural": False, "use_ranking": False, "use_eligibility": False, "use_critic": False,
                        "fixed_approach": "is_sharpe"},
    "baseline_robust": {"use_procedural": False, "use_ranking": False, "use_eligibility": False, "use_critic": False,
                        "fixed_approach": "robust"},
    "baseline_random": {"use_procedural": False, "use_ranking": False, "use_eligibility": False, "use_critic": False,
                        "fixed_approach": "random"},
}
# v3 (after the negative control): ties at the reference are a coin flip instead of a failure, and
# de-duplication of near-identical selections applies to every ranked arm, not only critic arms.
# pre-declared comparisons (memory arm  vs  the memory-free arm that differs only in memory) (memory arm  vs  the memory-free arm that differs only in memory)
COMPARISONS = [("memory", "baseline_critic"), ("memory_no_critic", "baseline_robust"), ("memory_no_critic", "baseline_greedy"),
               ("memory_no_critic", "baseline_random"), ("memory_no_critic_screening_only", "baseline_robust"),
               ("memory_no_critic_ranking_only", "baseline_robust")]

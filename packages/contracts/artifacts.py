"""Typed artefacts exchanged between research agents."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AGENTS = ("research_director", "market_intelligence", "strategy_inventor", "strategy_developer",
          "backtest_controller", "quant_auditor", "risk_portfolio", "learning", "execution_readiness")


class TaskMessage(BaseModel):
    sender: str
    recipient: str
    task_type: str
    hypothesis_id: str | None = None
    payload: dict = Field(default_factory=dict)


class MarketBrief(BaseModel):
    """Descriptive statistics of the DEVELOPMENT partition only."""
    dataset_id: str
    synthetic: bool
    window: tuple[str, str]
    symbols: int
    sessions: int
    median_annual_vol: float | None = None
    avg_pairwise_correlation: float | None = None
    momentum_score: float | None = None        # rank-IC of 120d return vs next 21d return
    reversal_score: float | None = None        # rank-IC of 3d return vs next 3d return (negative => reversal)
    lag1_autocorrelation: float | None = None
    recent_vol_vs_median: float | None = None
    median_daily_traded_value: float | None = None
    sector_returns: dict[str, float] = Field(default_factory=dict)
    family_priors: dict[str, float] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class CodeArtifact(BaseModel):
    hypothesis_id: str
    code_id: str | None
    sha256: str
    lines: int
    validation_ok: bool
    violations: list[str] = Field(default_factory=list)
    generator: str = "codegen"


class Evidence(BaseModel):
    """Everything the backtest controller measured for one hypothesis."""
    hypothesis_id: str
    chosen_params: dict = Field(default_factory=dict)
    lookahead: dict = Field(default_factory=dict)
    dev: dict = Field(default_factory=dict)
    dev_grid: list[dict] = Field(default_factory=list)
    cost_sensitivity: dict = Field(default_factory=dict)
    stability: dict = Field(default_factory=dict)
    stress: dict = Field(default_factory=dict)
    baselines: dict = Field(default_factory=dict)
    validation: dict = Field(default_factory=dict)
    bootstrap: dict = Field(default_factory=dict)
    monte_carlo: dict = Field(default_factory=dict)
    deflated_sharpe: dict = Field(default_factory=dict)
    regimes: dict = Field(default_factory=dict)
    walk_forward: dict = Field(default_factory=dict)
    pnl_by_symbol: dict = Field(default_factory=dict)
    run_ids: dict = Field(default_factory=dict)
    isolation: dict = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    stage: str = "dev"


class Finding(BaseModel):
    code: str
    severity: Literal["fail", "warn", "info"]
    message: str


class AuditReport(BaseModel):
    hypothesis_id: str
    verdict: Literal["pass", "fail"]
    findings: list[Finding] = Field(default_factory=list)
    reliability_caveats: list[str] = Field(default_factory=list)

    @property
    def fail_codes(self) -> list[str]:
        return [f.code for f in self.findings if f.severity == "fail"]


class RiskReport(BaseModel):
    hypothesis_id: str
    verdict: Literal["pass", "fail"]
    findings: list[Finding] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)


class Decision(BaseModel):
    hypothesis_id: str
    decision: Literal["rejected", "advance_to_sealed", "promoted_to_paper_candidate", "rejected_on_sealed"]
    reasons: list[str] = Field(default_factory=list)
    explanation: str = ""


class ReadinessReport(BaseModel):
    hypothesis_id: str
    status: Literal["PAPER_ONLY", "BLOCKED"]
    checks: list[Finding] = Field(default_factory=list)
    live_trading_enabled: bool = False

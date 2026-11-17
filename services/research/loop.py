"""The controlled autonomous research loop.

    inspect data -> pick an unexplored direction -> falsifiable hypothesis -> generated code
    -> static validation + sandbox -> development backtests -> costs / sensitivity / baselines
    -> chronological validation -> independent audit + risk review -> decision
    -> (finalists only) one sealed out-of-sample evaluation -> lesson -> next experiment

Hard limits: iterations, total backtests, wall-clock seconds and sealed evaluations. The sealed
window is never passed to any job requested on behalf of a hypothesis-generating agent; only
`_sealed_evaluation`, invoked by the director after a passed audit, uses it, once per hypothesis.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import pandas as pd

from nseql.agents import team
from nseql.agents.llm import LLMClient
from nseql.contracts.artifacts import AuditReport, Decision, Evidence, RiskReport, TaskMessage
from nseql.contracts.spec import StrategySpec
from nseql.data.store import Dataset
from nseql.research import sandbox, validation as V
from nseql.research.registry import Registry, SealedAccessError


@dataclass
class ResearchBudget:
    max_iterations: int = 4
    max_backtests: int = 250
    max_wall_seconds: float = 900.0
    max_sealed_evaluations: int = 2
    grid_points: int = 6
    walk_forward: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


class BudgetExhausted(RuntimeError):
    pass


def get_partition(reg: Registry, ds: Dataset) -> V.Partition:
    """The partition of a dataset is fixed the first time it is used and never moves."""
    key = f"partition:{ds.id}:{ds.content_hash[:12]}"
    stored = reg.get(key)
    if stored:
        return V.Partition(**stored)
    p = V.make_partition(ds)
    reg.put(key, p.to_dict())
    reg.audit("system", "PARTITION_FIXED", dataset=ds.id, **p.to_dict())
    return p


class ResearchLoop:
    def __init__(self, data_dir: Path, experiments_dir: Path, registry: Registry, *, dataset_id: str = "synthetic_v1",
                 seed: int = 0, llm: LLMClient | None = None, budget: ResearchBudget | None = None,
                 policy: team.AuditPolicy | None = None, notify: Callable[[str, str], None] | None = None,
                 live_enabled: bool = False, memory=None):
        self.data_dir, self.exp_dir, self.reg = Path(data_dir), Path(experiments_dir), registry
        self.ds = Dataset(self.data_dir / dataset_id)
        self.budget = budget or ResearchBudget()
        self.part = get_partition(registry, self.ds)
        self.intel = team.MarketIntelligenceAgent()
        self.inventor = team.StrategyInventor(seed, llm)
        self.developer = team.StrategyDeveloper()
        self.auditor = team.SkepticalQuantAuditor(policy)
        self.risk = team.RiskPortfolioAgent()
        self.learning = team.LearningAgent()
        self.readiness = team.ExecutionReadinessAgent()
        self.notify = notify or (lambda event, text: None)
        self.live_enabled = live_enabled
        self.episodic = memory          # optional nseql.brain.memory.EpisodicMemory: every hypothesis is recorded there too
        self.backtests = 0
        self.sealed_used = 0
        self.t0 = 0.0
        self.eid: str | None = None
        self.progress: Callable[[dict], None] = lambda d: None

    # ------------------------------------------------------------------ plumbing
    def _msg(self, sender: str, recipient: str, task: str, hid: str | None = None, **payload) -> None:
        m = TaskMessage(sender=sender, recipient=recipient, task_type=task, hypothesis_id=hid, payload=payload)
        self.reg.add_message(m.sender, m.recipient, m.task_type, m.payload, self.eid, hid)
        self.progress({"sender": sender, "recipient": recipient, "task": task, "hypothesis_id": hid})

    def _check_budget(self, need: int = 0) -> None:
        if self.backtests + need > self.budget.max_backtests:
            raise BudgetExhausted(f"backtest budget of {self.budget.max_backtests} would be exceeded")
        if time.monotonic() - self.t0 > self.budget.max_wall_seconds:
            raise BudgetExhausted(f"wall-clock budget of {self.budget.max_wall_seconds:.0f}s exceeded")

    def _evaluator(self, source: str, end: str) -> V.Evaluator:
        """An evaluator whose every job is clamped to `end` — the partition boundary it may see."""
        loop = self

        class Clamped(V.Evaluator):
            def run_many(self, variants):
                for v in variants:
                    if v.get("end") is None or v["end"] > end:
                        v["end"] = end
                loop._check_budget(len(variants))
                loop.backtests += len(variants)
                return super().run_many(variants)
        return Clamped(self.data_dir, "generated", source=source, base={"dataset_id": self.ds.id},
                       out_dir=self.exp_dir / "runs")

    def _record(self, r: dict, hid: str, partition: str, kind: str = "backtest") -> str | None:
        if not r.get("summary"):
            return None
        self.reg.record_run(r["run_id"], r["summary"], kind=kind, partition=partition, experiment_id=self.eid,
                            hypothesis_id=hid, path=r.get("path"))
        return r["run_id"]

    # ------------------------------------------------------------------ one hypothesis
    def _dev_stage(self, spec: StrategySpec, source: str, ev: Evidence) -> V.Evaluator:
        p = self.part
        e = self._evaluator(source, p.dev_end)
        self._check_budget(2)
        self.backtests += 2
        la = sandbox.run_jobs(source, [{"id": "la", "kind": "lookahead_audit",
                                        "config": {"dataset_id": self.ds.id, "start": p.dev_start, "end": p.dev_end,
                                                   "strategies": [{"name": "generated", "params": {}}]}}], self.data_dir)
        ev.isolation = la["isolation"]
        if la["results"]:
            ev.lookahead = {k: la["results"][0].get(k) for k in ("passed", "orders_checked", "first_difference", "error", "method")}
        else:
            ev.errors.append(f"look-ahead audit could not run: {la['error']}")
            return e
        pts = V.grid_points(spec.param_grid, self.budget.grid_points) or [{}]
        grid = e.run_many([{"params": pt, "start": p.dev_start, "end": p.dev_end} for pt in pts])
        scores = []
        for pt, r in zip(pts, grid):
            self._record(r, spec.id, "dev")
            sc = V.objective(r["summary"]) if r["ok"] else float("-inf")
            scores.append(sc)
            ev.dev_grid.append({"params": pt, "sharpe": r["summary"]["metrics"].get("sharpe") if r.get("summary") else None,
                                "trades": r["summary"]["metrics"].get("trades") if r.get("summary") else None,
                                "error": r.get("error")})
        if all(not r["ok"] for r in grid):
            ev.errors.append(f"every development backtest failed: {grid[0].get('error')}")
            return e
        best = max(range(len(pts)), key=lambda i: scores[i])
        ev.chosen_params = {**spec.params, **pts[best]}
        main = e.run(params=ev.chosen_params, start=p.dev_start, end=p.dev_end, save=True)
        ev.run_ids["dev"] = self._record(main, spec.id, "dev")
        if not main["ok"]:
            ev.errors.append(f"development run failed: {main.get('error')}")
            return e
        ev.dev = {"metrics": main["summary"]["metrics"], "flags": main["summary"]["flags"],
                  "reconciliation_ok": main["summary"]["reconciliation"]["ok"]}
        for t in main["trades"]:
            ev.pnl_by_symbol[t["symbol"]] = ev.pnl_by_symbol.get(t["symbol"], 0.0) + t["net"]
        ev.cost_sensitivity = V.cost_sensitivity(e, ev.chosen_params, p.dev_start, p.dev_end)
        ev.stability = V.parameter_stability(e, ev.chosen_params, spec.param_grid, p.dev_start, p.dev_end)
        ev.stress = V.stress_tests(e, ev.chosen_params, p.dev_start, p.dev_end)
        ev.baselines = V.baseline_comparison(e, ev.chosen_params, p.dev_start, p.dev_end)
        ev.monte_carlo = V.monte_carlo_trades([t["net"] for t in main["trades"]], spec.capital)
        return e

    def _validation_stage(self, spec: StrategySpec, source: str, ev: Evidence) -> pd.Series | None:
        p = self.part
        e = self._evaluator(source, p.val_end)
        r = e.run(params=ev.chosen_params, start=p.dev_start, end=p.val_end, save=True)
        ev.run_ids["validation"] = self._record(r, spec.id, "validation")
        ev.stage = "validation"
        if not r["ok"]:
            ev.errors.append(f"validation run failed: {r.get('error')}")
            return None
        daily = r["daily"]
        ev.validation = V.window_metrics(daily, r["trades"], p.val_start, p.val_end)
        rets = daily.pct_change().dropna()
        ev.bootstrap = V.block_bootstrap_sharpe(rets.to_numpy())
        ev.deflated_sharpe = V.deflated_sharpe(rets.to_numpy(), n_trials=self.reg.trial_count())
        idx = next((s for s, i in self.ds.instruments.items() if i.kind.value == "INDEX"), None)
        if idx:
            b = self.ds.bars(idx, "1d", p.dev_start, p.val_end)
            ev.regimes = V.regime_breakdown(daily, pd.Series(b["close"].to_numpy(), index=b["ts"]))
        if self.budget.walk_forward and spec.param_grid:
            try:
                wf = V.walk_forward(e, spec.param_grid, start=p.dev_start, end=p.val_end, train_days=250, test_days=60,
                                    max_points=4, dataset=self.ds, base_params=spec.params)
                wf.pop("curve", None)
                ev.walk_forward = wf
            except BudgetExhausted:
                ev.walk_forward = {"skipped": "budget exhausted before walk-forward"}
        return daily

    def _sealed_evaluation(self, spec: StrategySpec, source: str, ev: Evidence) -> dict:
        """The only code path that reads the sealed window. Director-only, once per hypothesis."""
        self.reg.claim_sealed(spec.id, "research_director")
        self.sealed_used += 1
        p = self.part
        e = self._evaluator(source, p.sealed_end)
        r = e.run(params=ev.chosen_params, start=p.dev_start, end=p.sealed_end, save=True)
        rid = self._record(r, spec.id, "sealed", kind="sealed")
        if not r["ok"]:
            return {"error": r.get("error"), "run_id": rid}
        m = V.window_metrics(r["daily"], r["trades"], p.sealed_start, p.sealed_end)
        m["run_id"] = rid
        v = ev.validation.get("sharpe")
        m["deterioration_vs_validation"] = (v - m["sharpe"]) if v is not None and m["sharpe"] is not None else None
        return m

    def _explain(self, spec: StrategySpec, ev: Evidence, audit: AuditReport | None, risk: RiskReport | None,
                 decision: str, sealed: dict | None, lesson: dict | None) -> str:
        p = self.part
        dm = ev.dev.get("metrics", {})
        f = lambda x, pat="{:.2f}": "n/a" if x is None else pat.format(x)
        lines = [
            f"WHAT WAS TESTED: {spec.hypothesis} Entry `{spec.entry}`, exit `{spec.exit}`, parameters {ev.chosen_params or spec.params}.",
            f"WHY: {spec.rationale or 'selected by the research director from the hypothesis grammar'} (origin: {spec.origin}).",
            f"INFORMATION AVAILABLE: hypothesis and parameters were chosen using only {p.dev_start}..{p.dev_end} "
            f"(development). Validation {p.val_start}..{p.val_end} was used once with parameters frozen. "
            f"The sealed window {p.sealed_start}..{p.sealed_end} was " + ("opened once for the final check." if sealed else "not opened."),
            f"RESULT: development Sharpe {f(dm.get('sharpe'))}, {dm.get('trades', 0)} trades, max drawdown {f(dm.get('max_drawdown'), '{:.1%}')}, "
            f"costs Rs {f(dm.get('total_costs'), '{:,.0f}')}"
            + (f"; validation Sharpe {f(ev.validation.get('sharpe'))} over {ev.validation.get('trades', 0)} trades" if ev.validation else "")
            + (f"; sealed Sharpe {f(sealed.get('sharpe'))}, return {f(sealed.get('total_return'), '{:.1%}')}" if sealed and 'sharpe' in sealed else "") + ".",
            "FAILED CHECKS: " + ("; ".join(x.message for x in (audit.findings if audit else []) if x.severity == "fail")
                                 + "; ".join(x.message for x in (risk.findings if risk else []) if x.severity == "fail") or "none"),
            "WHY THIS MAY BE UNRELIABLE: " + "; ".join((audit.reliability_caveats if audit else [])
                                                         + [x.message for x in (audit.findings if audit else []) if x.severity == "warn"]),
            f"DECISION: {decision}.",
            f"NEXT EXPERIMENT: {lesson['next_step'] if lesson else 'n/a'}",
        ]
        return "\n".join(lines)

    def _evaluate(self, spec: StrategySpec, existing: dict[str, pd.Series]) -> dict:
        hid = spec.id
        self.reg.add_hypothesis(spec.model_dump(), spec.structural_hash(), self.eid)
        self._msg("strategy_inventor", "strategy_developer", "IMPLEMENT_SPEC", hid, family=spec.family, entry=spec.entry, exit=spec.exit)
        code, source = self.developer.build(spec, self.reg)
        self.reg.add_artifact("CodeArtifact", "strategy_developer", code.model_dump(), self.eid, hid)
        ev = Evidence(hypothesis_id=hid)
        audit = risk = None
        sealed = None
        daily = None
        if not code.validation_ok:
            ev.errors.append("generated code failed static validation: " + "; ".join(code.violations[:3]))
            audit = self.auditor.review(ev, self.ds.synthetic)
        else:
            self._msg("strategy_developer", "backtest_controller", "RUN_DEVELOPMENT_TESTS", hid, code_id=code.code_id, sha256=code.sha256)
            self._dev_stage(spec, source, ev)
            self._msg("backtest_controller", "quant_auditor", "AUDIT_DEVELOPMENT", hid, run=ev.run_ids.get("dev"))
            audit = self.auditor.review(ev, self.ds.synthetic)
            if audit.verdict == "pass":
                self._msg("quant_auditor", "backtest_controller", "RUN_VALIDATION", hid)
                daily = self._validation_stage(spec, source, ev)
                self._msg("backtest_controller", "quant_auditor", "AUDIT_VALIDATION", hid, run=ev.run_ids.get("validation"))
                audit = self.auditor.review(ev, self.ds.synthetic)
                risk = self.risk.review(ev, daily, existing)
                self.reg.add_artifact("RiskReport", "risk_portfolio", risk.model_dump(), self.eid, hid)
                self._msg("risk_portfolio", "research_director", "RISK_REVIEW", hid, verdict=risk.verdict)
        self.reg.add_artifact("AuditReport", "quant_auditor", audit.model_dump(), self.eid, hid)
        self._msg("quant_auditor", "research_director", "AUDIT_VERDICT", hid, verdict=audit.verdict, fail_codes=audit.fail_codes)
        passed = audit.verdict == "pass" and risk is not None and risk.verdict == "pass"
        decision = "rejected"
        reasons = audit.fail_codes + ([x.code for x in risk.findings if x.severity == "fail"] if risk else [])
        if passed:
            decision = "advance_to_sealed"
            if self.sealed_used < self.budget.max_sealed_evaluations:
                try:
                    sealed = self._sealed_evaluation(spec, source, ev)
                    ok = sealed.get("sharpe") is not None and sealed["sharpe"] > 0 and (sealed.get("total_return") or 0) > 0
                    decision = "promoted_to_paper_candidate" if ok else "rejected_on_sealed"
                    if not ok:
                        reasons.append("SEALED_FAILED")
                except SealedAccessError as e:
                    reasons.append(f"SEALED_ACCESS_DENIED: {e}")
            else:
                reasons.append("sealed-evaluation budget exhausted; awaiting a later run")
        d = Decision(hypothesis_id=hid, decision=decision, reasons=reasons)
        lesson = self.learning.reflect(spec, d, audit, risk)
        d.explanation = self._explain(spec, ev, audit, risk, decision, sealed, lesson)
        status = {"promoted_to_paper_candidate": "paper_candidate"}.get(decision, decision)
        self.reg.set_hypothesis_status(hid, status, "; ".join(reasons) or "passed all checks")
        self.reg.add_artifact("Evidence", "backtest_controller", ev.model_dump(), self.eid, hid)
        self.reg.add_artifact("Decision", "research_director", {**d.model_dump(), "sealed": sealed}, self.eid, hid)
        self.reg.add_lesson(lesson["family"], lesson["outcome"], lesson["text"], lesson["evidence"], lesson["next_step"], self.eid, hid)
        self._msg("research_director", "learning", "DECISION", hid, decision=decision, reasons=reasons)
        if decision == "promoted_to_paper_candidate":
            rd = self.readiness.check(spec, ev, synthetic=self.ds.synthetic, live_enabled=self.live_enabled)
            self.reg.add_artifact("ReadinessReport", "execution_readiness", rd.model_dump(), self.eid, hid)
            self._msg("execution_readiness", "research_director", "READINESS", hid, status=rd.status)
            if daily is not None:
                existing[hid] = daily
                self.reg.put(f"candidate_equity:{hid}", {"dates": [str(x.date()) for x in daily.index], "values": [float(x) for x in daily]})
            self.reg.put(f"candidate_params:{hid}", ev.chosen_params)
            self.notify("candidate", f"Research: {hid} ({spec.family}) passed audit and the sealed check; now a PAPER candidate. "
                                     + ("SYNTHETIC data. " if self.ds.synthetic else "") + "Not approved for live trading.")
        if self.episodic is not None:
            self.episodic.append("research_loop", "hypothesis", {
                "experiment": self.eid, "hypothesis_id": hid, "family": spec.family, "statement": spec.hypothesis, "origin": spec.origin,
                "parent_id": spec.parent_id, "decision": decision, "reasons": reasons, "dataset": self.ds.id, "dataset_hash": self.ds.content_hash,
                "dev_sharpe": ev.dev.get("metrics", {}).get("sharpe"), "validation_sharpe": ev.validation.get("sharpe"),
                "sealed_opened": sealed is not None, "lookahead_passed": (ev.lookahead or {}).get("passed")},
                available_at=self.part.val_end, decided_at=self.part.val_end)
        return {"hypothesis_id": hid, "family": spec.family, "origin": spec.origin, "parent_id": spec.parent_id,
                "hypothesis": spec.hypothesis, "decision": decision, "reasons": reasons, "explanation": d.explanation,
                "dev_sharpe": ev.dev.get("metrics", {}).get("sharpe"), "validation_sharpe": ev.validation.get("sharpe"),
                "sealed": sealed, "run_ids": ev.run_ids, "code_id": code.code_id, "directive": lesson["directive"],
                "next_step": lesson["next_step"], "spec": spec}

    # ------------------------------------------------------------------ the loop
    def run(self, title: str = "autonomous research run") -> dict:
        self.t0 = time.monotonic()
        self.eid = self.reg.start_experiment(title, self.budget.to_dict())
        self.reg.audit("research_director", "EXPERIMENT_STARTED", experiment=self.eid, budget=self.budget.to_dict())
        self._msg("research_director", "market_intelligence", "DESCRIBE_DEVELOPMENT_DATA", window=[self.part.dev_start, self.part.dev_end])
        brief = self.intel.brief(self.ds, self.part.dev_start, self.part.dev_end)
        self.reg.add_artifact("MarketBrief", "market_intelligence", brief.model_dump(), self.eid)
        existing = {}
        for h in self.reg.hypotheses(500, status="paper_candidate"):
            eq = self.reg.get(f"candidate_equity:{h['id']}")
            if eq:
                existing[h["id"]] = pd.Series(eq["values"], index=pd.to_datetime(eq["dates"]))
        results, stop = [], "completed"
        pending: tuple[StrategySpec, str] | None = None
        for it in range(self.budget.max_iterations):
            try:
                self._check_budget()
                memory = self.reg.generation_view()                # sealed results are not in this view
                family, scores = self.learning.choose_family(memory, brief.family_priors)
                if pending:
                    family = pending[0].family
                self._msg("learning", "research_director", "DIRECTION", family=family,
                          scores={k: round(v["ucb"], 3) for k, v in scores.items()}, mutation=pending[1] if pending else None)
                self._msg("research_director", "strategy_inventor", "PROPOSE_HYPOTHESIS", family=family, iteration=it + 1)
                spec = self.inventor.propose(family, brief, memory, self.reg.seen_structures(), pending)
                pending = None
                if spec is None:
                    self._msg("strategy_inventor", "research_director", "NO_NEW_IDEA", family=family)
                    self.reg.add_lesson(family, "exhausted", f"[{family}] no untested structure left in the hypothesis grammar.",
                                        {}, "extend the grammar or enable the LLM inventor", self.eid)
                    self.reg.add_hypothesis({"id": f"exhausted_{self.eid}_{it}", "family": family, "hypothesis": "(none: grammar exhausted)",
                                             "falsification": "", "origin": "system"}, f"exhausted_{self.eid}_{it}", self.eid)
                    self.reg.set_hypothesis_status(f"exhausted_{self.eid}_{it}", "rejected", "grammar exhausted")
                    continue
                out = self._evaluate(spec, existing)
                results.append(out)
                if out["directive"]:
                    pending = (out.pop("spec"), out["directive"])
                else:
                    out.pop("spec")
            except BudgetExhausted as e:
                stop = f"budget_exhausted: {e}"
                break
        for r in results:
            r.pop("spec", None)
        usage = {"backtests": self.backtests, "sealed_evaluations": self.sealed_used, "iterations": len(results),
                 "wall_seconds": round(time.monotonic() - self.t0, 1)}
        last = self.reg.lessons(1)
        summary = {"experiment_id": self.eid, "status": stop, "dataset": self.ds.id, "synthetic": self.ds.synthetic,
                   "partition": self.part.to_dict(), "usage": usage, "budget": self.budget.to_dict(),
                   "inventor": "llm+grammar" if self.inventor.llm else "grammar (no LLM configured)",
                   "inventor_log": self.inventor.log, "results": results,
                   "candidates": [r["hypothesis_id"] for r in results if r["decision"] == "promoted_to_paper_candidate"],
                   "next_experiment": last[0]["next_step"] if last else None,
                   "disclaimer": "No result here is a prediction or a promise of profit."}
        self.reg.finish_experiment(self.eid, stop, usage, summary)
        self.reg.audit("research_director", "EXPERIMENT_FINISHED", experiment=self.eid, **usage)
        self.notify("research_done", f"Research run {self.eid}: {len(results)} hypotheses tested, "
                                     f"{len(summary['candidates'])} paper candidate(s), {usage['backtests']} backtests.")
        return summary

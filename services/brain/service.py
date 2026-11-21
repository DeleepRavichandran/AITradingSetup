"""Brain service: the single place the API and CLI go through."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np

from nseql.brain import benchmark as bench
from nseql.brain.memory import EpisodicMemory
from nseql.brain.policy import PROTOCOL_VERSION, ResearchPolicy
from nseql.brain.versions import SystemVersions, probes, propose_variation
from nseql.data.store import Dataset

KPI_DEFS = [
    ("oos_lift", "Out-of-sample lift", "Mean forward return per decision window, memory arm minus its memory-free counterpart (same budget, same "
     "critic), with block-bootstrap 90% CI and Holm-adjusted p across the pre-declared comparisons", "90% CI lower bound > 0"),
    ("forward_sharpe", "Forward Sharpe", "Annualised Sharpe of the stitched forward (unseen) returns of the selected portfolios", "memory >= counterpart"),
    ("calibration", "Calibration", "Prequential Brier skill of the meta-model's P(success) versus predicting the historical base rate, plus ECE", "Brier skill > 0"),
    ("research_efficiency", "Research efficiency", "Screen quality: share of screened configurations whose forward return beat the universe median "
     "(ties count half); 0.5 = no better than random screening", "memory > counterpart, CI > 0"),
    ("regime_robustness", "Regime robustness", "Worst mean forward return across market regimes with at least 3 decisions", "memory >= counterpart"),
    ("diversification", "Diversification", "Effective number of independent bets among the selected configurations (eigenvalue participation ratio)", "higher is better"),
    ("multiple_testing", "Overfitting control", "Mean probability of backtest overfitting (CSCV) of each screened batch, and trials counted", "PBO < 0.5"),
    ("negative_control", "Negative control", "Same protocol on a zero-drift random walk: the memory lift's 90% CI must include zero", "CI includes 0"),
    ("paper_forward", "Forward paper trading", "Return of the champion's selections traded in a paper session", "not measured yet"),
]


class BrainService:
    def __init__(self, experiments_dir: Path, data_dir: Path):
        self.root = Path(experiments_dir) / "brain"
        self.data_dir = Path(data_dir)
        (self.root / "reports").mkdir(parents=True, exist_ok=True)
        self.memory = EpisodicMemory(self.root / "memory.sqlite")
        self.versions = SystemVersions(self.memory)

    # ------------------------------------------------------------------ reports
    def reports(self) -> list[dict]:
        out = []
        for p in sorted((self.root / "reports").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            r = json.loads(p.read_text())
            out.append({"run_id": r["run_id"], "label": r.get("label"), "created": r["created"], "dataset": r["dataset"],
                        "protocol": r["protocol"], "verdict": r["verdict"], "verdict_basis": r.get("verdict_basis")})
        return out

    def report(self, run_id: str) -> dict:
        p = self.root / "reports" / f"{run_id}.json"
        if "/" in run_id or not p.exists():
            raise KeyError(run_id)
        r = json.loads(p.read_text())
        # Reports written before the Holm adjustment existed get it here: pure arithmetic on the stored
        # p-values, no data is re-read and no window (sealed or otherwise) is re-used.
        for comps in (r.get("comparisons"), (r.get("sealed_segment") or {}).get("comparisons")):
            if comps and any("p_holm" not in v.get("forward_return", {}) for v in comps.values()):
                bench.holm(comps)
        return r

    def run_benchmark(self, dataset_id: str, *, end_date: str | None, label: str = "", run_id: str | None = None,
                      cost: float | None = None, sealed_from: str | None = None, start_date: str | None = None, progress=None) -> dict:
        ds = Dataset(self.data_dir / dataset_id)
        rep = bench.run(ds, self.root / "work", self.memory, run_id=run_id, end_date=end_date, start_date=start_date, label=label,
                        cost=cost, sealed_from=sealed_from, progress=progress)
        bench.save(rep, self.root / "reports")
        return rep

    # ------------------------------------------------------------------ KPIs
    def kpis(self, main: dict | None, null: dict | None = None) -> list[dict]:
        rows = []
        prim = None
        if main:
            x, y = main["protocol"]["primary_comparison"]
            src = (main.get("sealed_segment") or {}).get("comparisons") or main["comparisons"]
            prim = src.get(f"{x}_vs_{y}")
        for key, name, definition, target in KPI_DEFS:
            row = {"key": key, "name": name, "definition": definition, "target": target, "value": None, "baseline": None, "status": "not measured", "evidence": None}
            if main and prim:
                X, Y = main["arms"][x], main["arms"][y]
                if key == "oos_lift":
                    f = prim["forward_return"]
                    row.update(value=f["mean_diff"], evidence=f, status="met" if f["ci90"][0] > 0 else "not met")
                elif key == "forward_sharpe":
                    row.update(value=X["forward"].get("sharpe"), baseline=Y["forward"].get("sharpe"),
                               status="met" if (X["forward"].get("sharpe") or -9) >= (Y["forward"].get("sharpe") or -9) else "not met")
                elif key == "calibration":
                    c = X["kpis"].get("calibration")
                    if c:
                        row.update(value=c["brier_skill"], evidence=c, status="met" if (c["brier_skill"] or -1) > 0 else "not met")
                elif key == "research_efficiency":
                    q = prim["screen_quality"]
                    row.update(value=X["kpis"]["screen_quality"], baseline=Y["kpis"]["screen_quality"], evidence=q,
                               status="met" if q.get("ci90", [0])[0] > 0 else "not met")
                elif key == "regime_robustness":
                    a, b = X["kpis"]["worst_regime_mean"], Y["kpis"]["worst_regime_mean"]
                    row.update(value=a, baseline=b, evidence={"memory": X["kpis"]["regime"], "baseline": Y["kpis"]["regime"]},
                               status="not measured" if a is None or b is None else ("met" if a >= b else "not met"))
                elif key == "diversification":
                    row.update(value=X["kpis"]["effective_bets"], baseline=Y["kpis"]["effective_bets"], status="reported")
                elif key == "multiple_testing":
                    pb = X["kpis"]["mean_pbo"]
                    row.update(value=pb, evidence={"trials": X["kpis"]["trials"], "holm_p": prim["forward_return"].get("p_holm")},
                               status="not measured" if pb is None else ("met" if pb < 0.5 else "not met"))
            if key == "negative_control" and null:
                nx, ny = null["protocol"]["primary_comparison"]
                f = null["comparisons"][f"{nx}_vs_{ny}"]["forward_return"]
                row.update(value=f["mean_diff"], evidence=f, status="met" if f["ci90"][0] <= 0 <= f["ci90"][1] else "not met (false positive)")
            if key == "paper_forward":
                row["evidence"] = ("screened configurations cannot yet be traded in a paper session; the paper engine runs library "
                                   "strategies only. Needs a library adapter for screened configurations.")
            rows.append(row)
        return rows

    # ------------------------------------------------------------------ system versions
    def ensure_seed(self) -> None:
        if self.versions.state()["champion"] is None:
            self.versions.seed(ResearchPolicy(), date.today().isoformat())

    def propose(self, changes: dict | None, rationale: str, seed: int | None = None) -> dict:
        self.ensure_seed()
        _, champ = self.versions.champion()
        if changes is None:
            changes, rationale = propose_variation(champ, np.random.default_rng(seed))
        vid, err = self.versions.propose(changes, origin="operator" if rationale else "meta_optimizer", as_of=date.today().isoformat(),
                                         rationale=rationale)
        return {"version": vid, "rejected": err, "changes": changes}

    def frozen_benchmark(self, vid: str, dataset_id: str, n_epochs: int = 12) -> dict:
        ds = Dataset(self.data_dir / dataset_id)
        P, res, cost, _ = bench.prepare(ds, self.root / "work")
        rows = bench.decision_rows(P)[:n_epochs]
        return self.versions.frozen_benchmark(vid, P, res, cost, rows, dataset_hash=ds.content_hash, as_of=date.today().isoformat())

    def approve(self, vid: str, approver: str, reason: str) -> dict:
        return self.versions.approve(vid, approver, reason, date.today().isoformat())

    def probes(self, dataset_id: str | None = None) -> dict:
        P = None
        if dataset_id:
            from nseql.evaluation import cache as cache_mod
            P = cache_mod.load(cache_mod.build(Dataset(self.data_dir / dataset_id), self.root / "work" / "cache"))
        return probes(self.memory, P)

    def summary(self) -> dict:
        self.ensure_seed()
        st = self.versions.state()
        reps = self.reports()
        main = next((self.report(r["run_id"]) for r in reps if not r["dataset"]["id"].startswith("null")), None)
        null = next((self.report(r["run_id"]) for r in reps if r["dataset"]["id"].startswith("null")), None)
        return {"protocol_version": PROTOCOL_VERSION, "champion": st["champion"], "versions": list(st["versions"].values()),
                "history": st["history"], "memory": {"episodes": self.memory.count(), "namespaces": len(self.memory.namespaces())},
                "reports": reps[:20], "kpis": self.kpis(main, null), "main_report": main["run_id"] if main else None,
                "null_report": null["run_id"] if null else None}

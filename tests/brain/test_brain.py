"""Learning Brain: immutable memory, point-in-time learning, learning a planted signal but not noise,
self-validation, self-criticism, protected settings, and controlled self-modification with rollback."""
import ast
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from nseql.brain import benchmark as bench
from nseql.brain.critic import benjamini_hochberg, pbo
from nseql.brain.diagnostics import HistoryView, LookAheadError
from nseql.brain.learner import BrainState
from nseql.brain.memory import EpisodicMemory
from nseql.brain.policy import ARMS, ProtectedSettingError, ResearchPolicy, check_protected
from nseql.brain.runner import Arm, Runner
from nseql.brain.versions import LifecycleError, SystemVersions, probes
from nseql.evaluation.space import facet_table, universe


# ------------------------------------------------------------------ episodic memory
def test_memory_is_append_only_point_in_time_and_tamper_evident(tmp_path):
    m = EpisodicMemory(tmp_path / "m.sqlite")
    m.append("a", "outcome", {"x": 1}, available_at="2023-01-10", arrays={"v": np.arange(3)})
    m.append("a", "outcome", {"x": 2}, available_at="2023-02-10")
    m.append("b", "outcome", {"x": 3}, available_at="2023-01-05")
    assert [e.summary["x"] for e in m.read("a", as_of="2023-01-31")] == [1]
    assert m.read("a")[0].arrays["v"].tolist() == [0, 1, 2] and m.verify()["ok"]
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        m.db.execute("UPDATE episodes SET summary = '{}' WHERE seq = 1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        m.db.execute("DELETE FROM episodes")
    raw = sqlite3.connect(tmp_path / "m.sqlite")          # an attacker who bypasses the triggers...
    raw.execute("DROP TRIGGER episodes_no_update")
    raw.execute("""UPDATE episodes SET summary = '{"x":99}' WHERE seq = 2""")
    raw.commit()
    v = EpisodicMemory(tmp_path / "m.sqlite").verify()
    assert v == {"ok": False, "rows": 2, "first_bad_seq": 2}     # ...is detected


def test_history_view_refuses_future_rows(screened):
    v = HistoryView(screened["res"]["R"], screened["res"]["G"], screened["res"]["E"], 300, screened["cost"])
    assert v.window([0, 1], 250).shape == (2, 250)
    with pytest.raises(LookAheadError):
        v.rows([0], 290, 302)


def test_brain_state_cannot_move_backwards():
    s = BrainState(EpisodicMemory(), "x")
    s.advance("2023-05-01")
    with pytest.raises(ValueError):
        s.advance("2023-04-01")


def test_learning_is_point_in_time_future_outcomes_cannot_change_past_decisions(screened):
    """Two worlds that agree up to a cut and differ (noise) afterwards. Every decision made at or before the
    cut must be identical: what was screened, what was selected and how it was ranked."""
    P, res = screened["P"], screened["res"]
    pol = ResearchPolicy(budget=300, min_evidence=200, eligibility_min_n=50)
    rows = bench.decision_rows(P)[:9]
    cut = rows[6]
    rng = np.random.default_rng(5)
    worlds = []
    for noisy in (False, True):
        arrs = {k: np.array(res[k], dtype=np.float32 if k != "E" else np.float16) for k in ("R", "G", "E")}
        if noisy:
            for k in ("R", "G"):
                arrs[k][:, cut + 1:] = rng.normal(0, 0.02, arrs[k][:, cut + 1:].shape)
        mem = EpisodicMemory()
        arm = Arm("memory", pol, "w")
        run = Runner(P, arrs, mem, screened["cost"], [arm], seed=3)
        decs = []
        for e, d in enumerate(rows):
            dec = run.decide(arm, e, d)
            decs.append((d, dec.screened.tolist(), {a: v.tolist() for a, v in dec.selections.items()}, dec.approach))
            run.settle(arm, dec)
        worlds.append(decs)
    assert any(x[3] for x in worlds[0])
    for (d, *a), (_, *b) in zip(*worlds):
        if d <= cut:
            assert a == b, f"decision at row {d} changed when only data after row {cut} changed"
    assert worlds[0][-1] != worlds[1][-1]                 # sanity: later decisions do react to the different data


# ------------------------------------------------------------------ learns a planted signal, not noise
def _plant(mem, ns, family_rate: dict, epochs=12, n=600, seed=0, regime="midvol_up"):
    rng = np.random.default_rng(seed)
    codes, values = facet_table()[1]["family"], facet_table()[0]["family"]
    for e in range(epochs):
        idx = rng.choice(len(universe()), n, replace=False)
        p = np.array([family_rate.get(values[c], 0.5) for c in codes[idx]])
        lab = (rng.random(n) < p).astype(np.int8)
        mem.append(ns, "outcome", {"regime": regime, "epoch": e}, available_at=f"2023-01-{e + 10:02d}",
                   arrays={"idx": idx.astype(np.int32), "diag": rng.normal(size=(n, 10)).astype(np.float32), "fwd": lab.astype(np.float32),
                           "label": lab, "explore": np.ones(n, bool)})


def test_semantic_and_procedural_memory_learn_a_planted_regularity():
    mem = EpisodicMemory()
    _plant(mem, "s", {"trend": 0.75, "meanrev": 0.25})
    st = BrainState(mem, "s")
    st.advance("2023-12-31")
    f = {(x["facet"], x["value"], x["regime"]) for x in st.findings()}
    assert ("family", "trend", "all") in f and ("family", "meanrev", "all") in f
    assert not any(k[0] == "sizing" for k in f)                         # nothing was planted on sizing
    fam = np.array([c.family for c in universe()])
    sc = np.mean([st.thompson_scores("midvol_up", 20.0, np.random.default_rng(i)) for i in range(5)], axis=0)
    assert sc[fam == "trend"].mean() > sc[fam == "momentum"].mean() > sc[fam == "meanrev"].mean()
    el = st.eligibility("midvol_up", 20.0, 0.40, 200)
    assert el["meanrev"]["eligible"] is False and el["trend"]["eligible"] is True


def test_no_findings_in_pure_noise():
    mem = EpisodicMemory()
    _plant(mem, "n", {}, epochs=12, seed=9)
    st = BrainState(mem, "n")
    st.advance("2023-12-31")
    assert st.findings() == []                                            # multiple-testing control holds
    assert all(v["eligible"] for v in st.eligibility("midvol_up", 20.0, 0.40, 200).values())


def test_learned_components_must_prove_themselves_before_they_are_trusted():
    mem = EpisodicMemory()
    rng = np.random.default_rng(2)
    for e in range(6):
        n = 1000
        lab = (rng.random(n) < 0.5).astype(np.int8)
        ex = np.r_[np.ones(500, bool), np.zeros(500, bool)]
        lab[~ex] = (rng.random(500) < 0.40).astype(np.int8)                  # the exploit half does WORSE than random
        mem.append("v", "outcome", {"regime": "lowvol_up"}, available_at=f"2023-03-{e + 10:02d}",
                   arrays={"idx": rng.choice(len(universe()), n, replace=False).astype(np.int32), "diag": np.zeros((n, 10), np.float32),
                           "fwd": lab.astype(np.float32), "label": lab, "explore": ex,
                           "meta_pred": np.clip(0.5 + rng.normal(0, 0.3, n), 0, 1).astype(np.float32)})   # confident but useless
    st = BrainState(mem, "v")
    st.advance("2023-12-31")
    assert st.screening_trusted() is False and st.screening_evidence()["lift"] < 0
    assert st.meta_trusted() is False and st.meta_skill() < 0


def test_tie_labels_do_not_penalise_flat_configurations(screened):
    """Regression for the bug the negative control found: a configuration that was flat (fwd == reference,
    typically 0 == 0) must be labelled by a fair coin, not as a failure."""
    from nseql.brain.runner import Decision
    P = screened["P"]
    R = np.zeros((len(universe()), len(P.dates())), np.float32)
    R[:20_000] = np.random.default_rng(0).normal(0, 0.01, (20_000, R.shape[1]))     # 60% of configurations are flat
    arm = Arm("x", ResearchPolicy(**ARMS["baseline_critic"]), "t")
    run = Runner(P, {"R": R, "G": R, "E": np.zeros_like(R, np.float16)}, EpisodicMemory(), 0.001, [arm])
    scr = np.random.default_rng(1).choice(len(universe()), 3000, replace=False)
    dec = Decision("x", 0, 300, P.dates()[300].isoformat(), {"label": "midvol_up"}, scr, np.ones(len(scr), bool), np.zeros((len(scr), 10)),
                   np.ones(len(scr)), None, "robust", {"robust": scr[:5]}, {}, None, None, 0, [])
    s = run.settle(arm, dec)
    flat = s["fwd"] == s["reference"]
    assert s["reference"] == 0.0 and flat.sum() > 1000
    assert 0.45 < s["label"][flat].mean() < 0.55


# ------------------------------------------------------------------ critic
def test_benjamini_hochberg_and_pbo():
    assert benjamini_hochberg(np.array([0.001, 0.02, 0.04, 0.5]), 0.1).tolist() == [True, True, True, False]
    rng = np.random.default_rng(0)
    R = rng.normal(0, 0.01, (400, 300)).astype(np.float32)
    v = HistoryView(R, R, np.ones_like(R, dtype=np.float16), 299, 0.001)
    assert 0.3 < pbo(v, np.arange(400)) < 0.7                            # noise: in-sample winner is a coin flip out of sample


# ------------------------------------------------------------------ protected settings & isolation
@pytest.mark.parametrize("key", ["max_order_notional", "risk_limits", "LIVE_TRADING_ENABLED", "kite_api_key", "broker", "kill_switch",
                                 "approval_secret", "execution", "sandbox_limits", "max_daily_loss_pct"])
def test_protected_settings_cannot_be_proposed(key):
    with pytest.raises(ProtectedSettingError):
        check_protected({key: 1})
    with pytest.raises(ProtectedSettingError):
        check_protected({"nested": {key: 1}})


def test_policy_schema_rejects_unknown_fields():
    with pytest.raises(ValueError):
        ResearchPolicy(**{"budget": 100, "something_else": 1})


def test_brain_and_workers_have_no_import_path_to_safety_systems():
    root = Path(__file__).resolve().parents[2] / "services"
    banned = ("nseql.execution", "nseql.risk.limits", "nseql.config", "nseql.api", "nseql.agents", "nseql.paper", "nseql.notify", "httpx", "socket")
    for pkg in ("brain", "evaluation"):
        for f in (root / pkg).glob("*.py"):
            for node in ast.walk(ast.parse(f.read_text())):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                for n in names:
                    assert not n.startswith(banned), f"{f.name} imports {n}"


# ------------------------------------------------------------------ controlled self-modification
def test_version_lifecycle_promotion_needs_a_human_and_regressions_roll_back(screened):
    mem = EpisodicMemory()
    sv = SystemVersions(mem, min_forward_epochs=4)
    sv.seed(ResearchPolicy(budget=300, min_evidence=200), "2022-01-01")
    vid, err = sv.propose({"max_order_notional": 1e9}, origin="meta_optimizer", as_of="2022-01-02")
    assert err and sv.state()["versions"][vid]["status"] == "rejected"
    vid, err = sv.propose({"top_k": 12, "explore_frac": 0.3}, origin="meta_optimizer", as_of="2022-01-02")
    assert err is None
    with pytest.raises(LifecycleError):
        sv.approve(vid, "R. Sharma", "looks good", "2022-01-03")               # not benchmarked / forward-tested yet
    mem.append("system", "version_benchmarked", {"version": vid, "passed": True, "result": {"note": "test"}}, available_at="2022-01-03")
    assert sv.state()["versions"][vid]["status"] == "forward_evaluation"
    for e, (c, ch) in enumerate([(0.010, 0.002), (0.004, 0.001), (0.006, 0.003), (0.008, 0.001)]):
        sv.record_forward(vid, e, c, ch, f"2022-02-0{e + 1}")
    assert sv.evaluate_readiness(vid, "2022-02-10", probes(mem))["ready"]
    for who in ("agent-research-director", "system", "Claude", "scheduler"):
        with pytest.raises(PermissionError):
            sv.approve(vid, who, "auto-approve", "2022-02-11")
    assert sv.approve(vid, "R. Sharma", "forward evidence reviewed", "2022-02-11")["previous"] == "v1"
    assert sv.state()["champion"] == vid
    for e in range(4):
        sv.record_champion_vs_previous(-0.004 - 0.001 * e, 0.003, f"2022-03-0{e + 1}")
    out = sv.monitor("2022-03-10", probes(mem))
    assert out["action"] == "rolled_back" and sv.state()["champion"] == "v1"
    assert [h["event"] for h in sv.state()["history"]] == ["promoted", "promoted", "rolled_back"]
    assert mem.verify()["ok"]


def test_frozen_benchmark_replays_both_versions_in_isolation_and_records_the_verdict(screened):
    mem = EpisodicMemory()
    sv = SystemVersions(mem)
    sv.seed(ResearchPolicy(budget=300, min_evidence=200), "2022-01-01")
    vid, _ = sv.propose({"top_k": 12}, origin="meta_optimizer", as_of="2022-01-02")
    rows = bench.decision_rows(screened["P"])[:4]
    fb = sv.frozen_benchmark(vid, screened["P"], screened["res"], screened["cost"], rows, dataset_hash="abc", as_of="2022-01-03")
    v = sv.state()["versions"][vid]
    assert fb["comparison"]["epochs"] == 4 and fb["dataset_hash"] == "abc" and fb["rows"] == rows
    assert v["status"] == ("forward_evaluation" if fb["passed"] else "rejected") and v["benchmark"]["rule"]
    assert mem.namespaces() == ["system"]                       # the replay used an isolated memory
    with pytest.raises(LifecycleError):
        sv.frozen_benchmark(vid, screened["P"], screened["res"], screened["cost"], rows, dataset_hash="abc", as_of="2022-01-04")


def test_correctness_probe_failure_rolls_back_immediately():
    mem = EpisodicMemory()
    sv = SystemVersions(mem, min_forward_epochs=3)
    sv.seed(ResearchPolicy(), "2022-01-01")
    vid, _ = sv.propose({"top_k": 15}, origin="operator", as_of="2022-01-02")
    mem.append("system", "version_benchmarked", {"version": vid, "passed": True, "result": {}}, available_at="2022-01-03")
    for e in range(3):
        sv.record_forward(vid, e, 0.01 + 0.001 * e, 0.0, f"2022-01-1{e}")
    assert sv.evaluate_readiness(vid, "2022-01-20", {"memory_chain_intact": True})["ready"]
    sv.approve(vid, "A. Operator", "approved for test", "2022-01-21")
    assert sv.monitor("2022-01-22", {"memory_chain_intact": True, "screener_causal": False})["action"] == "rolled_back"
    assert sv.state()["champion"] == "v1"


def test_probes_check_screener_causality(screened):
    assert probes(EpisodicMemory(), screened["P"]) == {"memory_chain_intact": True, "screener_causal": True}


# ------------------------------------------------------------------ benchmark end to end (small budget)
def test_benchmark_report_is_complete_and_auditable(screened, tmp_path):
    mem = EpisodicMemory(tmp_path / "m.sqlite")
    arms = {k: ARMS[k] for k in ("memory_no_critic", "baseline_robust", "baseline_random", "memory", "baseline_critic")}
    rep = bench.run(screened["ds"], screened["work"], mem, run_id="t", arms=arms, base_policy={"budget": 300, "min_evidence": 200},
                    sealed_from=screened["P"].dates()[-60].isoformat())
    assert rep["protocol"]["epochs"] >= 5 and rep["protocol"]["protocol_version"] == 3
    c = rep["comparisons"]["memory_no_critic_vs_baseline_robust"]["forward_return"]
    assert {"mean_diff", "ci90", "p_two_sided", "p_holm", "win_rate"} <= set(c)
    assert rep["sealed_segment"]["epochs"] >= 1 and rep["verdict_basis"] == "sealed window"
    for a in rep["arms"].values():
        assert len(a["epoch_returns"]) == rep["protocol"]["epochs"]
        assert {"screen_quality", "selected_hit_rate", "trials", "regime", "effective_bets"} <= set(a["kpis"])
    assert mem.verify()["ok"] and any(e.kind == "benchmark_report" for e in mem.read("system"))
    from nseql.brain.service import BrainService
    kp = {k["key"]: k for k in BrainService.kpis(None, rep, rep)}
    assert kp["oos_lift"]["status"] in ("met", "not met") and kp["paper_forward"]["status"] == "not measured"

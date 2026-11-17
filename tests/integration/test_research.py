"""AI research pipeline: spec language, code generation, sandbox, auditor, sealed set, full loop."""
import os
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from nseql.agents import team
from nseql.backtester.runner import BacktestConfig, StrategyRef
from nseql.contracts.artifacts import Evidence
from nseql.contracts.spec import ExpressionError, Feature, StrategySpec, parse_expression
from nseql.data.store import Dataset
from nseql.research import sandbox, validation as V
from nseql.research.loop import ResearchBudget, ResearchLoop, get_partition
from nseql.research.registry import Registry, SealedAccessError
from nseql.strategies.api import Strategy
from nseql.strategies.codegen import generate_source

HYP = dict(hypothesis="Fast average above slow average predicts positive drift for weeks.",
           falsification="Refuted if validation Sharpe is not above the random-entry baseline.")


def spec(**kw) -> StrategySpec:
    base = dict(id="t1", family="trend", params={"fast": 10, "slow": 40}, param_grid={"fast": [5, 10, 20]},
                features=[Feature(name="f", fn="sma", n="fast"), Feature(name="s", fn="sma", n="slow")],
                entry="f > s and prev(f) <= prev(s)", exit="f < s", stop_loss_pct=0.08, **HYP)
    return StrategySpec(**{**base, **kw})


# ------------------------------------------------------------------ specification language
@pytest.mark.parametrize("expr", ["__import__('os').system('x')", "f.__class__", "f[0]", "open('x')", "lambda: 1",
                                  "[x for x in f]", "f if s else 1", "'abc' == f", "unknown > 1", "prev(f, 2)", "f; s"])
def test_expression_language_rejects_everything_but_arithmetic_and_comparisons(expr):
    with pytest.raises(ExpressionError):
        parse_expression(expr, {"f", "s"})


def test_spec_requires_a_falsifiable_hypothesis_and_valid_references():
    with pytest.raises(ValueError):
        spec(falsification="tbd")
    with pytest.raises(ValueError):
        spec(entry="f > nope")
    with pytest.raises(ValueError):
        spec(features=[Feature(name="f", fn="sma", n="missing")])
    a, b = spec(), spec(id="t2", params={"fast": 20, "slow": 40})
    assert a.structural_hash() == b.structural_hash()          # same rule, different starting value of a tuned param
    assert a.structural_hash() != spec(exit="f < s * 0.99").structural_hash()


def test_generated_code_passes_validation_and_states_its_hypothesis():
    src = generate_source(spec())
    rep = sandbox.validate_source(src)
    assert rep.ok, rep.violations
    assert HYP["hypothesis"] in src and "class Gen_t1(Strategy)" in src


# ------------------------------------------------------------------ sandbox
HOSTILE = {
    "import os": "import os\nfrom nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n        os.environ['LIVE_TRADING_ENABLED'] = 'true'\n",
    "execution import": "from nseql.execution import gate\nfrom nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n        pass\n",
    "config import": "from nseql.config import get_settings\nfrom nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n        pass\n",
    "engine internals": "from nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n        ctx._e.kill.reset('me', 'x')\n",
    "dunder escape": "from nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n        (1).__class__.__base__.__subclasses__()\n",
    "getattr escape": "from nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n        getattr(ctx, '_' + 'e')\n",
    "eval": "from nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n        eval('1')\n",
    "file io": "from nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n        open('/etc/passwd')\n",
    "top-level code": "from nseql.strategies.api import Strategy\nx = [1][0]\nx.y()\nclass X(Strategy):\n    def on_bar(self, ctx):\n        pass\n",
    "string dunder": "from nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n        ctx.log('__globals__')\n",
    "infinite loop": "from nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n        while True:\n            pass\n",
    "not a strategy": "x = 1\n",
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_static_validation_rejects_hostile_generated_code(name, data_dir):
    rep = sandbox.validate_source(HOSTILE[name])
    assert not rep.ok and rep.violations
    out = sandbox.run_jobs(HOSTILE[name], [{"id": 0, "config": {"strategies": [{"name": "g"}]}}], data_dir)
    assert out["ok"] is False and "static validation failed" in out["error"]      # never reaches a process


def test_sandbox_child_has_no_secrets_and_cannot_load_the_execution_service(data_dir, monkeypatch):
    """Even code that bypassed static validation would find nothing to abuse in the child process."""
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "super-secret")
    monkeypatch.setenv("HUMAN_APPROVAL_SECRET", "approval-secret")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    probe = ("import os, json, sys\n"
             "out = {'env': sorted(os.environ), 'live': os.environ.get('LIVE_TRADING_ENABLED')}\n"
             "try:\n    import nseql.execution.gate\n    out['execution'] = 'importable'\n"
             "except Exception as e:\n    out['execution'] = type(e).__name__\n"
             "try:\n    import socket; socket.create_connection(('1.1.1.1', 53), timeout=2); out['net'] = 'open'\n"
             "except Exception as e:\n    out['net'] = 'blocked'\n"
             "open('results.json', 'w').write(json.dumps({'ok': True, 'results': [out], 'error': None}))\n")
    work = Path(__import__("tempfile").mkdtemp(prefix="sbx_probe_")) / "w"
    work.mkdir()
    (work / "probe.py").write_text(probe)
    sandbox.prepare_workdir(work)
    cmd = sandbox.isolated_command([sandbox.sys.executable, "-I", "probe.py"], work)
    import json, subprocess
    subprocess.run(cmd, cwd=work, env=sandbox.child_env(work), check=True, preexec_fn=sandbox._preexec(sandbox.SandboxLimits()))
    out = json.loads((work / "results.json").read_text())["results"][0]
    assert "KITE_ACCESS_TOKEN" not in out["env"] and "HUMAN_APPROVAL_SECRET" not in out["env"]
    assert out["live"] == "false" and out["execution"] in ("ImportError", "ModuleNotFoundError")   # not even mounted
    assert out["net"] == "blocked"


def test_sandbox_enforces_cpu_limit(data_dir):
    slow = ("from nseql.strategies.api import Strategy\nclass X(Strategy):\n    def on_bar(self, ctx):\n"
            "        t = 0\n        for i in range(10**9):\n            t += i\n")
    out = sandbox.run_jobs(slow, [{"id": 0, "config": {"strategies": [{"name": "g"}]}}], data_dir,
                           limits=sandbox.SandboxLimits(cpu_seconds=3, wall_seconds=30))
    assert out["ok"] is False and out["results"] == []


def test_generated_strategy_runs_in_sandbox_and_reconciles(data_dir):
    out = sandbox.run_jobs(generate_source(spec()), [{"id": 0, "config": {"strategies": [{"name": "g"}], "end": "2022-12-30"}}], data_dir)
    assert out["ok"], out["error"]
    s = out["results"][0]["summary"]
    assert s["reconciliation"]["ok"] and s["metrics"]["trades"] > 0 and s["dataset"]["synthetic"]
    assert out["isolation"]["env_scrubbed"] and out["isolation"]["static_validation"]


# ------------------------------------------------------------------ independent auditor
class Leaky(Strategy):
    """Cheats: reaches into engine internals to read tomorrow's close."""
    name = "leaky"

    def on_bar(self, ctx):
        portal = ctx._e.portal
        for s in ctx.universe("EQ"):
            a, i = portal._a[s], portal._pos[s]
            if i + 1 < len(a["close"]):
                up = a["close"][i + 1] > a["open"][i + 1] * 1.004
                if up and ctx.position(s) == 0 and not ctx.open_orders(s):
                    ctx.order_target_value(s, ctx.equity() * 0.08, tag="peek")
                elif not up and ctx.position(s) > 0:
                    ctx.order_target(s, 0)


def test_auditor_rejects_a_leaky_strategy(data_dir):
    ds = Dataset(data_dir / "synthetic_v1")
    cfg = BacktestConfig(strategies=[StrategyRef(name="leaky")], end="2022-12-30")
    leak = V.lookahead_audit(ds, cfg, lambda ref: Leaky())
    honest = V.lookahead_audit(ds, cfg, lambda ref: __import__("nseql.strategies.registry", fromlist=["x"]).create("sma_cross", {"fast": 5, "slow": 20}))
    assert honest["passed"] and honest["orders_checked"] > 0
    assert not leak["passed"] and leak["orders_checked"] > 0
    rep = team.SkepticalQuantAuditor().review(Evidence(hypothesis_id="leaky", lookahead=leak), synthetic=True)
    assert rep.verdict == "fail" and "LOOKAHEAD" in rep.fail_codes


def _evidence(**kw) -> Evidence:
    good = dict(hypothesis_id="h", stage="validation",
                lookahead={"passed": True, "orders_checked": 50},
                dev={"metrics": {"sharpe": 1.4, "trades": 80, "win_rate": 0.55, "total_return": 0.3, "max_drawdown": -0.1}},
                cost_sensitivity={"rows": [{"multiplier": 0.0, "total_return": 0.36}, {"multiplier": 2.0, "total_return": 0.2}]},
                stability={"neighbours": 4, "share_positive": 1.0},
                baselines={"strategy": {"sharpe": 1.4}, "random_entry": {"sharpe": 0.4}, "buy_and_hold": {"sharpe": 1.0}},
                validation={"sharpe": 1.0, "trades": 20, "max_drawdown": -0.08},
                bootstrap={"p_value": 0.03}, deflated_sharpe={"dsr": 0.9, "n_trials": 40})
    return Evidence(**{**good, **kw})


def test_auditor_passes_clean_evidence_and_always_lists_caveats():
    rep = team.SkepticalQuantAuditor().review(_evidence(), synthetic=True)
    assert rep.verdict == "pass" and rep.reliability_caveats[0].startswith("SYNTHETIC DATA")


@pytest.mark.parametrize("change,code", [
    (dict(validation={"sharpe": 0.05, "trades": 20}), "VALIDATION_FAILED"),                       # overfit: dies out of sample
    (dict(validation={"sharpe": 0.3, "trades": 20}), "VALIDATION_DECAY"),
    (dict(cost_sensitivity={"rows": [{"multiplier": 2.0, "total_return": -0.02}]}), "COST_FRAGILE"),
    (dict(stability={"neighbours": 4, "share_positive": 0.25}), "PARAM_UNSTABLE"),
    (dict(dev={"metrics": {"sharpe": 6.5, "trades": 80}}), "IMPLAUSIBLE"),
    (dict(dev={"metrics": {"sharpe": 1.4, "trades": 4}}), "TOO_FEW_TRADES"),
    (dict(baselines={"strategy": {"sharpe": 0.3}, "random_entry": {"sharpe": 0.4}}), "NOT_BETTER_THAN_RANDOM"),
    (dict(bootstrap={"p_value": 0.4}), "INSIGNIFICANT"),
    (dict(deflated_sharpe={"dsr": 0.1, "n_trials": 500}), "DSR_LOW"),
    (dict(lookahead={}), "LOOKAHEAD_UNTESTED"),
    (dict(errors=["boom"]), "RUNTIME_ERROR"),
])
def test_auditor_rejects_flawed_evidence(change, code):
    rep = team.SkepticalQuantAuditor().review(_evidence(**change), synthetic=False)
    assert rep.verdict == "fail" and code in rep.fail_codes


# ------------------------------------------------------------------ statistics
def test_deflated_sharpe_penalises_many_trials_and_bh_controls_fdr():
    r = np.random.default_rng(0).normal(0.0006, 0.01, 750)
    one, many = V.deflated_sharpe(r, 1)["dsr"], V.deflated_sharpe(r, 1000)["dsr"]
    assert 0 <= many < one <= 1
    assert V.benjamini_hochberg([0.001, 0.02, 0.04, 0.5, 0.9], 0.10) == [True, True, True, False, False]
    assert V.benjamini_hochberg([0.2, 0.5], 0.10) == [False, False]


def test_bootstrap_and_monte_carlo_are_seeded_and_sane():
    rng = np.random.default_rng(1)
    noise, edge = rng.normal(0, 0.01, 600), rng.normal(0.002, 0.01, 600)
    assert V.block_bootstrap_sharpe(noise) == V.block_bootstrap_sharpe(noise)
    assert V.block_bootstrap_sharpe(edge)["p_value"] < 0.05 < V.block_bootstrap_sharpe(noise)["p_value"]
    mc = V.monte_carlo_trades(list(rng.normal(500, 4000, 120)), 1_000_000)
    assert mc["max_drawdown_worst"] <= mc["max_drawdown_p95"] <= mc["max_drawdown_p50"] <= 0


def test_purged_kfold_removes_overlapping_labels():
    for train, test in V.purged_kfold(100, k=5, label_horizon=5, embargo=3):
        assert not set(train) & set(test)
        assert all(t < test[0] - 5 or t >= test[-1] + 1 + 3 + 5 for t in train)


def test_walk_forward_is_out_of_sample_and_auditable(data_dir, tmp_path):
    ev = V.Evaluator(data_dir, "sma_cross", out_dir=tmp_path)
    wf = V.walk_forward(ev, {"fast": [5, 20], "slow": [40, 80]}, start="2022-01-03", end="2023-06-30", train_days=200, test_days=60)
    assert len(wf["folds"]) >= 2 and wf["oos"]["sessions"] > 100
    for f in wf["folds"]:
        assert f["train"][1] < f["test"][0]                      # parameters chosen strictly before the test window
        assert set(f["chosen_params"]) == {"fast", "slow"}
    assert wf["backtests_run"] >= len(wf["folds"]) * 4


# ------------------------------------------------------------------ sealed data & the loop
def test_sealed_set_is_single_use_director_only_and_hidden_from_generators(tmp_path):
    reg = Registry(tmp_path / "r.sqlite")
    with pytest.raises(SealedAccessError):
        reg.claim_sealed("h1", "strategy_inventor")
    reg.claim_sealed("h1", "research_director")
    with pytest.raises(SealedAccessError):
        reg.claim_sealed("h1", "research_director")
    summary = {"strategies": [{"name": "x", "family": "trend", "params": {}}], "dataset": {"id": "d", "content_hash": "h", "synthetic": True},
               "fingerprint": "f", "result_hash": "r", "code_version": "v", "status": "ok", "metrics": {"sharpe": 9.9, "trades": 5},
               "period": {"start": "a", "end": "b"}}
    reg.record_run("dev1", summary, partition="dev", hypothesis_id="h1")
    reg.record_run("sealed1", summary, partition="sealed", hypothesis_id="h1", kind="sealed")
    view = reg.generation_view()
    assert [r["partition"] for r in view["runs"]] == ["dev"]
    assert reg.trial_count() == 1
    assert [a["action"] for a in reg.audit_entries()][::-1] == ["SEALED_ACCESS_DENIED", "SEALED_ACCESS_GRANTED", "SEALED_ACCESS_DENIED"]


@pytest.mark.slow
def test_research_loop_end_to_end_with_budgets_and_lineage(data_dir, tmp_path):
    reg = Registry(tmp_path / "registry.sqlite")
    loop = ResearchLoop(data_dir, tmp_path, reg, seed=1,
                        budget=ResearchBudget(max_iterations=2, max_backtests=160, walk_forward=False, max_sealed_evaluations=1))
    out = loop.run()
    part = get_partition(reg, loop.ds)
    assert out["results"] and out["usage"]["backtests"] <= 160 and out["usage"]["sealed_evaluations"] <= 1
    for r in out["results"]:
        assert r["decision"] in ("rejected", "advance_to_sealed", "promoted_to_paper_candidate", "rejected_on_sealed")
        for part_name in ("WHAT WAS TESTED", "WHY", "INFORMATION AVAILABLE", "RESULT", "WHY THIS MAY BE UNRELIABLE", "NEXT EXPERIMENT"):
            assert part_name in r["explanation"]
        code = reg.code_for(r["hypothesis_id"])
        assert code and sandbox.validate_source(code["source"]).ok
    # no run made before a sealed evaluation extends past the validation boundary
    for run in reg.runs(limit=1000):
        if run["partition"] != "sealed":
            assert run["period_end"][:10] <= part.val_end
    senders = {m["sender"] for m in reg.messages(out["experiment_id"])}
    assert {"research_director", "strategy_inventor", "strategy_developer", "backtest_controller", "quant_auditor", "learning"} <= senders
    assert len(reg.lessons()) >= len(out["results"]) and reg.experiment(out["experiment_id"])["status"] == "completed"
    tight = ResearchLoop(data_dir, tmp_path, reg, seed=2, budget=ResearchBudget(max_iterations=3, max_backtests=5)).run()
    assert tight["status"].startswith("budget_exhausted")

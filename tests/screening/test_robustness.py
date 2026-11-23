"""Robustness checks: Monte Carlo trade shuffle, walk-forward, parameter sensitivity."""
import numpy as np
import pytest
from nseql.screening import robustness as R


def test_monte_carlo_keeps_total_pnl_and_flags_sequence_risk():
    lucky = [100.0] * 30 + [-100.0] * 30                      # all wins first: actual drawdown is mild
    m = R.monte_carlo(lucky, 100_000)
    assert m["net_pnl_inr"] == 0 and m["trades"] == 60 and m["shuffles"] == 1000
    assert m["p95_worst"] <= m["median"] <= 0 and m["sequence_risk"] is False   # worst case equals what happened
    alternating = [100.0, -100.0] * 30                        # actual drawdown is tiny; shuffles can stack the losses
    a = R.monte_carlo(alternating, 100_000)
    assert a["sequence_risk"] is True and a["p95_worst"] <= 2 * a["actual_max_drawdown"] and "double" in a["plain"]
    assert R.monte_carlo(alternating, 100_000) == a           # seeded: reproducible
    assert R.monte_carlo([5.0], 100_000)["status"].startswith("not enough")


def _results(daily_by_variant):
    return {R._key(p): {"daily": d, "net_pnl_inr": 0, "trades": 10} for p, d in daily_by_variant}


def test_walk_forward_detects_memory():
    days = [f"2026-01-{d:02d}" for d in range(1, 21)]
    grid = [{"or_minutes": a, "breadth_threshold": b} for a in R.WF_GRID["or_minutes"] for b in R.WF_GRID["breadth_threshold"]]
    rng = np.random.default_rng(0)
    noise = {R._key(g): list(rng.normal(0, 0.005, 20)) for g in grid}   # pure noise: the winner of one window is random next
    res = {k: {"daily": v, "net_pnl_inr": 0, "trades": 10} for k, v in noise.items()}
    res[R._key({})] = res[R._key({"or_minutes": 30, "breadth_threshold": 0.5})]
    wf = R.walk_forward(res, days, 5)
    assert len(wf["folds"]) == 3 and all(f["out_of_sample"][0] > f["in_sample"][1] for f in wf["folds"])
    assert wf["mean_in_sample"] > wf["mean_out_of_sample"]             # selection bias shows up in-sample
    steady = {k: {"daily": [0.002] * 20, "net_pnl_inr": 0, "trades": 10} for k in res}
    assert R.walk_forward(steady, days, 5)["verdict"] == "holds out of sample"


def test_sensitivity_plateau_spike_and_no_edge():
    neigh = [({p: v}, None) for p, vals in R.SENSITIVITY.items() for v in vals]
    flat = _results([({}, [0.001] * 10)] + [(p, [0.0008] * 10) for p, _ in neigh])
    assert R.sensitivity(flat)["verdict"] == "plateau"
    spike = _results([({}, [0.001] * 10)] + [(p, [0.0008] * 10) for p, _ in neigh])
    spike[R._key({"breadth_threshold": 0.55})]["daily"] = [-0.001] * 10
    s = R.sensitivity(spike)
    assert s["verdict"] == "lonely spike" and "breadth_threshold=0.55" in s["plain"]
    losing = _results([({}, [-0.001] * 10)] + [(p, [0.001] * 10) for p, _ in neigh])
    assert R.sensitivity(losing)["verdict"] == "no edge to test"
    inert = _results([({}, [0.001] * 10)] + [(p, [0.001] * 10) for p, _ in neigh])     # nudges that change nothing
    assert set(R.sensitivity(inert)["no_effect"]) == set(R.SENSITIVITY)


@pytest.mark.slow
def test_fixture_end_to_end(tmp_path):
    rep = R.run_fixture(tmp_path, symbols_n=4, days_n=6, window=2, workers=2, incumbent=False)
    assert rep["kind"] == "synthetic" and rep["variants_run"] == 19 and (tmp_path / "report.json").exists()
    assert rep["walk_forward"]["folds"] and rep["sensitivity"]["verdict"] in ("plateau", "lonely spike", "no edge to test")
    assert "challenger" in rep["monte_carlo"]

"""High-throughput screening tier: determinism, causality, budgets, agreement with the event engine."""
import numpy as np
import pytest

from nseql.evaluation import cache as cache_mod, pool, space
from nseql.evaluation.agreement import compare
from nseql.evaluation.vector import evaluate, weights


def test_universe_is_deterministic_and_close_to_50k():
    U = space.universe()
    assert len(U) == 49_728
    keys = [c.key for c in U]
    assert len(set(keys)) == len(keys)
    assert space.universe()[12_345].key == keys[12_345]
    assert {c.family for c in U} == {"trend", "momentum", "breakout", "meanrev", "rsi"}


def test_all_configurations_were_evaluated_by_the_worker_pool(screened):
    st, res = screened["stats"], screened["res"]
    assert st["requested"] == 49_728 and bool(np.asarray(res["done"]).all())
    assert not np.isnan(np.asarray(res["R"][::997])).any()
    assert st["workers"] >= 1 and st["configs_per_second"] is None or st["configs_per_second"] > 100


def test_pool_matches_direct_evaluation_and_is_idempotent(screened, tmp_path):
    cdir = next((screened["work"] / "cache").iterdir())
    idx = [0, 777, 30_000, 49_727]
    out = tmp_path / "out"
    st1 = pool.evaluate_universe(cdir, out, screened["cost"], indices=idx, budget=pool.EvalBudget(workers=2, chunk=2))
    st2 = pool.evaluate_universe(cdir, out, screened["cost"], indices=idx)
    assert st1["evaluated_now"] == 4 and st2["evaluated_now"] == 0 and st2["already_done"] == 4
    R = pool.load_results(out)["R"]
    P = cache_mod.load(cdir)
    for i in idx:
        assert np.array_equal(R[i], evaluate(space.universe()[i], P, screened["cost"])[1])


def test_budgets_are_enforced(screened, tmp_path):
    cdir = next((screened["work"] / "cache").iterdir())
    with pytest.raises(pool.BudgetExceeded, match="budget is 10"):
        pool.evaluate_universe(cdir, tmp_path / "a", screened["cost"], indices=list(range(11)), budget=pool.EvalBudget(max_configs=10))
    with pytest.raises(pool.BudgetExceeded, match="wall-clock"):
        pool.evaluate_universe(cdir, tmp_path / "b", screened["cost"], indices=list(range(4000)),
                               budget=pool.EvalBudget(max_seconds=0.0, chunk=500, workers=2))


def _sample(n_per=3, seed=0):
    rng = np.random.default_rng(seed)
    U = space.universe()
    by = {}
    for c in U:
        by.setdefault((c.family, c.regime, c.universe, c.sizing), []).append(c.index)
    keys = sorted(by)
    return [U[rng.choice(by[keys[i]])] for i in rng.choice(len(keys), 40, replace=False)]


def test_screener_cannot_see_the_future(screened):
    """Replace every raw price after a cut with noise, rebuild all indicators, and check that weights up
    to the cut are bit-identical. Also: weights computed on a truncated panel equal the full ones."""
    ds = screened["ds"]
    raw = cache_mod.raw_panel(ds)
    T = len(raw["ts"])
    cut = int(T * 0.55)
    rng = np.random.default_rng(1)
    noisy = {k: (v.copy() if hasattr(v, "copy") else v) for k, v in raw.items()}
    for k in ("open", "high", "low", "close", "volume"):
        noisy[k][cut + 1:] *= rng.uniform(0.5, 1.5, noisy[k][cut + 1:].shape)
    noisy["idx_close"][cut + 1:] *= rng.uniform(0.5, 1.5, T - cut - 1)
    A, B = cache_mod.from_arrays(cache_mod.derive(raw)), cache_mod.from_arrays(cache_mod.derive(noisy))
    for c in _sample():
        wa, wb = weights(c, A), weights(c, B)
        assert np.array_equal(wa[: cut + 1], wb[: cut + 1]), c.describe()
        assert np.allclose(wa[: cut + 1], weights(c, A.truncated(cut + 1)), equal_nan=True), c.describe()
        ra, rb = evaluate(c, A, 0.001)[1], evaluate(c, B, 0.001)[1]
        assert np.array_equal(ra[: cut + 1], rb[: cut + 1])        # returns booked up to the cut cannot move either


@pytest.mark.parametrize("pick", ["trend", "momentum_overlays", "breakout"])
def test_screener_agrees_with_the_event_engine(screened, pick):
    U = space.universe()
    c = {"trend": next(x for x in U if x.family == "trend" and x.regime == "none" and x.vol is None and x.universe == "all" and x.sizing == "equal"),
         "momentum_overlays": next(x for x in U if x.family == "momentum" and x.regime == "sma200" and x.vol == 0.3 and x.universe == "liquid"),
         "breakout": next(x for x in U if x.family == "breakout" and x.regime == "none" and x.vol is None and x.universe == "all" and x.sizing == "invvol")}[pick]
    r = compare(screened["ds"], c, screened["P"], screened["cost"])
    assert r["reconciled"] and r["days"] > 100
    assert r["corr"] > 0.88, r
    # The screener is slightly optimistic (rebalancing to fixed weights as prices drift is free there). Measured
    # on the full 3-year fixture: +0.3% to +1.9% a year; on this 129-day test window the estimate is noisier.
    assert abs(r["ann_return_gap"]) < 0.06, r

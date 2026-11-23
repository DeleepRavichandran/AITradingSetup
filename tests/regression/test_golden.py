"""Golden-value regression tests.

These pin the exact numerical output of the fixture generator and of four backtests. They fail
on ANY change to data generation, execution rules, cost schedules or accounting — which is the
point: such a change must be deliberate, explained in the commit, and the values re-pinned.
(A numpy/pandas upgrade that changes floating-point results of the generator will also trip
the first test; re-pin after confirming the diff is numerical noise.)
"""
import pytest

from nseql.backtester.runner import BacktestConfig, StrategyRef, run_backtest
from nseql.data.store import Dataset

FIXTURE_HASH = "2edb0d99bd02277f05b10dca6dc4daf778ee59dbad4690ad04bc7add71f08dc7"
GOLDEN = {
    "sma_cross": ({}, "9ef4f0b7157ea032e6188ffe78412c887934c16ea719c579ef31d05e24149126", 20, 1434620.4),
    "momentum_rank": ({'lookback': 60}, "5f212e563aaf10e968c4a1e2abbf5810c00282e7c70cbde6ecd509954872c387", 46, 1572204.51),
    "futures_trend": ({'fast': 10, 'slow': 40}, "b70f968358f6f5c672293b2df336743369784a42faa44189a787807cf3cf304d", 22, 1494459.1),
    "option_structure": ({}, "4aa142c8bec36d96bf68250e33a56d808ad6b877ad115adf892402de7f7acbb7", 8, 989376.62),
}


def test_fixture_generator_output_is_pinned(data_dir):
    assert Dataset(data_dir / "synthetic_v1").content_hash == FIXTURE_HASH


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_backtest_results_are_pinned(name, data_dir):
    params, result_hash, trades, final_equity = GOLDEN[name]
    r = run_backtest(BacktestConfig(strategies=[StrategyRef(name=name, params=params)]), data_dir)
    assert r.summary["reconciliation"]["ok"]
    assert (r.metrics["trades"], round(r.metrics["final_equity"], 2)) == (trades, final_equity)
    assert r.summary["result_hash"] == result_hash

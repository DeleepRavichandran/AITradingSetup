"""Market context: point-in-time alignment, Kite import, the screener's context filters and the placebo test."""
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from nseql.data import context as ctx
from nseql.data.store import Dataset, DatasetWriter
from nseql.evaluation import cache as cache_mod
from nseql.evaluation import context_test, pool, space
from nseql.evaluation.vector import evaluate, side_cost
from nseql.instruments.models import Instrument, Kind


def _ctx_dataset(tmp_path, series: dict[str, list[tuple[str, float]]], lags=None) -> Dataset:
    w = DatasetWriter(tmp_path, "ctx_t", description="t", source="t", licence="t", synthetic=True, overwrite=True,
                      generator={"context": {k: {"lag": (lags or {}).get(k, ctx.BY_NAME[k].lag)} for k in series}})
    for name, rows in series.items():
        df = pd.DataFrame({"ts": pd.to_datetime([d for d, _ in rows]) + pd.Timedelta(hours=15, minutes=30),
                           "open": [v for _, v in rows], "high": [v for _, v in rows], "low": [v for _, v in rows],
                           "close": [v for _, v in rows], "volume": 0})
        w.write_bars(name, "1d", df)
        w.add_instrument(Instrument(name, Kind.INDEX, segment="CTX"))
    return w.finish()


def test_currency_and_commodity_values_are_only_used_from_the_next_day(tmp_path):
    days = pd.bdate_range("2026-01-01", periods=30)
    rows = [(str(d.date()), 80.0 + i) for i, d in enumerate(days)]
    d = _ctx_dataset(tmp_path, {"USDINR": rows, "INDIAVIX": rows})
    usd = ctx._aligned(d, "USDINR", days, 1)
    vix = ctx._aligned(d, "INDIAVIX", days, 0)
    assert np.isnan(usd.iloc[0]) and usd.iloc[5] == 84.0         # day 5 sees day 4's rupee close (it closes at 17:00)
    assert vix.iloc[5] == 85.0                                    # VIX closes with the stock market: same day
    f = ctx.features(d, days)
    assert f["usdinr_ret20"].iloc[21] == pytest.approx(100 / 80 - 1)     # (day 20) / (day 0)
    assert f["ctx_inr_stable"].iloc[21] == 0 and bool(f["ctx_inr_stable_known"].iloc[21])
    assert f["ctx_inr_stable"].iloc[3] == 1 and not f["ctx_inr_stable_known"].iloc[3]   # no data yet: allowed, flagged unknown


def test_missing_series_leave_their_filter_open_and_report_zero_coverage(tmp_path):
    days = pd.bdate_range("2026-01-01", periods=300)
    rng = np.random.default_rng(0)
    d = _ctx_dataset(tmp_path, {"INDIAVIX": [(str(x.date()), 14 + rng.normal()) for x in days]})
    f = ctx.features(d, days)
    cov = ctx.coverage(f)
    assert cov["ctx_crude_calm"] == 0.0 and (f["ctx_crude_calm"] == 1).all()
    assert 0.4 < cov["ctx_vix_calm"] < 0.7                        # needs 120 days of history before it is known


class FakeKite:
    def __init__(self):
        self.calls = []
        self.last_raw = []

    def instruments(self, exchange):
        if exchange == "NSE":
            return pd.DataFrame({"instrument_token": [1, 2, 3], "tradingsymbol": ["INDIA VIX", "NIFTY 50", "NIFTY BANK"],
                                 "name": ["INDIA VIX", "NIFTY 50", "NIFTY BANK"], "segment": "INDICES", "instrument_type": "EQ", "expiry": None})
        if exchange == "MCX":
            return pd.DataFrame({"instrument_token": [10, 11, 12], "tradingsymbol": ["CRUDEOIL26NOVFUT", "CRUDEOIL26DECFUT", "GOLD26DECFUT"],
                                 "name": ["CRUDEOIL", "CRUDEOIL", "GOLD"], "segment": "MCX-FUT", "instrument_type": "FUT",
                                 "expiry": [date(2026, 11, 18), date(2026, 12, 17), date(2026, 12, 4)]})
        raise PermissionError("segment not enabled")              # e.g. no currency segment on the account

    def candles(self, token, interval, start, end, oi=False, continuous=False):
        self.calls.append((token, continuous))
        days = pd.bdate_range("2026-06-01", "2026-11-18")
        self.last_raw = [[str(d.date()), 1, 1, 1, 1, 0] for d in days]
        return pd.DataFrame({"ts": days + pd.Timedelta(hours=15, minutes=30), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0, "oi": 0})


def test_kite_context_import_uses_continuous_nearest_futures_and_reports_gaps(tmp_path):
    api = FakeKite()
    ds, missing = ctx.import_context(api, date(2026, 6, 1), date(2026, 11, 18), tmp_path, "context_v1", licence="Kite Connect (test)",
                                     today=date(2026, 11, 19), progress=lambda *_: None)
    assert set(ds.symbols("1d")) == {"INDIAVIX", "NIFTY50", "NIFTYBANK", "CRUDEOIL", "GOLD"}
    assert (11, True) in api.calls and (10, True) not in api.calls        # the expired November contract is skipped
    assert (1, False) in api.calls                                         # indices are not "continuous"
    assert "USDINR" in missing and "NIFTYIT" in missing
    assert ds.manifest.generator["context"]["CRUDEOIL"]["lag"] == 1 and not ds.synthetic
    with pytest.raises(ValueError, match="licence"):
        ctx.import_context(api, date(2026, 6, 1), date(2026, 11, 18), tmp_path, "c2", licence=" ")


def test_kite_client_sends_the_continuous_flag():
    import httpx
    from nseql.data.adapters.kite import KiteHistorical
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json={"status": "success", "data": {"candles": []}})
    api = KiteHistorical("k", "t", transport=httpx.MockTransport(handler), min_interval_s=0)
    api.candles(5, "1d", datetime(2026, 1, 1), datetime(2026, 2, 1), continuous=True)
    api.candles(5, "1d", datetime(2026, 1, 1), datetime(2026, 2, 1))
    assert seen[0]["continuous"] == "1" and "continuous" not in seen[1]
    with pytest.raises(ValueError, match="daily"):
        api.candles(5, "5m", datetime(2026, 1, 1), datetime(2026, 1, 2), continuous=True)


@pytest.fixture(scope="module")
def ctx_panel(data_dir, tmp_path_factory):
    ds = Dataset(Path(data_dir) / "synthetic_v1")
    work = tmp_path_factory.mktemp("ctxwork")
    dates = cache_mod.raw_panel(ds)["ts"]
    cds = ctx.generate_context_fixture(work, pd.Timestamp(dates[0]).date() - pd.Timedelta(days=400), pd.Timestamp(dates[-1]).date())
    cdir = cache_mod.build(ds, work / "cache", context=cds)
    return {"cdir": cdir, "P": cache_mod.load(cdir), "work": work, "cds": cds, "ds": ds}


def test_context_universe_extends_the_base_without_renumbering_it():
    base, full = space.universe(), space.universe(True)
    assert len(base) == 49_728 and len(full) == 82_880
    assert all(full[i].key == base[i].key for i in (0, 12_345, 49_727))
    tw = space.twins()
    c = full[60_000]
    t = base[tw[60_000]]
    assert c.regime.startswith("ctx_") and t.regime == "none"
    assert (c.family, c.params, c.vol, c.universe, c.sizing) == (t.family, t.params, t.vol, t.universe, t.sizing)


def test_context_filter_only_removes_positions_on_closed_days(ctx_panel):
    P = ctx_panel["P"]
    assert P.meta["context"]["synthetic"] and set(P.meta["context"]["coverage"]) == set(ctx.STATES)
    full, tw = space.universe(True), space.twins()
    from nseql.evaluation.vector import weights
    for i in (50_000, 58_123, 66_789):
        w, w0 = weights(full[i], P), weights(full[tw[i]], P)
        off = np.asarray(P[full[i].regime]) == 0
        assert np.all(w[off] == 0)
        assert np.array_equal(w[~off] > 0, (w0[~off] > 0))          # on open days the same stocks are held


def test_pool_evaluates_context_configurations(ctx_panel, tmp_path):
    cost = side_cost(date(2023, 1, 2))
    idx = [5, 49_728, 82_879]
    st = pool.evaluate_universe(ctx_panel["cdir"], tmp_path / "o", cost, indices=idx, budget=pool.EvalBudget(workers=2, chunk=2))
    R = pool.load_results(tmp_path / "o")["R"]
    assert st["evaluated_now"] == 3 and R.shape[0] == 82_880
    for i in idx:
        assert np.array_equal(R[i], evaluate(space.universe(True)[i], ctx_panel["P"], cost)[1])


def test_random_context_does_not_pass_but_a_leaked_one_does(ctx_panel):
    """Negative control: the synthetic context is unrelated to the prices, so no filter may beat the placebo.
    Positive control: a filter that peeks at the next day's market move must be detected (the test has power)."""
    P = ctx_panel["P"]
    cost = side_cost(date(2023, 1, 2))
    T = P["ts"].shape[0]
    rows = (130, T)
    rep = context_test.run(P, cost, rows=rows, pairs=40, placebos=12, seed=1, states=tuple(ctx.STATES))
    assert all(v["verdict"] == "no better than a placebo filter" for v in rep["states"].values()), rep["states"]
    oo = np.asarray(P["oo"])
    future = np.zeros(T, dtype=np.int8)
    future[:-2] = (oo[2:].mean(axis=1) > 0).astype(np.int8)      # W[t] earns oo[t+2]: this filter knows the outcome
    leaked = cache_mod.from_arrays(dict(P), P.meta)
    leaked["ctx_broad"] = future
    rep2 = context_test.run(leaked, cost, rows=rows, pairs=40, placebos=12, seed=1, states=("ctx_broad",))
    s = rep2["states"]["ctx_broad"]
    assert s["verdict"] == "helps beyond chance" and s["median_sharpe_change"] > s["placebo_95th"]


def test_context_test_refuses_a_cache_without_context(screened):
    with pytest.raises(ValueError, match="no market context"):
        context_test.run(screened["P"], 0.001, rows=(0, 300))

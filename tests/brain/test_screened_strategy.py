"""A screened configuration traded as a normal strategy: identical to the screener's precomputed weights,
usable in paper sessions, and context-filtered configurations are refused."""
from pathlib import Path

import pytest
from nseql.backtester.engine import Allocation, BacktestEngine
from nseql.backtester.execution import ExecutionConfig
from nseql.data.portal import DataPortal
from nseql.data.store import Dataset
from nseql.evaluation import cache as cache_mod
from nseql.evaluation import space
from nseql.evaluation.agreement import ConfigStrategy
from nseql.evaluation.vector import weights
from nseql.strategies import registry


def _pick(family, **kw):
    return next(c for c in space.universe() if c.family == family and all(getattr(c, k) == v for k, v in kw.items()))


@pytest.mark.parametrize("cfg", [_pick("trend", regime="sma100", universe="all", sizing="equal", vol=None),
                                 _pick("momentum", regime="none", universe="liquid", sizing="invvol", vol=0.30),
                                 _pick("rsi", regime="none", universe="all", sizing="equal", vol=None)],
                         ids=["trend_regime", "momentum_overlays", "rsi_hysteresis"])
def test_live_recomputation_trades_exactly_like_the_precomputed_weights(data_dir, cfg):
    ds = Dataset(Path(data_dir) / "synthetic_v1")
    raw = cache_mod.raw_panel(ds)
    P = cache_mod.from_arrays(cache_mod.derive(raw), {"symbols": raw["symbols"], "index": raw["index_symbol"]})
    syms = [*raw["symbols"], raw["index_symbol"]]

    def run(strat):
        eng = BacktestEngine(DataPortal.from_dataset(ds, "1d", syms), [Allocation(strat, "b", 10_000_000)],
                             exec_cfg=ExecutionConfig(max_participation=1.0))
        res = eng.run()
        assert res.error is None and res.account.reconcile()["ok"]
        return [(str(f.ts), f.symbol, f.side, f.qty, f.price) for f in res.account.fills]

    live = run(registry.create("screened_config", {"key": cfg.key}))
    pre = run(ConfigStrategy(weights(cfg, P), raw["symbols"], P.dates() if hasattr(P, "dates") else None))
    assert live and live == pre


def test_unknown_and_context_configurations_are_refused():
    with pytest.raises(ValueError, match="no screened configuration"):
        registry.create("screened_config", {"key": "0000000000000000"})
    ctx_cfg = space.universe(True)[len(space.universe())]
    with pytest.raises(ValueError, match="NOT IMPLEMENTED"):
        registry.create("screened_config", {"key": ctx_cfg.key})


def test_a_screened_configuration_runs_as_a_paper_session(data_dir, tmp_path, monkeypatch):
    import os

    from fastapi.testclient import TestClient
    from nseql import config
    from nseql.api.main import create_app
    tok = "screened-paper-test-operator-token-01"
    for k, v in {"NSEQL_DATA_DIR": str(data_dir), "NSEQL_EXPERIMENTS_DIR": str(tmp_path), "NSEQL_AUTO_FIXTURE": "false",
                 "NSEQL_API_TOKEN": tok}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NSEQL_SCHEDULER", raising=False)
    config.get_settings.cache_clear()
    c = TestClient(create_app())
    c.headers.update({"Authorization": f"Bearer {tok}"})
    cfg = _pick("breakout", regime="none", universe="all", sizing="equal", vol=None)
    r = c.post("/api/paper/sessions", json={"strategy": "screened_config", "params": {"key": cfg.key}, "window": "validation"})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    for _ in range(3):
        assert c.post(f"/api/paper/sessions/{sid}/advance", json={"bars": 20}).status_code == 200
    st = c.get(f"/api/paper/sessions/{sid}").json()
    assert st["reconciliation"]["ok"] and any(f["book"] == "algo" for f in st["fills"])
    bad = c.post("/api/paper/sessions", json={"strategy": "screened_config", "params": {"key": space.universe(True)[60_000].key}})
    assert bad.status_code == 400 and "NOT IMPLEMENTED" in bad.json()["detail"]
    config.get_settings.cache_clear()
    assert os.environ["NSEQL_API_TOKEN"] == tok

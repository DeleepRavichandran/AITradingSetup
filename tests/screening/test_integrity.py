"""Evidence integrity (services/screening/integrity.py): the adversarial layer must try to disprove, abstain without
evidence, keep an immutable record, never loosen a control, and suspend only on predeclared conditions."""
import json
import re
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nseql.screening import integrity as I
from nseql.screening.execution import PortfolioRules
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols

DAYS = ["2026-11-16", "2026-11-17", "2026-11-18"]


def _account(path: Path, seed: int, meta: dict) -> Path:
    syms = symbols(6)
    sch = MinuteScheduler(path, syms, portfolio=PortfolioRules(capital=100_000), live_from=f"{DAYS[0]}T09:16:00", mode="live")
    sch.verbose = False
    for ts, bars in minute_stream(6, DAYS, seed=seed):
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    sch.store.db.commit()
    (path / "live.json").write_text(json.dumps({"live_from": f"{DAYS[0]}T09:16:00", "symbols": syms, "capital": 100_000,
                                                "start_cash": 100_000, **meta}))
    return path


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    exp = tmp_path_factory.mktemp("ei") / "exp"
    inc = _account(exp / "screening" / "live_t_v14", 21, {"rules_version": "1.4"})
    _account(exp / "screening" / "live_t_v14_x20", 22, {"challenger": "orb-x"})
    return exp, inc


def test_ledger_is_append_only_and_hash_chained(tmp_path):
    L = I.Ledger(tmp_path)
    L.append("hypothesis", "H-X", {"statement": "x"})
    L.append("result", "H-X", {"status": "falsified"})
    assert L.verify()["ok"] and L.verify()["entries"] == 2
    db = sqlite3.connect(L.path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE entries SET body='{}' WHERE seq=1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM entries WHERE seq=1")
    # tampering around the triggers (drop them, rewrite a row) is detected by the chain
    db.execute("DROP TRIGGER entries_no_update")
    db.execute("UPDATE entries SET body='{\"status\": \"survived\"}' WHERE seq=2")
    db.commit()
    v = L.verify()
    assert not v["ok"] and v["broken_at"] == 2


def test_seed_hypotheses_are_registered_once_with_falsification(tmp_path):
    L = I.Ledger(tmp_path)
    assert len(I.register_seed_hypotheses(L)) == len(I.HYPOTHESES)
    assert I.register_seed_hypotheses(L) == []
    for e in L.entries("hypothesis"):
        assert e["body"]["falsification"].startswith("Falsified if") and e["body"]["registered_at"]


def test_report_abstains_without_confirmatory_evidence(lab):
    exp, inc = lab
    r = I.report(exp)
    assert r["verdict"] == "abstain" and r["verdict_plain"].startswith("Abstain")
    edge = next(h for h in r["hypotheses"] if h["id"] == "H-EDGE")
    # registered today: every stored session is exploratory; nothing confirmatory yet
    assert edge["status"] == "abstain" and edge["confirmatory"]["sessions"] == 0 and edge["exploratory"]["sessions"] == len(DAYS)
    assert r["promotion"]["eligible_for_operator_review"] is False and r["promotion"]["promoted"] is False
    assert {b["bias"] for b in r["biases"]} >= {"Look-ahead", "Hindsight", "Survivorship", "Selection", "Recency", "Data snooping / multiple testing",
                                                 "Overfitting", "Confirmation", "Anchoring", "Outcome", "Overconfidence / calibration", "Automation"}
    assert next(b for b in r["biases"] if b["bias"] == "Survivorship")["status"] == "exposed"
    assert next(b for b in r["biases"] if b["bias"] == "Hindsight")["status"] == "guarded"


def test_only_sessions_after_registration_count(lab, tmp_path):
    exp, inc = lab
    import shutil
    exp2 = tmp_path / "exp"
    shutil.copytree(exp / "screening", exp2 / "screening")
    L = I.Ledger(exp2)
    I.register_seed_hypotheses(L, at="2026-11-16T20:00:00")          # registered after the first session
    r = I.report(exp2)
    edge = next(h for h in r["hypotheses"] if h["id"] == "H-EDGE")
    assert edge["exploratory"]["sessions"] == 1 and edge["confirmatory"]["sessions"] == 2 and edge["status"] == "abstain"


def test_validation_is_deterministic_and_complete(lab):
    exp, inc = lab
    a, b = I.report(exp), I.report(exp)
    va, vb = a["validation"], b["validation"]
    assert va["placebo"] == vb["placebo"] and va["stress"] == vb["stress"]
    if va["round_trips"] >= 2:
        assert 0 < va["placebo"]["p"] <= 1 and va["placebo"]["seed"] == I.SEED
        s = va["stress"]
        assert s["double_costs_inr"] < s["actual_inr"]            # doubling costs can only lower the result
    assert {c["check"] for c in a["critic"]} >= {"without the best session"}


def test_statistics():
    assert I.bh([0.001, 0.04, 0.2, None], 0.05) == [True, False, False, False]
    assert I.bh([0.01, 0.02, 0.03], 0.05) == [True, True, True]
    t = I.t_test([0.01] * 5 + [0.012] * 5)
    assert t["p"] < 0.001 and t["lo"] > 0
    rng = np.random.default_rng(0)
    steady = rng.normal(0, 1, 200)
    assert not I.page_hinkley(steady)["alarm"]
    drop = np.concatenate([rng.normal(1, 1, 100), rng.normal(-2, 1, 100)])
    ph = I.page_hinkley(drop)
    assert ph["alarm"] and ph["at"] > 100
    assert I.page_hinkley([1.0] * 10)["decided"] is False         # too few round trips: no decision


def test_suspension_engages_the_kill_switch_only_and_is_acknowledged(tmp_path):
    from nseql.risk.limits import KillSwitch
    k = KillSwitch()
    L = I.Ledger(tmp_path)
    rep = {"suspension": {"active": [{"rule": "S3", "id": "S3:acct", "text": I.SUSPEND["S3"], "detail": "-7%"}]}}
    assert I.enforce(rep, k, L) == ["S3:acct"] and k.engaged and "integrity suspension S3" in k.reason
    assert I.acknowledge(L, rep, "operator", "reviewed the drawdown, resuming") == ["S3:acct"]
    assert L.verify()["ok"] and {e["kind"] for e in L.entries()} == {"suspension", "acknowledged"}


def test_drawdown_rule_fires_from_the_ledger(lab, tmp_path, monkeypatch):
    exp, _ = lab
    monkeypatch.setattr(I, "DRAWDOWN_SUSPEND", 0.0000001)
    r = I.report(exp)
    dd = I._peak_drawdown(exp / "screening" / "live_t_v14")
    if dd is not None and dd < 0:
        assert any(t["rule"] == "S3" for t in r["suspension"]["triggered"])


def test_integrity_layer_cannot_change_rules_or_enable_real_orders():
    src = Path(I.__file__).read_text()
    assert not re.search(r"live_trading_enabled\s*=|LIVE_TRADING|PortfolioRules\(|\.rules\s*=|set_rules|reset\(", src)
    # trading modules never read integrity output either
    base = Path(I.__file__).parent
    for f in I.TRADING_SOURCES:
        assert not re.search(r"screening\.integrity|import integrity", (base / f).read_text()), f


def test_api_reads_with_the_viewer_key_and_only_the_operator_records_decisions(data_dir, tmp_path):
    import os
    from fastapi.testclient import TestClient
    from nseql import config
    keys = ("NSEQL_DATA_DIR", "NSEQL_EXPERIMENTS_DIR", "NSEQL_AUTO_FIXTURE", "NSEQL_API_TOKEN", "NSEQL_VIEWER_TOKEN")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update({"NSEQL_DATA_DIR": str(data_dir), "NSEQL_EXPERIMENTS_DIR": str(tmp_path / "exp"), "NSEQL_AUTO_FIXTURE": "false",
                       "NSEQL_API_TOKEN": "ei-operator-token-12345678", "NSEQL_VIEWER_TOKEN": "ei-viewer-token-123456789"})
    config.get_settings.cache_clear()
    try:
        from nseql.api.main import create_app
        app = create_app()
        op = TestClient(app, headers={"Authorization": "Bearer ei-operator-token-12345678"})
        viewer = TestClient(app, headers={"Authorization": "Bearer ei-viewer-token-123456789"})
        r = viewer.get("/api/integrity")
        assert r.status_code == 200
        j = r.json()
        assert j["verdict"] == "abstain" and len(j["hypotheses"]) == len(I.HYPOTHESES) and j["ledger"]["ok"]
        body = {"subject": "challenger", "decision": "approve", "reason": "Every gate passed on 20 forward sessions"}
        assert viewer.post("/api/integrity/decision", json=body).status_code in (401, 403)
        assert op.post("/api/integrity/decision", json={**body, "reason": "short"}).status_code == 422
        assert op.post("/api/integrity/decision", json={**body, "decision": "promote now"}).status_code == 422
        assert op.post("/api/integrity/decision", json=body).json()["recorded"] is True
        j = viewer.get("/api/integrity?refresh=true").json()
        assert j["decisions"][0]["decision"] == "approve" and j["ledger"]["ok"]
        assert j["promotion"]["promoted"] is False                       # an approval alone promotes nothing
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        config.get_settings.cache_clear()

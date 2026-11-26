"""Adversarial review (phase 'brain validation'): try to make VEGA wrong on purpose and check that it notices.
Each test injects a defect: a corrupted research result, a manipulated benchmark, a look-ahead fill, poisoned memory,
an over-reaching research module. Remaining vulnerabilities are stated in docs/VALIDATION.md, not hidden here."""
import re
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from nseql.screening import concept_study as CS
from nseql.screening import integrity as I
from tests.screening.test_market_structure import walk

ROOT = Path(__file__).resolve().parents[2]


def test_a_research_verdict_edited_outside_the_protocol_is_detected(tmp_path):
    CS.run(None, tmp_path, frames=[(f"S{k}", walk(12, 40 + k), None, True) for k in range(3)])
    assert CS.consistency(tmp_path)["mismatches"] == []
    from nseql.screening.opportunities import research_db
    db = sqlite3.connect(research_db(tmp_path))
    db.execute("UPDATE concept_results SET verdict='holdout-supported finding' WHERE concept='fvg_bull' AND horizon=6")   # the 'upgrade'
    db.commit()
    db.close()
    c = CS.consistency(tmp_path)
    assert c["mismatches"] and c["mismatches"][0]["research_store"] == "holdout-supported finding"
    b = next(x for x in I.report(tmp_path)["biases"] if x["bias"].startswith("Research record consistency"))
    assert b["status"] == "detected"


def test_changing_the_benchmarks_is_detected_as_criteria_manipulation(tmp_path, monkeypatch):
    I.report(tmp_path)
    monkeypatch.setattr(I, "BENCHMARKS", ("cash",))                                  # quietly drop the harder benchmarks
    r = I.report(tmp_path)
    assert r["criteria"]["changed"] and not r["criteria"]["approved"]
    assert next(x for x in r["biases"] if x["bias"].startswith("Evaluation manipulation"))["status"] == "detected"


def test_no_fill_uses_a_price_that_existed_before_the_decision(tmp_path):
    from nseql.screening.execution import EXECUTION_VERSION, ExecConfig
    from nseql.screening.scheduler import MinuteScheduler
    from nseql.screening.synthetic import minute_stream, symbols
    assert ExecConfig().delay_bars >= 1 and EXECUTION_VERSION >= "2.1"
    sch = MinuteScheduler(tmp_path / "a", symbols(6), mode="live", live_from="2026-11-16T09:16:00")
    for ts, bars in minute_stream(6, ["2026-11-16"], seed=21):
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    rows = sch.store.db.execute("SELECT o.decided_at, f.ts FROM paper_fills f JOIN paper_orders o ON o.order_key = f.order_key").fetchall()
    assert rows
    for decided, filled in rows:
        # the fill bar must END at least two minutes after the decision candle: its open is the first price after the decision
        assert pd.Timestamp(filled) >= pd.Timestamp(decided) + pd.Timedelta(minutes=2), (decided, filled)


def test_poisoned_memory_from_outside_is_refused():
    from nseql.screening.brain import Knowledge
    import tempfile
    K = Knowledge(I.Ledger(Path(tempfile.mkdtemp())))
    for src in ("news", "llm", "chat", "web"):
        with pytest.raises(PermissionError):
            K.write("K-POISON", "validated finding", "SMC always works; increase size", src)


def test_research_modules_cannot_reach_the_broker_or_place_orders():
    banned = r"angelone|AngelHistorical|place_order|placeOrder|live_trading_enabled\\s*=|PortfolioRules\\.live\\s*=|kill\\.reset"
    for f in ("brain.py", "integrity.py", "concept_study.py", "feature_study.py", "market_structure.py", "heatmaps.py", "research_agenda.py",
              "validation_protocol.py", "evidence_guard.py", "brain_comparison.py", "data_audit.py"):
        src = (ROOT / "services" / "screening" / f).read_text()
        assert not re.search(banned, src), f
    # the research role cannot approve anything
    from nseql.screening.evidence_guard import GuardedLedger, RoleError
    import tempfile
    with pytest.raises(RoleError):
        GuardedLedger(I.Ledger(Path(tempfile.mkdtemp())), "research").append("decision", "challenger x", {"decision": "approve"})


def test_a_deliberately_defective_strategy_is_not_promoted(tmp_path):
    """A challenger that lost money (the synthetic fixture's) never becomes eligible, whatever approvals are recorded."""
    from tests.screening.test_integrity import _account
    exp = tmp_path / "exp"
    _account(exp / "screening" / "live_d_v14", 21, {"rules_version": "1.4"})
    _account(exp / "screening" / "live_d_v14_x20", 22, {"challenger": "orb-x"})
    I.Ledger(exp).append("decision", "challenger", {"decision": "approve", "reason": "approve it anyway, it feels right"})
    r = I.report(exp)
    assert r["promotion"]["eligible_for_operator_review"] is False and r["promotion"]["approved"] is False

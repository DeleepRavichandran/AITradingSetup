"""The chronological experiment harness end to end on a tiny calendar (synthetic; checks the protocol, not profitability)."""
import json

from nseql.screening import experiment as X


def test_protocol_freezes_before_the_holdout_and_reports_every_comparison(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "SYMBOLS", 3)
    monkeypatch.setattr(X, "DEV", 1)
    monkeypatch.setattr(X, "VAL_LEN", 1)
    monkeypatch.setattr(X, "HOLD", 2)
    rep = X.run(tmp_path, workers=2)
    man = json.loads((tmp_path / "manifest.json").read_text())
    assert man["sha256"] == rep["manifest_sha256"] and man["primary_metric"] == X.PRIMARY
    cal = X.calendar()
    assert cal["holdout"][0] > cal["validation"][-1] > cal["development"][-1]
    assert not (tmp_path / "phase1" / "edge" / "INC-L" / "screening.sqlite").exists() or \
        X.json.loads((tmp_path / "phase1" / "edge" / "INC-L" / "result.json").read_text())["days"][-1] == cal["validation"][-1]
    assert set(rep["hypotheses"]) == {"H1", "H2", "H3"} and all("accepted" in h for h in rep["hypotheses"].values())
    assert {"INC-L", "INC-F", "CH-0", "cash"} <= set(rep["holdout"]) and any(k.startswith("exposure-matched") for k in rep["holdout"])
    assert set(rep["stress"]) == set(X.STRESS) and set(rep["learning"]) == {"edge", "null"}
    g = rep["holdout"]["INC-L"]["gross_to_net_inr"]
    assert abs(g["gross_at_bar_opens"] + g["spread_and_impact"] + g["charges"] - g["net"]) < 0.05
    lines = (tmp_path / "variants.jsonl").read_text().splitlines()
    assert len(lines) >= 7                                                      # every variant tried is recorded

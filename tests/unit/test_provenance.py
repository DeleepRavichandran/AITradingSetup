"""Market-data provenance labels: one classification for every page; 'verified' only from a passed run."""
import json
from datetime import datetime

from nseql.screening.provenance import classify, classify_dataset, verification_status


class F:
    def __init__(self, kind, label="x"):
        self.kind, self.label = kind, label


def test_kinds_and_delays(tmp_path):
    assert classify(None)["kind"] == "none"
    r = classify(F("replay"), synthetic=True)
    assert r["kind"] == "synthetic_replay" and r["synthetic"] and not r["live"] and "SYNTHETIC" in r["label"]
    assert classify(F("replay"), synthetic=False)["kind"] == "replay"
    e = classify(F("angel_daily"))
    assert e["kind"] == "end_of_day" and e["delay_seconds"] == 900 and not e["live"]
    lv = classify(F("live_poll"), experiments_dir=tmp_path)
    assert lv["kind"] == "live_unverified" and lv["live"] and not lv["verified"] and "mock-tested" in lv["detail"]


def test_verified_only_after_a_passed_verification_run(tmp_path):
    (tmp_path / "angel_verification.json").write_text(json.dumps({"passed": False, "checks": [{"name": "login", "status": "failed"}]}))
    assert classify(F("live_poll"), experiments_dir=tmp_path)["kind"] == "live_unverified"
    assert "login" in verification_status(tmp_path)["detail"]
    (tmp_path / "angel_verification.json").write_text(json.dumps({"passed": True, "at": "2026-11-24T10:00:00", "checks": []}))
    v = classify(F("live_poll"), experiments_dir=tmp_path)
    assert v["kind"] == "live_verified" and v["verified"]


def test_freshness_and_staleness_only_matter_while_the_market_is_open(tmp_path):
    now = datetime(2026, 11, 24, 11, 0, 0)
    ok = classify(F("live_poll"), last_bar="2026-11-24T10:59:00", now=now, market_open=True, experiments_dir=tmp_path)
    assert ok["freshness_seconds"] == 60 and ok["stale"] is False
    old = classify(F("live_poll"), last_bar="2026-11-24T10:50:00", now=now, market_open=True, experiments_dir=tmp_path)
    assert old["stale"] is True and "STALE" in old["label"]
    closed = classify(F("live_poll"), last_bar="2026-11-24T10:50:00", now=now, market_open=False, experiments_dir=tmp_path)
    assert closed["stale"] is False


def test_dataset_labels():
    class D:
        synthetic, id = True, "synthetic_v1"
    assert classify_dataset(D())["label"] == "SYNTHETIC dataset"

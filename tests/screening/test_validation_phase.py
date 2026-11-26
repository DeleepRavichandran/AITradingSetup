"""Phase 'brain validation': staged validation, clustered inference, holdout reuse, protected evidence, research agenda,
adaptation recommendations. Deliberate failure injection throughout."""
import hashlib
import json
import sqlite3

import numpy as np
import pytest

from nseql.screening import evidence_guard as G
from nseql.screening import integrity as I
from nseql.screening import research_agenda as RA
from nseql.screening import validation_protocol as V


# ------------------------------------------------------------------ inference
def test_correlated_observations_do_not_inflate_significance():
    rng = np.random.default_rng(1)
    naive = clus = 0
    for _ in range(300):
        x = rng.normal(0, 0.01, 30)[:, None] + rng.normal(0, 0.002, (30, 40))       # shared session moves, no signal
        naive += I.t_test(x.ravel())["p"] < 0.05
        clus += V.cluster_test(x.ravel(), np.repeat(np.arange(30), 40))["p"] < 0.05
    assert naive / 300 > 0.2 and clus / 300 <= 0.09                                  # the old way is badly inflated


def test_a_planted_session_level_signal_is_still_detected():
    rng = np.random.default_rng(2)
    found = 0
    for _ in range(50):
        x = 0.006 + rng.normal(0, 0.01, 30)[:, None] + rng.normal(0, 0.002, (30, 40))
        found += V.cluster_test(x.ravel(), np.repeat(np.arange(30), 40))["p"] < 0.05
    assert found / 50 >= 0.8          # power is kept (0.6% vs a 1% session spread over 30 sessions; 0.4% would be ~62%)


# ------------------------------------------------------------------ stages
def test_partition_is_registered_once_and_the_holdout_only_grows(tmp_path):
    L = I.Ledger(tmp_path)
    days = [f"2026-01-{d:02d}" for d in range(1, 31)]
    p = V.register_partition(L, "k", days, "fp1", "defs1")
    dev, emb, hold = V.split(days, p)
    assert dev[-1] < emb[0] < hold[0] and len(emb) == V.EMBARGO_SESSIONS
    more = days + ["2026-02-01", "2026-02-02"]
    assert V.register_partition(L, "k", more, "fp2", "defs2") == p                    # not re-split
    assert V.split(more, p)[2][-2:] == ["2026-02-01", "2026-02-02"]                 # new data joins the holdout


def test_holdout_reuse_by_modified_definitions_is_detected(tmp_path):
    L = I.Ledger(tmp_path)
    assert V.holdout_access(L, "k", "defs1", "fp")["reused"] is False
    assert V.holdout_access(L, "k", "defs1", "fp")["reused"] is False                # the same definitions again
    assert V.holdout_access(L, "k", "defs2-tuned", "fp")["reused"] is True           # tuned after looking: not independent


def test_missing_or_small_holdout_never_qualifies():
    dev = {"n": 400, "clusters": 20}
    assert V.decide(dev, True, None, False, False)[0] == "validation candidate"
    assert V.decide(dev, True, {"n": 10, "clusters": 2}, True, False)[0] == "validation candidate"
    assert V.decide(dev, True, {"n": 400, "clusters": 12}, True, True)[0] == "validation candidate"    # reused holdout
    assert V.decide(dev, True, {"n": 400, "clusters": 12}, False, False)[0] == "rejected"
    assert V.decide(dev, True, {"n": 400, "clusters": 12}, True, False)[0] == "holdout-supported finding"
    assert V.decide({"n": 10, "clusters": 3}, True, None, False, False)[0] == "insufficient evidence"
    assert "validated" not in " ".join(V.STATUSES)


# ------------------------------------------------------------------ protected evidence
def test_research_role_cannot_write_decisions_criteria_or_protocols(tmp_path):
    R = G.GuardedLedger(I.Ledger(tmp_path), "research")
    R.append("result", "X", {"status": "rejected"})
    for kind in ("decision", "criteria", "protocol", "holdout_access", "suspension", "acknowledged"):
        with pytest.raises(G.RoleError):
            R.append(kind, "X", {"decision": "approve"})
    with pytest.raises(G.RoleError):
        G.GuardedLedger(I.Ledger(tmp_path), "evaluation").append("decision", "challenger", {"decision": "approve"})


def test_a_rechained_rewrite_passes_verify_but_fails_the_anchor(tmp_path):
    L = I.Ledger(tmp_path)
    for k in range(5):
        L.append("result", f"H{k}", {"status": "falsified"})
    a = G.anchor(tmp_path, L, note="test")
    assert a["backup_ok"] and G.verify_anchors(tmp_path, L)["ok"]
    # an attacker with file access rewrites a failed result as a success and recomputes every hash
    db = sqlite3.connect(L.path)
    db.execute("DROP TRIGGER entries_no_update")
    rows = db.execute("SELECT seq, at, kind, subject, body FROM entries ORDER BY seq").fetchall()
    prev = "genesis"
    for seq, at, kind, subject, body in rows:
        if seq == 2:
            body = json.dumps({"status": "survived"}, sort_keys=True)
        d = I._digest(prev, at, kind, subject, body)
        db.execute("UPDATE entries SET body=?, prev=?, digest=? WHERE seq=?", (body, prev, d, seq))
        prev = d
    db.commit()
    db.close()
    assert L.verify()["ok"]                                                           # the chain alone is fooled
    v = G.verify_anchors(tmp_path, L)
    assert not v["ok"] and "history rewritten" in v["problems"][0]["problem"]          # the anchor is not
    assert "same-user" in v["residual_risk"]


def test_a_tampered_anchor_log_is_detected(tmp_path):
    L = I.Ledger(tmp_path)
    L.append("result", "H", {"status": "falsified"})
    G.anchor(tmp_path, L)
    G.anchor(tmp_path, L)
    f = G.anchor_dir(tmp_path) / "anchors.jsonl"
    lines = f.read_text().splitlines()
    first = json.loads(lines[0])
    first["entries"] = 99
    f.write_text("\n".join([json.dumps(first), *lines[1:]]) + "\n")
    assert any(p["problem"] == "anchor log altered" for p in G.verify_anchors(tmp_path, L)["problems"])


# ------------------------------------------------------------------ research agenda and adaptation
def test_rejected_questions_are_not_reopened_without_new_evidence():
    k = {"K-PATTERN-opening": {"id": "K-PATTERN-opening", "status": "unverified hypothesis", "statement": "Bought early. Proposed test: No entries before 09:46.",
                               "evidence": {"trades": 20, "days": 3, "lost_inr": -400}}}
    qs = RA.questions({}, knowledge=k)
    assert qs and qs[0]["falsification"] and qs[0]["experiment"] and qs[0]["alternatives"]
    qid = qs[0]["id"]
    k[qid] = {"id": qid, "status": "contradicted finding", "evidence": {"evidence_size": 20}}
    assert not any(q["id"] == qid for q in RA.questions({}, knowledge=k))           # rejected, nothing new: not reopened
    k["K-PATTERN-opening"]["evidence"]["trades"] = 40
    assert any(q["id"] == qid and q.get("reopened") for q in RA.questions({}, knowledge=k))


def test_recommendations_never_switch_on_recent_profit_alone():
    rep = {"hypotheses": [{"id": "H-EDGE", "status": "abstain"}], "validation": {"drift": {"alarm": False}}, "suspension": {"triggered": []}}
    hist = {"report": {"segments": {"holdout": {"vs": {"incumbent": {"cash": {"reading": "indistinguishable"}}},
                                                "challenger_vs_incumbent": {"reading": "ahead (interval above zero)"}}}}}
    recs = {r["methodology"]: r for r in RA.recommendations(rep, {}, hist)}
    assert recs["incumbent"]["action"] == "observe" and recs["challenger"]["action"] == "investigate"
    assert all(r["action"] in RA.ACTIONS for r in recs.values()) and "switch" not in json.dumps(recs).lower()
    rep["suspension"]["triggered"] = [{"rule": "S3", "detail": "-7%"}]
    assert {r["methodology"]: r for r in RA.recommendations(rep)}["incumbent"]["action"] == "suspend"


def test_a_deliberately_overfit_selection_fails_the_sealed_holdout():
    """Try 40 noise 'strategies' on development, keep the best (classic overfitting), then open the holdout once."""
    rng = np.random.default_rng(11)
    sess = np.repeat(np.arange(30), 20)
    passes = 0
    for trial in range(20):
        dev = [rng.normal(0, 0.01, 30 * 20) for _ in range(40)]
        best = int(np.argmin([V.cluster_test(x, sess)["p"] for x in dev]))
        assert V.cluster_test(dev[best], sess)["p"] < 0.2                            # it looked good where it was chosen
        hold = rng.normal(0, 0.01, 30 * 20)                                         # no edge exists, so the holdout is noise
        passes += V.cluster_test(hold, sess)["p"] < V.ALPHA
    assert passes <= 3                                                              # ~5% by chance, not the selection's promise


def test_shadow_b_is_compared_forward_and_never_counted_as_an_account(tmp_path):
    """Prospective shadow portfolio B: compared with the incumbent on the same sessions since B started, reported as
    hypothetical, never listed among the live accounts, and never 'ahead' on a handful of sessions."""
    from nseql.screening import brain as B, integrity as I
    from tests.screening.test_integrity import _account
    exp = tmp_path / "exp"
    inc = _account(exp / "screening" / "live_s_v14", 21, {"rules_version": "1.4"})
    _account(exp / "screening" / "live_s_v14_shadow_B_bc-1_1_x21", 23, {"shadow": "B"})
    acc = I.accounts(exp)
    assert [p.name for p in acc["incumbent"]] == ["live_s_v14"]                      # the shadow is not an account
    sb = B.shadow_forward(exp, inc)
    assert sb["account"].endswith("_shadow_B_bc-1_1_x21") and sb["sessions"] == 3
    assert sb["reading"] == "too few sessions" and "hypothetical" in sb["note"]
    assert set(sb["quality_B"]) == set(sb["quality_A"])


def test_few_sessions_never_read_as_ahead():
    """Found in the screenshot review: with sessions <= the bootstrap block length (5) every resample is the whole series, so
    the 'interval' is a point and one good day read as 'ahead (interval above zero)'. Below 20 sessions there is no reading,
    in the history ablation and in the promotion gate alike."""
    import numpy as np
    from nseql.screening import history_validation as H
    from nseql.screening.contest import block_bootstrap
    d = [0.004, 0.003]
    assert block_bootstrap(np.array(d))["low"] == block_bootstrap(np.array(d))["high"]          # the degenerate interval
    r = H._excess(d, [0.0, 0.0])
    assert r["reading"].startswith("too few sessions") and r["mean_daily_excess"] > 0
    rng = np.random.default_rng(1)
    a = list(0.003 + rng.normal(0, 0.002, 40))
    assert H._excess(a, [0.0] * 40)["reading"] == "ahead (interval above zero)"               # a real effect is still read

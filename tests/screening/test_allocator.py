"""Ranked capital allocation: evidence shrinkage, ranking, accepted / rejected / deferred, no future data,
many simultaneous BUYs, checkpoint round trip, and the first-come baseline still available."""
import numpy as np
import pandas as pd
import pytest

from nseql.screening.allocator import Allocator, AllocatorConfig
from nseql.screening.engine import ScreeningEngine
from nseql.screening.execution import ExecConfig, PortfolioRules
from nseql.screening.registry import build
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols

DAYS = ["2026-11-16", "2026-11-17"]


def test_shrinkage_confidence_and_qualification():
    a = Allocator(4, 2, 4, AllocatorConfig(k_config=10, k_group=20, min_trades=5))
    group = np.array([0, 0, 1, 1])
    est = a.estimates(group)
    assert np.allclose(est["mu"], 0) and np.allclose(est["p_win"], 0.5) and not est["qualified"].any()
    for _ in range(10):
        a.record_round_trip(0, 0.01)                     # config 0: ten wins of +1%
    est = a.estimates(group)
    g_mu = 0.1 / (10 + 20)                               # group pulled toward zero
    assert est["mu"][0] == pytest.approx((0.1 + 10 * g_mu) / 20)
    assert est["mu"][1] == pytest.approx(g_mu) and est["mu"][2] == 0     # same group borrows; other group does not
    assert est["confidence"][0] == pytest.approx(0.5) and est["qualified"][0] and not est["qualified"][1]


def test_reliability_drops_for_badly_calibrated_predictions():
    a = Allocator(1, 1, 1, AllocatorConfig(calib_min=30))
    assert a.reliability() == 1.0
    for k in range(40):                                   # predicts 90% wins; half the trades lose
        a.record_entry(0, 0.9)
        a.record_round_trip(0, 0.01 if k % 2 else -0.01)
    c = a.calibration()
    assert c["resolved_predictions"] == 40 and c["brier"] > c["base_rate_brier"] and 0.5 <= a.reliability() < 1.0


def _engine(allocation, max_positions=50, n_sym=6, minutes=40):
    reg = build(symbols(n_sym))
    eng = ScreeningEngine(reg, ExecConfig(), PortfolioRules(max_positions=max_positions, allocation=allocation))
    for ts, bars in minute_stream(n_sym, DAYS[:1], seed=3, missing=0)[:minutes]:
        eng.step(ts, [b["symbol"] for b in bars], *(np.array([b[k] for b in bars]) for k in ("open", "high", "low", "close", "volume")),
                 allow_orders=False)
    eng.pend[:] = 0
    return eng


def _two_candidates(eng):
    r = eng.reg
    lo = int(np.flatnonzero(r.sym == 0)[0])
    hi = int(np.flatnonzero(r.sym == 1)[-1])
    assert lo < hi
    return lo, hi


def test_proven_configuration_wins_the_last_slot_and_the_loser_is_deferred():
    em = 10**7
    for mode in ("ranked", "first_come"):
        eng = _engine(mode, max_positions=1)
        lo, hi = _two_candidates(eng)
        for _ in range(12):
            eng.x.alloc.record_round_trip(hi, 0.004)        # hi has a forward record; lo has none
        out = {o["config"]: o for o in eng.x.submit([lo, hi], {lo: "BUY", hi: "BUY"}, em, True)}
        winner, loser = (hi, lo) if mode == "ranked" else (lo, hi)
        assert out[winner]["alloc"] == "accepted" and out[winner]["status"] == "pending"
        assert out[loser]["alloc"] == "deferred" and "max 1 positions after" in out[loser]["reason"]
        if mode == "ranked":
            assert "(qualified)" in out[hi]["alloc_reason"] and "from 12 trades" in out[hi]["alloc_reason"]
            assert out[hi]["score"] > out[lo]["score"]
        else:
            assert out[lo]["alloc_reason"] == "registry order (first come)"


def test_a_limit_already_full_is_a_rejection_not_a_deferral():
    eng = _engine("ranked", max_positions=1)
    lo, hi = _two_candidates(eng)
    eng.pos[0] = 1                                        # the only slot is taken before this minute
    eng.qty[0] = 1
    out = eng.x.submit([lo, hi], {lo: "BUY", hi: "BUY"}, 10**7, True)
    assert {o["alloc"] for o in out} == {"rejected"} and all(o["reason"] == "max 1 positions" for o in out)


def test_one_new_entry_per_symbol_per_minute_when_ranked():
    eng = _engine("ranked")
    same = [int(i) for i in np.flatnonzero(eng.reg.sym == 2)[:5]]
    out = eng.x.submit(same, {i: "BUY" for i in same}, 10**7, True)
    assert [o["alloc"] for o in out].count("accepted") == 1
    assert all("redundant" in o["reason"] for o in out if o["alloc"] == "deferred") and len(out) == 5


def _run(root, stream, allocation, upto=None):
    sch = MinuteScheduler(root, symbols(12), portfolio=PortfolioRules(max_positions=8, allocation=allocation))
    for ts, bars in stream[:upto]:
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    return sch


def test_no_future_data_ranking_up_to_t_ignores_what_comes_after(tmp_path):
    base = minute_stream(12, DAYS, seed=21, missing=0)
    cut = 500
    alt = base[:cut] + minute_stream(12, DAYS, seed=99, missing=0)[cut:]      # a different future after minute `cut`
    a, b = _run(tmp_path / "a", base, "ranked"), _run(tmp_path / "b", alt, "ranked")
    t = base[cut - 1][0].strftime("%Y-%m-%dT%H:%M:%S")
    q = "SELECT config_id, bar_ts, decision, alloc_status, alloc_reason, alloc_score FROM signals WHERE bar_ts <= ? ORDER BY 1, 2"
    sa, sb = a.store.db.execute(q, (t,)).fetchall(), b.store.db.execute(q, (t,)).fetchall()
    assert len(sa) > 50 and [tuple(x) for x in sa] == [tuple(x) for x in sb]
    later = "SELECT COUNT(*) FROM signals WHERE bar_ts > ?"
    assert a.store.db.execute(later, (t,)).fetchone()[0] > 0


def test_many_simultaneous_buys_every_signal_explained_and_capital_goes_best_first(tmp_path):
    stream = minute_stream(12, DAYS, seed=8, missing=0)
    sch = _run(tmp_path / "r", stream, "ranked")
    db = sch.store.db
    rows = db.execute("SELECT bar_ts, alloc_status, alloc_reason, alloc_score FROM signals WHERE decision='BUY'").fetchall()
    assert rows and all(r[1] in ("accepted", "rejected", "deferred") and r[2] for r in rows)
    busiest = db.execute("SELECT bar_ts, COUNT(*) n FROM signals WHERE decision='BUY' GROUP BY bar_ts ORDER BY n DESC LIMIT 1").fetchone()
    assert busiest[1] >= 20, "need a minute with many simultaneous BUYs"
    for ts, in db.execute("SELECT DISTINCT bar_ts FROM signals WHERE decision='BUY' AND alloc_status='deferred' AND alloc_reason LIKE '%cash%'"):
        acc = [r[0] for r in db.execute("SELECT alloc_score FROM signals WHERE bar_ts=? AND alloc_status='accepted'", (ts,))]
        dfr = [r[0] for r in db.execute("SELECT alloc_score FROM signals WHERE bar_ts=? AND alloc_status='deferred' AND alloc_reason LIKE '%cash%'", (ts,))]
        assert min(acc) >= max(dfr) - 1e-9
    pf = sch.engine.x.snapshot()["allocation"]
    assert pf["method"] == "ranked" and pf["round_trips"] > 0


def test_allocator_state_survives_a_checkpoint(tmp_path):
    stream = minute_stream(12, DAYS, seed=8, missing=0)
    sch = _run(tmp_path / "c", stream, "ranked", upto=600)
    st = sch.engine.state()
    eng2 = ScreeningEngine(sch.engine.reg, ExecConfig(), PortfolioRules(max_positions=8))
    eng2.load(st)
    for k in Allocator.ARR:
        assert np.array_equal(np.nan_to_num(getattr(eng2.x.alloc, k), nan=-7), np.nan_to_num(getattr(sch.engine.x.alloc, k), nan=-7)), k
    assert (eng2.x.alloc.buf_pos, eng2.x.alloc.buf_n) == (sch.engine.x.alloc.buf_pos, sch.engine.x.alloc.buf_n)


def test_unknown_allocation_is_refused():
    with pytest.raises(ValueError):
        ScreeningEngine(build(symbols(1)), ExecConfig(), PortfolioRules(allocation="random"))


def test_proven_loser_gets_no_capital_but_unproven_configurations_still_trade():
    eng = _engine("ranked")
    lo, hi = _two_candidates(eng)
    for _ in range(8):
        eng.x.alloc.record_round_trip(hi, -0.003)          # eight forward losses after costs
    out = {o["config"]: o for o in eng.x.submit([lo, hi], {lo: "BUY", hi: "BUY"}, 10**7, True)}
    assert out[hi]["alloc"] == "rejected" and out[hi]["reason"].startswith("expected net return after costs -")
    assert out[lo]["alloc"] == "accepted"                   # no evidence yet: allowed to explore

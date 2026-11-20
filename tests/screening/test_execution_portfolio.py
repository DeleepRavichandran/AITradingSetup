"""Realistic fills (volume cap, spread, impact, partial fills) and the shared portfolio (one capital pot,
exposure limits, daily loss cap) in the one-minute loop."""
import math
from collections import defaultdict

import numpy as np
import pandas as pd
import pytest

from nseql.screening.execution import ExecConfig, PortfolioRules
from nseql.screening.features import SESSION_MINUTES, SESSION_OPEN_MIN
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols

DAYS = ["2026-11-16", "2026-11-17", "2026-11-18"]


def _drive(root, stream, syms, n=None, upto=None, **kw):
    sch = MinuteScheduler(root, syms, n, **kw)
    for ts, bars in stream[:upto]:
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    return sch


@pytest.fixture(scope="module")
def stream():
    return minute_stream(40, DAYS, seed=4)


@pytest.fixture(scope="module")
def run(stream, tmp_path_factory):
    """40 symbols, all 6,840 configurations, default realistic execution and shared portfolio."""
    root = tmp_path_factory.mktemp("real")
    checks = {"max_open": 0, "min_free_cash": float("inf"), "min_reserved": 0, "gross_over": 0.0}
    sch = MinuteScheduler(root, symbols(40))
    x, e = sch.engine.x, sch.engine
    for ts, bars in stream:
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
        checks["max_open"] = max(checks["max_open"], int(((e.pos == 1) | (e.pend == 1)).sum()))
        checks["min_free_cash"] = min(checks["min_free_cash"], (x.cash - x.reserved) / 100)
        checks["min_reserved"] = min(checks["min_reserved"], x.reserved)
    return {"sch": sch, "checks": checks}


def _vol(stream):
    return {(b["symbol"], ts.strftime("%Y-%m-%dT%H:%M:%S")): b for ts, bars in stream for b in bars}


def test_fills_never_take_more_than_the_participation_cap(run, stream):
    db, bars = run["sch"].store.db, _vol(stream)
    per = defaultdict(int)
    for sym, ts, q in db.execute("SELECT symbol, ts, qty FROM paper_fills"):
        per[(sym, ts)] += q
    assert per, "no fills happened"
    over = [(k, q, bars[k]["volume"]) for k, q in per.items() if q > int(0.10 * bars[k]["volume"])]
    assert over == []


def test_fill_prices_include_spread_and_impact_on_the_tick_grid(run, stream):
    db, bars, cfg = run["sch"].store.db, _vol(stream), ExecConfig()
    n = 0
    for sym, ts, side, q, px in db.execute("SELECT symbol, ts, side, qty, price FROM paper_fills"):
        b = bars[(sym, ts)]
        frac = (cfg.half_spread_bps + cfg.impact_bps * math.sqrt(q / b["volume"])) / 1e4
        raw = b["open"] * (1 + frac) if side == "BUY" else b["open"] * (1 - frac)
        assert (px >= raw - 1e-9) if side == "BUY" else (px <= raw + 1e-9)
        assert abs(px - raw) < 0.05 + 1e-9 and abs(round(px / 0.05) * 0.05 - px) < 1e-9
        n += 1
    assert n > 100


def test_one_capital_pot_cash_identity_and_limits(run):
    sch, chk = run["sch"], run["checks"]
    x, db, rules = sch.engine.x, sch.store.db, PortfolioRules()
    buys = db.execute("SELECT COALESCE(SUM(qty * price * 100), 0), COALESCE(SUM(charges_paise), 0) FROM paper_fills WHERE side='BUY'").fetchone()
    sells = db.execute("SELECT COALESCE(SUM(qty * price * 100), 0), COALESCE(SUM(charges_paise), 0) FROM paper_fills WHERE side='SELL'").fetchone()
    expect = round(rules.capital * 100 - buys[0] - buys[1] + sells[0] - sells[1])
    assert abs(x.cash - expect) <= 2                                          # paise rounding only
    assert chk["min_free_cash"] >= 0 and chk["min_reserved"] >= 0             # never spent the same rupee twice
    assert chk["max_open"] <= rules.max_positions
    held = defaultdict(int)
    for cid, side, q in db.execute("SELECT config_id, side, qty FROM paper_fills"):
        held[cid] += q if side == "BUY" else -q
    e = sch.engine
    for i in np.flatnonzero(e.qty):
        assert held[str(e.reg.ids[i])] == e.qty[i]
    assert all(v >= 0 for v in held.values())


def test_refused_orders_are_recorded_with_reasons(run):
    db = run["sch"].store.db
    reasons = dict(db.execute("SELECT reason, SUM(n) FROM refusals GROUP BY reason").fetchall())
    assert reasons and any(k.startswith("max 50 positions") or "cash" in k or "exposure" in k for k in reasons), reasons
    sig = db.execute("SELECT COUNT(*) FROM signals WHERE decision='BUY'").fetchone()[0]
    acc = db.execute("SELECT COUNT(*) FROM paper_orders WHERE side='BUY'").fetchone()[0]
    assert acc + sum(reasons.values()) == sig                                  # every BUY signal is either an order or a counted refusal
    assert db.execute("SELECT COUNT(*) FROM paper_orders WHERE status='rejected'").fetchone()[0] == 0


def test_partial_fills_and_entry_timeout(tmp_path):
    """A thinly traded symbol: entries need several bars; an entry still incomplete after 5 minutes keeps what filled."""
    stream = minute_stream(3, DAYS[:2], seed=9)
    for ts, bars in stream:
        for b in bars:
            b["volume"] = 40.0                                                 # 10% cap = 4 shares per minute
    sch = _drive(tmp_path, stream, symbols(3), portfolio=PortfolioRules(capital=50_000_000, per_trade_pct=0.001))
    db = sch.store.db
    multi = db.execute("SELECT order_key, COUNT(*) FROM paper_fills GROUP BY order_key HAVING COUNT(*) > 1").fetchall()
    assert multi, "expected orders split over several bars"
    part = db.execute("SELECT status, reason, qty, requested_qty FROM paper_orders WHERE status IN ('partial','cancelled') AND side='BUY'").fetchall()
    assert part and all("unfilled after 5 min" in r[1] for r in part)
    assert all(r[2] < r[3] for r in part)
    exits = db.execute("SELECT status FROM paper_orders WHERE side='SELL' AND status NOT IN ('filled','pending')").fetchall()
    assert exits == []                                                         # exits are never abandoned


def test_daily_loss_limit_stops_new_entries_for_the_day(tmp_path):
    syms = symbols(20)
    stream = minute_stream(20, DAYS[:2], seed=2, missing=0.0)
    # day 1 afternoon: every stock slides 6% between 12:00 and 13:00 - after the morning's entries
    for ts, bars in stream:
        m = ts.hour * 60 + ts.minute - SESSION_OPEN_MIN
        if ts.date() == pd.Timestamp(DAYS[0]).date() and 165 <= m <= SESSION_MINUTES:
            k = min(m - 165, 60) / 60
            for b in bars:
                for f in ("open", "high", "low", "close"):
                    b[f] = round(b[f] * (1 - 0.06 * k), 2)
    sch = _drive(tmp_path, stream, syms, portfolio=PortfolioRules(per_trade_pct=0.04, max_positions=25, daily_loss_pct=0.005))
    db = sch.store.db
    ev = db.execute("SELECT ts FROM portfolio_events WHERE event='DAILY_LOSS_LIMIT'").fetchall()
    assert len(ev) >= 1 and ev[0][0].startswith(DAYS[0])
    halt = ev[0][0].replace(" ", "T")
    after = db.execute("SELECT COUNT(*) FROM paper_orders WHERE side='BUY' AND status != 'rejected' AND decided_at > ? AND decided_at < ?",
                       (halt, DAYS[1])).fetchone()[0]
    refused = db.execute("SELECT COALESCE(SUM(n), 0) FROM refusals WHERE reason='daily loss limit reached'").fetchone()[0]
    nextday = db.execute("SELECT COUNT(*) FROM paper_orders WHERE side='BUY' AND status != 'rejected' AND decided_at > ?", (DAYS[1],)).fetchone()[0]
    assert after == 0 and refused > 0 and nextday > 0


def test_realistic_run_recovers_exactly_after_a_crash(stream, run, tmp_path):
    total = len(stream)
    a = _drive(tmp_path, stream, symbols(40), upto=600)
    del a
    b = MinuteScheduler(tmp_path, symbols(40))
    assert b.recovery["checkpoint"] is not None
    for ts, bars in stream:
        b.ingest(bars)
        b.run_cycle(ts + pd.Timedelta(seconds=6))
    q = lambda s, sql: [tuple(r) for r in s.store.db.execute(sql)]
    ref = run["sch"]
    for sql in ("SELECT * FROM paper_fills ORDER BY 1", "SELECT order_key, status, qty, price, charges_paise, realized_paise, reason FROM paper_orders ORDER BY 1", "SELECT * FROM refusals ORDER BY 1, 2",
                "SELECT minute, equity_inr, cash_inr, halted FROM cycles ORDER BY 1"):
        assert q(b, sql) == q(ref, sql)
    assert b.engine.x.cash == ref.engine.x.cash and total == 1125


def test_checkpoint_from_another_execution_model_is_refused(stream, tmp_path):
    _drive(tmp_path, stream, symbols(40), upto=40, execution="simple")
    with pytest.raises(ValueError, match="execution model"):
        MinuteScheduler(tmp_path, symbols(40))

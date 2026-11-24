"""Regression tests for the measurement findings of the 2026-11-24 review. Each test failed before its fix."""
import sqlite3

import numpy as np
import pandas as pd
import pytest
from nseql.screening.execution import PortfolioRules
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols

DAY = "2026-11-16"
LEDGER = ("SELECT * FROM paper_fills ORDER BY 1", "SELECT order_key, status, qty, price, charges_paise, realized_paise FROM paper_orders ORDER BY 1",
          "SELECT minute, status, equity_inr, cash_inr FROM cycles ORDER BY 1", "SELECT * FROM refusals ORDER BY 1, 2")


def _rows(sch, sql):
    return [tuple(r) for r in sch.store.db.execute(sql)]


@pytest.fixture(scope="module")
def stream():
    return minute_stream(6, [DAY], seed=21)


@pytest.fixture(scope="module")
def reference(stream, tmp_path_factory):
    sch = MinuteScheduler(tmp_path_factory.mktemp("ref"), symbols(6), portfolio=PortfolioRules(capital=100_000))
    for ts, bars in stream:
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    return sch


# ---------------------------------------------------------------- finding 2: failed ledger write
def test_failed_ledger_write_then_retry_equals_an_uninterrupted_run(stream, reference, tmp_path):
    sch = MinuteScheduler(tmp_path, symbols(6), portfolio=PortfolioRules(capital=100_000))
    real = sch.store.record
    fail_from = pd.Timestamp(stream[120][0]).isoformat()
    state = {"left": 2}                                              # the write fails twice, then the database recovers

    def flaky(cycle, *a, **k):
        if state["left"] and cycle["minute"] >= fail_from:
            state["left"] -= 1
            raise sqlite3.OperationalError("database is locked (simulated)")
        return real(cycle, *a, **k)
    sch.store.record = flaky
    errors = 0
    for ts, bars in stream:
        sch.ingest(bars)
        for _ in range(3):                                          # the scheduler retries on its next tick
            try:
                sch.run_cycle(ts + pd.Timedelta(seconds=6))
                break
            except sqlite3.OperationalError:
                errors += 1
    assert errors == 2
    for sql in LEDGER:
        assert _rows(sch, sql) == _rows(reference, sql), sql
    x, r = sch.engine.x, reference.engine.x
    assert x.cash == r.cash and np.array_equal(sch.engine.qty, reference.engine.qty) and np.array_equal(sch.engine.pos, reference.engine.pos)


# ---------------------------------------------------------------- finding 4: catch-up and genuine gaps
def test_catch_up_batch_marks_only_the_genuine_gap_as_no_data(stream, tmp_path):
    sch = MinuteScheduler(tmp_path, symbols(6), portfolio=PortfolioRules(capital=100_000))
    for ts, bars in stream[:30]:
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    batch = stream[30:36]                       # six minutes arrive together (catch-up); the fourth has no data at all
    gap = pd.Timestamp(batch[3][0]).isoformat()
    for k, (ts, bars) in enumerate(batch):
        if k != 3:
            sch.ingest(bars)
    sch.run_cycle(batch[-1][0] + pd.Timedelta(seconds=6))
    got = dict(_rows(sch, "SELECT minute, status FROM cycles WHERE minute >= '%s' AND minute <= '%s'"
                     % (pd.Timestamp(batch[0][0]).isoformat(), pd.Timestamp(batch[-1][0]).isoformat())))
    assert got == {pd.Timestamp(t).isoformat(): ("no_data" if pd.Timestamp(t).isoformat() == gap else "processed") for t, _ in batch}


# ---------------------------------------------------------------- finding 6: overnight swing trades in the daily report
def test_overnight_swing_exit_keeps_entry_time_and_buy_charges(tmp_path):
    from nseql.screening.rca import analyse
    from nseql.screening.store import ScreeningStore
    from nseql.screening.trade_report import round_trips
    st = ScreeningStore(tmp_path / "screening.sqlite")
    st.register_configs([("cfgswing", 0, "AAA", "1d", "swing", "cross", "{}", 0.05)])
    orders = [("b1", "cfgswing", "AAA", "BUY", "swing", "2026-11-16T15:30:00", "filled", "2026-11-17T09:16:00", 10, 100.0, "CNC", 1500, 0),
              ("s1", "cfgswing", "AAA", "SELL", "swing", "2026-11-18T13:00:00", "filled", "2026-11-18T13:01:00", 10, 104.0, "CNC", 1700, 3800)]
    st.db.executemany("INSERT INTO paper_orders (order_key, config_id, symbol, side, horizon, decided_at, status, fill_ts, qty, price, product, "
                      "charges_paise, realized_paise) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", orders)
    rt = round_trips(st.db, "2026-11-18")
    assert len(rt) == 1
    r = rt.iloc[0]
    assert str(r.entry_ts) == "2026-11-17 09:16:00" and r.entry_px == 100.0 and r.hold_min > 24 * 60
    assert r.charges_inr == pytest.approx(32.0) and r.gross_inr == pytest.approx(38.0 + 32.0)
    assert round_trips(st.db, "2026-11-17").empty                      # the entry day has no completed round trip
    rc = analyse(st.db, "2026-11-18", write=False)
    assert rc[0]["entry_ts"] is not None and rc[0]["charges_inr"] == pytest.approx(32.0)


def test_ledger_audit_labels_old_ledgers_without_modifying_them(tmp_path):
    import hashlib
    from nseql.screening.ledger_audit import audit
    from nseql.screening.store import ScreeningStore
    st = ScreeningStore(tmp_path / "screening.sqlite")
    st.db.execute("INSERT INTO events VALUES ('AAA','2026-11-23T10:00:00',1,1,1,1,1,'feed',0,0)")
    st.db.execute("INSERT INTO events VALUES ('AAA','2026-11-23T10:01:00',1,1,1,1,1,'feed',0,0)")
    st.db.execute("INSERT INTO cycles (minute, processed_at, status) VALUES ('2026-11-23T10:00:00', 0, 'no_data')")   # F4 trace
    st.db.execute("INSERT INTO cycles (minute, processed_at, status) VALUES ('2026-11-23T10:02:00', 0, 'processed')")  # 10:01 skipped: F2
    st.db.close()
    before = hashlib.sha256((tmp_path / "screening.sqlite").read_bytes()).hexdigest()
    rep = audit(tmp_path)
    # X: this hand-built ledger has no execution.json, i.e. it predates execution versioning (2026-11-25): flagged for recompute
    assert {f["id"] for f in rep["findings"]} == {"F2", "F4", "X"} and not rep["clean"] and rep["ledger_modified"] is False
    assert hashlib.sha256((tmp_path / "screening.sqlite").read_bytes()).hexdigest() == before
    assert (tmp_path / "LIMITATIONS.json").exists()

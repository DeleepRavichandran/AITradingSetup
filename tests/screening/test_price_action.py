"""Price action and candle patterns at entry: textbook patterns, point in time, recorded with every buy, used by the review."""
import json
import sqlite3

import pandas as pd

from nseql.screening import price_action as PA

DAY = "2026-11-16"


def c5(rows):
    return pd.DataFrame([{"open": o, "high": h, "low": lo, "close": c, "volume": 100, "n": 5, "end": None} for o, h, lo, c in rows])


def test_textbook_patterns():
    assert PA.pattern(c5([(105, 106, 99, 100), (99, 107, 98.5, 106)])) == "bullish engulfing"
    assert PA.pattern(c5([(100, 106, 99, 105), (106, 106.5, 98, 99)])) == "bearish engulfing"
    assert PA.pattern(c5([(100, 110, 90, 95), (96, 104, 94, 97)])) == "inside bar"
    assert PA.pattern(c5([(100, 100.2, 96, 100.1)])) == "doji"
    assert PA.pattern(c5([(99, 100.1, 94, 100)])) == "hammer"
    assert PA.pattern(c5([(100, 106, 99.9, 99)])) == "shooting star"
    assert PA.pattern(c5([(100, 105.1, 99.95, 105)])) == "green marubozu"
    assert PA.structure(c5([(1, 10, 5, 2), (1, 11, 6, 2), (1, 12, 7, 2)])) == "higher highs, higher lows (up)"
    assert PA.structure(c5([(1, 12, 7, 2), (1, 11, 6, 2), (1, 10, 5, 2)])) == "lower highs, lower lows (down)"


def _db(bars):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE events (symbol TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT, received_at REAL, late INTEGER)")
    db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,'t',0,0)", bars)
    return db


def test_only_bars_before_the_fill_are_used():
    t0 = pd.Timestamp(f"{DAY}T09:16:00")
    bars = [("AAA", (t0 + pd.Timedelta(minutes=k)).strftime("%Y-%m-%dT%H:%M:%S"), 100 + k * 0.1, 100.2 + k * 0.1, 99.9 + k * 0.1, 100.1 + k * 0.1, 1000)
            for k in range(20)]
    db = _db(bars)
    a = PA.at_entry(db, "AAA", f"{DAY}T09:31:00")                    # 15 bars before: three complete 5-minute candles
    assert a["structure"] == "higher highs, higher lows (up)" and a["day_position"] is not None and a["vs_vwap"] > 0
    db.execute("INSERT INTO events VALUES ('AAA', ?, 50, 51, 1, 2, 999999, 't', 0, 0)", (f"{DAY}T09:31:00",))   # the fill minute: a crash
    assert PA.at_entry(db, "AAA", f"{DAY}T09:31:00") == a            # never read
    assert len(PA.bars(db, "AAA", DAY, 5)) == 4 and PA.bars(db, "AAA", DAY, 1)[0]["t"] == "09:16"


def test_every_buy_records_price_action_and_the_review_uses_it(tmp_path):
    from nseql.screening.daily_report import run as trade_report
    from nseql.screening.execution import PortfolioRules
    from nseql.screening.loss_study import run as loss_study
    from nseql.screening.scheduler import MinuteScheduler
    from nseql.screening.synthetic import minute_stream, symbols
    acct = tmp_path / "acct"
    sch = MinuteScheduler(acct, symbols(6), portfolio=PortfolioRules(capital=100_000), live_from=f"{DAY}T09:16:00", mode="live")
    sch.verbose = False
    for ts, bars in minute_stream(6, [DAY], seed=13):
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    sch.store.db.commit()
    (acct / "live.json").write_text(json.dumps({"live_from": f"{DAY}T09:16:00", "symbols": symbols(6), "capital": 100_000}))
    rows = sch.store.db.execute("SELECT side, price_action FROM journal").fetchall()
    buys = [json.loads(p) for s, p in rows if s == "BUY"]
    assert buys and all("pattern" in b and "structure" in b for b in buys)
    assert all(p is None for s, p in rows if s == "SELL")
    tr = trade_report(acct, DAY)
    assert all("chart_at_buy" in t and t["chart_at_buy"]["pattern"] for t in tr["trades"])
    ls = loss_study(acct, DAY, tmp_path / "ls")
    assert {"pa_pattern", "pa_structure", "pa_location", "pa_vwap", "pa_volume"} <= {f["factor"] for f in ls["factors"]}

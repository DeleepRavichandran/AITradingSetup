"""TradingView chart exports: file-name parsing, Unix and ISO time, bar stamping, guard rails."""
from pathlib import Path

import pandas as pd
import pytest

from nseql.data.adapters.tradingview import import_tradingview, parse_name

DAILY = "time,open,high,low,close,MA,Volume\n1735702200,1200.5,1215,1195,1210.25,1190,3500000\n1735788600,1210,1222.4,1201,1219.9,1191,2800000\n"


def test_file_names():
    assert parse_name(Path("NSE_RELIANCE, 1D.csv")) == ("RELIANCE", "1d", "NSE")
    assert parse_name(Path("NSE_M&M, 5.csv")) == ("M&M", "5m", "NSE")
    assert parse_name(Path("NSE_TCS, 1D (1).csv")) == ("TCS", "1d", "NSE")
    assert parse_name(Path("INFY.csv")) == ("INFY", None, None)


def test_daily_export_with_unix_time(tmp_path):
    f = tmp_path / "NSE_RELIANCE, 1D.csv"
    f.write_text(DAILY)
    ds, notes = import_tradingview([f], tmp_path / "data", "tv1", licence="TradingView export, personal research")
    b = ds.bars("RELIANCE", "1d")
    # 1735702200 = 2025-01-01 03:30 UTC = 09:00 IST session start; stored as that day's 15:30 close
    assert list(b["ts"]) == [pd.Timestamp("2025-01-01 15:30"), pd.Timestamp("2025-01-02 15:30")]
    assert b["close"].tolist() == [1210.25, 1219.9] and b["volume"].tolist() == [3500000, 2800000]
    assert notes == [] and not ds.synthetic and ds.manifest.raw_files[0]["name"] == f.name


def test_intraday_iso_time_is_stamped_at_bar_end_and_missing_volume_is_flagged(tmp_path):
    f = tmp_path / "NSE_TCS, 5.csv"
    f.write_text("time,open,high,low,close\n2025-01-01T09:15:00+05:30,4000,4010,3995,4005\n2025-01-01T09:20:00+05:30,4005,4012,4001,4011\n")
    ds, notes = import_tradingview([f], tmp_path / "data", "tv2", licence="x")
    assert list(ds.bars("TCS", "5m")["ts"]) == [pd.Timestamp("2025-01-01 09:20"), pd.Timestamp("2025-01-01 09:25")]
    assert notes and "no Volume" in notes[0]


def test_guard_rails(tmp_path):
    a, b = tmp_path / "NSE_A, 1D.csv", tmp_path / "NSE_B, 5.csv"
    a.write_text(DAILY); b.write_text(DAILY)
    with pytest.raises(ValueError, match="mix intervals"):
        import_tradingview([a, b], tmp_path / "d", "x", licence="x")
    with pytest.raises(ValueError, match="licence"):
        import_tradingview([a], tmp_path / "d", "x", licence=" ")
    bad = tmp_path / "NSE_C, 1D.csv"
    bad.write_text("date,price\n2025-01-01,1\n")
    with pytest.raises(ValueError, match="not a TradingView export"):
        import_tradingview([bad], tmp_path / "d", "y", licence="x")


def test_millisecond_time_is_recognised_and_impossible_dates_are_refused(tmp_path):
    f = tmp_path / "NSE_SBIN, 1D.csv"
    f.write_text("time,open,high,low,close,Volume\n1735702200000,800,810,795,805,1\n")
    ds, _ = import_tradingview([f], tmp_path / "d", "ms", licence="x")
    assert ds.bars("SBIN", "1d")["ts"].iloc[0] == pd.Timestamp("2025-01-01 15:30")
    g = tmp_path / "NSE_ITC, 1D.csv"
    g.write_text("time,open,high,low,close,Volume\n1704080,800,810,795,805,1\n")
    with pytest.raises(ValueError, match="cannot be NSE data"):
        import_tradingview([g], tmp_path / "d", "bad", licence="x")

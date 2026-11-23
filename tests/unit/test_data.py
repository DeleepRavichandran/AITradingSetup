"""Dataset store, fixture determinism, quality checks, adapters, point-in-time events."""
import json
from datetime import date

import httpx
import pandas as pd
import pytest

from nseql.data.adapters.bhavcopy import detect_layout, import_bhavcopies
from nseql.data.adapters.generic_csv import import_csv
from nseql.data.adapters.kite import CredentialsMissing, KiteHistorical
from nseql.data.events import EventStore
from nseql.data.fixtures import generate_fixture
from nseql.data.quality import check_bars, dataset_quality
from nseql.data.store import Dataset, DatasetWriter
from nseql.instruments import structure
from nseql.instruments.models import Kind
from tests.helpers import frame

FIX = __import__("pathlib").Path(__file__).parent.parent / "fixtures"


def test_fixture_is_deterministic_and_labelled_synthetic(tmp_path):
    kw = dict(start=date(2023, 1, 2), end=date(2023, 4, 28), options_from=date(2023, 3, 1))
    a = generate_fixture(tmp_path / "a", **kw)
    b = generate_fixture(tmp_path / "b", **kw)
    assert a.content_hash == b.content_hash and a.synthetic
    assert all(s.startswith("SYN") for s in a.instruments)
    c = generate_fixture(tmp_path / "c", seed=1, **kw)
    assert c.content_hash != a.content_hash


def test_datasets_are_immutable_and_tamper_evident(tmp_path, data_dir):
    with pytest.raises(FileExistsError):
        DatasetWriter(data_dir, "synthetic_v1", description="x", source="x", licence="x", synthetic=True)
    w = DatasetWriter(tmp_path, "d", description="x", source="x", licence="test", synthetic=True)
    w.write_bars("AAA", "1d", frame([(1, 2, 1, 2)] * 3))
    ds = w.finish()
    assert ds.verify() == []
    f = frame([(1.0, 2.0, 1.0, 2.0)] * 3); f.loc[1, "close"] = 1.5
    f.to_parquet(tmp_path / "d" / "bars" / "1d" / "AAA.parquet", index=False)
    assert Dataset(tmp_path / "d").verify() == ["bars/1d/AAA.parquet"]


def test_duplicate_timestamps_are_corrected_with_an_audit_trail(tmp_path):
    w = DatasetWriter(tmp_path, "d", description="x", source="x", licence="test", synthetic=True)
    f = frame([(1, 2, 1, 2), (3, 4, 3, 4), (5, 6, 5, 6)])
    f.loc[2, "ts"] = f.loc[1, "ts"]
    w.write_bars("AAA", "1d", f)
    ds = w.finish()
    assert list(ds.bars("AAA", "1d")["close"]) == [2, 6]
    assert ds.manifest.audit[0]["action"] == "drop_duplicate_timestamps" and ds.manifest.audit[0]["removed"] == 1


def test_quality_checks_flag_bad_bars():
    f = frame([(100, 101, 99, 100), (100, 99, 101, 100), (100, 100, 100, 160), (-1, 1, 1, 1)])
    codes = {i["check"] for i in check_bars(f, "AAA")}
    assert {"ohlc_inconsistent", "non_positive_price", "unexplained_price_jump"} <= codes


def test_fixture_passes_quality_with_no_errors(data_dir):
    rep = dataset_quality(Dataset(data_dir / "synthetic_v1"), "1d", max_symbols=60)
    assert rep["issue_counts"]["error"] == 0 and rep["integrity_failures"] == [] and rep["synthetic"]
    assert not any(i["check"] == "unexplained_price_jump" and i["symbol"] == "SYNIT" for i in rep["issues"])


def test_bhavcopy_layouts_are_detected_and_imported(tmp_path):
    files = [FIX / "cm_legacy_sample.csv", FIX / "cm_udiff_sample.csv", FIX / "fo_udiff_sample.csv"]
    assert [detect_layout(pd.read_csv(f)) for f in files] == ["cm_legacy", "udiff", "udiff"]
    ds = import_bhavcopies(files, tmp_path, "bhav", licence="TEST FILES — made-up values in the exchange layout")
    assert not ds.synthetic and len(ds.manifest.raw_files) == 3 and ds.verify() == []
    assert list(ds.bars("SAMPLECO", "1d")["close"]) == [101.5, 103.0]                 # legacy day + UDiFF day
    assert "SAMPLEBE" in ds.instruments and "SAMPLEGS" not in ds.instruments         # non-equity series dropped
    fut = ds.instruments["SAMPLEIDX25JAN30FUT"]
    assert fut.kind is Kind.FUT and fut.lot_size == 75 and fut.expiry == date(2025, 1, 30) and fut.underlying_kind == "INDEX"
    opt = ds.instruments["SAMPLEIDX25JAN3023500CE"]
    assert opt.strike == 23500 and opt.option_type == "CE" and ds.bars(opt.symbol, "1d")["oi"][0] == 1500
    stk = ds.instruments["SAMPLECO25JAN30100PE"]
    assert stk.settlement == "physical" and stk.underlying_kind == "EQ"
    assert ds.instruments["SAMPLEIDX"].kind is Kind.INDEX and ds.bars("SAMPLEIDX", "1d")["close"][0] == 23450.0
    assert any(a.get("action") == "dropped_untraded_rows" for a in ds.manifest.audit)
    with pytest.raises(ValueError, match="licence"):
        import_bhavcopies(files, tmp_path, "x", licence=" ")


def test_legacy_fo_bhavcopy_needs_lot_sizes(tmp_path):
    with pytest.raises(ValueError, match="lot sizes"):
        import_bhavcopies([FIX / "fo_legacy_sample.csv"], tmp_path, "a", licence="test")
    ds = import_bhavcopies([FIX / "fo_legacy_sample.csv"], tmp_path, "b", licence="test", lot_sizes={"SAMPLEIDX": 50})
    f = ds.instruments["SAMPLEIDX23MAR29FUT"]
    assert f.lot_size == 50 and ds.bars(f.symbol, "1d")["volume"][0] == 1200 * 50


def test_csv_import_converts_bar_start_to_bar_end(tmp_path):
    p = tmp_path / "v.csv"
    p.write_text("time,o,h,l,c,v\n2025-01-06 09:15:00,1,2,1,2,10\n2025-01-06 09:16:00,2,3,2,3,10\n")
    ds = import_csv({"AAA": p}, tmp_path, "csv", interval="1m", licence="test", source="unit test",
                    columns={"ts": "time", "open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"})
    assert list(ds.bars("AAA", "1m")["ts"]) == [pd.Timestamp("2025-01-06 09:16"), pd.Timestamp("2025-01-06 09:17")]


def test_kite_adapter_request_and_parsing_with_mock_transport(monkeypatch):
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    with pytest.raises(CredentialsMissing):
        KiteHistorical()
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"] = str(req.url), req.headers["authorization"]
        return httpx.Response(200, json={"status": "success", "data": {"candles": [
            ["2025-01-06T09:15:00+0530", 100, 101, 99, 100.5, 1200], ["2025-01-06T09:16:00+0530", 100.5, 102, 100, 101, 900]]}})
    k = KiteHistorical("key", "tok", transport=httpx.MockTransport(handler), min_interval_s=0)
    df = k.candles(256265, "1m", pd.Timestamp("2025-01-06 09:15"), pd.Timestamp("2025-01-06 09:17"))
    assert "/instruments/historical/256265/minute" in seen["url"] and seen["auth"] == "token key:tok"
    assert list(df["ts"]) == [pd.Timestamp("2025-01-06 09:16"), pd.Timestamp("2025-01-06 09:17")]   # bar END, IST
    daily = KiteHistorical.parse([["2025-01-06T00:00:00+0530", 1, 2, 1, 2, 5]], "1d")
    assert daily["ts"][0] == pd.Timestamp("2025-01-06 15:30")


def test_event_store_is_point_in_time(tmp_path):
    es = EventStore(tmp_path / "ev.sqlite")
    es.add(published_at="2025-01-06 16:05", category="earnings", headline="AAA Q3 results", symbols=["AAA"],
           source="unit test", licence="test")
    es.add(published_at="2025-01-07 08:30", category="macro", headline="CPI print", source="unit test", licence="test")
    assert es.as_of("2025-01-06 15:30") == []                                  # not knowable at the close
    assert [e["headline"] for e in es.as_of("2025-01-06 16:05")] == ["AAA Q3 results"]
    assert [e["headline"] for e in es.as_of("2025-01-08", symbol="AAA")] == ["AAA Q3 results"]
    with pytest.raises(ValueError):
        es.add(published_at="2025-01-06", category="news", headline="x", source="", licence="")


def test_market_structure_timeline_warns_about_impossible_windows():
    w = structure.window_warnings({"OPT"}, date(1999, 1, 1), date(2002, 1, 1))
    assert any("Index options launched" in x for x in w)
    assert any("2026-04-01" in x for x in structure.window_warnings({"FUT"}, date(2026, 1, 1), date(2026, 6, 1)))
    assert all({"date", "title", "effect", "verified", "source"} <= set(e) for e in structure.timeline())

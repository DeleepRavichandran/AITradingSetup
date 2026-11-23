"""Kite login, instrument parsing, chunked candle fetch and dataset build — against a mock transport."""
import hashlib
import json
from datetime import date

import httpx
import pandas as pd
import pytest

from nseql.data.adapters.kite import KiteAuthError, KiteHistorical, exchange_request_token, load_session, login_url, save_session
from nseql.data.adapters.kite_import import import_kite, resolve

INSTR = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
         "738561,2885,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.1,1,EQ,NSE,NSE\n"
         "408065,1594,INFY,INFOSYS,0,,0,0.05,1,EQ,NSE,NSE\n"
         "256265,1001,NIFTY 50,NIFTY 50,0,,0,0,1,EQ,INDICES,NSE\n")


def candles_for(start: str, n: int):
    d = pd.bdate_range(start, periods=n)
    return [[f"{x:%Y-%m-%d}T00:00:00+0530", 100 + i, 102 + i, 99 + i, 101 + i, 1000 + i] for i, x in enumerate(d)]


def handler(calls):
    def h(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if req.url.path == "/instruments/NSE":
            return httpx.Response(200, text=INSTR)
        if "/instruments/historical/" in req.url.path:
            tok = req.url.path.split("/")[3]
            if tok == "999":
                return httpx.Response(200, json={"status": "success", "data": {"candles": []}})
            return httpx.Response(200, json={"status": "success", "data": {"candles": candles_for("2026-01-01", 6)}})
        return httpx.Response(404)
    return h


def api(calls, **kw):
    return KiteHistorical("k", "t", transport=httpx.MockTransport(handler(calls)), min_interval_s=0, **kw)


def test_login_url_and_token_exchange_uses_documented_checksum():
    assert login_url("abc") == "https://kite.zerodha.com/connect/login?v=3&api_key=abc"
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen.update(dict(x.split("=") for x in req.content.decode().split("&")))
        return httpx.Response(200, json={"status": "success", "data": {"access_token": "AT", "user_id": "AB1234"}})
    d = exchange_request_token("key", "secret", "reqtok", transport=httpx.MockTransport(h))
    assert d["access_token"] == "AT" and seen["checksum"] == hashlib.sha256(b"keyreqtoksecret").hexdigest()
    with pytest.raises(KiteAuthError):
        exchange_request_token("key", "secret", "bad", transport=httpx.MockTransport(lambda r: httpx.Response(403, text="no")))


def test_session_file_is_owner_only(tmp_path):
    save_session(tmp_path, "k", "tok")
    assert load_session(tmp_path)["access_token"] == "tok" and (tmp_path / "kite_session.json").stat().st_mode & 0o077 == 0


def test_expired_token_gives_an_actionable_error():
    a = KiteHistorical("k", "t", transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "TokenException"})), min_interval_s=0)
    with pytest.raises(KiteAuthError, match="kite-login"):
        a.instruments()


def test_resolve_equities_and_indices_and_reports_unknowns():
    a = api([])
    nse = a.instruments("NSE")
    found, missing = resolve(nse, nse[nse["segment"] == "INDICES"], ["reliance", "INFY", "NIFTY50", "NOSUCH"])
    assert [(f["symbol"], f["kind"]) for f in found] == [("RELIANCE", "EQ"), ("INFY", "EQ"), ("NIFTY50", "INDEX")] and missing == ["NOSUCH"]
    assert found[0]["tick"] == 0.1


def test_long_ranges_are_fetched_in_chunks():
    calls = []
    a = api(calls)
    a.candles(738561, "1m", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-04-15"))      # 104 days, 60-day limit
    assert len([c for c in calls if "historical" in c]) == 2


def test_import_builds_a_hashed_real_labelled_dataset(tmp_path):
    ds, missing = import_kite(api([]), ["RELIANCE", "INFY", "NOSUCH"], date(2026, 1, 1), date(2026, 1, 9), tmp_path, "kite_2026",
                              licence="Kite Connect subscription, personal research", progress=lambda *_: None)
    assert not ds.synthetic and ds.manifest.licence.startswith("Kite") and not ds.manifest.redistribution_allowed
    assert set(ds.instruments) == {"RELIANCE", "INFY", "NIFTY50"} and missing == ["NOSUCH"]
    assert ds.instruments["RELIANCE"].tick_size == 0.1 and ds.instruments["NIFTY50"].kind.value == "INDEX"
    b = ds.bars("RELIANCE", "1d")
    assert list(b["ts"].dt.strftime("%H:%M")) == ["15:30"] * 6 and b["close"][0] == 101.0
    assert len(ds.manifest.raw_files) == 3 and ds.verify() == []
    assert any(a.get("action") == "import_summary" and "no delisted names" in a["caveats"] for a in ds.manifest.audit)
    assert any(r["name"].startswith("kite_RELIANCE_1d") for r in ds.manifest.raw_files)


def test_import_refuses_without_licence_or_matches(tmp_path):
    with pytest.raises(ValueError, match="licence"):
        import_kite(api([]), ["RELIANCE"], date(2026, 1, 1), date(2026, 1, 9), tmp_path, "x", licence=" ")
    with pytest.raises(ValueError, match="none of the requested"):
        import_kite(api([]), ["NOSUCH"], date(2026, 1, 1), date(2026, 1, 9), tmp_path, "y", licence="l", include_nifty=False)

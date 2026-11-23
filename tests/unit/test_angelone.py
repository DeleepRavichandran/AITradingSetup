"""Angel One SmartAPI adapter (mocked transport), the 1-minute poller, warm-up and the unattended API start."""
import json
import os
import stat
import time
from datetime import datetime

import httpx
import numpy as np
import pandas as pd
import pytest
from nseql.data.adapters import angelone as A
from nseql.screening.feeds import BrokerMinutePoller, warm_up
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream

CREDS = dict(api_key="key123", client_code="C123", pin="1234", totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")


def test_totp_matches_the_rfc_6238_test_vectors():
    s = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"                     # base32 of "12345678901234567890"
    assert A.totp(s, at=59, digits=8) == "94287082" and A.totp(s, at=1111111109, digits=8) == "07081804"
    assert A.totp(s, at=59) == "287082" and A.totp(s.lower(), at=59) == "287082"


class Server:
    """Minimal SmartAPI double: login, candles (validates headers and range), scripted failures."""

    def __init__(self):
        self.calls, self.fail = [], []

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        self.calls.append((req.url.path, body, dict(req.headers)))
        if req.url.path.endswith("loginByPassword"):
            assert req.headers["x-privatekey"] == "key123" and body["clientcode"] == "C123" and body["password"] == "1234"
            assert len(body["totp"]) == 6 and body["totp"].isdigit()
            return httpx.Response(200, json={"status": True, "data": {"jwtToken": f"jwt{len(self.calls)}", "feedToken": "ft"}})
        if req.url.path.endswith("getCandleData"):
            if self.fail:
                kind = self.fail.pop(0)
                if kind == "expired":
                    return httpx.Response(200, json={"status": False, "message": "Invalid Token", "errorcode": "AG8001"})
                if kind == "rate":
                    return httpx.Response(200, json={"status": False, "message": "Too many requests", "errorcode": "AB1021"})
                if kind == "rate403":                                    # what the real service returned on 2026-11-23
                    return httpx.Response(403, text="Access denied because of exceeding access rate")
                return httpx.Response(200, json={"status": False, "message": "Something wrong", "errorcode": "AB2001"})
            assert req.headers["authorization"].startswith("Bearer jwt") and req.headers["x-privatekey"] == "key123"
            f, t = pd.Timestamp(body["fromdate"]), pd.Timestamp(body["todate"])
            if body["interval"] == "ONE_DAY":
                days = pd.bdate_range(f.normalize(), t.normalize())
                data = [[f"{d:%Y-%m-%d}T00:00:00+05:30", 100.0, 101.0, 99.0, 100.5, 1000] for d in days]
            else:
                mins = pd.date_range(f, t, freq="1min")
                data = [[f"{m:%Y-%m-%dT%H:%M:%S}+05:30", 100.0, 100.2, 99.9, 100.1, 50] for m in mins
                        if (9 * 60 + 15) <= m.hour * 60 + m.minute < 15 * 60 + 30 and m.weekday() < 5]
            return httpx.Response(200, json={"status": True, "data": data})
        return httpx.Response(404, json={"status": False})


def _api(srv, tmp_path, **kw):
    sleeps = []
    api = A.AngelHistorical(**CREDS, session_dir=tmp_path, transport=httpx.MockTransport(srv.handler), min_interval_s=0,
                            sleep=sleeps.append, **kw)
    return api, sleeps


def test_login_candles_chunking_and_bar_stamping(tmp_path):
    srv = Server()
    api, _ = _api(srv, tmp_path)
    df = api.candles("2885", "1m", datetime(2026, 9, 1, 9, 15), datetime(2026, 10, 15, 15, 30))
    candle_calls = [c for c in srv.calls if c[0].endswith("getCandleData")]
    assert len(candle_calls) == 2                                           # 1-minute history is fetched 30 days at a time
    assert candle_calls[0][1]["interval"] == "ONE_MINUTE" and candle_calls[0][1]["symboltoken"] == "2885"
    assert df["ts"].iloc[0] == pd.Timestamp("2026-09-01 09:16") and df["ts"].is_unique   # stamped at bar END
    d = api.candles("2885", "1d", datetime(2026, 11, 2), datetime(2026, 11, 6))
    assert list(d["ts"].dt.strftime("%H:%M").unique()) == ["15:30"] and len(d) == 5
    sess = tmp_path / "angel_session.json"
    assert sess.exists() and stat.S_IMODE(os.stat(sess).st_mode) == 0o600
    srv2 = Server()                                                         # a new process reuses the saved session: no login
    api2, _ = _api(srv2, tmp_path)
    api2.candles("2885", "1d", datetime(2026, 11, 2), datetime(2026, 11, 6))
    assert not any(c[0].endswith("loginByPassword") for c in srv2.calls)


def test_expired_session_relogs_once_and_rate_limits_back_off(tmp_path):
    srv = Server()
    api, sleeps = _api(srv, tmp_path)
    srv.fail = ["expired", "rate", "rate"]
    df = api.candles("2885", "1d", datetime(2026, 11, 2), datetime(2026, 11, 6))
    logins = [c for c in srv.calls if c[0].endswith("loginByPassword")]
    assert len(df) == 5 and len(logins) == 2 and sleeps[-2:] == [1, 2]
    srv.fail = ["other"]
    with pytest.raises(A.AngelError, match="Something wrong"):
        api.candles("2885", "1d", datetime(2026, 11, 2), datetime(2026, 11, 6))


def test_missing_credentials_are_named():
    with pytest.raises(A.AngelAuthError, match="ANGEL_PIN"):
        A.AngelHistorical(api_key="k", client_code="c", pin="", totp_secret="GEZDGNBV")


MASTER = pd.DataFrame([
    {"token": "2885", "symbol": "RELIANCE-EQ", "name": "RELIANCE", "exch_seg": "NSE", "instrumenttype": "", "tick_size": "10.000000"},
    {"token": "3045", "symbol": "SBIN-EQ", "name": "SBIN", "exch_seg": "NSE", "instrumenttype": "", "tick_size": "5.000000"},
    {"token": "3045", "symbol": "USDINR26DECFUT", "name": "USDINR", "exch_seg": "CDS", "instrumenttype": "FUTCUR", "tick_size": "0.25"},
    {"token": "99926000", "symbol": "Nifty 50", "name": "NIFTY", "exch_seg": "NSE", "instrumenttype": "AMXIDX", "tick_size": "0"},
    {"token": "50001", "symbol": "RELIANCE26DECFUT", "name": "RELIANCE", "exch_seg": "NFO", "instrumenttype": "FUTSTK", "tick_size": "10"},
])


def test_resolve_symbols_and_indices():
    found, missing = A.resolve(MASTER, ["reliance", "SBIN", "NIFTY 50", "NOPE"])
    by = {f["symbol"]: f for f in found}
    assert by["RELIANCE"]["token"] == "2885" and by["RELIANCE"]["tick"] == 0.10 and by["SBIN"]["token"] == "3045"
    assert by["NIFTY50"]["kind"] == "INDEX" and by["NIFTY50"]["token"] == "99926000" and missing == ["NOPE"]


class FakeApi:
    """candles() over a fixed synthetic minute stream; can fail for one symbol."""

    def __init__(self, stream, fail_symbol_token=None):
        rows = [dict(b, token=b["symbol"]) for _, bars in stream for b in bars]
        self.df = pd.DataFrame(rows)
        self.fail = fail_symbol_token
        self.calls = 0

    def candles(self, token, interval, start, end, **kw):
        self.calls += 1
        if token == self.fail:
            raise ConnectionError("timeout")
        d = self.df[(self.df["token"] == token) & (self.df["ts"] > pd.Timestamp(start)) & (self.df["ts"] <= pd.Timestamp(end))]
        return d[["ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def test_minute_poller_returns_each_completed_bar_once_and_isolates_failures():
    stream = minute_stream(3, ["2026-11-16"], seed=1, missing=0.0)
    api = FakeApi(stream, fail_symbol_token="SYN002")
    toks = [{"symbol": f"SYN00{i}", "token": f"SYN00{i}"} for i in range(3)]
    p = BrokerMinutePoller(toks, lambda: api)
    first = p.fetch(pd.Timestamp("2026-11-16 09:20:30"))
    again = p.fetch(pd.Timestamp("2026-11-16 09:20:40"))
    later = p.fetch(pd.Timestamp("2026-11-16 09:22:05"))
    assert {(b["symbol"], b["ts"]) for b in first} == {(s, pd.Timestamp(f"2026-11-16 09:{m}")) for s in ("SYN000", "SYN001") for m in (16, 17, 18, 19, 20)}
    assert again == [] and len(later) == 4 and "SYN002" in p.errors


def test_warm_up_never_trades_the_past_and_live_minutes_do(tmp_path):
    days = ["2026-11-16", "2026-11-17", "2026-11-18"]
    stream = minute_stream(20, days, seed=3, missing=0.0)
    api = FakeApi(stream)
    toks = [{"symbol": f"SYN{i:03d}", "token": f"SYN{i:03d}"} for i in range(20)]
    live_from = pd.Timestamp("2026-11-18 09:16")
    sch = MinuteScheduler(tmp_path, [t["symbol"] for t in toks], None, feed=BrokerMinutePoller(toks, lambda: api), live_from=live_from)
    rep = warm_up(sch, api, toks, days=3, now=pd.Timestamp("2026-11-17 15:30"))
    assert rep["minutes"] == 750 and sch.watermark == "2026-11-17T15:30:00"
    db = sch.store.db
    assert db.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] > 0               # it screened the past, it did not trade it
    assert dict(db.execute("SELECT mode, COUNT(*) FROM cycles GROUP BY mode").fetchall()) == {"warmup": 750}
    for ts, _ in stream[750:]:
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    assert db.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] > 0
    assert db.execute("SELECT MIN(decided_at) FROM paper_orders").fetchone()[0] >= "2026-11-18T09:16:00"


def test_unattended_api_start_with_angel_one(data_dir, tmp_path, monkeypatch):
    """With NSEQL_LIVE_SCREENING=angelone the API logs in, resolves the watchlist, warms up and starts the minute
    loop by itself, then downloads daily history; nothing for the operator to do."""
    from fastapi.testclient import TestClient
    from nseql import config
    stream = minute_stream(5, ["2026-11-16"], seed=2, missing=0.0)

    class FakeAngel(FakeApi):
        def __init__(self, **kw):
            super().__init__(stream)
            self.client_code = "C123"

        def ensure_login(self):
            pass

        def instruments(self, cache_dir=None):
            return pd.DataFrame([{"token": f"SYN{i:03d}", "symbol": f"SYN{i:03d}-EQ", "name": f"SYN{i:03d}", "exch_seg": "NSE",
                                  "instrumenttype": "", "tick_size": "5"} for i in range(5)])

        last_raw: list = []

        def candles(self, token, interval, start, end, **kw):
            if interval == "1d":
                d = pd.bdate_range(pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize())
                return pd.DataFrame({"ts": d + pd.Timedelta(hours=15, minutes=30), "open": 100.0, "high": 101.0, "low": 99.0,
                                     "close": 100.5, "volume": 1000, "oi": 0})
            return super().candles(token, interval, start, end)

    monkeypatch.setattr(A, "AngelHistorical", FakeAngel)
    (tmp_path / "exp").mkdir()
    (tmp_path / "exp" / "watchlist.txt").write_text("# test\nSYN000\nSYN001\nSYN002\nSYN003\nSYN004\nNOTREAL\n")
    env = {"NSEQL_DATA_DIR": str(tmp_path / "data"), "NSEQL_EXPERIMENTS_DIR": str(tmp_path / "exp"), "NSEQL_AUTO_FIXTURE": "false",
           "NSEQL_API_TOKEN": "angel-live-test-operator-token-1", "NSEQL_LIVE_SCREENING": "angelone",
           **{k: "x" for k in A.ENV}}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NSEQL_SCHEDULER", raising=False)
    config.get_settings.cache_clear()
    from nseql.api.main import create_app
    c = TestClient(create_app(), headers={"Authorization": "Bearer angel-live-test-operator-token-1"})
    st = {}
    for _ in range(100):
        st = c.get("/api/screening/status").json()
        if (st.get("live") or {}).get("history", "").startswith("dataset") or (st.get("live") or {}).get("state") == "error":
            break
        time.sleep(0.2)
    assert st["live"]["state"] == "running", st["live"].get("error")
    assert st["started"] and st["mode"] == "live" and st["symbols"] == 5 and st["live"]["not_found"] == ["NOTREAL", "NIFTY 50"]
    assert st["feed"].startswith("Angel One") and st["running"] is True and st["live_trading"] is False
    assert st["live"]["history"].startswith("dataset angel_daily")
    assert st["portfolio"]["equity_inr"] == 1_000_000 and st["live"]["account"] == "live_angelone_r2"
    # a different starting capital is a separate paper account, starting from scratch.
    # A slow login opens the race seen on 2026-11-23: dashboard requests during start-up must not reopen the
    # previous account read-only and hide the live bot.
    monkeypatch.setattr(FakeAngel, "ensure_login", lambda self: time.sleep(1.5))
    monkeypatch.setenv("NSEQL_PAPER_CAPITAL", "100000")
    config.get_settings.cache_clear()
    c2 = TestClient(create_app(), headers={"Authorization": "Bearer angel-live-test-operator-token-1"})
    early = c2.get("/api/today").json()
    assert early["started"] is False and early["data_source"]["kind"] == "none"
    for _ in range(100):
        st2 = c2.get("/api/screening/status").json()
        if (st2.get("live") or {}).get("state") in ("running", "error"):
            break
        time.sleep(0.2)
    assert st2["live"]["state"] == "running", st2["live"].get("error")
    assert st2["live"]["account"] == "live_angelone_100000_r2" and st2["portfolio"]["rules"]["capital"] == 100_000
    assert st2["feed"].startswith("Angel One") and st2["running"] is True                    # the live bot, not a read-only copy
    assert st2["portfolio"]["cash_inr"] == 100_000 and st2["paper_orders"] == 0             # fresh: no inherited cash, positions or orders
    assert (tmp_path / "exp" / "screening" / "live_angelone_r2").exists()                    # the old account is kept, untouched
    monkeypatch.setenv("NSEQL_PAPER_CAPITAL", "5000")
    config.get_settings.cache_clear()
    c3 = TestClient(create_app(), headers={"Authorization": "Bearer angel-live-test-operator-token-1"})
    for _ in range(50):
        st3 = c3.get("/api/screening/status").json()
        if (st3.get("live") or {}).get("state") == "error":
            break
        time.sleep(0.2)
    assert "at least 10000" in st3["live"]["error"]
    config.get_settings.cache_clear()
    assert np.isfinite(1.0)


def test_access_rate_403_is_a_rate_limit_not_a_login_failure(tmp_path):
    srv = Server()
    api, sleeps = _api(srv, tmp_path)
    api.login()
    logins = sum(1 for c in srv.calls if c[0].endswith("loginByPassword"))
    srv.fail = ["rate403", "rate403"]
    df = api.candles("2885", "1d", datetime(2026, 11, 16), datetime(2026, 11, 20))
    assert len(df) == 5 and sleeps[-2:] == [1, 2]                         # backed off twice, then succeeded
    assert sum(1 for c in srv.calls if c[0].endswith("loginByPassword")) == logins   # no re-login storm


def test_pre_open_bars_are_dropped_and_never_block_the_session(tmp_path):
    stream = minute_stream(2, ["2026-11-16"], seed=3, missing=0.0)
    sch = MinuteScheduler(tmp_path / "s", ["SYN000", "SYN001"])
    pre = [{"symbol": "SYN000", "ts": pd.Timestamp("2026-11-16 09:09"), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 5}]
    assert sch.ingest(pre)["outside_session"] == 1
    sch.store.add_events(pre, processed_upto=None, source="older version")   # as stored by the version that ran this morning
    for ts, bars in stream[:5]:
        sch.ingest(bars)
        c = sch.run_cycle(ts + pd.Timedelta(seconds=6))
        assert "error" not in c
    assert sch.watermark == "2026-11-16T09:20:00" and sch.store.counts()["cycles"] == 5

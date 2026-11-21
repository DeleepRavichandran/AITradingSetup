"""`nseql angel-verify` (against a mocked SmartAPI), the poller's capacity / rotation / staleness, and the
rule that only a passed real-service verification turns live data "verified"."""
import json

import httpx
import pandas as pd
import pytest
from nseql.data.adapters import angelone as A
from nseql.data.adapters.angel_verify import mask, verify
from nseql.screening.feeds import BrokerMinutePoller
from nseql.screening.provenance import verification_status

CREDS = {"ANGEL_API_KEY": "apikey-SECRET-1", "ANGEL_CLIENT_CODE": "C1234567", "ANGEL_PIN": "4321",
         "ANGEL_TOTP_SECRET": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"}
MASTER = [{"token": "2885", "symbol": "RELIANCE-EQ", "name": "RELIANCE", "exch_seg": "NSE", "instrumenttype": "", "tick_size": "5.0"},
          {"token": "99926000", "symbol": "Nifty 50", "name": "NIFTY", "exch_seg": "NSE", "instrumenttype": "AMXIDX", "tick_size": "5.0"}]


class Smart:
    def __init__(self):
        self.logins = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "margincalculator.angelbroking.com":
            return httpx.Response(200, json=MASTER)
        body = json.loads(req.content or b"{}")
        if req.url.path.endswith("loginByPassword"):
            self.logins += 1
            return httpx.Response(200, json={"status": True, "data": {"jwtToken": f"eyJwt-SECRET-{self.logins}", "feedToken": "feed-SECRET"}})
        if req.headers.get("authorization") == "Bearer corrupted-token":
            return httpx.Response(200, json={"status": False, "message": "Invalid Token", "errorcode": "AG8001"})
        f, t = pd.Timestamp(body["fromdate"]), pd.Timestamp(body["todate"])
        if body["interval"] == "ONE_DAY":
            data = [[f"{d:%Y-%m-%d}T00:00:00+05:30", 100, 101, 99, 100.5, 1000] for d in pd.bdate_range(f.normalize(), t.normalize())]
        else:
            data = [[f"{m:%Y-%m-%dT%H:%M:%S}+05:30", 100.0, 100.2, 99.9, 100.1, 50] for m in pd.date_range(f.ceil("1min"), t.floor("1min"), freq="1min")
                    if 9 * 60 + 15 <= m.hour * 60 + m.minute < 15 * 60 + 30 and m.weekday() < 5]
        return httpx.Response(200, json={"status": True, "data": data})


@pytest.fixture
def env(monkeypatch):
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)


def _run(tmp_path, now):
    srv = Smart()
    make = lambda **kw: A.AngelHistorical(session_dir=tmp_path, transport=httpx.MockTransport(srv.handler),
                                          **{"min_interval_s": 0, "sleep": lambda s: None, **kw})
    return verify(tmp_path, now=pd.Timestamp(now), symbol="RELIANCE", make_api=make, sleep=lambda s: None, revision_wait_s=0, progress=lambda m: None), srv


def test_outside_market_hours_the_report_is_written_but_not_passed(env, tmp_path):
    rep, _ = _run(tmp_path, "2026-11-21 11:00")                  # a Saturday
    by = {c["name"]: c["status"] for c in rep["checks"]}
    assert by["completed_minute"] == "skipped" and rep["passed"] is False and "completed_minute" in rep["detail"]
    assert all(by[k] == "passed" for k in ("credentials", "login", "instruments", "candle_format", "session_recovery", "rate_limit",
                                           "network_failure", "stale_detection", "no_secrets"))
    assert verification_status(tmp_path)["verified"] is False


def test_market_hours_run_passes_and_marks_live_data_verified_without_writing_secrets(env, tmp_path):
    rep, srv = _run(tmp_path, "2026-11-23 11:00:20")             # a Monday, market open
    assert rep["passed"] is True, rep["detail"]
    cm = next(c for c in rep["checks"] if c["name"] == "completed_minute")
    assert cm["in_progress_returned"] is True and "dropped by end-stamping: True" in cm["detail"]
    assert srv.logins >= 2                                          # the corrupted token forced exactly the re-login path
    text = (tmp_path / "angel_verification.json").read_text()
    for secret in ("apikey-SECRET-1", "4321", CREDS["ANGEL_TOTP_SECRET"], "eyJwt-SECRET", "feed-SECRET", "C1234567"):
        assert secret not in text
    assert mask("C1234567") in text and verification_status(tmp_path)["verified"] is True


def test_missing_credentials_fail_cleanly(monkeypatch, tmp_path):
    for k in CREDS:
        monkeypatch.delenv(k, raising=False)
    rep = verify(tmp_path, now=pd.Timestamp("2026-11-23 11:00"), progress=lambda m: None)
    assert rep["passed"] is False and rep["checks"][0]["status"] == "failed" and "ANGEL_API_KEY" in rep["checks"][0]["detail"]


class Api:
    def __init__(self):
        self.calls = []

    def candles(self, token, interval, start, end):
        self.calls.append(token)
        end = pd.Timestamp(end).floor("1min")
        return pd.DataFrame({"ts": [end - pd.Timedelta(minutes=1), end + pd.Timedelta(minutes=1)], "open": [1, 1], "high": [1, 1],
                             "low": [1, 1], "close": [1, 1], "volume": [10, 10]})


def test_poller_rotates_beyond_capacity_and_reports_it():
    api = Api()
    toks = [{"symbol": f"S{i:03d}", "token": f"S{i:03d}"} for i in range(300)]
    p = BrokerMinutePoller(toks, lambda: api)                     # 0.4 s spacing, 50 s budget -> 125 a minute
    assert p.capacity == 125
    seen = set()
    for k in range(3):
        bars = p.fetch(pd.Timestamp("2026-11-23 10:00:05") + pd.Timedelta(minutes=k))
        assert len(api.calls) == 125 * (k + 1) and all(b["ts"] <= pd.Timestamp("2026-11-23 10:00:05") + pd.Timedelta(minutes=k) for b in bars)
        seen |= {b["symbol"] for b in bars}
    assert seen == {t["symbol"] for t in toks}                   # everyone polled within ceil(300 / 125) = 3 minutes
    h = p.health(pd.Timestamp("2026-11-23 10:02:10"), market_open=True)
    assert h["degraded"] and h["rotation_minutes"] == 3 and h["last_cycle"]["requests"] == 125


def test_poller_flags_stale_symbols_only_while_the_market_is_open():
    api = Api()
    p = BrokerMinutePoller([{"symbol": "A", "token": "A"}, {"symbol": "B", "token": "B"}], lambda: api)
    p.fetch(pd.Timestamp("2026-11-23 10:00:05"))
    p.last["B"] = pd.Timestamp("2026-11-23 09:55")
    assert p.health(pd.Timestamp("2026-11-23 10:00:30"), market_open=True)["stale_symbols"] == ["B"]
    assert p.health(pd.Timestamp("2026-11-23 10:00:30"), market_open=False)["stale_symbols"] == []
    assert not p.health(pd.Timestamp("2026-11-23 10:00:30"), market_open=True)["degraded"]

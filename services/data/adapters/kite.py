"""Zerodha Kite Connect historical-candle adapter.

Requires a Kite Connect subscription with the historical-data add-on and a valid daily
access token (KITE_API_KEY / KITE_ACCESS_TOKEN in the environment — never in code or logs).

STATUS: UNVERIFIED. Request construction and response parsing are unit-tested
against a mocked transport using the documented response shape. No live call has been made
from this environment, so behaviour against the real API is unverified.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import time
from datetime import datetime, timedelta

import httpx
import pandas as pd

from nseql.instruments.calendar import INTERVAL_MINUTES, is_intraday

BASE_URL = "https://api.kite.trade"
KITE_INTERVAL = {"1m": "minute", "3m": "3minute", "5m": "5minute", "15m": "15minute", "30m": "30minute",
                 "1h": "60minute", "1d": "day"}
MAX_DAYS = {"1m": 60, "3m": 100, "5m": 100, "15m": 200, "30m": 200, "1h": 400, "1d": 2000}


class CredentialsMissing(RuntimeError):
    pass


class KiteAuthError(PermissionError):
    """The access token is missing, expired or rejected. Kite tokens expire every day (~06:00 IST)."""


LOGIN_URL = "https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"


def login_url(api_key: str) -> str:
    return LOGIN_URL.format(api_key=api_key)


def exchange_request_token(api_key: str, api_secret: str, request_token: str, *,
                           transport: httpx.BaseTransport | None = None) -> dict:
    """Step 2 of the daily login: swap the request_token (from the redirect URL) for an access token."""
    checksum = hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
    with httpx.Client(base_url=BASE_URL, transport=transport, timeout=30.0, headers={"X-Kite-Version": "3"}) as c:
        r = c.post("/session/token", data={"api_key": api_key, "request_token": request_token, "checksum": checksum})
    if r.status_code != 200:
        raise KiteAuthError(f"token exchange failed (HTTP {r.status_code}): {r.text[:200]}")
    return r.json()["data"]


def token_file(experiments_dir: str | os.PathLike) -> "os.PathLike":
    from pathlib import Path
    return Path(experiments_dir) / "kite_session.json"


def save_session(experiments_dir, api_key: str, access_token: str) -> None:
    """The token is a secret: stored in the git-ignored experiments folder, readable only by the owner."""
    from datetime import datetime
    f = token_file(experiments_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"api_key": api_key, "access_token": access_token, "saved_at": datetime.now().isoformat(timespec="seconds")}))
    f.chmod(stat.S_IRUSR | stat.S_IWUSR)


def load_session(experiments_dir) -> dict | None:
    f = token_file(experiments_dir)
    return json.loads(f.read_text()) if f.exists() else None


class KiteHistorical:
    def __init__(self, api_key: str | None = None, access_token: str | None = None, *,
                 transport: httpx.BaseTransport | None = None, min_interval_s: float = 0.35):
        self.api_key = api_key or os.environ.get("KITE_API_KEY")
        self.access_token = access_token or os.environ.get("KITE_ACCESS_TOKEN")
        if not (self.api_key and self.access_token):
            raise CredentialsMissing("KITE_API_KEY and KITE_ACCESS_TOKEN must be set (see .env.example)")
        self._client = httpx.Client(base_url=BASE_URL, transport=transport, timeout=30.0, headers={
            "X-Kite-Version": "3", "Authorization": f"token {self.api_key}:{self.access_token}"})
        self._min_interval = min_interval_s      # stay under the documented 3 req/s historical limit
        self._last = 0.0

    def _get(self, path: str, **params) -> httpx.Response:
        wait = self._min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        r = self._client.get(path, params=params)
        self._last = time.monotonic()
        if r.status_code in (401, 403):
            raise KiteAuthError("Kite rejected the access token (it expires daily). Run `nseql kite-login` again.")
        r.raise_for_status()
        return r

    def instruments(self, exchange: str = "NSE") -> pd.DataFrame:
        """Instrument master for one exchange (CSV). Lists CURRENT instruments only: expired contracts are not included."""
        df = pd.read_csv(io.StringIO(self._get(f"/instruments/{exchange}").text))
        for c in ("expiry",):
            if c in df:
                df[c] = pd.to_datetime(df[c], errors="coerce").dt.date
        return df

    def candles(self, instrument_token: int, interval: str, start: datetime, end: datetime, oi: bool = False,
                continuous: bool = False) -> pd.DataFrame:
        """Bars in the platform convention (exchange-local, tz-naive, stamped at bar END).

        continuous=True (daily futures only) asks Kite to stitch expired contracts behind the current one,
        which is the only way to get futures history: the instrument master lists live contracts only."""
        if continuous and interval != "1d":
            raise ValueError("Kite serves continuous futures data for daily candles only")
        out = []
        step = timedelta(days=MAX_DAYS[interval])
        cur = start
        while cur <= end:
            to = min(cur + step, end)
            r = self._get(f"/instruments/historical/{instrument_token}/{KITE_INTERVAL[interval]}",
                          **{"from": cur.strftime("%Y-%m-%d %H:%M:%S"), "to": to.strftime("%Y-%m-%d %H:%M:%S"), "oi": int(oi),
                             **({"continuous": 1} if continuous else {})})
            body = r.json()
            if body.get("status") != "success":
                raise RuntimeError(f"kite error: {body.get('message', body)}")
            out += body["data"]["candles"]
            cur = to + timedelta(seconds=1)
        self.last_raw = out
        return self.parse(out, interval)

    @staticmethod
    def parse(candles: list[list], interval: str) -> pd.DataFrame:
        cols = ["ts", "open", "high", "low", "close", "volume", "oi"]
        df = pd.DataFrame([c + [0] * (7 - len(c)) for c in candles], columns=cols)
        if df.empty:
            return df
        ts = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        df["ts"] = (ts + pd.Timedelta(minutes=INTERVAL_MINUTES[interval]) if is_intraday(interval)
                    else ts.dt.normalize() + pd.Timedelta(hours=15, minutes=30))
        return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)

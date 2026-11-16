"""Zerodha Kite Connect historical-candle adapter.

Requires a Kite Connect subscription with the historical-data add-on and a valid daily
access token (KITE_API_KEY / KITE_ACCESS_TOKEN in the environment — never in code or logs).

STATUS: BLOCKED / UNVERIFIED. Request construction and response parsing are unit-tested
against a mocked transport using the documented response shape. No live call has been made
from this environment, so behaviour against the real API is unverified.
"""
from __future__ import annotations

import os
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

    def candles(self, instrument_token: int, interval: str, start: datetime, end: datetime, oi: bool = False) -> pd.DataFrame:
        """Bars in the platform convention (exchange-local, tz-naive, stamped at bar END)."""
        out = []
        step = timedelta(days=MAX_DAYS[interval])
        cur = start
        while cur <= end:
            to = min(cur + step, end)
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            r = self._client.get(f"/instruments/historical/{instrument_token}/{KITE_INTERVAL[interval]}",
                                 params={"from": cur.strftime("%Y-%m-%d %H:%M:%S"), "to": to.strftime("%Y-%m-%d %H:%M:%S"),
                                         "oi": int(oi)})
            self._last = time.monotonic()
            r.raise_for_status()
            body = r.json()
            if body.get("status") != "success":
                raise RuntimeError(f"kite error: {body.get('message', body)}")
            out += body["data"]["candles"]
            cur = to + timedelta(seconds=1)
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

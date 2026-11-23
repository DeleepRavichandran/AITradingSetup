"""Angel One SmartAPI: automatic login, instrument master and historical candles.

Why Angel One: its API is free for Angel One clients and its login can be completed by software with a
TOTP code (RFC 6238) generated from the secret shown once when SmartAPI two-factor authentication is set
up. So unlike Kite and ICICI Breeze, no person has to log in each morning. The session lasts until about
05:00 the next day and is renewed automatically.

Credentials come only from the environment (.env), never from code or logs:
    ANGEL_API_KEY       the SmartAPI app's API key
    ANGEL_CLIENT_CODE   the Angel One client id
    ANGEL_PIN           the 4-digit MPIN used to log in
    ANGEL_TOTP_SECRET   the TOTP secret text shown under the QR code (treat it like a password: with it and
                        the MPIN, anyone can log in to the account)

`AngelHistorical.candles(token, interval, start, end)` returns the same frame as the Kite adapter, so the
daily paper feed and the importers work with either broker.

Limits reported by Angel One's forum (verify against the official docs): per request at most 30 days of
1-minute, 90 of 5-minute, 180 of 15-minute, 365 of hourly and 2000 of daily candles; about 3 historical
requests per second ("AB1021 too many requests" is retried with back-off); no history for expired F&O
contracts; current listings only.

STATUS: UNVERIFIED. Request construction, TOTP, chunking, retries and parsing are tested against a mocked
transport using the documented response shape. No call to the real service has been made from here.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
import struct
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

from nseql.instruments.calendar import INTERVAL_MINUTES, is_intraday

BASE_URL = os.environ.get("ANGEL_BASE_URL", "https://apiconnect.angelone.in")
SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
INTERVAL = {"1m": "ONE_MINUTE", "3m": "THREE_MINUTE", "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE", "30m": "THIRTY_MINUTE",
            "1h": "ONE_HOUR", "1d": "ONE_DAY"}
MAX_DAYS = {"1m": 30, "3m": 60, "5m": 90, "15m": 180, "30m": 180, "1h": 365, "1d": 2000}
ENV = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET")


class AngelError(RuntimeError):
    pass


class AngelAuthError(AngelError, PermissionError):
    pass


def totp(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 time-based one-time password (HMAC-SHA1), as authenticator apps produce."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((time.time() if at is None else at) // step)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def credentials_present(env=os.environ) -> bool:
    return all(env.get(k) for k in ENV)


def missing_credentials(env=os.environ) -> list[str]:
    return [k for k in ENV if not env.get(k)]


class AngelHistorical:
    def __init__(self, *, api_key: str | None = None, client_code: str | None = None, pin: str | None = None,
                 totp_secret: str | None = None, session_dir: Path | None = None, transport: httpx.BaseTransport | None = None,
                 min_interval_s: float = 0.4, max_retries: int = 4, clock=time.time, sleep=time.sleep):
        e = os.environ
        self.api_key = api_key or e.get("ANGEL_API_KEY")
        self.client_code = client_code or e.get("ANGEL_CLIENT_CODE")
        self.pin = pin or e.get("ANGEL_PIN")
        self.totp_secret = totp_secret or e.get("ANGEL_TOTP_SECRET")
        miss = [k for k, v in zip(ENV, (self.api_key, self.client_code, self.pin, self.totp_secret)) if not v]
        if miss:
            raise AngelAuthError(f"missing in .env: {', '.join(miss)}")
        self.session_file = Path(session_dir) / "angel_session.json" if session_dir else None
        self._client = httpx.Client(base_url=BASE_URL, transport=transport, timeout=30.0)
        self._transport = transport
        self._min, self._retries, self._clock, self._sleep = min_interval_s, max_retries, clock, sleep
        self._last = 0.0
        self.jwt: str | None = None
        self.feed_token: str | None = None
        self.last_raw: list = []
        self._load_session()

    # ------------------------------------------------------------------ session
    def _headers(self, auth: bool) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB",
             "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1", "X-MACAddress": "00:00:00:00:00:00",
             "X-PrivateKey": self.api_key}
        if auth:
            h["Authorization"] = f"Bearer {self.jwt}"
        return h

    def _load_session(self) -> None:
        if self.session_file and self.session_file.exists():
            try:
                d = json.loads(self.session_file.read_text())
                if d.get("client_code") == self.client_code and d.get("expires", 0) > self._clock():
                    self.jwt, self.feed_token = d["jwt"], d.get("feed_token")
            except (ValueError, KeyError):
                pass

    def _save_session(self) -> None:
        if not self.session_file:
            return
        now = datetime.fromtimestamp(self._clock())
        expires = (now + timedelta(days=1)).replace(hour=5, minute=0, second=0, microsecond=0)
        if now.hour < 5:
            expires -= timedelta(days=1)
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text(json.dumps({"client_code": self.client_code, "jwt": self.jwt, "feed_token": self.feed_token,
                                                 "expires": expires.timestamp(), "logged_in_at": now.isoformat(timespec="seconds")}))
        self.session_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def login(self) -> dict:
        body = {"clientcode": self.client_code, "password": self.pin, "totp": totp(self.totp_secret, self._clock())}
        r = self._client.post("/rest/auth/angelbroking/user/v1/loginByPassword", headers=self._headers(False), json=body)
        d = self._json(r)
        if not d.get("status") or not (d.get("data") or {}).get("jwtToken"):
            raise AngelAuthError(f"Angel One login failed: {d.get('message') or r.text[:200]} ({d.get('errorcode') or d.get('errorCode') or r.status_code})")
        self.jwt, self.feed_token = d["data"]["jwtToken"], d["data"].get("feedToken")
        self._save_session()
        return {"client_code": self.client_code, "logged_in": True}

    def ensure_login(self) -> None:
        if not self.jwt:
            self.login()

    # ------------------------------------------------------------------ requests
    @staticmethod
    def _json(r: httpx.Response) -> dict:
        try:
            return r.json()
        except ValueError:
            return {"status": False, "message": r.text[:200], "errorcode": str(r.status_code)}

    def _post(self, path: str, body: dict) -> dict:
        self.ensure_login()
        relogged, busy = False, 0
        for _ in range(self._retries + 2):
            wait = self._min - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
            r = self._client.post(path, headers=self._headers(True), json=body)
            self._last = self._clock()
            d = self._json(r)
            code = str(d.get("errorcode") or d.get("errorCode") or "")
            rate_limited = r.status_code == 429 or code == "AB1021" or "access rate" in str(d.get("message") or r.text).lower()
            if not rate_limited and (r.status_code in (401, 403) or code in ("AG8001", "AG8002", "AG8003", "AB1010")):
                if relogged:
                    raise AngelAuthError(f"Angel One rejected the session: {d.get('message')} ({code or r.status_code})")
                self.jwt = None
                self.login()
                relogged = True
                continue
            if rate_limited or r.status_code >= 500:
                if busy >= self._retries:
                    break
                self._sleep(min(2 ** busy, 16))
                busy += 1
                continue
            if not d.get("status"):
                raise AngelError(f"Angel One error: {d.get('message')} ({code or r.status_code})")
            return d
        raise AngelError(f"Angel One kept refusing {path} after {self._retries} retries (rate limit or server error)")

    def candles(self, token, interval: str, start: datetime, end: datetime, oi: bool = False, exchange: str = "NSE") -> pd.DataFrame:
        """Bars in the platform convention (exchange-local, tz-naive, stamped at bar END; daily at 15:30)."""
        if interval not in INTERVAL:
            raise ValueError(f"unsupported interval {interval}; use one of {sorted(INTERVAL)}")
        out, step, cur = [], timedelta(days=MAX_DAYS[interval]), pd.Timestamp(start).to_pydatetime()
        end = pd.Timestamp(end).to_pydatetime()
        while cur <= end:
            to = min(cur + step, end)
            d = self._post("/rest/secure/angelbroking/historical/v1/getCandleData",
                           {"exchange": exchange, "symboltoken": str(token), "interval": INTERVAL[interval],
                            "fromdate": cur.strftime("%Y-%m-%d %H:%M"), "todate": to.strftime("%Y-%m-%d %H:%M")})
            out += d.get("data") or []
            cur = to + timedelta(minutes=1)
        self.last_raw = out
        return self.parse(out, interval)

    @staticmethod
    def parse(candles: list[list], interval: str) -> pd.DataFrame:
        cols = ["ts", "open", "high", "low", "close", "volume"]
        df = pd.DataFrame([c[:6] for c in candles], columns=cols)
        if df.empty:
            return df.assign(oi=pd.Series(dtype="int64"))
        ts = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        df["ts"] = (ts + pd.Timedelta(minutes=INTERVAL_MINUTES[interval]) if is_intraday(interval)
                    else ts.dt.normalize() + pd.Timedelta(hours=15, minutes=30))
        df["oi"] = 0
        return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)

    # ------------------------------------------------------------------ instruments
    def instruments(self, cache_dir: Path | None = None, max_age_hours: float = 20) -> pd.DataFrame:
        """Angel One's public instrument master (tokens change; refreshed daily). Cached on disk."""
        f = Path(cache_dir) / "angel_scrip_master.json" if cache_dir else None
        if f and f.exists() and (self._clock() - f.stat().st_mtime) < max_age_hours * 3600:
            raw = json.loads(f.read_text())
        else:
            with httpx.Client(transport=self._transport, timeout=60.0) as c:
                r = c.get(SCRIP_MASTER_URL)
                r.raise_for_status()
                raw = r.json()
            if f:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(raw))
        return pd.DataFrame(raw)


# Same list as config/watchlist_default.txt (used when that file is not shipped, e.g. a non-editable install).
DEFAULT_WATCHLIST = ("RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "BHARTIARTL", "SBIN", "ITC", "LT", "KOTAKBANK", "AXISBANK",
                     "HINDUNILVR", "BAJFINANCE", "MARUTI", "SUNPHARMA", "TITAN", "ULTRACEMCO", "NTPC", "TATAMOTORS", "ASIANPAINT")


def watchlist(experiments_dir: Path) -> list[str]:
    """experiments/watchlist.txt if present, else config/watchlist_default.txt, else the built-in default."""
    for f in (Path(experiments_dir) / "watchlist.txt", Path(__file__).resolve().parents[3] / "config" / "watchlist_default.txt"):
        if f.exists():
            return [l.split("#")[0].strip().upper() for l in f.read_text().splitlines() if l.split("#")[0].strip()]
    return list(DEFAULT_WATCHLIST)


INDEX_NAMES = {"NIFTY 50": "NIFTY50", "NIFTY": "NIFTY50", "NIFTY50": "NIFTY50", "NIFTY BANK": "NIFTYBANK", "BANKNIFTY": "NIFTYBANK",
               "INDIA VIX": "INDIAVIX", "INDIAVIX": "INDIAVIX"}


def resolve(master: pd.DataFrame, wanted: list[str]) -> tuple[list[dict], list[str]]:
    """Map NSE symbols (RELIANCE) and index names (NIFTY 50) to Angel One tokens -> (found, not found).
    Output entries match the Kite resolver's shape so the same feeds and importers can use them."""
    m = master.copy()
    m["exch_seg"] = m["exch_seg"].astype(str).str.upper()
    nse = m[m["exch_seg"] == "NSE"]
    eq = nse[nse["symbol"].astype(str).str.endswith("-EQ")]
    idx = nse[nse["instrumenttype"].astype(str).str.upper().isin(["AMXIDX", "INDEX"])]
    found, missing = [], []
    for w in wanted:
        key = w.strip().upper()
        hit = eq[eq["symbol"].astype(str).str.upper() == f"{key}-EQ"]
        if len(hit):
            r = hit.iloc[0]
            found.append({"symbol": key, "token": str(r["token"]), "kind": "EQ", "tick": float(r.get("tick_size") or 5) / 100,
                          "name": r.get("name"), "broker": "angelone"})
            continue
        alias = INDEX_NAMES.get(key)
        names = {k for k, v in INDEX_NAMES.items() if v == alias} if alias else {key}
        hit = idx[idx["name"].astype(str).str.upper().isin(names) | idx["symbol"].astype(str).str.upper().isin(names)]
        if len(hit):
            r = hit.iloc[0]
            found.append({"symbol": alias or key.replace(" ", ""), "token": str(r["token"]), "kind": "INDEX", "tick": 0.05,
                          "name": r.get("name"), "broker": "angelone"})
        else:
            missing.append(w)
    return found, missing

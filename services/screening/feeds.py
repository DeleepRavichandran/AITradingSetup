"""Live 1-minute feeds for the screening scheduler.

`BrokerMinutePoller` polls a broker's historical-candle API once per cycle for each watched symbol and
returns every COMPLETED 1-minute bar not returned before. It needs no WebSocket, so it works with the
same adapter as the importers (Angel One or Kite). Cost: one request per symbol per minute, so with
Angel One's ~3 requests/second it suits watchlists of up to about 100 symbols. A failure for one symbol
is recorded and retried on the next cycle; it never stops the others. Bars are stamped at their close.

STATUS: UNVERIFIED against the real Angel One service (mock-tested).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd


class BrokerMinutePoller:
    kind = "live_poll"

    def __init__(self, tokens: list[dict], api_factory, label: str = "Angel One 1-minute polling", lookback_minutes: int = 30):
        self.tokens = tokens
        self.api_factory = api_factory
        self.label = label
        self.lookback = lookback_minutes
        self.last: dict[str, pd.Timestamp] = {}
        self.errors: dict[str, str] = {}
        self.status = "never polled"

    def fetch(self, now) -> list[dict]:
        now = pd.Timestamp(now)
        try:
            api = self.api_factory()
        except Exception as e:                      # e.g. missing credentials or login refused
            self.status = f"error: {type(e).__name__}: {e}"
            return []
        out = []
        day_open = now.normalize() + pd.Timedelta(hours=9, minutes=15)
        for t in self.tokens:
            s = t["symbol"]
            since = self.last.get(s, max(day_open, now - pd.Timedelta(minutes=self.lookback)) - pd.Timedelta(minutes=1))
            try:
                df = api.candles(t["token"], "1m", since.to_pydatetime(), now.to_pydatetime())
            except Exception as e:
                self.errors[s] = f"{type(e).__name__}: {e}"
                continue
            self.errors.pop(s, None)
            if df.empty:
                continue
            df = df[(df["ts"] > self.last.get(s, pd.Timestamp.min)) & (df["ts"] <= now)]
            for r in df.itertuples():
                out.append({"symbol": s, "ts": r.ts, "open": float(r.open), "high": float(r.high), "low": float(r.low),
                            "close": float(r.close), "volume": float(r.volume)})
            if len(df):
                self.last[s] = df["ts"].iloc[-1]
        self.status = f"ok: {len(out)} new bars, {len(self.errors)} symbol error(s)" if not self.errors or out else \
            f"errors on {len(self.errors)} symbol(s): {next(iter(self.errors.values()))}"
        return out


def warm_up(sch, api, tokens: list[dict], days: int = 5, now: datetime | None = None, progress=print) -> dict:
    """Load the last `days` calendar days of 1-minute history into the scheduler so indicators are ready.
    Paper orders stay off for these minutes (the scheduler's `live_from`), so nothing is traded on the past."""
    now = pd.Timestamp(now or datetime.now())
    start = (now - pd.Timedelta(days=days)).normalize() + pd.Timedelta(hours=9, minutes=15)
    bars, failed = [], []
    for t in tokens:
        try:
            df = api.candles(t["token"], "1m", start.to_pydatetime(), now.to_pydatetime())
        except Exception as e:
            failed.append(f"{t['symbol']}: {type(e).__name__}: {e}")
            continue
        bars += [{"symbol": t["symbol"], "ts": r.ts, "open": float(r.open), "high": float(r.high), "low": float(r.low),
                  "close": float(r.close), "volume": float(r.volume)} for r in df.itertuples() if r.ts <= now]
    got = sch.ingest(bars, source="warm-up history")
    minutes = sorted({b["ts"] for b in bars})
    if minutes:
        sch.run_cycle(minutes[-1] + timedelta(seconds=sch.grace + 1))
    progress(f"warm-up: {len(bars)} bars over {len(minutes)} minutes for {len(tokens) - len(failed)} symbols")
    return {"bars": len(bars), "minutes": len(minutes), "ingested": got, "failed": failed}

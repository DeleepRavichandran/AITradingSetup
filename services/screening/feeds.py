"""Live 1-minute feeds for the screening scheduler.

`BrokerMinutePoller` polls a broker's historical-candle API once per cycle for each watched symbol and
returns every COMPLETED 1-minute bar not returned before. It needs no WebSocket, so it works with the
same adapter as the importers (Angel One or Kite). Cost: one request per symbol per minute; with Angel
One's request spacing at most 125 symbols fit in a minute, and larger watchlists are polled in rotation
(see the class docstring and docs/benchmarks/poller_capacity_*.json). A failure for one symbol
is recorded and retried on the next cycle; it never stops the others. Bars are stamped at their close.

STATUS: UNVERIFIED against the real Angel One service (mock-tested).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pandas as pd


class BrokerMinutePoller:
    """Polls completed 1-minute candles, one historical request per symbol per cycle.

    Scaling limit: requests are spaced by the adapter's ``min_interval_s`` (0.4 s for Angel One, about the
    documented 3 requests/second), and a cycle must finish within ``budget_s`` (50 s) to keep up with the
    minute. So at most budget_s / min_interval_s symbols (125 at the defaults) can be polled every minute.
    A larger watchlist is polled in rotation: each symbol every ceil(n / capacity) minutes, with the missed
    minutes back-filled from the lookback window; ``health()`` reports this as degraded. This does not
    scale indefinitely: a streaming feed (WebSocket) is needed for hundreds of symbols at 1-minute freshness.
    """
    kind = "live_poll"

    def __init__(self, tokens: list[dict], api_factory, label: str = "Angel One 1-minute polling", lookback_minutes: int = 30,
                 min_interval_s: float = 0.4, budget_s: float = 50.0, clock=time.perf_counter):
        self.tokens = tokens
        self.api_factory = api_factory
        self.label = label
        self.lookback = lookback_minutes
        self.min_interval_s, self.budget_s, self._clock = min_interval_s, budget_s, clock
        self.last: dict[str, pd.Timestamp] = {}
        self.errors: dict[str, str] = {}
        self.status = "never polled"
        self.cursor = 0
        self.last_cycle = {"requests": 0, "seconds": None, "polled": 0}

    @property
    def capacity(self) -> int:
        return max(1, int(self.budget_s / max(self.min_interval_s, 1e-6) + 1e-9))

    def fetch(self, now, need=None) -> list[dict]:
        """need: a retry within the minute asks again only for symbols that do not have the bar for `need` yet."""
        now = pd.Timestamp(now)
        try:
            api = self.api_factory()
        except Exception as e:  # noqa: BLE001 - e.g. missing credentials or login refused
            self.status = f"error: {type(e).__name__}: {e}"
            return []
        out = []
        day_open = now.normalize() + pd.Timedelta(hours=9, minutes=15)
        n, cap = len(self.tokens), self.capacity
        if need is not None:
            need = pd.Timestamp(need)
            batch = [t for t in self.tokens if self.last.get(t["symbol"], pd.Timestamp.min) < need][:cap]
        else:
            batch = self.tokens if n <= cap else [self.tokens[(self.cursor + k) % n] for k in range(cap)]
            self.cursor = (self.cursor + len(batch)) % max(n, 1)
        t0, reqs = self._clock(), 0
        for t in batch:
            s = t["symbol"]
            since = self.last.get(s, max(day_open, now - pd.Timedelta(minutes=self.lookback)) - pd.Timedelta(minutes=1))
            reqs += 1
            try:
                df = api.candles(t["token"], "1m", since.to_pydatetime(), now.to_pydatetime())
            except Exception as e:  # noqa: BLE001 - one symbol failing must not stop the others
                self.errors[s] = f"{type(e).__name__}: {e}"
                continue
            self.errors.pop(s, None)
            if df.empty:
                continue
            df = df[(df["ts"] > self.last.get(s, pd.Timestamp.min)) & (df["ts"] <= now)]       # end-stamped: the forming minute is > now
            for r in df.itertuples():
                out.append({"symbol": s, "ts": r.ts, "open": float(r.open), "high": float(r.high), "low": float(r.low),
                            "close": float(r.close), "volume": float(r.volume)})
            if len(df):
                self.last[s] = df["ts"].iloc[-1]
        self.last_cycle = {"requests": reqs, "seconds": round(self._clock() - t0, 3), "polled": len(batch), "retry": need is not None}
        self.status = f"ok: {len(out)} new bars, {len(self.errors)} symbol error(s)" if not self.errors or out else \
            f"errors on {len(self.errors)} symbol(s): {next(iter(self.errors.values()))}"
        return out

    def health(self, now, market_open: bool) -> dict:
        """Per-symbol freshness: symbols with no bar for more than STALE_AFTER seconds while the market is open."""
        from nseql.screening.provenance import STALE_AFTER
        now = pd.Timestamp(now)
        stale = [t["symbol"] for t in self.tokens if market_open and
                 (t["symbol"] not in self.last or (now - self.last[t["symbol"]]).total_seconds() > STALE_AFTER)]
        n, cap = len(self.tokens), self.capacity
        return {"symbols": n, "capacity_per_minute": cap, "rotation_minutes": -(-n // cap) if n > cap else 1,
                "degraded": n > cap, "stale_symbols": stale, "errors": len(self.errors), "last_cycle": self.last_cycle,
                "min_interval_s": self.min_interval_s, "budget_s": self.budget_s}


def warm_up(sch, api, tokens: list[dict], days: int = 5, now: datetime | None = None, progress=print) -> dict:
    """Load the last `days` calendar days of 1-minute history into the scheduler so indicators are ready.
    Paper orders stay off for these minutes (the scheduler's `live_from`), so nothing is traded on the past."""
    now = pd.Timestamp(now or datetime.now())
    start = (now - pd.Timedelta(days=days)).normalize() + pd.Timedelta(hours=9, minutes=15)
    bars, failed = [], []
    for t in tokens:
        try:
            df = api.candles(t["token"], "1m", start.to_pydatetime(), now.to_pydatetime())
        except Exception as e:  # noqa: BLE001 - reported per symbol
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

"""Market-data feeds for paper trading.

`ReplayFeed` replays a stored dataset bar by bar. It is a REPLAY of historical (or synthetic)
data, clearly labelled as such — it is not a live market connection.

`LiveFeed` is the interface a real broker WebSocket adapter must implement. No live adapter is
implemented: streaming authorised real-time data is BLOCKED until broker credentials and a
market-data entitlement are available (acceptance test #13).
"""
from __future__ import annotations

from typing import Iterator, Protocol

import pandas as pd

from nseql.data.portal import Bar, DataPortal
from nseql.data.store import Dataset


class FeedError(ConnectionError):
    pass


class LiveFeed(Protocol):
    kind: str

    def connect(self) -> None: ...
    def bars(self) -> Iterator[tuple[pd.Timestamp, dict[str, Bar], bool]]: ...
    def close(self) -> None: ...


class ReplayFeed:
    kind = "replay"

    def __init__(self, ds: Dataset, symbols: list[str], interval: str = "1d", start=None, end=None):
        self.ds, self.interval = ds, interval
        self._portal = DataPortal.from_dataset(ds, interval, symbols, start, end)
        self.instruments = self._portal.instruments
        self.actions = self._portal.actions
        self._k = 0

    def skip(self, n: int) -> None:
        """Advance the cursor past n steps without emitting them (used when a session is restored)."""
        for _ in range(n):
            if self.next() is None:
                break

    @property
    def remaining(self) -> int:
        return len(self._portal.timeline) - self._k

    def next(self) -> tuple[pd.Timestamp, dict[str, Bar], bool] | None:
        """-> (bar close time, bars, is last bar of its session), or None when exhausted."""
        tl = self._portal.timeline
        if self._k >= len(tl):
            return None
        ts = int(tl[self._k])
        bars = self._portal.advance(ts)
        t = pd.Timestamp(ts)
        last = self._k == len(tl) - 1 or pd.Timestamp(tl[self._k + 1]).date() != t.date()
        self._k += 1
        return t, bars, last


class KiteDailyFeed:
    """End-of-day paper trading from Kite's historical API: after each close, the day's completed
    daily candle for every symbol becomes one step. No WebSocket needed. Orders placed on day D are
    simulated against day D+1's candle, exactly as in a daily backtest.

    Catch-up safe: if a poll is missed (expired login, server down), the next poll returns every
    completed day since the last one, in order. Days before `live_from` are warm-up: they build
    indicator history but nothing may trade.

    STATUS: UNVERIFIED against the real API (mock-tested).
    """
    kind = "kite_daily"
    interval = "1d"
    CLOSE_READY = (15, 45)       # a day's candle is treated as final from 15:45 IST

    def __init__(self, tokens: list[dict], live_from, *, api_factory=None, warmup_days: int = 420, last_date=None):
        from nseql.instruments.models import Instrument, Kind
        self.tokens = tokens
        self.live_from = pd.Timestamp(live_from).date()
        self.api_factory = api_factory
        self.warmup_days = warmup_days
        self.last_date = last_date
        self.instruments = {t["symbol"]: Instrument(t["symbol"], Kind.INDEX if t["kind"] == "INDEX" else Kind.EQ,
                                                    segment="IDX" if t["kind"] == "INDEX" else "CM", tick_size=t.get("tick", 0.05))
                            for t in tokens}
        self.actions: list = []
        self.status = "never polled"

    def poll(self, now) -> list[tuple[pd.Timestamp, dict[str, Bar], bool, bool]]:
        """-> [(bar time, bars, session_end=True, warmup)] for every completed day not yet delivered."""
        now = pd.Timestamp(now)
        if self.api_factory is None:
            raise FeedError("no Kite session: run `nseql kite-login`")
        try:
            api = self.api_factory()
            start = (pd.Timestamp(self.last_date) + pd.Timedelta(days=1)) if self.last_date else (pd.Timestamp(self.live_from) - pd.Timedelta(days=self.warmup_days))
            if start.normalize() > now.normalize():
                return []
            by_day: dict = {}
            for t in self.tokens:
                df = api.candles(t["token"], "1d", start.to_pydatetime(), now.to_pydatetime())
                for r in df.itertuples():
                    by_day.setdefault(r.ts.date(), {})[t["symbol"]] = Bar(t["symbol"], r.ts, float(r.open), float(r.high), float(r.low),
                                                                           float(r.close), int(r.volume), int(getattr(r, "oi", 0) or 0))
        except FeedError:
            raise
        except Exception as e:
            self.status = f"error: {type(e).__name__}: {e}"
            raise FeedError(str(e)) from e
        final_today = (now.hour, now.minute) >= self.CLOSE_READY
        out = []
        for d in sorted(by_day):
            if d > now.date() or (d == now.date() and not final_today) or (self.last_date and d <= self.last_date):
                continue
            out.append((pd.Timestamp(f"{d} 15:30:00"), by_day[d], True, d < self.live_from))
            self.last_date = d
        self.status = f"ok: last candle {self.last_date}"
        return out


class KiteTickerFeed:
    """NOT IMPLEMENTED: Zerodha KiteTicker WebSocket (binary tick protocol) adapter."""
    kind = "live"

    def connect(self) -> None:
        raise NotImplementedError("KiteTicker live feed is NOT IMPLEMENTED (needs broker credentials and a data entitlement)")

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


class KiteTickerFeed:
    """NOT IMPLEMENTED: Zerodha KiteTicker WebSocket (binary tick protocol) adapter."""
    kind = "live"

    def connect(self) -> None:
        raise NotImplementedError("KiteTicker live feed is NOT IMPLEMENTED (needs broker credentials and a data entitlement)")

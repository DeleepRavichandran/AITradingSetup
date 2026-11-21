"""Point-in-time market data access for the event loop.

The portal owns the full arrays but only ever exposes data at or before its cursor.
`history()` returns *copies* of the trailing window, so strategy code can neither
mutate the store nor reach forward through a view.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from nseql.data.store import Dataset
from nseql.instruments.models import CorporateAction, Instrument

FIELDS = ("open", "high", "low", "close", "volume", "oi")


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    ts: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int = 0


class DataPortal:
    def __init__(self, frames: dict[str, pd.DataFrame], instruments: dict[str, Instrument],
                 actions: list[CorporateAction] | None = None, interval: str = "1d"):
        self.interval = interval
        self.instruments = instruments
        self.actions = list(actions or [])
        self._a: dict[str, dict[str, np.ndarray]] = {}
        self._adj: dict[str, np.ndarray] = {}
        for sym in sorted(frames):
            df = frames[sym]
            if len(df) == 0:
                continue
            ts = df["ts"].to_numpy().astype("datetime64[ns]").astype("int64")
            if np.any(np.diff(ts) <= 0):
                raise ValueError(f"{sym}: timestamps must be strictly increasing")
            self._a[sym] = {"ts": ts, **{f: df[f].to_numpy().copy() for f in FIELDS}}
            g = np.ones(len(ts))
            for ca in self.actions:
                if ca.symbol == sym and ca.kind in ("split", "bonus"):
                    ex = np.datetime64(ca.ex_date).astype("datetime64[ns]").astype("int64")
                    g[ts >= ex] *= ca.ratio
            self._adj[sym] = g
        all_ts = np.unique(np.concatenate([a["ts"] for a in self._a.values()])) if self._a else np.array([], "int64")
        self.timeline = all_ts
        self._pos = {s: -1 for s in self._a}
        self._now: int = -1
        self._current: dict[str, Bar] = {}

    # ------------------------------------------------------------ construction
    @classmethod
    def from_dataset(cls, ds: Dataset, interval: str, symbols: list[str], start=None, end=None) -> "DataPortal":
        frames = {s: ds.bars(s, interval, start, end) for s in symbols if ds.has_bars(s, interval)}
        return cls(frames, {s: i for s, i in ds.instruments.items()}, ds.corporate_actions(), interval)

    def frames(self) -> dict[str, pd.DataFrame]:
        return {s: pd.DataFrame({"ts": a["ts"].astype("datetime64[ns]"), **{f: a[f] for f in FIELDS}})
                for s, a in self._a.items()}

    def perturbed_after(self, cut_ts: int, seed: int) -> "DataPortal":
        """Copy of this portal in which every price strictly after `cut_ts` is scrambled.
        Used by the look-ahead audit: a causal strategy must behave identically up to the cut."""
        rng = np.random.default_rng(seed)
        frames = {}
        for s, df in self.frames().items():
            m = (df["ts"].to_numpy().astype("int64") > cut_ts)
            k = int(m.sum())
            if k:
                f = np.exp(rng.normal(0, 0.05, k)).cumprod() * rng.uniform(0.7, 1.3)
                for c in ("open", "high", "low", "close"):
                    df.loc[m, c] = np.round(df.loc[m, c].to_numpy() * f / 0.05) * 0.05 + 0.05
                df.loc[m, "high"] = df.loc[m, ["open", "high", "low", "close"]].max(axis=1)
                df.loc[m, "low"] = df.loc[m, ["open", "high", "low", "close"]].min(axis=1)
            frames[s] = df
        return DataPortal(frames, self.instruments, self.actions, self.interval)

    def truncated(self, cut_ts: int) -> "DataPortal":
        frames = {s: df[df["ts"].to_numpy().astype("int64") <= cut_ts] for s, df in self.frames().items()}
        return DataPortal(frames, self.instruments, self.actions, self.interval)

    # ------------------------------------------------------------ event loop
    @property
    def symbols(self) -> list[str]:
        return list(self._a)

    def advance(self, ts: int) -> dict[str, Bar]:
        if ts <= self._now:
            raise ValueError("portal can only move forward in time")
        self._now = ts
        cur: dict[str, Bar] = {}
        t = pd.Timestamp(ts)
        for s, a in self._a.items():
            i = self._pos[s] + 1
            if i < len(a["ts"]) and a["ts"][i] == ts:
                self._pos[s] = i
                cur[s] = Bar(s, t, float(a["open"][i]), float(a["high"][i]), float(a["low"][i]),
                             float(a["close"][i]), int(a["volume"][i]), int(a["oi"][i]))
        self._current = cur
        return cur

    @property
    def now(self) -> pd.Timestamp:
        return pd.Timestamp(self._now)

    def current(self, symbol: str) -> Bar | None:
        return self._current.get(symbol)

    def history(self, symbol: str, field: str = "close", n: int | None = None, adjusted: bool = True) -> np.ndarray:
        """Trailing window ending at the latest bar at or before now (oldest first). Always a copy.

        adjusted=True back-adjusts for splits/bonuses that have ALREADY taken effect, so the
        series is continuous without using any action that is still in the future."""
        a = self._a.get(symbol)
        i = self._pos.get(symbol, -1)
        if a is None or i < 0:
            return np.empty(0)
        lo = 0 if n is None else max(i + 1 - n, 0)
        out = a[field][lo:i + 1].astype(float)   # astype copies
        if adjusted and field != "oi":
            g = self._adj[symbol]
            f = g[lo:i + 1] / g[i]
            out = out / f if field == "volume" else out * f
        return out

    def last(self, symbol: str, field: str = "close") -> float | None:
        a = self._a.get(symbol)
        i = self._pos.get(symbol, -1)
        return None if a is None or i < 0 else float(a[field][i])

    def last_ts(self, symbol: str) -> pd.Timestamp | None:
        a = self._a.get(symbol)
        i = self._pos.get(symbol, -1)
        return None if a is None or i < 0 else pd.Timestamp(a["ts"][i])

    def bars_seen(self, symbol: str) -> int:
        return self._pos.get(symbol, -1) + 1

    def listed_now(self, d: date) -> list[str]:
        return [s for s in self._a if self._pos[s] >= 0 and self.instruments[s].listed_on(d)]


class GuardedPortal(DataPortal):
    """Look-ahead trap. Every value that lies in the future of the cursor is replaced by noise and
    the true value is revealed only when the cursor reaches it. A causal strategy cannot tell the
    difference; a strategy that reads ahead — by any route, including engine internals — sees
    garbage and behaves differently from its run on the clean portal."""

    def __init__(self, source: DataPortal, seed: int = 7):
        super().__init__(source.frames(), source.instruments, source.actions, source.interval)
        rng = np.random.default_rng(seed)
        self._real = {s: {f: a[f].copy() for f in FIELDS} for s, a in self._a.items()}
        for a in self._a.values():
            n = len(a["ts"])
            for f in FIELDS:
                a[f] = (a[f] * rng.uniform(0.4, 1.6, n)).astype(a[f].dtype)

    def advance(self, ts: int) -> dict[str, Bar]:
        for s, a in self._a.items():
            i = self._pos[s] + 1
            if i < len(a["ts"]) and a["ts"][i] == ts:
                for f in FIELDS:
                    a[f][i] = self._real[s][f][i]
        return super().advance(ts)


def derivative_symbols(ds: Dataset, underlying: str, kinds=("FUT", "OPT")) -> list[str]:
    return sorted(s for s, i in ds.instruments.items() if i.underlying == underlying and i.kind.value in kinds)

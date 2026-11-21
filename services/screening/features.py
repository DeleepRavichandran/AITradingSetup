"""Shared, incremental market features for the one-minute screening loop.

One FeatureEngine serves every registered configuration. It ingests 1-minute bars (stamped at bar
CLOSE, exchange-local, as everywhere in the platform), aggregates them into higher timeframes and
updates indicators only for the timeframes whose bar closed this minute, and only for the symbols
that had data. Nothing is recomputed from scratch per minute and nothing is computed per strategy.

Timeframes: 1m, 5m, 15m (intraday) and 1d (swing). A 5-minute bar closes at minute 5, 10, ... of the
session (09:20 is the first), the daily bar at the session's last minute (15:30).

Indicator definitions (identical in `batch_features`, the reference used by the parity test):
  sma{w}   mean of the last w closes of that timeframe
  hh{n}    highest close of the n bars BEFORE the current one; ll{n} likewise the lowest
  rsi{n}   Wilder-style RSI using exponential averages with alpha = 1/n seeded by the first change
           (pandas ewm(alpha=1/n, adjust=False)); valid once n changes have been seen
A feature is NaN until enough bars exist. A symbol with no 1-minute bar in a bucket gets no bar for
that bucket (illiquid symbols simply have fewer bars); its features are not updated that minute.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

SESSION_OPEN_MIN = 9 * 60 + 15
SESSION_CLOSE_MIN = 15 * 60 + 30
SESSION_MINUTES = SESSION_CLOSE_MIN - SESSION_OPEN_MIN          # 375
TIMEFRAMES = {"1m": 1, "5m": 5, "15m": 15, "1d": SESSION_MINUTES}
SMA_W = (5, 10, 20, 50, 100, 200)
CHAN_N = (10, 20, 55)
RSI_N = (2, 5, 14)
RING = max(max(SMA_W), max(CHAN_N) + 1) + 1


def minute_of_session(ts: pd.Timestamp) -> int:
    """1 for the 09:16 bar ... 375 for the 15:30 bar."""
    return ts.hour * 60 + ts.minute - SESSION_OPEN_MIN


def bucket_closes(tf: str, m: int) -> bool:
    n = TIMEFRAMES[tf]
    return m % n == 0 or m == SESSION_MINUTES


@dataclass
class _Agg:
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    v: np.ndarray
    has: np.ndarray


class FeatureEngine:
    def __init__(self, symbols: list[str]):
        self.symbols = list(symbols)
        self.index = {s: i for i, s in enumerate(self.symbols)}
        S = len(self.symbols)
        self.S = S
        self.ring = {tf: np.full((S, RING), np.nan) for tf in TIMEFRAMES}
        self.head = {tf: np.zeros(S, dtype=np.int64) for tf in TIMEFRAMES}
        self.count = {tf: np.zeros(S, dtype=np.int64) for tf in TIMEFRAMES}
        self.agg = {tf: _Agg(*(np.full(S, np.nan) for _ in range(5)), np.zeros(S, dtype=bool)) for tf in TIMEFRAMES}
        self.rsi_state = {tf: {n: np.full((S, 3), np.nan) for n in RSI_N} for tf in TIMEFRAMES}   # prev close, avg gain, avg loss
        self.rsi_seen = {tf: {n: np.zeros(S, dtype=np.int64) for n in RSI_N} for tf in TIMEFRAMES}
        self.feat = {tf: self._empty() for tf in TIMEFRAMES}
        self.last_ts = np.full(S, np.datetime64("NaT", "m"))
        self.last_close = np.full(S, np.nan)
        self.last_low = np.full(S, np.nan)
        self.last_open = np.full(S, np.nan)
        self.updated = {tf: np.zeros(S, dtype=bool) for tf in TIMEFRAMES}   # features changed this minute

    def _empty(self) -> dict:
        names = ["close", *(f"sma{w}" for w in SMA_W), *(f"hh{n}" for n in CHAN_N), *(f"ll{n}" for n in CHAN_N), *(f"rsi{n}" for n in RSI_N)]
        return {k: np.full(self.S, np.nan) for k in names}

    # ------------------------------------------------------------------ update
    def update(self, ts: pd.Timestamp, sel: np.ndarray, o, h, lo, c, v) -> list[str]:
        """Ingest one minute. sel = symbol indices with a bar; o..v aligned with sel. Returns the
        timeframes whose bar closed this minute."""
        m = minute_of_session(ts)
        for u in self.updated.values():
            u[:] = False
        self.last_ts[sel] = np.datetime64(ts, "m")
        self.last_close[sel], self.last_low[sel], self.last_open[sel] = c, lo, o
        closed = []
        for tf in TIMEFRAMES:
            a = self.agg[tf]
            new = ~a.has[sel]
            fresh, cont = sel[new], sel[~new]
            if len(fresh):
                k = np.flatnonzero(new)
                a.o[fresh], a.h[fresh], a.lo[fresh], a.v[fresh] = o[k], h[k], lo[k], v[k]
                a.has[fresh] = True
            if len(cont):
                k = np.flatnonzero(~new)
                a.h[cont] = np.maximum(a.h[cont], h[k])
                a.lo[cont] = np.minimum(a.lo[cont], lo[k])
                a.v[cont] = a.v[cont] + v[k]
            a.c[sel] = c
            if bucket_closes(tf, m):
                done = np.flatnonzero(a.has)
                if len(done):
                    self._append(tf, done, a.c[done])
                    self.updated[tf][done] = True
                a.has[:] = False
                closed.append(tf)
        return closed

    def _append(self, tf: str, idx: np.ndarray, close: np.ndarray) -> None:
        R, hd = self.ring[tf], self.head[tf]
        R[idx, hd[idx]] = close
        hd[idx] = (hd[idx] + 1) % RING
        self.count[tf][idx] += 1
        F = self.feat[tf]
        F["close"][idx] = close
        cnt = self.count[tf][idx]

        def window(w: int, skip: int = 0) -> np.ndarray:
            p = (hd[idx][:, None] - 1 - skip - np.arange(w)[None, :]) % RING
            return R[idx[:, None], p]

        for w in SMA_W:
            F[f"sma{w}"][idx] = np.where(cnt >= w, window(w).mean(axis=1), np.nan)
        for n in CHAN_N:
            win = window(n, skip=1)
            ok = cnt >= n + 1
            F[f"hh{n}"][idx] = np.where(ok, win.max(axis=1), np.nan)
            F[f"ll{n}"][idx] = np.where(ok, win.min(axis=1), np.nan)
        for n in RSI_N:
            st, seen = self.rsi_state[tf][n], self.rsi_seen[tf][n]
            prev = st[idx, 0]
            has_prev = ~np.isnan(prev)
            d = np.where(has_prev, close - prev, np.nan)
            g, lo_ = np.clip(d, 0, None), np.clip(-d, 0, None)
            first = has_prev & (seen[idx] == 0)
            later = has_prev & (seen[idx] > 0)
            alpha = 1.0 / n
            ag, al = st[idx, 1], st[idx, 2]
            ag = np.where(first, g, np.where(later, ag + alpha * (g - ag), ag))
            al = np.where(first, lo_, np.where(later, al + alpha * (lo_ - al), al))
            st[idx, 1], st[idx, 2], st[idx, 0] = ag, al, close
            seen[idx] += has_prev
            with np.errstate(divide="ignore", invalid="ignore"):
                r = np.where(al > 0, 100 - 100 / (1 + ag / al), np.where(ag > 0, 100.0, 50.0))
            F[f"rsi{n}"][idx] = np.where(seen[idx] >= n, r, np.nan)

    # ------------------------------------------------------------------ checkpoint
    def state(self) -> dict:
        out = {"last_ts": self.last_ts.astype("int64"), "last_close": self.last_close, "last_low": self.last_low, "last_open": self.last_open}
        for tf in TIMEFRAMES:
            out[f"ring_{tf}"], out[f"head_{tf}"], out[f"count_{tf}"] = self.ring[tf], self.head[tf], self.count[tf]
            a = self.agg[tf]
            for k in ("o", "h", "lo", "c", "v", "has"):
                out[f"agg_{tf}_{k}"] = getattr(a, k)
            for n in RSI_N:
                out[f"rsi_{tf}_{n}"], out[f"rsiseen_{tf}_{n}"] = self.rsi_state[tf][n], self.rsi_seen[tf][n]
            for k, v in self.feat[tf].items():
                out[f"feat_{tf}_{k}"] = v
        return out

    def load(self, st: dict) -> None:
        self.last_ts = np.asarray(st["last_ts"]).astype("datetime64[m]")
        self.last_close, self.last_low, self.last_open = (np.array(st[k]) for k in ("last_close", "last_low", "last_open"))
        for tf in TIMEFRAMES:
            self.ring[tf], self.head[tf], self.count[tf] = (np.array(st[f"{k}_{tf}"]) for k in ("ring", "head", "count"))
            for k in ("o", "h", "lo", "c", "v", "has"):
                setattr(self.agg[tf], k, np.array(st[f"agg_{tf}_{k}"]))
            for n in RSI_N:
                self.rsi_state[tf][n], self.rsi_seen[tf][n] = np.array(st[f"rsi_{tf}_{n}"]), np.array(st[f"rsiseen_{tf}_{n}"])
            for k in self.feat[tf]:
                self.feat[tf][k] = np.array(st[f"feat_{tf}_{k}"])


def batch_features(minute: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Reference implementation for one symbol, from a full history of 1-minute bars (columns ts, open,
    high, low, close, volume). Returns per timeframe a frame indexed by bar-close time with the same
    features. Written independently of FeatureEngine (pandas rolling/ewm) so the parity test is real."""
    df = minute.sort_values("ts").reset_index(drop=True)
    m = (df["ts"].dt.hour * 60 + df["ts"].dt.minute - SESSION_OPEN_MIN).to_numpy()
    day = df["ts"].dt.normalize()
    out = {}
    for tf, n in TIMEFRAMES.items():
        bucket_end = np.minimum(((m + n - 1) // n) * n, SESSION_MINUTES)
        close_ts = day + pd.to_timedelta(SESSION_OPEN_MIN + bucket_end, unit="min")
        bars = df.assign(bar_ts=close_ts).groupby("bar_ts", sort=True).agg(close=("close", "last"))
        c = bars["close"]
        f = pd.DataFrame(index=bars.index)
        f["close"] = c
        for w in SMA_W:
            f[f"sma{w}"] = c.rolling(w, min_periods=w).mean()
        for k in CHAN_N:
            f[f"hh{k}"] = c.shift(1).rolling(k, min_periods=k).max()
            f[f"ll{k}"] = c.shift(1).rolling(k, min_periods=k).min()
        d = c.diff()
        for k in RSI_N:
            g = d.clip(lower=0).ewm(alpha=1 / k, adjust=False).mean()
            lo = (-d.clip(upper=0)).ewm(alpha=1 / k, adjust=False).mean()
            g.iloc[0] = lo.iloc[0] = np.nan
            with np.errstate(divide="ignore", invalid="ignore"):
                r = pd.Series(np.where(lo > 0, 100 - 100 / (1 + g / lo), np.where(g > 0, 100.0, 50.0)), index=c.index)
            seen = d.notna().cumsum()
            f[f"rsi{k}"] = r.where(seen >= k)
        out[tf] = f
    return out

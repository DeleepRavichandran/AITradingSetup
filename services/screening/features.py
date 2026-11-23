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
Added 2026-11-23 (more rule families):
  bbu20/bbl20  sma20 +/- 2 x sample standard deviation (ddof=1) of the last 20 closes; z20 = (close - sma20) / sd,
           undefined when sd is zero (sd below 1e-7 of the mean counts as zero: floating-point noise on flat prices)
  macd_F_S / macds_F_S   EMA(F) - EMA(S) of closes and its EMA(9) signal (pandas ewm(span, adjust=False), seeded by the
           first close / first MACD value); valid once S + 9 bars exist
  roc{n}   close / close n bars earlier - 1
  vbo{n}   1 if close > hh{n} and this bar's volume > 1.5 x the mean volume of the previous 20 bars, else 0
  Session features (from 1-minute bars of the current session, as of the bar's close):
  vwapdev  close / VWAP - 1, VWAP = cumulative sum(typical price x volume) / cumulative volume, typical = (h + l + c) / 3
  orb_up / orb_mid   close / opening-range high - 1, close / opening-range middle - 1; opening range = high and low of
           the 09:16 .. 09:30 one-minute bars; NaN before 09:30
  gap      session open / previous session's last close - 1;  gapgo = gap when close > session open, else NaN
  gapdn    close / previous close - 1 when gap < -0.5%, else NaN;  gapfill = close / previous close - 1
  dayret   close / session open - 1;  pdh_dev = close / previous session's high - 1
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
MACD_SET = ((12, 26), (8, 17))
MACD_SIG = 9
ROC_N = (10, 20, 55)
VBO_N = (10, 20)
VOL_AVG = 20
VOL_MULT = 1.5
OR_MINUTES = 15
GAP_MIN = -0.005
SD_EPS = 1e-7                 # a standard deviation below this share of the mean is treated as zero
RING = max(max(SMA_W), max(CHAN_N) + 1, max(ROC_N) + 1, VOL_AVG + 1) + 1
SESSION_FEATURES = ("vwapdev", "orb_up", "orb_mid", "gap", "gapgo", "gapdn", "gapfill", "dayret", "pdh_dev")
FEATURE_NAMES = ["close", *(f"sma{w}" for w in SMA_W), *(f"hh{n}" for n in CHAN_N), *(f"ll{n}" for n in CHAN_N), *(f"rsi{n}" for n in RSI_N),
                 "bbu20", "bbl20", "z20", *(x for f, sl in MACD_SET for x in (f"macd_{f}_{sl}", f"macds_{f}_{sl}")),
                 *(f"roc{n}" for n in ROC_N), *(f"vbo{n}" for n in VBO_N), *SESSION_FEATURES]


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
        self.vring = {tf: np.full((S, RING), np.nan) for tf in TIMEFRAMES}                   # bar volumes, same head as ring
        self.ema = {tf: np.full((S, 3 * len(MACD_SET)), np.nan) for tf in TIMEFRAMES}        # fast, slow, signal per MACD
        # session state (per symbol): day, open, high, low, last close, cum pv, cum v, opening-range high/low,
        # previous session close/high
        self.sess = np.full((S, 11), np.nan)
        self.feat = {tf: self._empty() for tf in TIMEFRAMES}
        self.last_ts = np.full(S, np.datetime64("NaT", "m"))
        self.last_close = np.full(S, np.nan)
        self.last_low = np.full(S, np.nan)
        self.last_open = np.full(S, np.nan)
        self.updated = {tf: np.zeros(S, dtype=bool) for tf in TIMEFRAMES}   # features changed this minute

    def _empty(self) -> dict:
        return {k: np.full(self.S, np.nan) for k in FEATURE_NAMES}

    def _session(self, ts: pd.Timestamp, sel: np.ndarray, o, h, lo, c, v) -> None:
        """Session state from this minute's 1-minute bars (before any timeframe closes)."""
        S_ = self.sess
        day = float(ts.toordinal())
        m = minute_of_session(ts)
        new = S_[sel, 0] != day
        if new.any():
            k = sel[new]
            had = ~np.isnan(S_[k, 0])
            S_[k, 9] = np.where(had, S_[k, 4], np.nan)       # previous session's last close
            S_[k, 10] = np.where(had, S_[k, 2], np.nan)      # previous session's high
            S_[k, 0], S_[k, 1], S_[k, 2], S_[k, 3] = day, o[new], h[new], lo[new]
            S_[k, 5], S_[k, 6], S_[k, 7], S_[k, 8] = 0.0, 0.0, np.nan, np.nan
        S_[sel, 2] = np.fmax(S_[sel, 2], h)
        S_[sel, 3] = np.fmin(S_[sel, 3], lo)
        S_[sel, 4] = c
        S_[sel, 5] += (h + lo + c) / 3.0 * v
        S_[sel, 6] += v
        if m <= OR_MINUTES:
            S_[sel, 7] = np.fmax(S_[sel, 7], h)
            S_[sel, 8] = np.fmin(S_[sel, 8], lo)

    # ------------------------------------------------------------------ update
    def update(self, ts: pd.Timestamp, sel: np.ndarray, o, h, lo, c, v) -> list[str]:
        """Ingest one minute. sel = symbol indices with a bar; o..v aligned with sel. Returns the
        timeframes whose bar closed this minute."""
        m = minute_of_session(ts)
        self._m = m
        for u in self.updated.values():
            u[:] = False
        self._session(ts, sel, o, h, lo, c, v)
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
                    self._append(tf, done, a.c[done], a.v[done])
                    self.updated[tf][done] = True
                a.has[:] = False
                closed.append(tf)
        return closed

    def _append(self, tf: str, idx: np.ndarray, close: np.ndarray, vol: np.ndarray) -> None:
        R, hd = self.ring[tf], self.head[tf]
        R[idx, hd[idx]] = close
        self.vring[tf][idx, hd[idx]] = vol
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
        # Bollinger / z-score (20 bars, sample sd)
        w20 = window(20)
        ok20 = cnt >= 20
        mu, sd = w20.mean(axis=1), w20.std(axis=1, ddof=1)
        sd = np.where(sd <= SD_EPS * np.abs(mu), 0.0, sd)               # flat prices: rounding noise is not volatility
        F["bbu20"][idx] = np.where(ok20, mu + 2 * sd, np.nan)
        F["bbl20"][idx] = np.where(ok20, mu - 2 * sd, np.nan)
        with np.errstate(divide="ignore", invalid="ignore"):
            F["z20"][idx] = np.where(ok20 & (sd > 0), (close - mu) / sd, np.nan)
        # MACD
        E = self.ema[tf]
        for j, (f, sl) in enumerate(MACD_SET):
            ef, es, sg = E[idx, 3 * j], E[idx, 3 * j + 1], E[idx, 3 * j + 2]
            af, asl, ag = 2 / (f + 1), 2 / (sl + 1), 2 / (MACD_SIG + 1)
            ef = np.where(np.isnan(ef), close, ef + af * (close - ef))
            es = np.where(np.isnan(es), close, es + asl * (close - es))
            mac = ef - es
            sg = np.where(np.isnan(sg), mac, sg + ag * (mac - sg))
            E[idx, 3 * j], E[idx, 3 * j + 1], E[idx, 3 * j + 2] = ef, es, sg
            okm = cnt >= sl + MACD_SIG
            F[f"macd_{f}_{sl}"][idx] = np.where(okm, mac, np.nan)
            F[f"macds_{f}_{sl}"][idx] = np.where(okm, sg, np.nan)
        # rate of change
        for n in ROC_N:
            prev = R[idx, (hd[idx] - 1 - n) % RING]
            with np.errstate(divide="ignore", invalid="ignore"):
                F[f"roc{n}"][idx] = np.where(cnt >= n + 1, close / prev - 1, np.nan)
        # volume breakout
        VR = self.vring[tf]
        pv = VR[idx[:, None], (hd[idx][:, None] - 2 - np.arange(VOL_AVG)[None, :]) % RING]
        vavg = pv.mean(axis=1)
        okv = cnt >= VOL_AVG + 1
        for n in VBO_N:
            hh = F[f"hh{n}"][idx]
            hit = (close > hh) & (vol > VOL_MULT * vavg)
            F[f"vbo{n}"][idx] = np.where(okv & ~np.isnan(hh), hit.astype(float), np.nan)
        # session features, as of this bar's close
        S_ = self.sess[idx]
        with np.errstate(divide="ignore", invalid="ignore"):
            vwap = np.where(S_[:, 6] > 0, S_[:, 5] / S_[:, 6], np.nan)
            F["vwapdev"][idx] = close / vwap - 1
            orr = self._m >= OR_MINUTES
            F["orb_up"][idx] = np.where(orr, close / S_[:, 7] - 1, np.nan)
            F["orb_mid"][idx] = np.where(orr, close / ((S_[:, 7] + S_[:, 8]) / 2) - 1, np.nan)
            gap = S_[:, 1] / S_[:, 9] - 1
            F["gap"][idx] = gap
            F["gapgo"][idx] = np.where(close > S_[:, 1], gap, np.nan)
            fill = close / S_[:, 9] - 1
            F["gapfill"][idx] = fill
            F["gapdn"][idx] = np.where(gap < GAP_MIN, fill, np.nan)
            F["dayret"][idx] = close / S_[:, 1] - 1
            F["pdh_dev"][idx] = close / S_[:, 10] - 1

    # ------------------------------------------------------------------ checkpoint
    def state(self) -> dict:
        out = {"last_ts": self.last_ts.astype("int64"), "last_close": self.last_close, "last_low": self.last_low, "last_open": self.last_open,
               "sess": self.sess}
        for tf in TIMEFRAMES:
            out[f"ring_{tf}"], out[f"head_{tf}"], out[f"count_{tf}"] = self.ring[tf], self.head[tf], self.count[tf]
            a = self.agg[tf]
            for k in ("o", "h", "lo", "c", "v", "has"):
                out[f"agg_{tf}_{k}"] = getattr(a, k)
            for n in RSI_N:
                out[f"rsi_{tf}_{n}"], out[f"rsiseen_{tf}_{n}"] = self.rsi_state[tf][n], self.rsi_seen[tf][n]
            out[f"vring_{tf}"], out[f"ema_{tf}"] = self.vring[tf], self.ema[tf]
            for k, v in self.feat[tf].items():
                out[f"feat_{tf}_{k}"] = v
        return out

    def load(self, st: dict) -> None:
        self.last_ts = np.asarray(st["last_ts"]).astype("datetime64[m]")
        self.last_close, self.last_low, self.last_open = (np.array(st[k]) for k in ("last_close", "last_low", "last_open"))
        self.sess = np.array(st["sess"])
        for tf in TIMEFRAMES:
            self.ring[tf], self.head[tf], self.count[tf] = (np.array(st[f"{k}_{tf}"]) for k in ("ring", "head", "count"))
            for k in ("o", "h", "lo", "c", "v", "has"):
                setattr(self.agg[tf], k, np.array(st[f"agg_{tf}_{k}"]))
            for n in RSI_N:
                self.rsi_state[tf][n], self.rsi_seen[tf][n] = np.array(st[f"rsi_{tf}_{n}"]), np.array(st[f"rsiseen_{tf}_{n}"])
            self.vring[tf], self.ema[tf] = np.array(st[f"vring_{tf}"]), np.array(st[f"ema_{tf}"])
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
        bars = df.assign(bar_ts=close_ts).groupby("bar_ts", sort=True).agg(close=("close", "last"), volume=("volume", "sum"))
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
        sma20 = c.rolling(20, min_periods=20).mean()
        sd20 = c.rolling(20, min_periods=20).apply(lambda x: x.std(ddof=1), raw=True)   # per window, not pandas' running update
        sd20 = sd20.where(~(sd20 <= SD_EPS * sma20.abs()), 0.0)
        f["bbu20"], f["bbl20"] = sma20 + 2 * sd20, sma20 - 2 * sd20
        f["z20"] = ((c - sma20) / sd20).where(sd20 > 0)
        nbar = pd.Series(np.arange(1, len(c) + 1), index=c.index)
        for fa, sl in MACD_SET:
            mac = c.ewm(span=fa, adjust=False).mean() - c.ewm(span=sl, adjust=False).mean()
            sig = mac.ewm(span=MACD_SIG, adjust=False).mean()
            f[f"macd_{fa}_{sl}"], f[f"macds_{fa}_{sl}"] = mac.where(nbar >= sl + MACD_SIG), sig.where(nbar >= sl + MACD_SIG)
        for k in ROC_N:
            f[f"roc{k}"] = c / c.shift(k) - 1
        vol = bars["volume"]
        vavg = vol.shift(1).rolling(VOL_AVG, min_periods=VOL_AVG).mean()
        for k in VBO_N:
            hit = ((c > f[f"hh{k}"]) & (vol > VOL_MULT * vavg)).astype(float)
            f[f"vbo{k}"] = hit.where(vavg.notna() & f[f"hh{k}"].notna())
        # session features: state of the session as of the last 1-minute bar in each bucket
        last_min = df.assign(bar_ts=close_ts).groupby("bar_ts", sort=True)["ts"].last()
        sess = _session_reference(df).reindex(last_min.to_numpy())
        sess.index = bars.index
        bucket_min = (bars.index.hour * 60 + bars.index.minute - SESSION_OPEN_MIN).to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            f["vwapdev"] = c / sess["vwap"] - 1
            orr = bucket_min >= OR_MINUTES
            f["orb_up"] = (c / sess["or_hi"] - 1).where(orr)
            f["orb_mid"] = (c / ((sess["or_hi"] + sess["or_lo"]) / 2) - 1).where(orr)
            gap = sess["day_open"] / sess["prev_close"] - 1
            f["gap"] = gap
            f["gapgo"] = gap.where(c > sess["day_open"])
            fill = c / sess["prev_close"] - 1
            f["gapfill"] = fill
            f["gapdn"] = fill.where(gap < GAP_MIN)
            f["dayret"] = c / sess["day_open"] - 1
            f["pdh_dev"] = c / sess["prev_high"] - 1
        out[tf] = f
    return out


def _session_reference(df: pd.DataFrame) -> pd.DataFrame:
    """Per 1-minute bar: session VWAP, opening range, session open, previous session's close and high."""
    d = df.sort_values("ts").set_index("ts")
    day = d.index.normalize()
    m = d.index.hour * 60 + d.index.minute - SESSION_OPEN_MIN
    tp = (d["high"] + d["low"] + d["close"]) / 3 * d["volume"]
    g = d.groupby(day)
    out = pd.DataFrame(index=d.index)
    cv = d["volume"].groupby(day).cumsum()
    out["vwap"] = (tp.groupby(day).cumsum() / cv).where(cv > 0)
    in_or = pd.Series(m <= OR_MINUTES, index=d.index)
    out["or_hi"] = d["high"].where(in_or).groupby(day).cummax()
    out["or_lo"] = d["low"].where(in_or).groupby(day).cummin()
    out["or_hi"] = out["or_hi"].groupby(day).ffill()
    out["or_lo"] = out["or_lo"].groupby(day).ffill()
    out["day_open"] = g["open"].transform("first")
    daily = g.agg(close=("close", "last"), high=("high", "max"))
    prev = daily.shift(1)
    out["prev_close"] = prev["close"].reindex(day).to_numpy()
    out["prev_high"] = prev["high"].reindex(day).to_numpy()
    return out

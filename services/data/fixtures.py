"""Deterministic SYNTHETIC fixture dataset.

*** NOTHING IN THIS DATASET IS REAL MARKET DATA. ***
Symbols are prefixed SYN so they can never be mistaken for listed securities, the
manifest carries synthetic=true, and every report produced from it is labelled.

The generator is a pure function of `seed`. It produces a small but structurally
complete NSE-like universe so that every engine path can be exercised without
licensed data:

  * 12 cash equities across sectors (one delisted mid-sample, one listed late)
  * a split and several dividends
  * an index (SYNNIFTY) with point-in-time membership
  * monthly index futures (3 serial contracts) priced with cost of carry
  * monthly index option chains with a skewed, regime-dependent volatility surface,
    realistic zero-volume days in the wings, and settlement at intrinsic value
  * 1-minute bars for two equities over the last 40 sessions
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from nseql.analytics.greeks import bs_price
from nseql.data.store import Dataset, DatasetWriter
from nseql.instruments.models import CorporateAction, IndexMembership, Instrument, Kind

FIXTURE_ID = "synthetic_v1"
FIXTURE_SEED = 20240101
RATE = 0.065
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

EQUITIES = [  # symbol, sector, start price, beta, idio vol (daily), drift (daily), mean-reversion strength
    ("SYNBANK", "Financials", 1450.0, 1.15, 0.011, 0.0004, 0.00),
    ("SYNIT", "IT", 3200.0, 0.90, 0.012, 0.0005, 0.00),
    ("SYNAUTO", "Auto", 720.0, 1.05, 0.014, 0.0002, 0.00),
    ("SYNPHARMA", "Pharma", 880.0, 0.60, 0.012, 0.0003, 0.04),
    ("SYNFMCG", "FMCG", 2300.0, 0.55, 0.008, 0.0003, 0.06),
    ("SYNENERGY", "Energy", 2100.0, 1.00, 0.012, 0.0003, 0.00),
    ("SYNMETAL", "Metals", 410.0, 1.40, 0.018, 0.0001, 0.00),
    ("SYNINFRA", "Infra", 1250.0, 1.20, 0.013, 0.0004, 0.00),
    ("SYNTELCO", "Telecom", 560.0, 0.80, 0.013, 0.0002, 0.03),
    ("SYNCEMENT", "Cement", 5200.0, 0.95, 0.012, 0.0002, 0.00),
    ("SYNRETAIL", "Retail", 310.0, 1.10, 0.020, -0.0006, 0.00),   # delisted mid-sample
    ("SYNMEDIA", "Media", 240.0, 0.85, 0.019, 0.0003, 0.00),      # listed late
]
DELIST = {"SYNRETAIL": 0.55}   # fraction of the calendar after which it stops trading
LATE = {"SYNMEDIA": 0.30}


def _tick(x, tick=0.05):
    return np.maximum(np.round(np.asarray(x) / tick) * tick, tick).round(2)


def _calendar(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _last_thursday(y: int, m: int, cal: set[date]) -> date:
    d = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    while d.weekday() != 3:
        d -= timedelta(days=1)
    while d not in cal:
        d -= timedelta(days=1)
    return d


def _ohlc(rng, close: np.ndarray, vol_d: np.ndarray, first_open: float):
    n = len(close)
    gap = rng.normal(0, 0.35, n) * vol_d
    prev = np.concatenate([[first_open], close[:-1]])
    opn = prev * np.exp(gap)
    hi = np.maximum(opn, close) * np.exp(np.abs(rng.normal(0, 0.45, n)) * vol_d)
    lo = np.minimum(opn, close) * np.exp(-np.abs(rng.normal(0, 0.45, n)) * vol_d)
    return opn, hi, lo


def generate_fixture(data_dir: Path, *, seed: int = FIXTURE_SEED, dataset_id: str = FIXTURE_ID,
                     start: date = date(2021, 1, 1), end: date = date(2023, 12, 29),
                     options_from: date = date(2022, 1, 1), overwrite: bool = False) -> Dataset:
    rng = np.random.default_rng(seed)
    cal = _calendar(start, end)
    n = len(cal)
    ts = pd.to_datetime([f"{d.isoformat()} 15:30:00" for d in cal])
    w = DatasetWriter(data_dir, dataset_id, synthetic=True, overwrite=overwrite,
                      description="SYNTHETIC deterministic fixture universe (not real market data)",
                      source="nseql.data.fixtures.generate_fixture", licence="generated; no third-party rights",
                      redistribution_allowed=True, created_at="1970-01-01T00:00:00",
                      generator={"seed": seed, "start": start.isoformat(), "end": end.isoformat(),
                                 "calendar": "weekdays only; no exchange holidays are modelled"})

    # ---- market factor with a 3-state volatility regime (persistent Markov chain)
    regime_vol = np.array([0.006, 0.011, 0.021])
    regime_drift = np.array([0.0005, 0.0001, -0.0012])
    P = np.array([[0.985, 0.013, 0.002], [0.020, 0.965, 0.015], [0.010, 0.050, 0.940]])
    reg = np.zeros(n, dtype=int)
    reg[0] = 1
    u = rng.random(n)
    for i in range(1, n):
        reg[i] = int(np.searchsorted(np.cumsum(P[reg[i - 1]]), u[i]))
    mkt = regime_drift[reg] + regime_vol[reg] * rng.standard_normal(n)

    # ---- equities
    closes: dict[str, np.ndarray] = {}
    live: dict[str, tuple[int, int]] = {}
    split_i = int(n * 0.45)   # SYNIT 1:2 split
    for sym, sector, p0, beta, ivol, drift, mr in EQUITIES:
        i0 = int(n * LATE[sym]) if sym in LATE else 0
        i1 = int(n * DELIST[sym]) if sym in DELIST else n
        m = i1 - i0
        eps = rng.standard_normal(m) * ivol
        mom = np.zeros(m)                      # mild return autocorrelation -> trends exist
        for i in range(1, m):
            mom[i] = 0.90 * mom[i - 1] + 0.10 * eps[i - 1]
        logp = np.zeros(m)
        anchor = 0.0
        for i in range(1, m):
            r = 0.4 * drift + beta * mkt[i0 + i] + eps[i] + 0.6 * mom[i] - mr * (logp[i - 1] - anchor)
            anchor += 0.4 * drift + beta * mkt[i0 + i] * 0.5
            logp[i] = logp[i - 1] + r
        adj_close = p0 * np.exp(logp)
        vol_d = 0.6 * (ivol + beta * regime_vol[reg[i0:i1]])
        opn, hi, lo = _ohlc(rng, adj_close, vol_d, p0)
        factor = np.ones(m)
        if sym == "SYNIT":                     # raw prices are 2x before the split ex-date
            factor[: split_i - i0] = 2.0
            w.add_corporate_action(CorporateAction(sym, cal[split_i], "split", ratio=2.0))
        base_vol = 4e8 / p0
        volume = (base_vol * np.exp(rng.normal(0, 0.35, m)) * (1 + 25 * np.abs(np.diff(logp, prepend=0)))) / factor
        df = pd.DataFrame({"ts": ts[i0:i1], "open": _tick(opn * factor), "high": _tick(hi * factor),
                           "low": _tick(lo * factor), "close": _tick(adj_close * factor),
                           "volume": volume.astype("int64"), "oi": 0})
        df["high"] = df[["open", "high", "close"]].max(axis=1)
        df["low"] = df[["open", "low", "close"]].min(axis=1)
        w.write_bars(sym, "1d", df)
        w.add_instrument(Instrument(sym, Kind.EQ, segment="CM", sector=sector, price_band_pct=0.20,
                                    listed_from=cal[i0], listed_to=cal[i1 - 1] if i1 < n else None,
                                    isin=f"SYN{abs(hash_str(sym)) % 10**9:09d}"))
        w.add_membership(IndexMembership("SYNNIFTY", sym, cal[i0], cal[i1 - 1] if i1 < n else None))
        closes[sym] = adj_close
        live[sym] = (i0, i1)
        if sym in ("SYNBANK", "SYNFMCG", "SYNENERGY"):
            for k in range(1, 4):
                j = int(n * (0.2 + 0.27 * (k - 1))) + (3 if sym == "SYNFMCG" else 0)
                if i0 < j < i1:
                    w.add_corporate_action(CorporateAction(sym, cal[j], "dividend",
                                                           amount=round(float(adj_close[j - i0]) * 0.006, 1)))
        if sym in ("SYNBANK", "SYNIT"):
            _intraday(w, rng, sym, df.tail(40), float(ivol + beta * 0.011))

    # ---- index: equal-weighted return of the constituents live on each day
    ret = np.zeros(n)
    cnt = np.zeros(n)
    for sym, c in closes.items():
        i0, i1 = live[sym]
        r = np.diff(np.log(c))
        ret[i0 + 1:i1] += r
        cnt[i0 + 1:i1] += 1
    idx_close = 15000.0 * np.exp(np.cumsum(ret / np.maximum(cnt, 1)))
    idx_vol = 0.5 * regime_vol[reg] + 0.003
    io, ih, il = _ohlc(rng, idx_close, idx_vol, 15000.0)
    idx = pd.DataFrame({"ts": ts, "open": _tick(io), "high": _tick(ih), "low": _tick(il),
                        "close": _tick(idx_close), "volume": 0, "oi": 0})
    idx["high"] = idx[["open", "high", "close"]].max(axis=1)
    idx["low"] = idx[["open", "low", "close"]].min(axis=1)
    w.write_bars("SYNNIFTY", "1d", idx)
    w.add_instrument(Instrument("SYNNIFTY", Kind.INDEX, segment="IDX", listed_from=cal[0]))

    # ---- derivatives
    calset = set(cal)
    pos = {d: i for i, d in enumerate(cal)}
    expiries = sorted({_last_thursday(d.year, d.month, calset) for d in cal})
    expiries = [e for e in expiries if e <= cal[-1]]
    ann_vol = regime_vol[reg] * math.sqrt(252)
    iv_base = pd.Series(ann_vol).ewm(span=10).mean().to_numpy() * 1.12 + 0.02   # IV > realised (vol risk premium)
    O, H, L, C = (idx[c].to_numpy() for c in ("open", "high", "low", "close"))
    for e in expiries:
        ei = pos[e]
        # futures: listed ~3 months before expiry
        fi = max(ei - 63, 0)
        sl = slice(fi, ei + 1)
        tau = np.array([(e - cal[i]).days / 365 for i in range(fi, ei + 1)])
        carry = np.exp(RATE * tau)
        basis = 1 + rng.normal(0, 0.0004, len(tau)) * (tau > 0)
        near = np.clip(1.5 - tau * 12, 0.05, 1.0)       # activity concentrates in the near month
        fsym = f"SYNNIFTY{e:%y}{MON[e.month - 1]}FUT"
        fdf = pd.DataFrame({"ts": ts[sl], "open": _tick(O[sl] * carry * basis), "high": _tick(H[sl] * carry),
                            "low": _tick(L[sl] * carry), "close": _tick(C[sl] * carry * basis),
                            "volume": (2.5e6 * near * np.exp(rng.normal(0, 0.25, len(tau)))).astype("int64") // 50 * 50,
                            "oi": (9e6 * near).astype("int64") // 50 * 50})
        fdf["high"] = fdf[["open", "high", "close"]].max(axis=1)
        fdf["low"] = fdf[["open", "low", "close"]].min(axis=1)
        w.write_bars(fsym, "1d", fdf)
        w.add_instrument(Instrument(fsym, Kind.FUT, segment="FO", lot_size=50, underlying="SYNNIFTY",
                                    underlying_kind="INDEX", expiry=e, listed_from=cal[fi], listed_to=e))
        if e < options_from:
            continue
        # options: listed 45 sessions before expiry, 25 strikes around spot at listing
        oi0 = max(ei - 45, 0)
        osl = slice(oi0, ei + 1)
        m = ei + 1 - oi0
        tau = np.array([(e - cal[i]).days / 365 for i in range(oi0, ei + 1)])
        atm = round(C[oi0] / 100) * 100
        for strike in [atm + 100 * j for j in range(-12, 13)]:
            for kind in ("CE", "PE"):
                px = np.zeros((4, m))
                for j in range(m):
                    s_close = C[oi0 + j]
                    t = tau[j]
                    mny = math.log(strike / s_close) / max(math.sqrt(max(t, 1 / 365)), 1e-6)
                    iv = iv_base[oi0 + j] * (1 + 0.9 * max(-mny, 0) + 0.25 * max(mny, 0))
                    s_hi, s_lo = (H[oi0 + j], L[oi0 + j]) if kind == "CE" else (L[oi0 + j], H[oi0 + j])
                    t_open = t + 0.25 / 365 if t > 0 else 0.25 / 365
                    px[0, j] = bs_price(O[oi0 + j], strike, t_open, iv, RATE, kind)
                    px[1, j] = bs_price(s_hi, strike, t_open if t == 0 else t, iv, RATE, kind)
                    px[2, j] = bs_price(s_lo, strike, t_open if t == 0 else t, iv, RATE, kind)
                    px[3, j] = bs_price(s_close, strike, t, iv, RATE, kind)   # expiry-day close = intrinsic
                dist = np.abs(strike - C[osl]) / C[osl]
                liq = np.exp(-dist * 60) * np.clip(1.6 - tau * 14, 0.08, 1.0)
                vol = (6e6 * liq * np.exp(rng.normal(0, 0.4, m))).astype("int64") // 50 * 50
                vol[liq < 0.02] = 0                       # illiquid wings do not trade every day
                odf = pd.DataFrame({"ts": ts[osl], "open": _tick(px[0]), "high": _tick(px[1]), "low": _tick(px[2]),
                                    "close": _tick(px[3]), "volume": vol,
                                    "oi": (4e6 * liq).astype("int64") // 50 * 50})
                odf["high"] = odf[["open", "high", "close"]].max(axis=1)
                odf["low"] = odf[["open", "low", "close"]].min(axis=1)
                osym = f"SYNNIFTY{e:%y}{MON[e.month - 1]}{int(strike)}{kind}"
                w.write_bars(osym, "1d", odf)
                w.add_instrument(Instrument(osym, Kind.OPT, segment="FO", lot_size=50, underlying="SYNNIFTY",
                                            underlying_kind="INDEX", expiry=e, strike=float(strike),
                                            option_type=kind, listed_from=cal[oi0], listed_to=e))
    return w.finish()


def hash_str(s: str) -> int:
    """Stable (non-salted) string hash."""
    h = 0
    for ch in s:
        h = (h * 131 + ord(ch)) % (2**31)
    return h


def _intraday(w: DatasetWriter, rng, sym: str, daily: pd.DataFrame, vol_d: float) -> None:
    """1-minute bars: a Brownian bridge from the daily open to the daily close, kept inside the
    daily range. Bar timestamps are bar-close times (09:16 ... 15:30)."""
    rows = []
    nb = 375
    shape = 1.0 + 1.8 * np.exp(-np.arange(nb) / 25.0) + 1.2 * np.exp(-(nb - 1 - np.arange(nb)) / 30.0)
    for _, d in daily.iterrows():
        steps = rng.standard_normal(nb) * vol_d / math.sqrt(nb) * np.sqrt(shape / shape.mean())
        walk = np.cumsum(steps)
        target = math.log(d.close / d.open)
        path = d.open * np.exp(walk + (target - walk[-1]) * np.arange(1, nb + 1) / nb)
        path = np.clip(path, d.low, d.high)
        o = np.concatenate([[d.open], path[:-1]])
        wig = np.abs(rng.normal(0, vol_d / math.sqrt(nb) * 0.6, nb))
        hi = np.minimum(np.maximum(o, path) * (1 + wig), d.high)
        lo = np.maximum(np.minimum(o, path) * (1 - wig), d.low)
        vshare = shape / shape.sum() * np.exp(rng.normal(0, 0.3, nb))
        vol = (d.volume * vshare / vshare.sum()).astype("int64")
        day = pd.Timestamp(d.ts).normalize()
        t = day + pd.Timedelta(hours=9, minutes=15) + pd.to_timedelta(np.arange(1, nb + 1), unit="min")
        df = pd.DataFrame({"ts": t, "open": _tick(o), "high": _tick(hi), "low": _tick(lo), "close": _tick(path),
                           "volume": vol, "oi": 0})
        df["high"] = df[["open", "high", "close"]].max(axis=1)
        df["low"] = df[["open", "low", "close"]].min(axis=1)
        rows.append(df)
    w.write_bars(sym, "1m", pd.concat(rows, ignore_index=True))


def ensure_fixture(data_dir: Path) -> Dataset:
    root = Path(data_dir) / FIXTURE_ID
    if (root / "manifest.json").exists():
        return Dataset(root)
    return generate_fixture(Path(data_dir))

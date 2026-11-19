"""Market context: the backdrop a stock trades in, from data Kite already serves.

    India VIX            expected volatility (fear gauge)              NSE index,      known at 15:30 the same day
    NIFTY 50 + sectors   how broad a move is                           NSE indices,    known at 15:30 the same day
    USDINR futures       rupee strength                                NSE currency,   closes 17:00  -> used from the NEXT day
    MCX crude, gold      oil shock / flight to safety                  MCX,            closes 23:30  -> used from the NEXT day

The lag matters. A daily candle is stamped with its date, but a currency or commodity candle for
day d is only complete hours after the stock market closed on d. A rule that reads it at the 15:30
close of d would be reading the future, so those series are shifted to the next trading day.

Context lives in its own dataset (e.g. `context_v1`), never mixed into a trading dataset, so it can
never be mistaken for something to trade. Global markets (S&P 500, US yields) are NOT included:
Kite does not serve them and no other source has been chosen.
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from nseql.data.store import Dataset, DatasetWriter
from nseql.instruments.models import Instrument, Kind


@dataclass(frozen=True)
class Series:
    name: str              # symbol inside the context dataset
    exchange: str          # Kite exchange of the instrument master
    lookup: str            # index tradingsymbol, or futures underlying name
    future: bool = False   # True -> nearest contract, continuous history
    lag: int = 0           # trading days after its date that a value may be used
    role: str = ""


SERIES: tuple[Series, ...] = (
    Series("INDIAVIX", "NSE", "INDIA VIX", role="volatility"),
    Series("NIFTY50", "NSE", "NIFTY 50", role="market"),
    *(Series(f"NIFTY{s.replace(' ', '')}", "NSE", f"NIFTY {s}", role="sector")
      for s in ("BANK", "IT", "PHARMA", "AUTO", "FMCG", "METAL", "ENERGY", "REALTY")),
    Series("USDINR", "CDS", "USDINR", future=True, lag=1, role="currency"),
    Series("CRUDEOIL", "MCX", "CRUDEOIL", future=True, lag=1, role="commodity"),
    Series("GOLD", "MCX", "GOLD", future=True, lag=1, role="commodity"),
)
BY_NAME = {s.name: s for s in SERIES}

# Context states used as filters by the screener. 1 = "conditions OK, trading allowed".
STATES = {
    "ctx_vix_calm": "India VIX below its 1-year 70th percentile",
    "ctx_no_vix_spike": "India VIX has not jumped more than 15% in 5 days",
    "ctx_broad": "at least half the sector indices are above their 50-day average",
    "ctx_inr_stable": "rupee has not weakened more than 1% against the dollar in 20 days",
    "ctx_crude_calm": "crude oil has not risen more than 8% in 20 days",
}
# Scheduled-event states (nseql.data.scheduled): flat over the 2 trading days before a known event.
EVENT_STATES = {
    "ctx_no_results": "flat in a stock over its results announcement",
    "ctx_no_macro_event": "flat over RBI policy, the Union Budget and US Fed decisions",
    "ctx_no_expiry": "flat over monthly F&O expiry",
}


def _resolve(api, s: Series, today: date) -> dict | None:
    df = api.instruments(s.exchange)
    if s.future:
        f = df[(df["name"].astype(str).str.upper() == s.lookup) & (df["instrument_type"] == "FUT")].copy()
        f = f[pd.to_datetime(f["expiry"]).dt.date >= today]
        if f.empty:
            return None
        r = f.sort_values("expiry").iloc[0]
    else:
        f = df[(df["segment"] == "INDICES") & ((df["tradingsymbol"].str.upper() == s.lookup) | (df["name"].astype(str).str.upper() == s.lookup))]
        if f.empty:
            return None
        r = f.iloc[0]
    return {"token": int(r["instrument_token"]), "tradingsymbol": str(r["tradingsymbol"])}


def import_context(api, start: date, end: date, data_dir: Path, dataset_id: str, *, licence: str,
                   names: list[str] | None = None, today: date | None = None, progress=print) -> tuple[Dataset, list[str]]:
    """Pull the context series from Kite into a new dataset. Returns (dataset, names not available)."""
    if not licence.strip():
        raise ValueError("a licence / entitlement statement is required")
    wanted = [BY_NAME[n] for n in (names or [s.name for s in SERIES])]
    w = DatasetWriter(data_dir, dataset_id, synthetic=False, licence=licence, redistribution_allowed=False,
                      description=f"Market context series {start} → {end}", source="Zerodha Kite Connect historical API (api.kite.trade)",
                      generator={"context": {s.name: {"lag": s.lag, "role": s.role, "exchange": s.exchange} for s in wanted}})
    got, missing = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for s in wanted:
            try:
                hit = _resolve(api, s, today or date.today())
            except Exception as e:                       # e.g. no MCX/CDS segment on the account
                hit = None
                progress(f"{s.name}: instrument list for {s.exchange} unavailable ({type(e).__name__}: {e})")
            if hit is None:
                missing.append(s.name)
                continue
            df = api.candles(hit["token"], "1d", datetime.combine(start, datetime.min.time()),
                             datetime.combine(end, datetime.max.time().replace(microsecond=0)), continuous=s.future)
            if df.empty:
                missing.append(s.name)
                progress(f"{s.name}: no candles")
                continue
            raw = Path(tmp) / f"kite_ctx_{s.name}_{start}_{end}.json"
            raw.write_text(json.dumps({"instrument_token": hit["token"], "tradingsymbol": hit["tradingsymbol"],
                                       "continuous": s.future, "candles": api.last_raw}))
            w.add_raw(raw)
            w.write_bars(s.name, "1d", df)
            w.add_instrument(Instrument(s.name, Kind.INDEX, exchange=s.exchange, segment="CTX", tick_size=0.0025 if s.future else 0.05,
                                        listed_from=df["ts"].iloc[0].date()))
            got.append(s.name)
            progress(f"{s.name}: {len(df)} days {df['ts'].iloc[0]:%Y-%m-%d} → {df['ts'].iloc[-1]:%Y-%m-%d}")
    if not got:
        raise ValueError("Kite returned no context series")
    w.audit(action="context_import", imported=got, not_imported=missing,
            caveats="futures are Kite's continuous series (rolls are not adjusted); currency and commodity values are used from the next trading day")
    return w.finish(), missing


def _lags(ctx: Dataset) -> dict[str, int]:
    meta = (ctx.manifest.generator or {}).get("context", {})
    return {s: int(meta.get(s, {}).get("lag", BY_NAME[s].lag if s in BY_NAME else 1)) for s in ctx.symbols("1d")}


def _aligned(ctx: Dataset, name: str, dates: pd.DatetimeIndex, lag: int) -> pd.Series:
    """Close of `name` as usable at the close of each target date: lag 0 -> same day allowed, lag 1 -> strictly earlier."""
    df = ctx.bars(name, "1d")[["ts", "close"]]
    df["ts"] = df["ts"].dt.normalize().astype("datetime64[ns]")
    tgt = pd.DataFrame({"ts": dates.normalize().astype("datetime64[ns]")})
    m = pd.merge_asof(tgt, df.sort_values("ts"), on="ts", allow_exact_matches=lag == 0, direction="backward")
    return pd.Series(m["close"].to_numpy(), index=dates)


def features(ctx: Dataset, dates) -> pd.DataFrame:
    """Context features and states for each target date, point-in-time. Missing data -> NaN features and
    state 1 (a filter with no data does not block trading; coverage is reported separately)."""
    dates = pd.DatetimeIndex(pd.to_datetime(list(dates)))
    lags = _lags(ctx)
    have = set(ctx.symbols("1d"))
    out = pd.DataFrame(index=dates)
    if "INDIAVIX" in have:
        v = _aligned(ctx, "INDIAVIX", dates, lags["INDIAVIX"])
        out["vix"] = v
        out["vix_pct"] = v.rolling(250, min_periods=120).apply(lambda a: (a[:-1] < a[-1]).mean(), raw=True)
        out["vix_chg5"] = v / v.shift(5) - 1
    sectors = [s.name for s in SERIES if s.role == "sector" and s.name in have]
    if sectors:
        above = []
        for s in sectors:
            c = _aligned(ctx, s, dates, lags[s])
            above.append((c > c.rolling(50, min_periods=50).mean()).where(c.rolling(50, min_periods=50).mean().notna()))
        out["breadth"] = pd.concat(above, axis=1).mean(axis=1)
    for name, col in (("USDINR", "usdinr_ret20"), ("CRUDEOIL", "crude_ret20"), ("GOLD", "gold_ret20")):
        if name in have:
            c = _aligned(ctx, name, dates, lags[name])
            out[col] = c / c.shift(20) - 1
    rules = {"ctx_vix_calm": ("vix_pct", lambda x: x < 0.7), "ctx_no_vix_spike": ("vix_chg5", lambda x: x < 0.15),
             "ctx_broad": ("breadth", lambda x: x >= 0.5), "ctx_inr_stable": ("usdinr_ret20", lambda x: x < 0.01),
             "ctx_crude_calm": ("crude_ret20", lambda x: x < 0.08)}
    for st, (col, f) in rules.items():
        x = out[col] if col in out else pd.Series(np.nan, index=dates)
        out[st] = np.where(x.isna(), 1, f(x)).astype(np.int8)
        out[f"{st}_known"] = x.notna().to_numpy()
    return out


def coverage(feat: pd.DataFrame) -> dict[str, float]:
    """Share of dates each state is actually based on data (the rest default to 'allowed')."""
    return {st: round(float(feat[f"{st}_known"].mean()), 3) for st in STATES}


def generate_context_fixture(data_dir: Path, start: date, end: date, *, seed: int = 7, dataset_id: str = "context_synthetic") -> Dataset:
    """SYNTHETIC context series, statistically unrelated to any price fixture: a filter built from them
    should NOT improve a strategy beyond chance. Used by tests and as a negative control."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, end)
    w = DatasetWriter(data_dir, dataset_id, synthetic=True, overwrite=True, licence="generated; no third-party rights", redistribution_allowed=True,
                      description="SYNTHETIC market context (random; unrelated to any price data)", source="nseql.data.context.generate_context_fixture",
                      created_at="1970-01-01T00:00:00", generator={"seed": seed, "context": {s.name: {"lag": s.lag, "role": s.role} for s in SERIES}})
    for s in SERIES:
        if s.name == "INDIAVIX":
            x = np.empty(len(days)); x[0] = 15
            for i in range(1, len(days)):
                x[i] = max(8.0, x[i - 1] + 0.08 * (15 - x[i - 1]) + rng.normal(0, 0.9))
        else:
            base = {"USDINR": 80.0, "CRUDEOIL": 6000.0, "GOLD": 60000.0}.get(s.name, 20000.0)
            x = base * np.exp(np.cumsum(rng.normal(0.0002, 0.006 if s.name == "USDINR" else 0.012, len(days))))
        df = pd.DataFrame({"ts": days + pd.Timedelta(hours=15, minutes=30), "open": x, "high": x * 1.005, "low": x * 0.995,
                           "close": x, "volume": 0})
        w.write_bars(s.name, "1d", df)
        w.add_instrument(Instrument(s.name, Kind.INDEX, segment="CTX", listed_from=days[0].date()))
    return w.finish()

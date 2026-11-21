"""Import chart data exported from TradingView ("Export chart data…" on the chart menu).

TradingView has no public data API and its terms forbid automated scraping, so this reads only files the
operator exported by hand. One file per symbol and timeframe; the default file name looks like
``NSE_RELIANCE, 1D.csv`` (exchange_symbol, interval). Columns: ``time`` (Unix seconds in UTC by default,
or ISO text if "ISO time" was chosen), ``open``, ``high``, ``low``, ``close`` and usually ``Volume``;
indicator columns on the chart are ignored. Bars are stamped at their START by TradingView; the platform
stores bar END (daily bars at 15:30 IST).

Check TradingView's terms for your plan before using exported data in other software.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from nseql.data.store import Dataset, DatasetWriter
from nseql.instruments.calendar import INTERVAL_MINUTES, is_intraday
from nseql.instruments.models import Instrument, Kind

_NAME = re.compile(r"^(?:(?P<ex>[A-Z]+)_)?(?P<sym>[A-Z0-9&\-.]+?)(?:,\s*(?P<iv>\w+))?(?:\s*\(\d+\))?$")
_IV = {"1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m", "60": "1h", "1D": "1d", "D": "1d", "1W": "1w", "W": "1w"}
INDEX_NAMES = {"NIFTY", "NIFTY50", "BANKNIFTY", "CNXIT", "FINNIFTY", "INDIAVIX", "CNX100", "CNX500", "NIFTYJR", "CNXMIDCAP"}


def parse_name(path: Path) -> tuple[str, str | None, str | None]:
    """'NSE_RELIANCE, 1D.csv' -> ('RELIANCE', '1d', 'NSE')."""
    m = _NAME.match(Path(path).stem.strip())
    if not m:
        raise ValueError(f"cannot read a symbol from the file name {Path(path).name!r}; rename it like 'NSE_RELIANCE, 1D.csv'")
    iv = m.group("iv")
    return m.group("sym"), (_IV.get(iv.upper()) if iv else None), m.group("ex")


def read_export(path: Path, interval: str) -> pd.DataFrame:
    raw = pd.read_csv(path)
    cols = {c.strip().lower(): c for c in raw.columns}
    missing = [k for k in ("time", "open", "high", "low", "close") if k not in cols]
    if missing:
        raise ValueError(f"{Path(path).name}: not a TradingView export (missing columns {missing}); found {list(raw.columns)}")
    t = raw[cols["time"]]
    if pd.api.types.is_numeric_dtype(t):
        mag = float(t.abs().median())                         # Unix time: ~1.7e9 s, ~1.7e12 ms, ~1.7e15 us
        unit = "s" if mag < 1e11 else "ms" if mag < 1e14 else "us"
        ts = pd.to_datetime(t, unit=unit, utc=True)
    else:
        ts = pd.to_datetime(t, utc=True)
    ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    lo, hi = ts.min(), ts.max()
    if lo < pd.Timestamp("1994-01-01") or hi > pd.Timestamp.now() + pd.Timedelta(days=2):
        raise ValueError(f"{Path(path).name}: timestamps run from {lo} to {hi}, which cannot be NSE data; "
                         "check the export's time format (Unix seconds or ISO)")
    ts = ts + pd.Timedelta(minutes=INTERVAL_MINUTES[interval]) if is_intraday(interval) else ts.dt.normalize() + pd.Timedelta(hours=15, minutes=30)
    df = pd.DataFrame({"ts": ts, **{k: pd.to_numeric(raw[cols[k]], errors="coerce") for k in ("open", "high", "low", "close")}})
    df["volume"] = pd.to_numeric(raw[cols["volume"]], errors="coerce").fillna(0) if "volume" in cols else 0
    return df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


def import_tradingview(files: list[Path], data_dir: Path, dataset_id: str, *, licence: str, interval: str | None = None,
                       overwrite: bool = False) -> tuple[Dataset, list[str]]:
    """Returns (dataset, notes). Every file must have the same interval (from its name, or `interval`)."""
    if not licence.strip():
        raise ValueError("a licence / entitlement statement is required (e.g. 'TradingView export, personal research')")
    if not files:
        raise ValueError("no files given")
    parsed = []
    for f in files:
        sym, iv, ex = parse_name(f)
        iv = interval or iv
        if iv is None:
            raise ValueError(f"{Path(f).name}: interval not in the file name; pass --interval")
        if ex and ex not in ("NSE", "BSE", "NSE_DLY"):
            raise ValueError(f"{Path(f).name}: exchange {ex} is not NSE/BSE")
        parsed.append((Path(f), sym, iv))
    ivs = {p[2] for p in parsed}
    if len(ivs) > 1:
        raise ValueError(f"files mix intervals {sorted(ivs)}; import each interval as its own dataset")
    iv = ivs.pop()
    w = DatasetWriter(data_dir, dataset_id, synthetic=False, licence=licence, overwrite=overwrite, redistribution_allowed=False,
                      description=f"TradingView chart export ({iv})", source="TradingView 'Export chart data' CSV files (manual export)")
    notes = []
    for path, sym, _ in parsed:
        df = read_export(path, iv)
        if df.empty:
            notes.append(f"{sym}: no bars")
            continue
        if (df["volume"] == 0).all():
            notes.append(f"{sym}: no Volume column in the export; volume set to 0 (liquidity limits cannot be applied)")
        w.add_raw(path)
        w.write_bars(sym, iv, df)
        kind = Kind.INDEX if sym in INDEX_NAMES else Kind.EQ
        w.add_instrument(Instrument(sym, kind, segment="IDX" if kind is Kind.INDEX else "CM", listed_from=df["ts"].iloc[0].date()))
    w.audit(action="tradingview_import", files=[p.name for p, _, _ in parsed], notes=notes,
            caveats="manual export; history length depends on the TradingView plan; no corporate-action adjustment is known")
    return w.finish(), notes

"""Importer for vendor OHLCV CSV files (one file per symbol) with explicit column mapping."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from nseql.data.store import Dataset, DatasetWriter
from nseql.instruments.calendar import INTERVAL_MINUTES, is_intraday
from nseql.instruments.models import Instrument, Kind


def import_csv(files: dict[str, Path], data_dir: Path, dataset_id: str, *, interval: str, licence: str, source: str,
               columns: dict[str, str] | None = None, tz: str | None = None, timestamp_is: str = "bar_start",
               instruments: dict[str, Instrument] | None = None, overwrite: bool = False) -> Dataset:
    """files: {symbol: csv path}. `columns` maps our names (ts, open, high, low, close, volume, oi) to
    the vendor's. `tz` is the vendor timestamp timezone if not exchange-local (converted to IST).
    Intraday vendor bars are usually stamped at bar START; we store bar END."""
    if not licence.strip():
        raise ValueError("a licence / entitlement statement is required to import market data")
    cmap = {"ts": "ts", "open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume", **(columns or {})}
    w = DatasetWriter(data_dir, dataset_id, synthetic=False, licence=licence, source=source, overwrite=overwrite,
                      description=f"CSV import ({interval})")
    for sym, path in sorted(files.items()):
        w.add_raw(Path(path))
        raw = pd.read_csv(path)
        df = pd.DataFrame({k: raw[v] for k, v in cmap.items() if v in raw})
        ts = pd.to_datetime(df["ts"])
        if tz:
            ts = ts.dt.tz_localize(tz) if ts.dt.tz is None else ts
            ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        elif ts.dt.tz is not None:
            ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        if is_intraday(interval):
            if timestamp_is == "bar_start":
                ts = ts + pd.Timedelta(minutes=INTERVAL_MINUTES[interval])
        else:
            ts = ts.dt.normalize() + pd.Timedelta(hours=15, minutes=30)
        df["ts"] = ts
        w.write_bars(sym, interval, df)
        w.add_instrument((instruments or {}).get(sym) or Instrument(sym, Kind.EQ, listed_from=ts.iloc[0].date()))
    return w.finish()

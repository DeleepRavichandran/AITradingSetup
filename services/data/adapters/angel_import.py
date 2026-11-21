"""Build a research dataset from Angel One SmartAPI historical candles (see angelone.py for limits)."""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime
from pathlib import Path

from nseql.data.adapters.angelone import AngelHistorical, resolve
from nseql.data.store import Dataset, DatasetWriter
from nseql.instruments.models import Instrument, Kind


def import_angel(api: AngelHistorical, symbols: list[str], start: date, end: date, data_dir: Path, dataset_id: str, *,
                 licence: str, interval: str = "1d", include_nifty: bool = True, cache_dir: Path | None = None, overwrite: bool = False,
                 progress=print) -> tuple[Dataset, list[str]]:
    if not licence.strip():
        raise ValueError("a licence / entitlement statement is required (e.g. 'Angel One SmartAPI, personal research')")
    master = api.instruments(cache_dir)
    wanted = list(dict.fromkeys([*symbols, *(["NIFTY 50"] if include_nifty else [])]))
    found, missing = resolve(master, wanted)
    if not found:
        raise ValueError(f"none of the requested symbols are in Angel One's instrument master: {missing}")
    w = DatasetWriter(data_dir, dataset_id, synthetic=False, overwrite=overwrite, licence=licence, redistribution_allowed=False,
                      description=f"Angel One SmartAPI candles {start} -> {end} ({interval})",
                      source="Angel One SmartAPI historical API (apiconnect.angelone.in)")
    got = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, f in enumerate(found, 1):
            df = api.candles(f["token"], interval, datetime.combine(start, datetime.min.time().replace(hour=9, minute=15)),
                             datetime.combine(end, datetime.min.time().replace(hour=15, minute=30)))
            if df.empty:
                missing.append(f["symbol"])
                progress(f"[{i}/{len(found)}] {f['symbol']}: no candles in range")
                continue
            raw = Path(tmp) / f"angel_{f['symbol']}_{interval}_{start}_{end}.json"
            raw.write_text(json.dumps({"symboltoken": f["token"], "interval": interval, "candles": api.last_raw}))
            w.add_raw(raw)
            w.write_bars(f["symbol"], interval, df)
            kind = Kind.INDEX if f["kind"] == "INDEX" else Kind.EQ
            w.add_instrument(Instrument(f["symbol"], kind, segment="IDX" if kind is Kind.INDEX else "CM", tick_size=f["tick"],
                                        listed_from=df["ts"].iloc[0].date()))
            got.append(f["symbol"])
            progress(f"[{i}/{len(found)}] {f['symbol']}: {len(df)} bars {df['ts'].iloc[0]:%Y-%m-%d} -> {df['ts'].iloc[-1]:%Y-%m-%d}")
    if not got:
        raise ValueError("Angel One returned no candles for any symbol in that range")
    w.audit(action="import_summary", imported=got, not_imported=missing,
            caveats="current listings only; no delisted names; no corporate-action adjustment applied; volume as reported by Angel One")
    return w.finish(), missing

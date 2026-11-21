"""Build a research dataset from Kite Connect historical candles.

What Kite can and cannot give you (so results are not over-read):
  * cash equities and index levels, daily or intraday bars, for instruments listed NOW;
  * NOT expired futures/options contracts — the instrument master only lists live contracts;
  * NOT point-in-time index membership, delisted securities, or a corporate-actions feed;
  * prices are whatever Kite serves; check the quality report for unexplained jumps (splits
    and bonuses) and supply corporate actions yourself before trusting multi-month returns.

Every API response is saved byte-for-byte as an immutable raw file in the dataset, and the
licence statement you pass is recorded in the manifest and shown in every report.
"""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from nseql.data.adapters.kite import KiteHistorical
from nseql.data.store import Dataset, DatasetWriter
from nseql.instruments.models import Instrument, Kind

INDEX_ALIASES = {"NIFTY50": "NIFTY 50", "NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "NIFTYBANK": "NIFTY BANK",
                 "FINNIFTY": "NIFTY FIN SERVICE", "MIDCPNIFTY": "NIFTY MID SELECT", "NIFTYNEXT50": "NIFTY NEXT 50"}


def _dataset_symbol(name: str) -> str:
    return name.replace(" ", "").replace("&", "AND")


def resolve(nse: pd.DataFrame, indices: pd.DataFrame, wanted: list[str]) -> tuple[list[dict], list[str]]:
    """Map requested names to Kite instrument tokens. Returns (found, not_found)."""
    found, missing = [], []
    eq = nse[(nse["segment"] == "NSE") & (nse["instrument_type"] == "EQ")]
    for w in wanted:
        key = w.strip().upper()
        idx_name = INDEX_ALIASES.get(key.replace(" ", ""), key)
        hit = eq[eq["tradingsymbol"] == key]
        if len(hit):
            r = hit.iloc[0]
            found.append({"symbol": key, "token": int(r["instrument_token"]), "kind": "EQ", "tick": float(r["tick_size"]), "name": r.get("name")})
            continue
        hit = indices[(indices["tradingsymbol"].str.upper() == idx_name) | (indices["name"].astype(str).str.upper() == idx_name)]
        if len(hit):
            r = hit.iloc[0]
            found.append({"symbol": _dataset_symbol(r["tradingsymbol"]), "token": int(r["instrument_token"]), "kind": "INDEX", "tick": 0.05, "name": r["tradingsymbol"]})
        else:
            missing.append(w)
    return found, missing


def import_kite(api: KiteHistorical, symbols: list[str], start: date, end: date, data_dir: Path, dataset_id: str, *,
                licence: str, interval: str = "1d", include_nifty: bool = True, overwrite: bool = False,
                progress=print) -> tuple[Dataset, list[str]]:
    if not licence.strip():
        raise ValueError("a licence / entitlement statement is required (e.g. 'Kite Connect subscription, personal research use')")
    nse = api.instruments("NSE")
    wanted = list(dict.fromkeys([*symbols, *(["NIFTY 50"] if include_nifty else [])]))
    found, missing = resolve(nse, nse[nse["segment"] == "INDICES"], wanted)
    if not found:
        raise ValueError(f"none of the requested symbols exist in Kite's NSE instrument list: {missing}")
    w = DatasetWriter(data_dir, dataset_id, synthetic=False, overwrite=overwrite, licence=licence, redistribution_allowed=False,
                      description=f"Kite Connect historical candles {start} → {end} ({interval})",
                      source="Zerodha Kite Connect historical API (api.kite.trade)")
    w.audit(action="instrument_master", exchange="NSE", rows=int(len(nse)), fetched=datetime.now().isoformat(timespec="seconds"))
    got = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, f in enumerate(found, 1):
            df = api.candles(f["token"], interval, datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.max.time().replace(microsecond=0)))
            if df.empty:
                missing.append(f["symbol"])
                progress(f"[{i}/{len(found)}] {f['symbol']}: no candles in range")
                continue
            raw = Path(tmp) / f"kite_{f['symbol']}_{interval}_{start}_{end}.json"
            raw.write_text(json.dumps({"instrument_token": f["token"], "interval": interval, "candles": api.last_raw}))
            w.add_raw(raw)
            w.write_bars(f["symbol"], interval, df)
            kind = Kind.INDEX if f["kind"] == "INDEX" else Kind.EQ
            w.add_instrument(Instrument(f["symbol"], kind, segment="IDX" if kind is Kind.INDEX else "CM", tick_size=f["tick"],
                                        listed_from=df["ts"].iloc[0].date()))
            got.append(f["symbol"])
            progress(f"[{i}/{len(found)}] {f['symbol']}: {len(df)} bars {df['ts'].iloc[0]:%Y-%m-%d} → {df['ts'].iloc[-1]:%Y-%m-%d}")
    if not got:
        raise ValueError("Kite returned no candles for any symbol in that range")
    w.audit(action="import_summary", imported=got, not_imported=missing,
            caveats="current listings only; no delisted names; no corporate-action adjustment applied; price bands unknown")
    return w.finish(), missing

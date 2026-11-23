"""Importer for NSE end-of-day "bhavcopy" files that the USER has obtained under their own
entitlement. This module never downloads anything: it parses files already on disk.

Supported layouts (detected from the header row):
  * CM legacy   SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,...
  * FO legacy   INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,...
  * UDiFF       TradDt,BizDt,Sgmt,Src,FinInstrmTp,...,TckrSymb,SctySrs,XpryDt,...,StrkPric,OptnTp,...
                (common CM and FO layout used by NSE from July 2024)

Every source file is copied byte-for-byte into the dataset's raw/ area and hashed.
Prices are stored UNADJUSTED; corporate actions must be supplied separately.

STATUS: the parsers are tested against format-conformant sample files with made-up values
(tests/fixtures). They have NOT been validated against a real exchange file in this
environment — acceptance test #1 stays BLOCKED until a real file is imported and checked.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd

from nseql.data.store import Dataset, DatasetWriter
from nseql.instruments.models import Instrument, Kind

EQUITY_SERIES = {"EQ", "BE", "BZ"}
PHYSICAL_SETTLEMENT_FROM = date(2019, 10, 1)


def _read(path: Path) -> pd.DataFrame:
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            data = z.read(name)
        df = pd.read_csv(io.BytesIO(data))
    else:
        df = pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]
    return df.loc[:, [c for c in df.columns if not c.startswith("Unnamed")]]


def detect_layout(df: pd.DataFrame) -> str:
    cols = set(df.columns)
    if {"TradDt", "TckrSymb", "ClsPric"} <= cols:
        return "udiff"
    if {"INSTRUMENT", "EXPIRY_DT", "STRIKE_PR"} <= cols:
        return "fo_legacy"
    if {"SYMBOL", "SERIES", "TOTTRDQTY", "TIMESTAMP"} <= cols:
        return "cm_legacy"
    raise ValueError(f"unrecognised bhavcopy layout; columns={sorted(cols)[:12]}")


def deriv_symbol(und: str, expiry: date, strike: float | None, typ: str | None) -> str:
    base = f"{und}{expiry:%y%b%d}".upper()
    return f"{base}FUT" if typ is None else f"{base}{strike:g}{typ}"


def _normalise(df: pd.DataFrame, layout: str, lot_sizes: dict[str, int] | None) -> pd.DataFrame:
    """-> rows: date, symbol, kind, underlying, underlying_kind, expiry, strike, option_type, lot,
    open, high, low, close, volume, oi, isin, underlying_price"""
    if layout == "cm_legacy":
        d = df[df["SERIES"].astype(str).str.strip().isin(EQUITY_SERIES)]
        return pd.DataFrame({
            "date": pd.to_datetime(d["TIMESTAMP"].astype(str).str.strip(), format="%d-%b-%Y").dt.date,
            "symbol": d["SYMBOL"].astype(str).str.strip(), "kind": "EQ", "underlying": None, "underlying_kind": None,
            "expiry": None, "strike": None, "option_type": None, "lot": 1, "open": d["OPEN"], "high": d["HIGH"],
            "low": d["LOW"], "close": d["CLOSE"], "volume": d["TOTTRDQTY"], "oi": 0,
            "isin": d["ISIN"] if "ISIN" in d else None, "underlying_price": None})
    if layout == "fo_legacy":
        d = df.copy()
        inst = d["INSTRUMENT"].astype(str).str.strip()
        und = d["SYMBOL"].astype(str).str.strip()
        typ = pd.Series([t.strip() if i.startswith("OPT") else None
                         for t, i in zip(d["OPTION_TYP"].astype(str), inst)], index=d.index, dtype=object)
        lots = und.map(lot_sizes or {})
        if lots.isna().any():
            missing = sorted(set(und[lots.isna()]))[:8]
            raise ValueError("legacy F&O bhavcopies do not carry lot sizes; pass lot_sizes={underlying: lot} "
                             f"from the exchange contract master. Missing: {missing}")
        exp = pd.to_datetime(d["EXPIRY_DT"].astype(str).str.strip(), format="%d-%b-%Y").dt.date
        return pd.DataFrame({
            "date": pd.to_datetime(d["TIMESTAMP"].astype(str).str.strip(), format="%d-%b-%Y").dt.date,
            "symbol": [deriv_symbol(u, e, s if t else None, t) for u, e, s, t in zip(und, exp, d["STRIKE_PR"], typ)],
            "kind": ["OPT" if t else "FUT" for t in typ], "underlying": und,
            "underlying_kind": ["INDEX" if i.endswith("IDX") else "EQ" for i in inst], "expiry": exp,
            "strike": [s if t else None for s, t in zip(d["STRIKE_PR"], typ)], "option_type": typ, "lot": lots.astype(int),
            "open": d["OPEN"], "high": d["HIGH"], "low": d["LOW"], "close": d["CLOSE"],
            "volume": d["CONTRACTS"] * lots.astype(int), "oi": d["OPEN_INT"], "isin": None, "underlying_price": None})
    d = df.copy()
    tp = d["FinInstrmTp"].astype(str).str.strip()
    tick = d["TckrSymb"].astype(str).str.strip()
    is_fut, is_opt = tp.isin(["IDF", "STF"]), tp.isin(["IDO", "STO"])
    is_cm = ~(is_fut | is_opt)
    if is_cm.any():
        ser = d["SctySrs"].astype(str).str.strip() if "SctySrs" in d else pd.Series("EQ", index=d.index)
        keep = (is_cm & ser.isin(EQUITY_SERIES)) | is_fut | is_opt
        d, tp, tick, is_fut, is_opt = d[keep], tp[keep], tick[keep], is_fut[keep], is_opt[keep]
    exp = pd.to_datetime(d["XpryDt"], errors="coerce").dt.date if "XpryDt" in d else pd.Series(None, index=d.index)
    typ = pd.Series([str(t).strip() if o else None for t, o in zip(d["OptnTp"], is_opt)] if "OptnTp" in d
                    else [None] * len(d), index=d.index, dtype=object)
    lot = d["NewBrdLotQty"].fillna(1).astype(int) if "NewBrdLotQty" in d else pd.Series(1, index=d.index)
    kind = ["FUT" if f else "OPT" if o else "EQ" for f, o in zip(is_fut, is_opt)]
    sym = [t if k == "EQ" else deriv_symbol(t, e, s if k == "OPT" else None, o if k == "OPT" else None)
           for t, k, e, s, o in zip(tick, kind, exp, d.get("StrkPric", pd.Series(None, index=d.index)), typ)]
    return pd.DataFrame({
        "date": pd.to_datetime(d["TradDt"]).dt.date, "symbol": sym, "kind": kind,
        "underlying": tick.where(is_fut | is_opt, None),
        "underlying_kind": ["INDEX" if t in ("IDF", "IDO") else "EQ" if t in ("STF", "STO") else None for t in tp],
        "expiry": exp.where(is_fut | is_opt, None), "strike": d["StrkPric"].where(is_opt, None) if "StrkPric" in d else None,
        "option_type": typ, "lot": lot.where(is_fut | is_opt, 1), "open": d["OpnPric"], "high": d["HghPric"],
        "low": d["LwPric"], "close": d["ClsPric"], "volume": d["TtlTradgVol"],
        "oi": d["OpnIntrst"].fillna(0) if "OpnIntrst" in d else 0, "isin": d["ISIN"] if "ISIN" in d else None,
        "underlying_price": d["UndrlygPric"] if "UndrlygPric" in d else None})


def import_bhavcopies(files: list[Path], data_dir: Path, dataset_id: str, *, licence: str, description: str = "",
                      lot_sizes: dict[str, int] | None = None, overwrite: bool = False) -> Dataset:
    """Build a daily-bar dataset from bhavcopy files. `licence` must state the basis on which
    the user holds the data (it is recorded in the manifest and shown in every report)."""
    if not licence.strip():
        raise ValueError("a licence / entitlement statement is required to import market data")
    w = DatasetWriter(data_dir, dataset_id, synthetic=False, overwrite=overwrite, licence=licence,
                      description=description or "NSE bhavcopy import", source="user-supplied NSE bhavcopy files",
                      redistribution_allowed=False)
    parts = []
    for f in sorted(map(Path, files)):
        raw = _read(f)
        layout = detect_layout(raw)
        w.add_raw(f)
        n = _normalise(raw, layout, lot_sizes)
        w.audit(action="parsed", file=f.name, layout=layout, rows_in=len(raw), rows_kept=len(n))
        parts.append(n)
    allrows = pd.concat(parts, ignore_index=True)
    bad = (allrows[["open", "high", "low", "close"]] <= 0).any(axis=1)
    if bad.any():      # contracts that did not trade are published with zero prices
        w.audit(action="dropped_untraded_rows", rows=int(bad.sum()), reason="non-positive OHLC (no trade that day)")
        allrows = allrows[~bad]
    for sym, g in allrows.groupby("symbol", sort=True):
        g = g.sort_values("date")
        first = g.iloc[0]
        k = Kind(first["kind"])
        exp = first["expiry"] if isinstance(first["expiry"], date) else None
        w.add_instrument(Instrument(
            sym, k, segment="CM" if k is Kind.EQ else "FO", lot_size=int(first["lot"]),
            underlying=first["underlying"], underlying_kind=first["underlying_kind"], expiry=exp,
            strike=float(first["strike"]) if k is Kind.OPT else None, option_type=first["option_type"] if k is Kind.OPT else None,
            listed_from=g["date"].iloc[0], listed_to=exp, isin=first["isin"] if isinstance(first["isin"], str) else None,
            settlement="physical" if (k is not Kind.EQ and first["underlying_kind"] == "EQ" and exp
                                      and exp >= PHYSICAL_SETTLEMENT_FROM) else "cash"))
        w.write_bars(sym, "1d", pd.DataFrame({
            "ts": pd.to_datetime(g["date"].astype(str) + " 15:30:00"), "open": g["open"].astype(float),
            "high": g["high"].astype(float), "low": g["low"].astype(float), "close": g["close"].astype(float),
            "volume": g["volume"].fillna(0).astype("int64"), "oi": pd.to_numeric(g["oi"], errors="coerce").fillna(0).astype("int64")}))
    # index levels: derivative rows carry the underlying's value; keep it as a close-only reference series
    und = allrows[(allrows["underlying_kind"] == "INDEX") & allrows["underlying_price"].notna()]
    for u, g in und.groupby("underlying"):
        px = g.groupby("date")["underlying_price"].last()
        px = px[px > 0]
        if len(px) and u not in w._instruments:
            w.add_instrument(Instrument(u, Kind.INDEX, segment="IDX", listed_from=px.index[0]))
            w.write_bars(u, "1d", pd.DataFrame({"ts": pd.to_datetime(pd.Series(px.index).astype(str) + " 15:30:00"),
                                                "open": px.values, "high": px.values, "low": px.values,
                                                "close": px.values, "volume": 0, "oi": 0}))
            w.audit(action="derived_index_series", symbol=u, note="close-only, taken from UndrlygPric of F&O rows")
    return w.finish()

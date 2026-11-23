"""Splits and bonuses: find them in price data, and attach them from a file.

The platform stores prices UNADJUSTED and applies corporate actions itself (the backtester splits
open positions; history is back-adjusted on read). That only works if every split and bonus in the
data is listed. Kite gives no corporate-action feed, so:

  * `suspected_actions(df)` looks for the fingerprint of a split or bonus: an overnight drop that
    matches a common ratio (1:2, 1:5, bonus 1:1 ...) almost exactly, usually with volume rising by
    about the same factor. It reports candidates; it never adds anything on its own.
  * `read_actions_csv(path)` reads the actions the operator confirmed (from the exchange's corporate
    announcements), and `attach_actions(...)` writes a NEW dataset version that carries them.
    An action is refused if the prices do not actually drop on that date, because adding a split to
    data that is already adjusted would halve the history a second time.
"""
from __future__ import annotations

import csv
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from nseql.data.store import Dataset, DatasetWriter
from nseql.instruments.models import CorporateAction

# shares held after the action per share held before: splits (face value 10->5, 10->2, 10->1, 5->1 ...)
# and bonuses (1:1 -> 2, 1:2 -> 1.5, 2:1 -> 3, 3:1 -> 4, 1:4 -> 1.25 ...)
COMMON_RATIOS = (1.25, 4 / 3, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 10.0)


def _parse_ratio(kind: str, raw: str) -> float:
    s = raw.strip().replace(" ", "")
    if "->" in s:                                         # face value: 10->2 means 1 share becomes 5
        old, new = (float(x) for x in s.split("->"))
        return old / new
    if ":" in s:                                          # bonus a:b = a new shares for every b held
        a, b = (float(x) for x in s.split(":"))
        if kind != "bonus":
            raise ValueError(f"'{raw}': a:b is bonus notation; for a split give the face values (10->2) or the share ratio (5)")
        return (a + b) / b
    return float(s)


def read_actions_csv(path: str | Path) -> list[CorporateAction]:
    """Columns: symbol, ex_date (YYYY-MM-DD), kind (split | bonus | dividend), ratio, amount.

    ratio: shares held after per share before (5 for a 10->2 face-value split), or `10->2` for a split,
    or `1:1` for a bonus. amount: rupees per share, dividends only.
    """
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for n, row in enumerate(csv.DictReader(f), 2):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            kind = row.get("kind", "").lower()
            if kind not in ("split", "bonus", "dividend"):
                raise ValueError(f"{path}:{n}: kind must be split, bonus or dividend, got {kind!r}")
            d = date.fromisoformat(row["ex_date"])
            if kind == "dividend":
                amt = float(row.get("amount") or 0)
                if amt <= 0:
                    raise ValueError(f"{path}:{n}: dividend needs a positive amount")
                out.append(CorporateAction(row["symbol"].upper(), d, kind, 1.0, amt))
            else:
                r = _parse_ratio(kind, row.get("ratio") or "")
                if r <= 1:
                    raise ValueError(f"{path}:{n}: ratio must be above 1 (shares after per share before); got {r}")
                out.append(CorporateAction(row["symbol"].upper(), d, kind, r, 0.0))
    return out


def suspected_actions(df: pd.DataFrame, symbol: str, *, tol: float = 0.04, min_drop: float = 1.2) -> list[dict]:
    """Days where the open fell from the previous close by close to a common split/bonus ratio."""
    if len(df) < 2:
        return []
    c, o, v = (df[k].to_numpy(dtype=float) for k in ("close", "open", "volume"))
    ts = pd.to_datetime(df["ts"]).dt.date.to_numpy()
    out = []
    for i in range(1, len(df)):
        if o[i] <= 0 or c[i - 1] <= 0:
            continue
        r = c[i - 1] / o[i]
        if r < min_drop:
            continue
        best = min(COMMON_RATIOS, key=lambda x: abs(r / x - 1))
        err = abs(r / best - 1)
        if err > tol:
            continue
        before = np.median(v[max(0, i - 20):i]) if i else 0
        after = np.median(v[i:i + 20])
        vol_ratio = float(after / before) if before > 0 else None
        evidence = "strong" if err < 0.015 and vol_ratio and vol_ratio > best * 0.5 else "possible"
        out.append({"symbol": symbol, "ex_date": str(ts[i]), "ratio": round(best, 4), "observed_drop": round(float(r), 4),
                    "volume_ratio": round(vol_ratio, 2) if vol_ratio else None, "evidence": evidence,
                    "kind_hint": "split or bonus (check the exchange announcement)"})
    return out


def scan_dataset(ds: Dataset, interval: str = "1d") -> list[dict]:
    """Suspected splits/bonuses that are NOT already listed in the dataset."""
    known = {(a.symbol, str(a.ex_date)) for a in ds.corporate_actions() if a.kind in ("split", "bonus")}
    out = []
    for s in ds.symbols(interval):
        if not ds.instruments[s].is_cash:
            continue
        out += [x for x in suspected_actions(ds.bars(s, interval), s) if (s, x["ex_date"]) not in known]
    return out


def check_action(ds: Dataset, a: CorporateAction, interval: str = "1d", tol: float = 0.1) -> str | None:
    """Return a reason to refuse `a`, or None if the price data is consistent with it."""
    if a.symbol not in ds.instruments:
        return f"{a.symbol} is not in dataset {ds.id}"
    if a.kind == "dividend":
        return None
    df = ds.bars(a.symbol, interval)
    d = pd.to_datetime(df["ts"]).dt.date
    after = df[d >= a.ex_date]
    before = df[d < a.ex_date]
    if after.empty or before.empty:
        return None                                           # outside the data: harmless, kept for completeness
    drop = before["close"].iloc[-1] / after["open"].iloc[0]
    if abs(drop / a.ratio - 1) > tol:
        return (f"{a.symbol} {a.ex_date}: prices fall by x{drop:.2f}, not the x{a.ratio:.2f} a {a.kind} implies. "
                "Either the date or ratio is wrong, or this data is already adjusted (then do not add it).")
    return None


def attach_actions(ds: Dataset, actions: list[CorporateAction], data_dir: Path, new_id: str, *, source: str,
                   interval: str = "1d", force: bool = False) -> Dataset:
    """Write a new dataset version = `ds` + `actions` (bars and raw files copied unchanged)."""
    if not source.strip():
        raise ValueError("give the source of the corporate actions (e.g. 'NSE corporate announcements, checked 2026-11-19')")
    problems = [p for a in actions if (p := check_action(ds, a, interval))]
    if problems and not force:
        raise ValueError("refused:\n  " + "\n  ".join(problems))
    existing = {(a.symbol, a.ex_date, a.kind) for a in ds.corporate_actions()}
    m = ds.manifest
    w = DatasetWriter(data_dir, new_id, description=f"{m.description} + corporate actions", source=m.source, licence=m.licence,
                      synthetic=m.synthetic, redistribution_allowed=m.redistribution_allowed, generator=m.generator)
    for r in ds.root.joinpath("raw").glob("*"):
        shutil.copyfile(r, w.root / "raw" / r.name)
    w.manifest.raw_files = list(m.raw_files)
    w.manifest.audit = list(m.audit)
    for inst in ds.instruments.values():
        w.add_instrument(inst)
    for a in ds.corporate_actions():
        w.add_corporate_action(a)
    added = []
    for a in actions:
        if (a.symbol, a.ex_date, a.kind) not in existing:
            w.add_corporate_action(a)
            added.append(a.to_dict())
    for mem in ds.index_membership():
        w.add_membership(mem)
    for iv in ds.intervals():
        for s in ds.symbols(iv):
            w.write_bars(s, iv, ds.bars(s, iv))
    w.audit(action="corporate_actions_attached", parent=ds.id, parent_content_hash=ds.content_hash, source=source,
            added=added, forced_despite=problems if force else [])
    return w.finish()

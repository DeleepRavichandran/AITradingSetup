"""Shared market data and indicators for the evaluation workers.

Built once per (dataset content hash, interval, cache version) and stored as plain .npy arrays that
every worker process memory-maps read-only, so 4 or 400 workers share one copy in the page cache.
All indicators are causal: row t depends only on rows <= t (checked by tests/brain/test_evaluation.py).
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from nseql.data.store import Dataset

CACHE_VERSION = "1"
SMA = (5, 10, 15, 20, 30, 40, 50, 60, 80, 100, 120, 150, 200, 250)
CHANNEL = (5, 10, 20, 40, 55, 80, 100)
ZWIN = (5, 10, 20, 30, 60)
RSIN = (2, 3, 5, 7, 14)


def raw_panel(ds: Dataset, interval: str = "1d", start=None, end=None) -> dict:
    """Split-adjusted price/volume panel [T, N] for the cash equities, plus the first index."""
    eq = sorted(s for s in ds.symbols(interval) if ds.instruments[s].is_cash)
    idxs = [s for s in ds.symbols(interval) if ds.instruments[s].kind.value == "INDEX"]
    idx = "NIFTY50" if "NIFTY50" in idxs else (idxs[0] if idxs else None)
    if not eq or idx is None:
        raise ValueError("dataset needs cash equities and one index")
    frames = {s: ds.bars(s, interval, start, end).set_index("ts") for s in [*eq, idx]}
    ts = frames[idx].index
    acts = [a for a in ds.corporate_actions() if a.kind in ("split", "bonus")]
    out = {"ts": np.array(ts.values.astype("datetime64[ns]")), "symbols": eq, "index_symbol": idx}
    for f in ("open", "high", "low", "close", "volume"):
        m = np.full((len(ts), len(eq)), np.nan)
        for j, s in enumerate(eq):
            col = frames[s][f].reindex(ts).to_numpy(dtype=float)
            g = np.ones(len(ts))
            for a in acts:
                if a.symbol == s:
                    g[ts.date < a.ex_date] *= a.ratio
            m[:, j] = col * g if f == "volume" else col / g
        out[f] = m
    out["idx_close"] = frames[idx]["close"].reindex(ts).to_numpy(dtype=float)
    return out


def _roll(df: pd.DataFrame, n: int, how: str) -> np.ndarray:
    r = df.rolling(n, min_periods=n)
    return getattr(r, how)().to_numpy()


def derive(raw: dict) -> dict:
    """Every indicator the strategy space needs, computed causally from the raw panel."""
    c = pd.DataFrame(raw["close"])
    o = raw["open"]
    out = {k: raw[k] for k in ("open", "close", "volume", "idx_close", "ts")}
    out["alive"] = (~np.isnan(raw["close"])).astype(np.int8)
    oo = np.full_like(o, np.nan)
    oo[1:] = o[1:] / o[:-1] - 1.0
    out["oo"] = np.nan_to_num(oo, nan=0.0, posinf=0.0, neginf=0.0)
    for n in SMA:
        out[f"sma_{n}"] = _roll(c, n, "mean")
    for n in CHANNEL:
        out[f"hh_{n}"] = np.vstack([np.full((1, c.shape[1]), np.nan), _roll(c, n, "max")[:-1]])   # prior n closes
        out[f"ll_{n}"] = np.vstack([np.full((1, c.shape[1]), np.nan), _roll(c, n, "min")[:-1]])
    for n in ZWIN:
        m, s = _roll(c, n, "mean"), _roll(c, n, "std")
        with np.errstate(invalid="ignore", divide="ignore"):
            out[f"z_{n}"] = np.where(s > 0, (c.to_numpy() - m) / s, 0.0)
    d = c.diff()
    for n in RSIN:
        up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        with np.errstate(invalid="ignore", divide="ignore"):
            out[f"rsi_{n}"] = np.where(dn.to_numpy() > 0, 100 - 100 / (1 + up.to_numpy() / dn.to_numpy()), np.where(up.to_numpy() > 0, 100.0, 50.0))
    lr = np.log(c).diff()
    out["rv20"] = lr.rolling(20, min_periods=20).std().to_numpy() * math.sqrt(252)
    out["rv60"] = lr.rolling(60, min_periods=60).std().to_numpy() * math.sqrt(252)
    out["tv60"] = (c * pd.DataFrame(raw["volume"])).rolling(60, min_periods=20).median().to_numpy()
    ic = pd.Series(raw["idx_close"])
    for n in (100, 200):
        out[f"idx_sma_{n}"] = ic.rolling(n, min_periods=n).mean().to_numpy()
    out["idx_rv20"] = np.log(ic).diff().rolling(20, min_periods=20).std().to_numpy() * math.sqrt(252)
    for k in ("liquid", "lowvol", "highvol"):
        src = {"liquid": out["tv60"], "lowvol": out["rv60"], "highvol": out["rv60"]}[k]
        rk = pd.DataFrame(np.where(out["alive"] > 0, src, np.nan)).rank(axis=1, pct=True).to_numpy()
        out[f"u_{k}"] = (rk <= 0.5 if k == "lowvol" else rk > 0.5).astype(np.int8)
    return out


def _events_tag(events) -> str:
    if events is None:
        return ""
    from nseql.data.scheduled import scheduled
    rows = [(e["id"], e["event_time"], e["published_at"], e["symbols"]) for e in scheduled(events)]
    return "|evt:" + hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]


def cache_dir(root: Path, ds: Dataset, interval: str = "1d", context: Dataset | None = None, events=None) -> Path:
    ctx = (f"|ctx:{context.content_hash}" if context is not None else "") + _events_tag(events)
    tag = hashlib.sha256(f"{ds.content_hash}|{interval}|{CACHE_VERSION}{ctx}".encode()).hexdigest()[:16]
    return Path(root) / f"{ds.id}_{interval}_{tag}"


def build(ds: Dataset, root: Path, interval: str = "1d", context: Dataset | None = None, events=None) -> Path:
    """context: a market-context dataset; events: an EventStore with scheduled events. Either adds the
    point-in-time ctx_* state arrays the context extension of the configuration universe filters on
    (a state with no data is all-ones, i.e. it never blocks)."""
    d = cache_dir(root, ds, interval, context, events)
    if (d / "meta.json").exists():
        return d
    raw = raw_panel(ds, interval)
    arr = derive(raw)
    ctx_meta = None
    if context is not None or events is not None:
        from nseql.data.context import STATES, coverage, features
        from nseql.data.scheduled import blackout
        T = len(raw["ts"])
        ctx_meta = {"dataset": None, "content_hash": None, "synthetic": False, "coverage": {}, "on_fraction": {}}
        if context is not None:
            feat = features(context, pd.to_datetime(raw["ts"]))
            for st in STATES:
                arr[st] = feat[st].to_numpy(dtype=np.int8)
            ctx_meta.update(dataset=context.id, content_hash=context.content_hash, synthetic=context.synthetic, coverage=coverage(feat),
                            on_fraction={st: round(float(feat[st].mean()), 3) for st in STATES})
        else:
            for st in STATES:
                arr[st] = np.ones(T, dtype=np.int8)
                ctx_meta["coverage"][st] = 0.0
        ev = blackout(events, raw["ts"], raw["symbols"])
        for st in ("ctx_no_results", "ctx_no_macro_event", "ctx_no_expiry"):
            arr[st] = ev[st]
            ctx_meta["on_fraction"][st] = round(float(ev[st].mean()), 3)
        ctx_meta["events"] = ev["counts"]
    tmp = d.with_suffix(".tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    for k, v in arr.items():
        np.save(tmp / f"{k}.npy", np.ascontiguousarray(v))
    (tmp / "meta.json").write_text(json.dumps({"dataset": ds.id, "content_hash": ds.content_hash, "interval": interval,
                                               "symbols": raw["symbols"], "index": raw["index_symbol"], "rows": int(len(raw["ts"])),
                                               "synthetic": ds.synthetic, "version": CACHE_VERSION, "context": ctx_meta}))
    tmp.rename(d)
    return d


class Panel(dict):
    """Arrays by name (memory-mapped when loaded from disk) plus metadata."""
    meta: dict

    def dates(self) -> list[date]:
        return [pd.Timestamp(x).date() for x in self["ts"]]

    def truncated(self, rows: int) -> "Panel":
        p = Panel({k: (v[:rows] if hasattr(v, "shape") and v.shape and v.shape[0] == self["ts"].shape[0] else v) for k, v in self.items()})
        p.meta = self.meta
        return p


def load(d: Path) -> Panel:
    d = Path(d)
    p = Panel({f.stem: np.load(f, mmap_mode="r") for f in d.glob("*.npy")})
    p.meta = json.loads((d / "meta.json").read_text())
    return p


def from_arrays(arr: dict, meta: dict | None = None) -> Panel:
    p = Panel(arr)
    p.meta = meta or {}
    return p

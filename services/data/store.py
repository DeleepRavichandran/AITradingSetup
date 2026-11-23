"""Dataset store: immutable raw files, research-ready Parquet bars, provenance manifest.

Layout of one dataset version:

    <data_dir>/<dataset_id>/
        manifest.json            provenance, licence, synthetic flag, per-file sha256
        instruments.json         instrument / contract master with listing periods
        corporate_actions.json
        index_membership.json    point-in-time constituents
        raw/<sha256>__<name>     byte-exact copies of every imported source file (write-once)
        bars/<interval>/<SYMBOL>.parquet

A dataset version is identified by `content_hash` — a hash over every bar file and the
master files. Every backtest records it, so a result can be tied to exact bytes.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from nseql.instruments.models import CorporateAction, IndexMembership, Instrument

BAR_COLUMNS = ["ts", "open", "high", "low", "close", "volume", "oi"]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe(symbol: str) -> str:
    return symbol.replace("/", "_").replace("&", "_AND_")


@dataclass
class Manifest:
    dataset_id: str
    description: str
    source: str
    licence: str
    synthetic: bool
    created_at: str
    redistribution_allowed: bool = False
    generator: dict = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)       # relative path -> sha256
    raw_files: list[dict] = field(default_factory=list)       # {name, sha256, imported_at}
    audit: list[dict] = field(default_factory=list)           # corrections applied on ingest
    content_hash: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class Dataset:
    def __init__(self, root: Path):
        self.root = Path(root)
        mf = self.root / "manifest.json"
        if not mf.exists():
            raise FileNotFoundError(f"no dataset at {self.root} (manifest.json missing)")
        self.manifest = Manifest(**json.loads(mf.read_text()))
        self._instruments: dict[str, Instrument] | None = None

    # ---------------------------------------------------------------- reading
    @property
    def id(self) -> str:
        return self.manifest.dataset_id

    @property
    def synthetic(self) -> bool:
        return self.manifest.synthetic

    @property
    def content_hash(self) -> str:
        return self.manifest.content_hash

    @property
    def instruments(self) -> dict[str, Instrument]:
        if self._instruments is None:
            raw = json.loads((self.root / "instruments.json").read_text())
            self._instruments = {d["symbol"]: Instrument.from_dict(d) for d in raw}
        return self._instruments

    def corporate_actions(self) -> list[CorporateAction]:
        p = self.root / "corporate_actions.json"
        return [CorporateAction.from_dict(d) for d in json.loads(p.read_text())] if p.exists() else []

    def index_membership(self) -> list[IndexMembership]:
        p = self.root / "index_membership.json"
        if not p.exists():
            return []
        return [IndexMembership(d["index"], d["symbol"], date.fromisoformat(d["start"]),
                                date.fromisoformat(d["end"]) if d.get("end") else None)
                for d in json.loads(p.read_text())]

    def intervals(self) -> list[str]:
        b = self.root / "bars"
        return sorted(p.name for p in b.iterdir() if p.is_dir()) if b.exists() else []

    def symbols(self, interval: str) -> list[str]:
        d = self.root / "bars" / interval
        if not d.exists():
            return []
        by_file = {_safe(s): s for s in self.instruments}
        return sorted(by_file.get(p.stem, p.stem) for p in d.glob("*.parquet"))

    def has_bars(self, symbol: str, interval: str) -> bool:
        return (self.root / "bars" / interval / f"{_safe(symbol)}.parquet").exists()

    def bars(self, symbol: str, interval: str, start=None, end=None) -> pd.DataFrame:
        p = self.root / "bars" / interval / f"{_safe(symbol)}.parquet"
        if not p.exists():
            raise KeyError(f"no {interval} bars for {symbol} in dataset {self.id}")
        df = pd.read_parquet(p)
        if start is not None:
            df = df[df["ts"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["ts"] < pd.Timestamp(end) + pd.Timedelta(days=1)]
        return df.reset_index(drop=True)

    def verify(self) -> list[str]:
        """Recompute every file hash; returns the list of files that no longer match."""
        return [rel for rel, h in self.manifest.files.items()
                if not (self.root / rel).exists() or _logical_hash(self.root / rel) != h]

    def date_range(self, interval: str) -> tuple[str, str] | None:
        lo, hi = None, None
        for s in self.symbols(interval):
            df = pd.read_parquet(self.root / "bars" / interval / f"{_safe(s)}.parquet", columns=["ts"])
            if len(df):
                a, b = df["ts"].iloc[0], df["ts"].iloc[-1]
                lo = a if lo is None or a < lo else lo
                hi = b if hi is None or b > hi else hi
        return (str(lo), str(hi)) if lo is not None else None


class DatasetWriter:
    """Builds a new dataset version. Refuses to overwrite an existing one (immutability)."""

    def __init__(self, data_dir: Path, dataset_id: str, *, description: str, source: str, licence: str,
                 synthetic: bool, redistribution_allowed: bool = False, generator: dict | None = None,
                 created_at: str | None = None, overwrite: bool = False):
        self.root = Path(data_dir) / dataset_id
        if self.root.exists():
            if not overwrite:
                raise FileExistsError(f"dataset {dataset_id} already exists; datasets are immutable — "
                                      "choose a new id/version")
            shutil.rmtree(self.root)
        (self.root / "raw").mkdir(parents=True)
        self.manifest = Manifest(dataset_id, description, source, licence, synthetic,
                                 created_at or datetime.now().isoformat(timespec="seconds"),
                                 redistribution_allowed, generator or {})
        self._instruments: dict[str, Instrument] = {}
        self._actions: list[CorporateAction] = []
        self._membership: list[IndexMembership] = []

    def add_raw(self, path: Path) -> str:
        h = sha256_file(path)
        dest = self.root / "raw" / f"{h[:16]}__{Path(path).name}"
        if not dest.exists():
            shutil.copyfile(path, dest)
            dest.chmod(0o444)
        self.manifest.raw_files.append({"name": Path(path).name, "sha256": h,
                                        "imported_at": datetime.now().isoformat(timespec="seconds")})
        return h

    def add_instrument(self, inst: Instrument) -> None:
        self._instruments[inst.symbol] = inst

    def add_corporate_action(self, ca: CorporateAction) -> None:
        self._actions.append(ca)

    def add_membership(self, m: IndexMembership) -> None:
        self._membership.append(m)

    def audit(self, **entry) -> None:
        self.manifest.audit.append(entry)

    def write_bars(self, symbol: str, interval: str, df: pd.DataFrame) -> None:
        df = df.copy()
        if "oi" not in df:
            df["oi"] = 0
        df = df[BAR_COLUMNS]
        df["ts"] = pd.to_datetime(df["ts"]).astype("datetime64[ns]")
        n0 = len(df)
        df = df.sort_values("ts", kind="stable").drop_duplicates("ts", keep="last").reset_index(drop=True)
        if len(df) != n0:
            self.audit(action="drop_duplicate_timestamps", symbol=symbol, interval=interval,
                       removed=n0 - len(df), policy="keep last occurrence")
        for c in ("open", "high", "low", "close"):
            df[c] = df[c].astype("float64")
        df["volume"] = df["volume"].astype("int64")
        df["oi"] = df["oi"].astype("int64")
        d = self.root / "bars" / interval
        d.mkdir(parents=True, exist_ok=True)
        df.to_parquet(d / f"{_safe(symbol)}.parquet", index=False, compression="zstd")

    def finish(self) -> Dataset:
        key = lambda i: i.symbol
        (self.root / "instruments.json").write_text(
            json.dumps([i.to_dict() for i in sorted(self._instruments.values(), key=key)], indent=1))
        (self.root / "corporate_actions.json").write_text(
            json.dumps([a.to_dict() for a in sorted(self._actions, key=lambda a: (a.ex_date, a.symbol))], indent=1))
        (self.root / "index_membership.json").write_text(json.dumps(
            [{"index": m.index, "symbol": m.symbol, "start": m.start.isoformat(),
              "end": m.end.isoformat() if m.end else None} for m in self._membership], indent=1))
        files = {}
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root).as_posix()
            if p.is_file() and rel != "manifest.json" and not rel.startswith("raw/"):
                files[rel] = _logical_hash(p)
        self.manifest.files = files
        h = hashlib.sha256()
        for rel, fh in files.items():
            h.update(rel.encode()); h.update(fh.encode())
        self.manifest.content_hash = h.hexdigest()
        (self.root / "manifest.json").write_text(json.dumps(self.manifest.to_dict(), indent=1))
        return Dataset(self.root)


def _logical_hash(p: Path) -> str:
    """Hash of file *content*. Parquet bytes vary with library version, so bars are hashed
    over their values, which keeps content_hash stable for identical data."""
    if p.suffix != ".parquet":
        return sha256_file(p)
    df = pd.read_parquet(p)
    h = hashlib.sha256()
    for c in df.columns:
        col = df[c]
        arr = col.astype("int64").to_numpy() if c == "ts" or str(col.dtype).startswith("int") else col.to_numpy()
        h.update(c.encode()); h.update(arr.tobytes())
    return h.hexdigest()


def list_datasets(data_dir: Path) -> list[Dataset]:
    data_dir = Path(data_dir)
    if not data_dir.exists():
        return []
    return [Dataset(p) for p in sorted(data_dir.iterdir()) if (p / "manifest.json").exists()]

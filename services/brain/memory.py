"""Episodic memory: an append-only, hash-chained, point-in-time record of everything the research
system observed and decided.

* Append-only: SQLite triggers abort any UPDATE or DELETE on the episodes table.
* Tamper-evident: every row stores sha256(previous hash + its own content); `verify()` recomputes
  the chain, so an edit made by bypassing the triggers is detected.
* Point-in-time: each row has `available_at` — the market date from which its content was known.
  Readers ask for memory "as of" a date and never see rows that were not yet available, which is
  what keeps learning free of look-ahead (tests/brain/test_brain.py perturbs future outcomes and
  checks that earlier decisions do not change).
* Namespaced: each research arm / system version learns only from its own namespace.

Large batches (thousands of evaluations) are stored as one row with a compressed numpy payload.
"""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import threading
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
  seq INTEGER PRIMARY KEY, namespace TEXT NOT NULL, kind TEXT NOT NULL, epoch INTEGER, decided_at TEXT,
  available_at TEXT NOT NULL, summary TEXT NOT NULL, blob BLOB, prev_hash TEXT NOT NULL, hash TEXT NOT NULL,
  wall_time TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_ep ON episodes(namespace, kind, available_at);
CREATE TRIGGER IF NOT EXISTS episodes_no_update BEFORE UPDATE ON episodes
  BEGIN SELECT RAISE(ABORT, 'episodic memory is append-only'); END;
CREATE TRIGGER IF NOT EXISTS episodes_no_delete BEFORE DELETE ON episodes
  BEGIN SELECT RAISE(ABORT, 'episodic memory is append-only'); END;
"""
GENESIS = "0" * 64


def _pack(arrays: dict | None) -> bytes | None:
    if not arrays:
        return None
    buf = io.BytesIO()
    np.savez(buf, **{k: np.asarray(v) for k, v in arrays.items()})
    return zlib.compress(buf.getvalue(), 6)


def _unpack(blob: bytes | None) -> dict:
    if not blob:
        return {}
    with np.load(io.BytesIO(zlib.decompress(blob)), allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def _digest(prev: str, ns: str, kind: str, epoch, decided, avail: str, summary: str, blob: bytes | None) -> str:
    h = hashlib.sha256()
    for part in (prev, ns, kind, str(epoch), str(decided), avail, summary, hashlib.sha256(blob or b"").hexdigest()):
        h.update(part.encode())
        h.update(b"\x1f")
    return h.hexdigest()


@dataclass
class Episode:
    seq: int
    namespace: str
    kind: str
    epoch: int | None
    decided_at: str | None
    available_at: str
    summary: dict
    _blob: bytes | None

    @property
    def arrays(self) -> dict:
        return _unpack(self._blob)


class EpisodicMemory:
    def __init__(self, path: Path | str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self.db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def append(self, namespace: str, kind: str, summary: dict, *, available_at: str, arrays: dict | None = None,
               epoch: int | None = None, decided_at: str | None = None) -> int:
        s = json.dumps(summary, sort_keys=True, separators=(",", ":"), default=str)
        blob = _pack(arrays)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT hash FROM episodes ORDER BY seq DESC LIMIT 1").fetchone()
                prev = row[0] if row else GENESIS
                h = _digest(prev, namespace, kind, epoch, decided_at, str(available_at), s, blob)
                cur = self.db.execute(
                    "INSERT INTO episodes(namespace, kind, epoch, decided_at, available_at, summary, blob, prev_hash, hash, wall_time) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (namespace, kind, epoch, decided_at, str(available_at), s, blob, prev, h, datetime.now().isoformat(timespec="seconds")))
                self.db.execute("COMMIT")
                return int(cur.lastrowid)
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def read(self, namespace: str | None = None, kinds: tuple[str, ...] | None = None, *, as_of: str | None = None,
             after_seq: int = 0, limit: int | None = None, newest_first: bool = False) -> list[Episode]:
        q, a = "SELECT seq, namespace, kind, epoch, decided_at, available_at, summary, blob FROM episodes WHERE seq > ?", [after_seq]
        if namespace is not None:
            q += " AND namespace = ?"; a.append(namespace)
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"; a += list(kinds)
        if as_of is not None:
            q += " AND available_at <= ?"; a.append(str(as_of))
        q += " ORDER BY seq" + (" DESC" if newest_first else "")
        if limit:
            q += f" LIMIT {int(limit)}"
        return [Episode(r[0], r[1], r[2], r[3], r[4], r[5], json.loads(r[6]), r[7]) for r in self.db.execute(q, a)]

    def count(self, namespace: str | None = None) -> int:
        if namespace is None:
            return self.db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        return self.db.execute("SELECT COUNT(*) FROM episodes WHERE namespace = ?", (namespace,)).fetchone()[0]

    def namespaces(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT namespace FROM episodes ORDER BY namespace")]

    def verify(self) -> dict:
        """Recompute the whole hash chain. ok=False names the first row that does not match."""
        prev = GENESIS
        n = 0
        for seq, ns, kind, ep, dec, av, s, blob, ph, h in self.db.execute(
                "SELECT seq, namespace, kind, epoch, decided_at, available_at, summary, blob, prev_hash, hash FROM episodes ORDER BY seq"):
            n += 1
            if ph != prev or _digest(prev, ns, kind, ep, dec, av, s, blob) != h:
                return {"ok": False, "rows": n, "first_bad_seq": seq}
            prev = h
        return {"ok": True, "rows": n, "head": prev}

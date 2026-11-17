"""Durable paper sessions.

A session is a deterministic function of its configuration and its inputs. Every input — each
bar step, each manual order, each halt — is appended to a journal file BEFORE it is processed.
After a restart the session is rebuilt by replaying the journal through the same engine, which
reproduces positions, cash, orders and the equity curve exactly. Nothing derived is stored, so
stored state can never disagree with what the engine would compute.

    <experiments>/paper/<session_id>/meta.json       configuration (never contains secrets)
    <experiments>/paper/<session_id>/journal.jsonl   append-only input log
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class SessionStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, object] = {}

    def _dir(self, sid: str) -> Path:
        if not sid.replace("_", "").isalnum():
            raise ValueError("bad session id")
        return self.root / sid

    def create(self, sid: str, meta: dict) -> None:
        d = self._dir(sid)
        d.mkdir(parents=True, exist_ok=False)
        self.write_meta(sid, meta)

    def write_meta(self, sid: str, meta: dict) -> None:
        tmp = self._dir(sid) / "meta.tmp"
        tmp.write_text(json.dumps(meta, indent=1, default=str))
        tmp.replace(self._dir(sid) / "meta.json")

    def meta(self, sid: str) -> dict:
        return json.loads((self._dir(sid) / "meta.json").read_text())

    def append(self, sid: str, record: dict) -> None:
        f = self._files.get(sid)
        if f is None:
            f = self._files[sid] = open(self._dir(sid) / "journal.jsonl", "a", buffering=1)
        f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())

    def records(self, sid: str) -> list[dict]:
        p = self._dir(sid) / "journal.jsonl"
        if not p.exists():
            return []
        out = []
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                break            # a torn final line from a crash mid-write: that step never ran
        return out

    def sessions(self, include_closed: bool = False) -> list[str]:
        out = []
        for d in sorted(self.root.iterdir()):
            if (d / "meta.json").exists():
                m = json.loads((d / "meta.json").read_text())
                if include_closed or not m.get("closed"):
                    out.append(d.name)
        return out

    def close(self, sid: str) -> None:
        m = self.meta(sid)
        m["closed"] = True
        self.write_meta(sid, m)
        f = self._files.pop(sid, None)
        if f is not None:
            f.close()

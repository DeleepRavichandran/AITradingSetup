"""Protecting the evidence from the system being evaluated (docs/VALIDATION.md, "trust boundary").

The hash-chained ledger detects edits, but a writer with file access could rewrite records AND recompute every hash. Three
further layers, and an honest statement of what remains:

1. Roles. Research code (concept/feature studies, brain, history replays) appends through a `research` ledger handle that
   refuses the kinds only the operator or the evaluation layer may write: decisions, acknowledgements, criteria,
   protocols, holdout-access logs and suspensions. The operator's endpoint and the evaluation layer use their own handles.
2. Anchors. After every review the ledger head (entry count + digest) is anchored outside the ledger: in a separate
   append-only anchor log (itself chained), in the registry's audit log, and in the API for an outside witness (the
   assistant reads and records it daily). A later rewrite of any anchored entry changes the digest at that position, so
   it no longer matches its anchor even when the chain was recomputed.
3. Backups. Each anchor copies the ledger to a dated backup and verifies the copy reproduces the anchored head.

What remains (documented, not hidden): a process running as the same OS user on the laptop can still edit the ledger, the
anchors and the backups together. Only the outside witness (anchors recorded off the machine) detects that. Separate OS
accounts or write-once storage would close it; neither exists on the laptop today.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

OPERATOR_KINDS = {"decision", "acknowledged"}
EVALUATION_KINDS = {"criteria", "protocol", "holdout_access", "suspension", "ledger_digest", "hypothesis"}
RESEARCH_KINDS = {"result", "knowledge", "concept"}


class RoleError(PermissionError):
    pass


class GuardedLedger:
    """A ledger handle that may only append the kinds its role allows."""

    ALLOWED = {"research": RESEARCH_KINDS, "evaluation": RESEARCH_KINDS | EVALUATION_KINDS,
               "operator": OPERATOR_KINDS | RESEARCH_KINDS | EVALUATION_KINDS}

    def __init__(self, ledger, role: str):
        if role not in self.ALLOWED:
            raise ValueError(role)
        self._l, self.role = ledger, role

    def append(self, kind: str, subject, body: dict, at: str | None = None) -> str:
        if kind not in self.ALLOWED[self.role]:
            raise RoleError(f"the {self.role} role may not write '{kind}' records")
        return self._l.append(kind, subject, body, at=at)

    def __getattr__(self, name):                       # reads pass through
        if name in ("append",):
            raise AttributeError(name)
        return getattr(self._l, name)


def anchor_dir(exp_dir: Path) -> Path:
    d = Path(exp_dir) / "integrity_anchors"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _line_hash(prev: str, body: dict) -> str:
    return hashlib.sha256((prev + json.dumps(body, sort_keys=True)).encode()).hexdigest()


def anchors(exp_dir: Path) -> list[dict]:
    f = anchor_dir(exp_dir) / "anchors.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines() if x.strip()] if f.exists() else []


def anchor(exp_dir: Path, ledger, audit=None, note: str = "") -> dict:
    """Anchor the current ledger head outside the ledger, with a verified backup."""
    v = ledger.verify()
    prev = anchors(exp_dir)
    prev_hash = prev[-1]["line_hash"] if prev else "genesis"
    body = {"at": datetime.now().isoformat(timespec="seconds"), "entries": v["entries"], "head": v.get("head"), "chain_ok": v["ok"], "note": note}
    bk = anchor_dir(exp_dir) / "backups" / f"ledger_{body['at'].replace(':', '')}.sqlite"
    bk.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ledger.path, bk)
    body["backup"] = bk.name
    body["backup_ok"] = _head_of(bk, v["entries"]) == v.get("head")
    line = {**body, "prev_line_hash": prev_hash, "line_hash": _line_hash(prev_hash, body)}
    with open(anchor_dir(exp_dir) / "anchors.jsonl", "a") as fh:
        fh.write(json.dumps(line) + "\n")
    if audit is not None:
        audit("system", "EVIDENCE_LEDGER_ANCHOR", entries=v["entries"], head=v.get("head"))
    return line


def _head_of(path: Path, n: int) -> str | None:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        r = db.execute("SELECT digest FROM entries ORDER BY seq LIMIT 1 OFFSET ?", (n - 1,)).fetchone() if n else None
    return r[0] if r else None


def verify_anchors(exp_dir: Path, ledger) -> dict:
    """Every anchored head must still be the digest at that position, and the anchor log's own chain must hold."""
    al = anchors(exp_dir)
    bad, prev = [], "genesis"
    for a in al:
        body = {k: a[k] for k in a if k not in ("prev_line_hash", "line_hash")}
        if a["prev_line_hash"] != prev or _line_hash(prev, body) != a["line_hash"]:
            bad.append({"anchor": a["at"], "problem": "anchor log altered"})
        prev = a["line_hash"]
        if a.get("entries"):
            now = _head_of(ledger.path, a["entries"])
            if now != a["head"]:
                bad.append({"anchor": a["at"], "problem": f"ledger entry {a['entries']} no longer matches its anchor (history rewritten)"})
    return {"anchors": len(al), "ok": not bad, "problems": bad, "latest": al[-1] if al else None,
            "residual_risk": "a same-user process could rewrite ledger, anchors and backups together; only anchors recorded off "
                             "the machine (the assistant's daily record) detect that"}

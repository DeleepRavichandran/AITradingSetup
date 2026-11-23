"""Experiment registry and research memory (SQLite).

Every backtest, hypothesis, generated code version, agent message, audit verdict, lesson and
sealed-set access is recorded here with enough lineage to reproduce it: dataset content hash,
configuration fingerprint, strategy source hash, code version and result hash.

Rows are append-only from the application's point of view; nothing is updated except a
hypothesis' status and nothing is deleted.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, created_at TEXT, kind TEXT, partition TEXT, experiment_id TEXT, hypothesis_id TEXT,
  label TEXT, strategy TEXT, family TEXT, params TEXT, dataset_id TEXT, dataset_hash TEXT, synthetic INTEGER,
  fingerprint TEXT, result_hash TEXT, code_version TEXT, source_hash TEXT, status TEXT, metrics TEXT, flags TEXT,
  path TEXT, period_start TEXT, period_end TEXT, extra TEXT);
CREATE TABLE IF NOT EXISTS hypotheses (
  id TEXT PRIMARY KEY, created_at TEXT, family TEXT, statement TEXT, falsification TEXT, spec TEXT, structural_hash TEXT,
  origin TEXT, parent_id TEXT, rationale TEXT, status TEXT, status_reason TEXT, experiment_id TEXT);
CREATE TABLE IF NOT EXISTS code (
  code_id TEXT PRIMARY KEY, created_at TEXT, hypothesis_id TEXT, sha256 TEXT, source TEXT, validation TEXT);
CREATE TABLE IF NOT EXISTS experiments (
  id TEXT PRIMARY KEY, created_at TEXT, finished_at TEXT, title TEXT, status TEXT, budget TEXT, usage TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, created_at TEXT, type TEXT, producer TEXT, experiment_id TEXT, hypothesis_id TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, experiment_id TEXT, sender TEXT, recipient TEXT,
  task_type TEXT, hypothesis_id TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, experiment_id TEXT, hypothesis_id TEXT, family TEXT,
  outcome TEXT, text TEXT, evidence TEXT, next_step TEXT);
CREATE TABLE IF NOT EXISTS sealed_access (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, hypothesis_id TEXT UNIQUE, run_id TEXT, requester TEXT);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, actor TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""
J = lambda x: json.dumps(x, default=str)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SealedAccessError(PermissionError):
    pass


class Registry:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    # ------------------------------------------------------------------ helpers
    def _x(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _q(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self.db.execute(sql, args).fetchall()]

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("params", "metrics", "flags", "extra", "spec", "validation", "budget", "usage", "summary", "payload",
                  "evidence", "detail"):
            if k in d and isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass
        return d

    # ------------------------------------------------------------------ audit log
    def audit(self, actor: str, action: str, **detail) -> None:
        self._x("INSERT INTO audit_log(created_at, actor, action, detail) VALUES (?,?,?,?)", (now(), actor, action, J(detail)))

    def audit_entries(self, limit: int = 200) -> list[dict]:
        return self._q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ runs
    def record_run(self, run_id: str, summary: dict, *, kind: str = "backtest", partition: str = "full",
                   experiment_id: str | None = None, hypothesis_id: str | None = None, path: str | None = None,
                   extra: dict | None = None) -> None:
        s0 = summary["strategies"][0] if summary.get("strategies") else {}
        names = "+".join(s["name"] for s in summary.get("strategies", []))
        if hypothesis_id and names in ("generated", "g", "editor"):
            names = f"gen:{hypothesis_id}"
        self._x("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            run_id, now(), kind, partition, experiment_id, hypothesis_id, summary.get("label", ""), names,
            s0.get("family", ""), J(s0.get("params", {})), summary["dataset"]["id"], summary["dataset"]["content_hash"],
            int(summary["dataset"]["synthetic"]), summary["fingerprint"], summary["result_hash"], summary["code_version"],
            J(summary.get("strategy_source_hashes", {})), summary["status"], J(summary["metrics"]), J(summary.get("flags", [])),
            path, summary["period"]["start"], summary["period"]["end"], J(extra or {})))

    def runs(self, *, limit: int = 200, kind: str | None = None, experiment_id: str | None = None,
             hypothesis_id: str | None = None, include_sealed: bool = True) -> list[dict]:
        q, a = "SELECT * FROM runs WHERE 1=1", []
        for col, val in (("kind", kind), ("experiment_id", experiment_id), ("hypothesis_id", hypothesis_id)):
            if val:
                q += f" AND {col} = ?"; a.append(val)
        if not include_sealed:
            q += " AND partition != 'sealed'"
        return self._q(q + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*a, limit))

    def run(self, run_id: str) -> dict | None:
        r = self._q("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        return r[0] if r else None

    def trial_count(self, family: str | None = None) -> int:
        """Number of backtests ever run on non-sealed data — the multiple-testing denominator."""
        q = "SELECT COUNT(*) AS n FROM runs WHERE partition != 'sealed'"
        return self._q(q + (" AND family = ?" if family else ""), (family,) if family else ())[0]["n"]

    # ------------------------------------------------------------------ hypotheses & code
    def add_hypothesis(self, spec: dict, structural_hash: str, experiment_id: str | None) -> None:
        self._x("INSERT OR REPLACE INTO hypotheses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            spec["id"], now(), spec["family"], spec["hypothesis"], spec["falsification"], J(spec), structural_hash,
            spec.get("origin", ""), spec.get("parent_id"), spec.get("rationale", ""), "proposed", "", experiment_id))

    def set_hypothesis_status(self, hid: str, status: str, reason: str) -> None:
        self._x("UPDATE hypotheses SET status = ?, status_reason = ? WHERE id = ?", (status, reason, hid))

    def hypotheses(self, limit: int = 200, status: str | None = None) -> list[dict]:
        if status:
            return self._q("SELECT * FROM hypotheses WHERE status = ? ORDER BY created_at DESC, rowid DESC LIMIT ?", (status, limit))
        return self._q("SELECT * FROM hypotheses ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))

    def hypothesis(self, hid: str) -> dict | None:
        r = self._q("SELECT * FROM hypotheses WHERE id = ?", (hid,))
        return r[0] if r else None

    def seen_structures(self) -> set[str]:
        return {r["structural_hash"] for r in self._q("SELECT structural_hash FROM hypotheses")}

    def add_code(self, hypothesis_id: str, source: str, validation: dict) -> str:
        code_id = f"code_{validation['sha256'][:12]}"
        self._x("INSERT OR REPLACE INTO code VALUES (?,?,?,?,?,?)", (code_id, now(), hypothesis_id, validation["sha256"], source, J(validation)))
        return code_id

    def code_for(self, hypothesis_id: str) -> dict | None:
        r = self._q("SELECT * FROM code WHERE hypothesis_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (hypothesis_id,))
        return r[0] if r else None

    def code(self, code_id: str) -> dict | None:
        r = self._q("SELECT * FROM code WHERE code_id = ?", (code_id,))
        return r[0] if r else None

    # ------------------------------------------------------------------ experiments / artefacts / messages
    def start_experiment(self, title: str, budget: dict) -> str:
        eid = f"exp_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}"
        self._x("INSERT INTO experiments VALUES (?,?,?,?,?,?,?,?)", (eid, now(), None, title, "running", J(budget), J({}), J({})))
        return eid

    def finish_experiment(self, eid: str, status: str, usage: dict, summary: dict) -> None:
        self._x("UPDATE experiments SET finished_at = ?, status = ?, usage = ?, summary = ? WHERE id = ?",
                (now(), status, J(usage), J(summary), eid))

    def experiments(self, limit: int = 50) -> list[dict]:
        return self._q("SELECT * FROM experiments ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))

    def experiment(self, eid: str) -> dict | None:
        r = self._q("SELECT * FROM experiments WHERE id = ?", (eid,))
        return r[0] if r else None

    def add_artifact(self, typ: str, producer: str, payload: dict, experiment_id: str | None = None,
                     hypothesis_id: str | None = None) -> str:
        aid = f"art_{uuid.uuid4().hex[:12]}"
        self._x("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)", (aid, now(), typ, producer, experiment_id, hypothesis_id, J(payload)))
        return aid

    def artifacts(self, experiment_id: str | None = None, hypothesis_id: str | None = None, typ: str | None = None,
                  limit: int = 500) -> list[dict]:
        q, a = "SELECT * FROM artifacts WHERE 1=1", []
        for col, val in (("experiment_id", experiment_id), ("hypothesis_id", hypothesis_id), ("type", typ)):
            if val:
                q += f" AND {col} = ?"; a.append(val)
        return self._q(q + " ORDER BY created_at, rowid LIMIT ?", (*a, limit))

    def add_message(self, sender: str, recipient: str, task_type: str, payload: dict, experiment_id: str | None = None,
                    hypothesis_id: str | None = None) -> None:
        self._x("INSERT INTO messages(created_at, experiment_id, sender, recipient, task_type, hypothesis_id, payload) "
                "VALUES (?,?,?,?,?,?,?)", (now(), experiment_id, sender, recipient, task_type, hypothesis_id, J(payload)))

    def messages(self, experiment_id: str | None = None, limit: int = 500) -> list[dict]:
        if experiment_id:
            return self._q("SELECT * FROM messages WHERE experiment_id = ? ORDER BY id LIMIT ?", (experiment_id, limit))
        return self._q("SELECT * FROM (SELECT * FROM messages ORDER BY id DESC LIMIT ?) ORDER BY id", (limit,))

    # ------------------------------------------------------------------ lessons
    def add_lesson(self, family: str, outcome: str, text: str, evidence: dict, next_step: str,
                   experiment_id: str | None = None, hypothesis_id: str | None = None) -> None:
        self._x("INSERT INTO lessons(created_at, experiment_id, hypothesis_id, family, outcome, text, evidence, next_step) "
                "VALUES (?,?,?,?,?,?,?,?)", (now(), experiment_id, hypothesis_id, family, outcome, text, J(evidence), next_step))

    def lessons(self, limit: int = 200, family: str | None = None) -> list[dict]:
        if family:
            return self._q("SELECT * FROM lessons WHERE family = ? ORDER BY id DESC LIMIT ?", (family, limit))
        return self._q("SELECT * FROM lessons ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ sealed set
    def claim_sealed(self, hypothesis_id: str, requester: str) -> None:
        """A hypothesis may be evaluated on the sealed set exactly once, and only by the director."""
        if requester != "research_director":
            self.audit(requester, "SEALED_ACCESS_DENIED", hypothesis_id=hypothesis_id, reason="requester not permitted")
            raise SealedAccessError(f"{requester} may not access sealed data")
        try:
            with self._lock:
                self.db.execute("INSERT INTO sealed_access(created_at, hypothesis_id, run_id, requester) VALUES (?,?,?,?)",
                                (now(), hypothesis_id, None, requester))
                self.db.commit()
        except sqlite3.IntegrityError:
            self.audit(requester, "SEALED_ACCESS_DENIED", hypothesis_id=hypothesis_id, reason="already evaluated once")
            raise SealedAccessError(f"hypothesis {hypothesis_id} has already used its single sealed evaluation") from None
        self.audit(requester, "SEALED_ACCESS_GRANTED", hypothesis_id=hypothesis_id)

    def sealed_evaluations(self) -> list[dict]:
        return self._q("SELECT * FROM sealed_access ORDER BY id")

    # ------------------------------------------------------------------ key/value settings
    def get(self, key: str, default=None):
        r = self._q("SELECT v FROM kv WHERE k = ?", (key,))
        return json.loads(r[0]["v"]) if r else default

    def put(self, key: str, value) -> None:
        self._x("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, J(value)))

    # ------------------------------------------------------------------ what generation agents may read
    def generation_view(self) -> dict:
        """Research memory visible to hypothesis-generating agents. Sealed-partition results are
        excluded so the sealed set cannot leak into what gets proposed next."""
        return {
            "lessons": self.lessons(100),
            "hypotheses": [{k: h[k] for k in ("id", "family", "statement", "status", "status_reason", "structural_hash", "parent_id")}
                           for h in self.hypotheses(300)],
            "runs": [{"hypothesis_id": r["hypothesis_id"], "family": r["family"], "partition": r["partition"],
                      "sharpe": (r["metrics"] or {}).get("sharpe"), "trades": (r["metrics"] or {}).get("trades")}
                     for r in self.runs(limit=500, include_sealed=False) if r["hypothesis_id"]],
        }

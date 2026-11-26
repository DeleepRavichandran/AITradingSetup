"""Staged validation shared by every research study (docs/VALIDATION.md). Replaces whole-sample significance.

Stages
  1. registration   the partition (development / embargo / holdout) and the definitions' hash are written once to the
                    evidence ledger with the dataset fingerprint. The holdout only grows forward; it is never re-split.
  2. development    every hypothesis is tested on development sessions only; multiple-testing correction across all of
                    them (concepts x horizons x comparisons). Only development survivors become validation candidates.
  3. sealed holdout evaluated only for candidates, only once per definition: each access is logged with the definitions'
                    hash. A holdout already inspected under different definitions is no longer independent evidence.
  4. forward        sessions after registration in shadow paper portfolios (shadow / contest code), not here.
  5. promotion      integrity.py gates plus the operator's approval bound to the exact candidate.

Inference is session-clustered: outcomes are averaged within each session first (many signals in one session share the
market's move, overlapping windows and correlated stocks), then tested across sessions, with a session bootstrap interval.
A one-session embargo separates development from the holdout.

Statuses (never "validated"): insufficient evidence, rejected, exploratory finding, validation candidate,
holdout-supported finding, forward-supported finding, promotion-eligible methodology. Missing or too-small holdout evidence
keeps a candidate at "validation candidate"; it can never qualify.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime

import numpy as np
import pandas as pd

VERSION = "vp-1.0"
STATUSES = ("insufficient evidence", "rejected", "exploratory finding", "validation candidate", "holdout-supported finding",
            "forward-supported finding", "promotion-eligible methodology")
DEV_SHARE = 0.6
EMBARGO_SESSIONS = 1
MIN_DEV_SESSIONS = 8
MIN_HOLDOUT_SESSIONS = 8
MIN_EVENTS = 30
ALPHA = 0.05
BOOT = 2000
SEED = 20261126


def fingerprint(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


# ================================================================================================ partitions
def register_partition(ledger, key: str, sessions: list[str], data_fingerprint: str, definitions_hash: str) -> dict:
    """Once per study key (e.g. 'concepts live_x 5-minute'): development / embargo / holdout boundaries by date."""
    for e in ledger.entries("protocol"):
        if e["subject"] == key:
            return e["body"]
    s = sorted(set(sessions))
    k = max(1, int(len(s) * DEV_SHARE))
    body = {"key": key, "registered_at": datetime.now().isoformat(timespec="seconds"), "dev_until": s[k - 1] if s else None,
            "embargo": s[k:k + EMBARGO_SESSIONS], "holdout_from": s[k + EMBARGO_SESSIONS] if len(s) > k + EMBARGO_SESSIONS else None,
            "data_fingerprint_at_registration": data_fingerprint, "definitions_hash": definitions_hash, "version": VERSION,
            "rule": "development selects; the holdout only grows forward and is accessed once per definition"}
    ledger.append("protocol", key, body)
    return body


def split(sessions: list[str], part: dict) -> tuple[list[str], list[str], list[str]]:
    s = sorted(set(sessions))
    dev = [d for d in s if part["dev_until"] and d <= part["dev_until"]]
    emb = [d for d in s if d in part.get("embargo", [])]
    hold = [d for d in s if part.get("holdout_from") and d >= part["holdout_from"]]
    if not part.get("holdout_from"):                        # registered before any holdout existed: later sessions form it
        hold = [d for d in s if d > (part["dev_until"] or "") and d not in emb]
    return dev, emb, hold


def holdout_access(ledger, key: str, definitions_hash: str, data_fingerprint: str) -> dict:
    """Log an access to the holdout. Returns whether this holdout was already inspected under OTHER definitions (then it
    is no longer independent evidence for these)."""
    prior = [e["body"] for e in ledger.entries("holdout_access") if e["subject"] == key]
    reused = any(p["definitions_hash"] != definitions_hash for p in prior)
    ledger.append("holdout_access", key, {"definitions_hash": definitions_hash, "data_fingerprint": data_fingerprint,
                                          "previous_accesses": len(prior), "reused_by_other_definitions": reused})
    return {"accesses": len(prior) + 1, "reused": reused}


# ================================================================================================ inference
def cluster_test(values, clusters, alternative: str = "greater") -> dict:
    """Average within clusters (sessions), then a one-sided t-test and a cluster bootstrap interval across clusters."""
    from scipy import stats
    df = pd.DataFrame({"v": np.asarray(values, dtype=float), "c": np.asarray(clusters)}).dropna()
    if df.empty:
        return {"n": 0, "clusters": 0, "mean": None, "p": None, "lo": None, "hi": None}
    m = df.groupby("c").v.mean().to_numpy()
    k = len(m)
    out = {"n": int(len(df)), "clusters": int(k), "mean": float(m.mean()), "p": None, "lo": None, "hi": None}
    if k >= 2 and m.std(ddof=1) > 0:
        t = m.mean() / (m.std(ddof=1) / math.sqrt(k))
        out["p"] = float(stats.t.sf(t, k - 1)) if alternative == "greater" else float(2 * stats.t.sf(abs(t), k - 1))
        rng = np.random.default_rng(SEED)
        boots = m[rng.integers(0, k, size=(BOOT, k))].mean(axis=1)
        out["lo"], out["hi"] = float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))
    return out


def paired_cluster_test(a: pd.DataFrame, b: pd.DataFrame) -> dict:
    """a, b: columns v, c (value, session). Session means of a minus session means of b, over sessions present in both."""
    ma, mb = a.groupby("c").v.mean(), b.groupby("c").v.mean()
    j = ma.index.intersection(mb.index)
    d = (ma[j] - mb[j])
    return cluster_test(d.to_numpy(), d.index.to_numpy())


def bh(pvalues: list, alpha: float = ALPHA) -> list[bool]:
    from nseql.screening.integrity import bh as _bh
    return _bh(pvalues, alpha)


# ================================================================================================ decision
def decide(dev: dict, dev_pass: bool, hold: dict | None, hold_pass: bool, reused: bool) -> tuple[str, str]:
    """Status from the two stages. Missing holdout never qualifies."""
    if dev.get("clusters", 0) < MIN_DEV_SESSIONS or dev.get("n", 0) < MIN_EVENTS:
        return "insufficient evidence", f"development has {dev.get('clusters', 0)} sessions / {dev.get('n', 0)} observations " \
                                        f"(needs {MIN_DEV_SESSIONS} / {MIN_EVENTS})"
    if not dev_pass:
        return "rejected", "no value in development after multiple-testing correction (session-clustered)"
    if hold is None or hold.get("clusters", 0) < MIN_HOLDOUT_SESSIONS or hold.get("n", 0) < MIN_EVENTS:
        n = 0 if hold is None else hold.get("clusters", 0)
        return "validation candidate", f"passed development; holdout has {n} sessions (needs {MIN_HOLDOUT_SESSIONS}): not yet independent evidence"
    if reused:
        return "validation candidate", "the holdout was already inspected under other definitions: it is not independent evidence here"
    if not hold_pass:
        return "rejected", "passed development but failed the sealed holdout"
    return "holdout-supported finding", "passed development and the sealed holdout (session-clustered, corrected); forward evidence still needed"

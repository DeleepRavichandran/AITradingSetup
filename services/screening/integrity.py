"""Evidence integrity: an adversarial layer that tries to DISPROVE the bot's hypotheses (docs/INTEGRITY.md).

The objective is not to prove that the bot works. It is to find out whether it works, under which conditions, and when it
must stop. Everything here is deterministic (seeded), reads the ledgers read-only and never changes a trading rule.

1. Immutable evidence ledger (experiments/integrity/ledger.sqlite): append-only, each entry hash-chained to the previous
   one; SQLite triggers refuse UPDATE and DELETE. Hypotheses, results (passed AND failed), operator decisions and
   suspensions are recorded here and never rewritten.
2. Pre-registration. A hypothesis is registered with machine-checkable falsification criteria BEFORE its test data
   exists: only sessions strictly after the registration date count as confirmatory evidence. Earlier sessions are
   shown as exploratory (the hypothesis was shaped on them) and never decide anything.
3. The critic. For every hypothesis the same contradicting checks run: without the best day, without the best stock,
   doubled costs, a one-minute delay, random-entry placebo, market beta (buy and hold), regime concentration.
4. Bias audit: twelve named biases, each with the guard that addresses it, a deterministic check on the real ledger,
   and a status: guarded / detected / exposed (a known weakness) / not measurable yet.
5. Multiple-testing correction (Benjamini-Hochberg) across the confirmatory hypotheses and across every configuration
   that has traded; calibration of the allocator's score against outcomes; drift detection (Page-Hinkley) on the net
   result per round trip; regime sensitivity from the trade journal.
6. Decision quality is judged separately from the outcome: a trade taken with positive expected edge at decision time
   that lost money is a good decision with a bad outcome, and the reverse.
7. Abstention: with insufficient evidence the verdict is "abstain", never "works".
8. Promotion eligibility: every gate must pass, and promotion still needs the operator's recorded approval.
9. Predeclared suspension rules (SUSPEND). When one fires, the API engages the kill switch (reduce-only: exits continue,
   no new buys). Only a human can reset it. Nothing here can loosen a limit or enable real orders.

Agreement between AI systems is not evidence and is never read here: only ledgers, prices and seeded statistics.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

VERSION = "ei-1.0"
MIN_SESSIONS = 20            # confirmatory sessions before a daily-return hypothesis can be decided
MIN_TRADES = 30              # round trips before a trade-level hypothesis, calibration or drift can be decided
ALPHA = 0.05                 # one-sided, after Benjamini-Hochberg across the confirmatory hypotheses
FDR_CONFIGS = 0.10           # BH level for configuration-level evidence
PLACEBO_DRAWS = 2000
COST_MULT = 2.0
DELAY_MIN = 1
SEED = 20261125

# Predeclared suspension rules: fixed here, in code, before any data is seen. Changing them needs a commit and review.
SUSPEND = {
    "S1": "a fill earlier than its decision allows, or a trade decided on retrospective (catch-up) data, in the current account",
    "S2": "the evidence ledger's hash chain is broken, or an anchored entry no longer matches its anchor (history rewritten)",
    "S3": "the current account is 6% or more below its own peak equity",
    "S4": "performance deterioration (Page-Hinkley alarm on net per round trip, at least 30 round trips) while the "
          "random-entry placebo shows no skill (p > 0.5)",
    "S5": "the financial record does not hold: cash differs from the fills by more than Rs 1, or a recorded day's trades were "
          "altered or removed afterwards",
}
DRAWDOWN_SUSPEND = 0.06

# Seed hypotheses: what the platform currently claims or assumes. Falsification is fixed at registration.
HYPOTHESES = [
    {"id": "H-EDGE", "statement": "The incumbent paper account earns more than cash after all costs.",
     "metric": "daily_return", "subject": "incumbent", "compare": "cash",
     "falsification": f"Falsified if, after {MIN_SESSIONS} confirmatory sessions, the one-sided p-value for a mean daily return above 0 "
                      f"is above {ALPHA} after correction, or the 95% upper bound is below 0."},
    {"id": "H-INDEX", "statement": "The incumbent beats NIFTY 50 after costs.", "metric": "daily_excess", "subject": "incumbent",
     "compare": "NIFTY 50",
     "falsification": f"Falsified if, after {MIN_SESSIONS} confirmatory sessions, the mean daily excess over NIFTY 50 is not above 0 at {ALPHA}."},
    {"id": "H-SIMPLE", "statement": "The incumbent beats simply buying and holding the watchlist.", "metric": "daily_excess",
     "subject": "incumbent", "compare": "buy and hold watchlist",
     "falsification": f"Falsified if, after {MIN_SESSIONS} confirmatory sessions, the mean daily excess over buy and hold is not above 0 at {ALPHA}."},
    {"id": "H-CHALLENGER", "statement": "The challenger beats the incumbent on the same sessions after costs.", "metric": "paired_daily",
     "subject": "challenger", "compare": "incumbent",
     "falsification": f"Falsified if, after {MIN_SESSIONS} paired confirmatory sessions, the mean daily difference is not above 0 at {ALPHA}."},
    {"id": "H-SKILL", "statement": "The bot's entries beat random entries in the same stock, on the same day, held as long.",
     "metric": "placebo", "subject": "incumbent", "compare": "random entries",
     "falsification": f"Falsified if, after {MIN_TRADES} confirmatory round trips, fewer than {1 - ALPHA:.0%} of {PLACEBO_DRAWS:,} seeded "
                      "random-entry portfolios do worse than the bot."},
    {"id": "H-ABSTAIN", "statement": "Entries that pass every uncertainty gate (fresh, complete, positive expected edge, familiar "
     "conditions) do better than entries that fail at least one.", "metric": "gate_split", "subject": "incumbent", "compare": "entries failing a gate",
     "falsification": f"Falsified if, with at least {MIN_TRADES} confirmatory round trips in each group, entries passing every gate are not "
                      f"better per rupee at {ALPHA}."},
    {"id": "H-BLACKOUT", "statement": "Entries after the first 30 minutes do better than entries in them (rules 1.3).",
     "metric": "late_minus_early", "subject": "challenger", "compare": "entries before 09:46",
     "falsification": f"Falsified if, with at least {MIN_TRADES} confirmatory round trips in each group (measured on the challenger, which "
                      f"has no blackout), late entries are not better per rupee invested at {ALPHA}."},
]


# ================================================================================================ 1. immutable ledger
LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT,
  body TEXT NOT NULL, prev TEXT NOT NULL, digest TEXT NOT NULL UNIQUE);
CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries BEGIN SELECT RAISE(ABORT, 'evidence ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries BEGIN SELECT RAISE(ABORT, 'evidence ledger is append-only'); END;
"""
KINDS = ("hypothesis", "result", "decision", "suspension", "acknowledged", "criteria", "ledger_digest", "knowledge", "concept", "protocol", "holdout_access")


def _digest(prev: str, at: str, kind: str, subject: str | None, body: str) -> str:
    return hashlib.sha256("\x1f".join([prev, at, kind, subject or "", body]).encode()).hexdigest()


class Ledger:
    def __init__(self, exp_dir: Path):
        self.path = Path(exp_dir) / "integrity" / "ledger.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.executescript(LEDGER_SCHEMA)

    def append(self, kind: str, subject: str | None, body: dict, at: str | None = None) -> str:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        at = at or datetime.now().isoformat(timespec="seconds")
        b = json.dumps(body, sort_keys=True, default=str)
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT digest FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row[0] if row else "genesis"
            d = _digest(prev, at, kind, subject, b)
            db.execute("INSERT INTO entries (at, kind, subject, body, prev, digest) VALUES (?,?,?,?,?,?)", (at, kind, subject, b, prev, d))
        return d

    def entries(self, kind: str | None = None, subject: str | None = None) -> list[dict]:
        q, p = "SELECT seq, at, kind, subject, body, digest FROM entries WHERE 1=1", []
        if kind:
            q, p = q + " AND kind = ?", [*p, kind]
        if subject:
            q, p = q + " AND subject = ?", [*p, subject]
        with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as db:
            return [{"seq": s, "at": a, "kind": k, "subject": su, "body": json.loads(b), "digest": d}
                    for s, a, k, su, b, d in db.execute(q + " ORDER BY seq", p)]

    def verify(self) -> dict:
        prev, n = "genesis", 0
        with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as db:
            for seq, at, kind, subject, body, p, d in db.execute("SELECT seq, at, kind, subject, body, prev, digest FROM entries ORDER BY seq"):
                if p != prev or _digest(prev, at, kind, subject, body) != d:
                    return {"ok": False, "entries": n, "broken_at": seq}
                prev, n = d, n + 1
        return {"ok": True, "entries": n, "broken_at": None, "head": prev}


def register_seed_hypotheses(ledger: Ledger, at: str | None = None) -> list[str]:
    """Register each seed hypothesis once. Its confirmatory window starts the session AFTER registration."""
    have = {e["subject"] for e in ledger.entries("hypothesis")}
    new = []
    for h in HYPOTHESES:
        if h["id"] not in have:
            when = at or datetime.now().isoformat(timespec="seconds")
            ledger.append("hypothesis", h["id"], {**h, "registered_at": when, "confirmatory_from": "first session after " + when[:10],
                                                  "version": VERSION}, at=when)
            new.append(h["id"])
    return new


# ================================================================================================ 1b. protected criteria
BENCHMARKS = ("cash", "NIFTY 50", "buy and hold watchlist", "trend rule (above 20-day average)")


def criteria_manifest() -> dict:
    """Everything that decides a verdict. Its hash is registered once; a later change is visible and needs the
    operator's recorded approval (subject 'criteria <hash>'), so the evaluated system cannot quietly loosen its own test."""
    return {"version": VERSION, "min_sessions": MIN_SESSIONS, "min_trades": MIN_TRADES, "alpha": ALPHA, "fdr_configs": FDR_CONFIGS,
            "placebo_draws": PLACEBO_DRAWS, "seed": SEED, "cost_mult": COST_MULT, "delay_min": DELAY_MIN, "drawdown_suspend": DRAWDOWN_SUSPEND,
            "suspend": SUSPEND, "benchmarks": list(BENCHMARKS),
            "hypotheses": [{k: h[k] for k in ("id", "statement", "metric", "compare", "falsification")} for h in HYPOTHESES]}


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def check_criteria(ledger: "Ledger", register: bool = True) -> dict:
    cur = _hash(criteria_manifest())
    recs = ledger.entries("criteria")
    if not recs:
        if register:
            ledger.append("criteria", cur[:16], {"hash": cur, "manifest": criteria_manifest()})
        return {"hash": cur, "registered": cur, "changed": False, "approved": True}
    first = recs[0]["body"]["hash"]
    if cur != recs[-1]["body"]["hash"] and register:
        ledger.append("criteria", cur[:16], {"hash": cur, "previous": recs[-1]["body"]["hash"], "manifest": criteria_manifest()})
    approved = cur == first or any(e["subject"] == f"criteria {cur[:16]}" and e["body"].get("decision") == "approve"
                                   for e in ledger.entries("decision"))
    return {"hash": cur, "registered": first, "changed": cur != first, "approved": approved}


def cash_reconciliation(account: Path) -> dict:
    """Independent accounting check: start cash + every recorded fill (proceeds minus cost minus charges) must equal the
    cash the engine reported at its last processed minute."""
    m = _meta(account)
    con = _ro(account)
    try:
        fills = con.execute("SELECT side, qty, price, charges_paise FROM paper_fills").fetchall()
        last = con.execute("SELECT cash_inr FROM cycles WHERE cash_inr IS NOT NULL ORDER BY minute DESC LIMIT 1").fetchone()
    finally:
        con.close()
    start = m.get("start_cash") or m.get("capital")
    if last is None or start is None:
        return {"checked": False}
    calc = float(start) + sum((-1 if sd == "BUY" else 1) * q * p - (c or 0) / 100 for sd, q, p, c in fills)
    return {"checked": True, "from_fills_inr": round(calc, 2), "engine_inr": round(float(last[0]), 2), "difference_inr": round(float(last[0]) - calc, 2),
            "fills": len(fills), "ok": abs(float(last[0]) - calc) <= 1.0}


def day_digest(account: Path, day: str) -> dict:
    con = _ro(account)
    try:
        rows = con.execute("SELECT fill_key, side, qty, price, charges_paise, realized_paise FROM paper_fills WHERE ts >= ? AND ts < ? ORDER BY fill_key",
                           (day, _next(day))).fetchall()
    finally:
        con.close()
    return {"fills": len(rows), "net_paise": int(sum(r[5] or 0 for r in rows)), "digest": _hash(rows)}


def _next(day: str) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def record_digests(ledger: "Ledger", exp_dir: Path, before_day: str) -> int:
    """Seal each closed session's fills (all accounts) once, so hiding or editing a losing trade later is detected."""
    have = {e["subject"] for e in ledger.entries("ledger_digest")}
    n = 0
    for kind, accs in accounts(exp_dir).items():
        for a in accs:
            con = _ro(a)
            try:
                days = [r[0] for r in con.execute("SELECT DISTINCT substr(ts,1,10) FROM paper_fills ORDER BY 1")]
            finally:
                con.close()
            for d in days:
                key = f"{a.name}:{d}"
                if d < before_day and key not in have:
                    ledger.append("ledger_digest", key, day_digest(a, d))
                    n += 1
    return n


def verify_digests(ledger: "Ledger", exp_dir: Path) -> dict:
    base = Path(exp_dir) / "screening"
    bad, n = [], 0
    for e in ledger.entries("ledger_digest"):
        acc, day = e["subject"].rsplit(":", 1)
        if not (base / acc / "screening.sqlite").exists():
            bad.append({"day": e["subject"], "problem": "account ledger missing"})
            continue
        n += 1
        now = day_digest(base / acc, day)
        if now["digest"] != e["body"]["digest"]:
            bad.append({"day": e["subject"], "problem": f"fills changed after sealing: {e['body']['fills']} -> {now['fills']} fills, "
                                                        f"net {e['body']['net_paise'] / 100:,.2f} -> {now['net_paise'] / 100:,.2f} INR"})
    return {"sealed_days": n, "altered": bad, "ok": not bad}


# ================================================================================================ 2. ledgers -> evidence
def _ro(account: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(account) / 'screening.sqlite'}?mode=ro", uri=True)


def accounts(exp_dir: Path) -> dict[str, list[Path]]:
    """Live paper accounts: incumbents (oldest first) and challengers."""
    base = Path(exp_dir) / "screening"
    inc, ch = [], []
    for f in sorted(base.glob("*/live.json")) if base.exists() else []:
        if not (f.parent / "screening.sqlite").exists():
            continue
        try:
            m = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if m.get("shadow"):
            continue                                   # shadow portfolios are hypothetical: never the incumbent's evidence
        (ch if m.get("challenger") else inc).append(f.parent)
    key = lambda p: (p / "live.json").stat().st_mtime
    return {"incumbent": sorted(inc, key=key), "challenger": sorted(ch, key=key)}


def _meta(account: Path) -> dict:
    try:
        return json.loads((Path(account) / "live.json").read_text())
    except (OSError, ValueError):
        return {}


def rules_version(account: Path) -> str:
    m = _meta(account)
    if m.get("rules_version"):
        return str(m["rules_version"])
    v = re.search(r"_v(\d)(\d)", Path(account).name)
    return f"{v.group(1)}.{v.group(2)}" if v else "1.0"


def daily_returns(account: Path) -> pd.Series:
    """Session return after costs: last equity of each session over the previous session's (or the start cash)."""
    con = _ro(account)
    try:
        cyc = pd.read_sql_query("SELECT minute, equity_inr FROM cycles WHERE equity_inr IS NOT NULL AND status='processed' AND mode != 'warmup' "
                                "ORDER BY minute", con)
    finally:
        con.close()
    if cyc.empty:
        return pd.Series(dtype=float)
    eq = cyc.groupby(cyc.minute.str[:10]).equity_inr.last()
    m = _meta(account)
    start = float(m.get("start_cash") or m.get("capital") or eq.iloc[0])
    return (eq / eq.shift(1).fillna(start) - 1).astype(float)


RT_COLS = ["config_id", "symbol", "entry_ts", "exit_ts", "decided_at", "qty", "entry", "exit", "charges", "net", "ret", "score", "regime"]


def round_trips(account: Path) -> pd.DataFrame:
    """Closed round trips: each SELL paired with the latest earlier BUY of the same configuration. Net is after both legs'
    charges (as recorded); `ret` is net over the buy value. Entry score is the allocator's score recorded at decision time."""
    con = _ro(account)
    try:
        o = pd.read_sql_query("SELECT config_id, symbol, side, decided_at, fill_ts, qty, price, charges_paise, realized_paise FROM paper_orders "
                              "WHERE status IN ('filled','partial') AND fill_ts IS NOT NULL ORDER BY fill_ts", con)
        sc = pd.read_sql_query("SELECT config_id, bar_ts, alloc_score FROM signals WHERE decision='BUY' AND alloc_status='accepted'", con)
        try:
            jr = pd.read_sql_query("SELECT config_id, ts, regime FROM journal WHERE side='BUY'", con)
        except Exception:       # noqa: BLE001 - older ledgers have no journal
            jr = pd.DataFrame(columns=["config_id", "ts", "regime"])
    finally:
        con.close()
    cols = RT_COLS
    if o.empty:
        return pd.DataFrame(columns=cols)
    score = {(c, t): s for c, t, s in sc.itertuples(index=False)}
    regime = {(c, t): r for c, t, r in jr.itertuples(index=False)}
    open_buy: dict[str, dict] = {}
    rows = []
    for r in o.itertuples(index=False):
        if r.side == "BUY":
            open_buy[r.config_id] = r._asdict()
            continue
        b = open_buy.pop(r.config_id, None)
        if b is None or not b["price"] or not b["qty"]:
            continue
        net = float(r.realized_paise or 0) / 100
        val = float(b["price"]) * float(b["qty"])
        rows.append({"config_id": r.config_id, "symbol": r.symbol, "entry_ts": b["fill_ts"], "exit_ts": r.fill_ts, "decided_at": b["decided_at"],
                     "qty": float(b["qty"]), "entry": float(b["price"]), "exit": float(r.price), "charges": (float(b["charges_paise"] or 0) + float(r.charges_paise or 0)) / 100,
                     "net": net, "ret": net / val, "score": score.get((b["config_id"], b["decided_at"])),
                     "regime": regime.get((b["config_id"], b["fill_ts"]))})
    return pd.DataFrame(rows, columns=cols)


# ================================================================================================ 3. statistics
def t_test(x, alternative: str = "greater") -> dict:
    """One-sample t-test of mean > 0, with a 95% two-sided interval. Deterministic."""
    from scipy import stats
    x = np.asarray([v for v in x if v is not None and math.isfinite(v)], dtype=float)
    n = len(x)
    if n < 2 or float(np.std(x, ddof=1)) == 0:
        return {"n": n, "mean": float(x.mean()) if n else None, "p": None, "lo": None, "hi": None}
    m, se = float(x.mean()), float(np.std(x, ddof=1) / math.sqrt(n))
    t = m / se
    p = float(stats.t.sf(t, n - 1)) if alternative == "greater" else float(2 * stats.t.sf(abs(t), n - 1))
    q = float(stats.t.ppf(0.975, n - 1))
    return {"n": n, "mean": m, "p": p, "lo": m - q * se, "hi": m + q * se}


def bh(pvalues: list[float | None], alpha: float) -> list[bool]:
    """Benjamini-Hochberg: which hypotheses survive at false-discovery rate `alpha` (None = not tested)."""
    idx = [i for i, p in enumerate(pvalues) if p is not None]
    out = [False] * len(pvalues)
    if not idx:
        return out
    ps = sorted((pvalues[i], i) for i in idx)
    m, k = len(ps), 0
    for rank, (p, _) in enumerate(ps, 1):
        if p <= alpha * rank / m:
            k = rank
    for p, i in ps[:k]:
        out[i] = True
    return out


def page_hinkley(x, delta: float | None = None, lam: float | None = None) -> dict:
    """Page-Hinkley test for a DROP in the mean of x (net per round trip, in order). Thresholds scale with the series'
    own spread: delta = 0.5 sd (tolerated drift), lambda = 8 sd. Calibrated by simulation (seeds 0-299): 0.3% false alarms
    on 200 steady round trips; a fall of 1.5 sd in the mean is detected every time. Returns the first alarm index, if any."""
    x = np.asarray(x, dtype=float)
    if len(x) < MIN_TRADES:
        return {"alarm": False, "at": None, "n": len(x), "decided": False}
    sd = float(np.std(x, ddof=1)) or 1.0
    delta = 0.5 * sd if delta is None else delta
    lam = 8 * sd if lam is None else lam
    mean, cum, low = 0.0, 0.0, 0.0
    for i, v in enumerate(x, 1):
        mean += (v - mean) / i
        cum += mean - v - delta                 # grows while values sit below the running mean
        low = min(low, cum)
        if cum - low > lam:
            return {"alarm": True, "at": i, "n": len(x), "decided": True}
    return {"alarm": False, "at": None, "n": len(x), "decided": True}


def spearman(a, b) -> float | None:
    a, b = pd.Series(a, dtype=float), pd.Series(b, dtype=float)
    ok = a.notna() & b.notna()
    if ok.sum() < 3:
        return None
    r = a[ok].rank().corr(b[ok].rank())
    return None if pd.isna(r) else float(r)


# ================================================================================================ 4. the critic
def _bars(account: Path, symbols: list[str], days: list[str]) -> dict:
    if not symbols or not days:
        return {}
    con = _ro(account)
    try:
        q = (f"SELECT symbol, ts, open, close FROM events WHERE late=0 AND symbol IN ({','.join('?' * len(symbols))}) "
             f"AND substr(ts,1,10) IN ({','.join('?' * len(days))}) ORDER BY symbol, ts")
        b = pd.read_sql_query(q, con, params=[*symbols, *days])
    finally:
        con.close()
    out = {}
    for (s, d), f in b.groupby([b.symbol, b.ts.str[:10]]):
        out[(s, d)] = (pd.to_datetime(f.ts).to_numpy(), f.open.to_numpy(float), f.close.to_numpy(float))
    return out


def stress(account: Path, rt: pd.DataFrame) -> dict:
    """Doubled costs, and a one-minute later entry and exit (open of the next bar), on the real round trips."""
    if rt.empty:
        return {"n": 0}
    cost2 = rt.net - (COST_MULT - 1) * rt.charges
    bars = _bars(account, sorted(rt.symbol.unique()), sorted(rt.entry_ts.str[:10].unique()))
    delayed = []
    for r in rt.itertuples(index=False):
        k = bars.get((r.symbol, str(r.entry_ts)[:10]))
        if k is None:
            delayed.append(np.nan)
            continue
        ts, op, _ = k
        i = np.searchsorted(ts, np.datetime64(pd.Timestamp(r.entry_ts)), side="right") + DELAY_MIN - 1
        j = np.searchsorted(ts, np.datetime64(pd.Timestamp(r.exit_ts)), side="right") + DELAY_MIN - 1
        if i >= len(ts) or j >= len(ts):
            delayed.append(np.nan)
            continue
        delayed.append(r.net + r.qty * ((op[j] - r.exit) - (op[i] - r.entry)))
    d = pd.Series(delayed, dtype=float)
    return {"n": len(rt), "actual_inr": round(float(rt.net.sum()), 2), "double_costs_inr": round(float(cost2.sum()), 2),
            "one_minute_delay_inr": round(float(d.sum()), 2) if d.notna().any() else None, "delay_measured": int(d.notna().sum()),
            "survives_costs": bool(cost2.sum() > 0), "survives_delay": bool(d.notna().any() and d.sum() > 0)}


def placebo(account: Path, rt: pd.DataFrame, draws: int = PLACEBO_DRAWS, seed: int = SEED) -> dict:
    """Random-entry control: for each round trip, a random entry minute in the same stock on the same day, held for the
    same number of minutes, with the same cost fraction. p = share of random portfolios whose mean return per rupee is at
    least the bot's. Seeded, so the result reproduces exactly."""
    if len(rt) < 2:
        return {"n": len(rt), "p": None}
    bars = _bars(account, sorted(rt.symbol.unique()), sorted(rt.entry_ts.str[:10].unique()))
    rng = np.random.default_rng(seed)
    sims, actual = [], []
    for r in rt.itertuples(index=False):
        k = bars.get((r.symbol, str(r.entry_ts)[:10]))
        if k is None:
            continue
        ts, op, cl = k
        hold = max(1, int((pd.Timestamp(r.exit_ts) - pd.Timestamp(r.entry_ts)).total_seconds() // 60))
        if len(ts) <= hold + 1:
            continue
        starts = rng.integers(0, len(ts) - hold - 1, size=draws)
        cost = r.charges / (r.entry * r.qty)
        sims.append(cl[starts + hold] / op[starts] - 1 - cost)
        actual.append(r.ret)
    if len(actual) < 2:
        return {"n": len(actual), "p": None}
    sim_mean = np.mean(np.vstack(sims), axis=0)
    a = float(np.mean(actual))
    return {"n": len(actual), "actual_mean_ret": round(a, 6), "placebo_mean_ret": round(float(sim_mean.mean()), 6),
            "p": round(float((np.sum(sim_mean >= a) + 1) / (draws + 1)), 4), "draws": draws, "seed": seed}


def critic(rt: pd.DataFrame, daily: pd.Series, bench: dict | None) -> list[dict]:
    """Contradicting checks on the same evidence. Each returns whether the claim survives it."""
    out = []
    if len(daily) >= 2:
        best = daily.idxmax()
        rest = daily.drop(best)
        out.append({"check": "without the best session", "survives": bool(rest.mean() > 0),
                    "detail": f"mean daily return {rest.mean():+.3%} without {best} (with it {daily.mean():+.3%})"})
    if len(rt) >= 2:
        by = rt.groupby("symbol").net.sum().sort_values()
        out.append({"check": "without the best stock", "survives": bool(rt.net.sum() - by.iloc[-1] > 0),
                    "detail": f"net Rs {rt.net.sum() - by.iloc[-1]:,.0f} without {by.index[-1]} (with it Rs {rt.net.sum():,.0f})"})
        wins = rt[rt.net > 0].net.sum()
        out.append({"check": "costs versus gross edge", "survives": bool(rt.charges.sum() < rt.net.sum() + rt.charges.sum()),
                    "detail": f"charges Rs {rt.charges.sum():,.0f} against a gross result of Rs {rt.net.sum() + rt.charges.sum():,.0f}"
                              f" (winning trades Rs {wins:,.0f})"})
    if bench and bench.get("series"):
        s = bench["series"]
        a = s.get("account (after costs)", {}).get("return")
        for k in ("buy and hold watchlist", "NIFTY 50"):
            if k in s and a is not None:
                out.append({"check": f"market beta: versus {k}", "survives": bool(a > s[k]["return"]),
                            "detail": f"account {a:+.2%} versus {k} {s[k]['return']:+.2%} over the same sessions"})
    if len(rt) and rt.regime.notna().any():
        g = rt.groupby(rt.regime.fillna("unknown")).net.agg(["sum", "count"])
        top = g["sum"].idxmax()
        out.append({"check": "regime concentration", "survives": bool(g["sum"].drop(top).sum() > 0) if len(g) > 1 else False,
                    "detail": "; ".join(f"{k}: Rs {v['sum']:,.0f} over {int(v['count'])}" for k, v in g.iterrows())})
    return out


# ================================================================================================ 5. hypotheses
def _split(index, registered_at: str):
    """Exploratory (on or before the registration day: the hypothesis was shaped on it) versus confirmatory (after)."""
    day = registered_at[:10]
    return [i for i in index if str(i)[:10] <= day], [i for i in index if str(i)[:10] > day]


def evaluate(h: dict, ev: dict) -> dict:
    reg_at = h["registered_at"]
    m = h["metric"]
    inc, chl = ev["incumbent"], ev["challenger"]
    res = {"id": h["id"], "statement": h["statement"], "falsification": h["falsification"], "registered_at": reg_at, "compare": h["compare"]}

    def daily_case(x: pd.Series, need: int):
        ex, cf = _split(x.index, reg_at)
        res["exploratory"] = {"sessions": len(ex), **{k: v for k, v in t_test(x.loc[ex]).items() if k != "n"}} if ex else None
        tt = t_test(x.loc[cf]) if cf else {"n": 0, "mean": None, "p": None, "lo": None, "hi": None}
        res["confirmatory"] = {"sessions": len(cf), **{k: v for k, v in tt.items() if k != "n"}}
        res["need"] = f"{need} confirmatory sessions"
        res["enough"] = len(cf) >= need
        return tt

    def trades_case(r: pd.DataFrame, need: int):
        ex = r[r.exit_ts.str[:10] <= reg_at[:10]]
        cf = r[r.exit_ts.str[:10] > reg_at[:10]]
        res["exploratory"] = {"round_trips": len(ex), **{k: v for k, v in t_test(ex.ret).items() if k != "n"}} if len(ex) else None
        res["need"] = f"{need} confirmatory round trips"
        res["enough"] = len(cf) >= need
        return ex, cf

    if m == "daily_return":
        tt = daily_case(inc["daily"], MIN_SESSIONS)
    elif m == "daily_excess":
        b = inc["bench_daily"].get(h["compare"])
        if b is None:
            res.update(status="not measurable yet", reason=f"{h['compare']} is not available for every session", p=None)
            return res
        tt = daily_case((inc["daily"] - b).dropna(), MIN_SESSIONS)
    elif m == "paired_daily":
        if chl is None:
            res.update(status="not measurable yet", reason="no challenger account is running", p=None)
            return res
        both = pd.concat([chl["daily"], inc["daily"]], axis=1, keys=["c", "i"]).dropna()
        tt = daily_case(both.c - both.i, MIN_SESSIONS)
    elif m == "placebo":
        ex, cf = trades_case(inc["rt"], MIN_TRADES)
        pl = placebo(inc["account"], cf) if len(cf) >= 2 else {"n": len(cf), "p": None}
        res["confirmatory"] = {"round_trips": len(cf), **pl}
        res["exploratory_placebo"] = placebo(inc["account"], ex) if len(ex) >= 2 else None
        tt = {"p": pl.get("p"), "lo": None, "hi": None, "mean": pl.get("actual_mean_ret")}
    elif m == "gate_split":
        from nseql.screening.brain import gate_table
        g = pd.concat([gate_table(a) for a in ev["incumbent_accounts"]], ignore_index=True) if ev["incumbent_accounts"] else pd.DataFrame()
        if g.empty:
            res.update(status="abstain", reason="no completed entries yet", p=None, need=f"{MIN_TRADES} per group", enough=False,
                       confirmatory={"round_trips": 0})
            return res
        g = g.assign(late=~g.abstain)                       # 'late' = the group expected to be better (passes every gate)
        r = g.rename(columns={"exit_ts": "exit_ts"})
        ex = r[r.exit_ts.str[:10] <= reg_at[:10]]
        cf = r[r.exit_ts.str[:10] > reg_at[:10]]
        from scipy import stats as _s

        def welch2(d):
            a, b = d[d.late].ret.to_numpy(), d[~d.late].ret.to_numpy()
            if len(a) < 2 or len(b) < 2:
                return {"pass_n": len(a), "fail_n": len(b), "p": None, "diff": None}
            t = _s.ttest_ind(a, b, equal_var=False, alternative="greater")
            return {"pass_n": len(a), "fail_n": len(b), "diff": float(a.mean() - b.mean()), "p": float(t.pvalue)}
        res["exploratory"] = welch2(ex) if len(ex) else None
        w = welch2(cf)
        res["confirmatory"] = {"round_trips": len(cf), **w}
        res["need"] = f"{MIN_TRADES} confirmatory round trips per group"
        res["enough"] = min(w["pass_n"], w["fail_n"]) >= MIN_TRADES
        tt = {"p": w["p"], "mean": w["diff"], "lo": None, "hi": None}
    elif m == "late_minus_early":
        if chl is None:
            res.update(status="not measurable yet", reason="no challenger account is running", p=None)
            return res
        r = chl["rt"].copy()
        mins = pd.to_datetime(r.entry_ts)
        r["late"] = (mins.dt.hour * 60 + mins.dt.minute) >= 9 * 60 + 46
        ex, cf = trades_case(r, MIN_TRADES)
        from scipy import stats as _s
        def welch(d):
            a, b = d[d.late].ret.to_numpy(), d[~d.late].ret.to_numpy()
            if len(a) < 2 or len(b) < 2:
                return {"late_n": len(a), "early_n": len(b), "p": None, "diff": None}
            t = _s.ttest_ind(a, b, equal_var=False, alternative="greater")
            return {"late_n": len(a), "early_n": len(b), "diff": float(a.mean() - b.mean()), "p": float(t.pvalue)}
        res["exploratory"] = welch(ex) if len(ex) else None
        w = welch(cf)
        res["confirmatory"] = {"round_trips": len(cf), **w}
        res["enough"] = min(w["late_n"], w["early_n"]) >= MIN_TRADES
        tt = {"p": w["p"], "mean": w["diff"], "lo": None, "hi": None}
    else:
        raise ValueError(m)
    res["p"] = tt.get("p")
    res["_hi"] = tt.get("hi")
    return res


def _status(res: dict, survives_bh: bool) -> None:
    if res.get("status"):
        return
    if not res.get("enough"):
        res["status"], res["reason"] = "abstain", f"insufficient confirmatory evidence (needs {res['need']}); no claim is made"
    elif res.get("p") is None:
        res["status"], res["reason"] = "abstain", "the test could not be computed on the confirmatory data"
    elif res.get("_hi") is not None and res["_hi"] < 0:
        res["status"], res["reason"] = "falsified", "the 95% interval lies entirely below zero"
    elif survives_bh:
        res["status"], res["reason"] = "survived", f"p = {res['p']:.4f} survives Benjamini-Hochberg at {ALPHA}; survival is not proof"
    else:
        res["status"], res["reason"] = "falsified", f"p = {res['p']:.4f} does not survive Benjamini-Hochberg at {ALPHA}"


# ================================================================================================ 5b. promotion helpers
REQUIRED_BENCHMARKS = ("cash", "NIFTY 50", "buy and hold watchlist", "buy and hold at the challenger's exposure", "trend rule (above 20-day average)")
ECONOMIC_EXCESS = 0.0002                     # 0.02% a day (about 5% a year) after costs: smaller edges are not worth the risk


def _exposure(account: Path) -> float | None:
    con = _ro(account)
    try:
        r = con.execute("SELECT AVG(MAX(0, (equity_inr - cash_inr) / equity_inr)) FROM cycles WHERE equity_inr > 0 AND cash_inr IS NOT NULL").fetchone()
    finally:
        con.close()
    return None if not r or r[0] is None else float(r[0])


def candidate_fingerprint(chl: dict, exp_dir: Path) -> dict:
    """Everything an approval is bound to: challenger version and configuration, its risk policy, the code commit, the
    execution model version, the frozen forward manifest and the account. Any change gives a new fingerprint."""
    from nseql.screening.challenger import CHALLENGER_CONFIG, CHALLENGER_VERSION, challenger_rules
    from nseql.screening.contest import code_version
    from nseql.screening.execution import EXECUTION_VERSION, LIVE_RULES_VERSION
    meta = _meta(chl["account"])
    parts = {"challenger": CHALLENGER_VERSION, "config": CHALLENGER_CONFIG.to_dict(),
             "risk_policy": challenger_rules(float(meta.get("start_cash") or 100_000)).to_dict(), "incumbent_rules": LIVE_RULES_VERSION,
             "commit": (code_version() or {}).get("commit"), "execution_version": EXECUTION_VERSION,
             "manifest_sha256": meta.get("manifest_sha256"), "account": chl["account"].name}
    return {"fingerprint": hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest(), "parts": parts}


# ================================================================================================ 6. bias audit
TRADING_SOURCES = ("engine.py", "scheduler.py", "execution.py", "allocator.py", "challenger.py", "features.py")


def _lookahead(account: Path) -> dict:
    """Fills earlier than the decision cycle allows; orders decided in a catch-up (retrospective) minute."""
    con = _ro(account)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(cycles)")}
        if "earliest_fill" not in cols:
            return {"checked": False, "early_fills": None, "catch_up_orders": None}
        early = con.execute("SELECT COUNT(*) FROM paper_orders o JOIN cycles c ON c.minute = o.decided_at WHERE o.status IN ('filled','partial') "
                            "AND c.earliest_fill IS NOT NULL AND o.fill_ts < c.earliest_fill").fetchone()[0]
        cu = con.execute("SELECT COUNT(*) FROM paper_orders o JOIN cycles c ON c.minute = o.decided_at WHERE c.mode = 'catch-up' "
                         "AND o.side = 'BUY'").fetchone()[0]
        late = con.execute("SELECT COUNT(*) FROM events WHERE late = 1").fetchone()[0]
    finally:
        con.close()
    return {"checked": True, "early_fills": int(early), "catch_up_orders": int(cu), "late_bars_excluded": int(late)}


def _config_evidence(rts: list[pd.DataFrame]) -> dict:
    r = pd.concat([x for x in rts if len(x)], ignore_index=True) if any(len(x) for x in rts) else pd.DataFrame(columns=["config_id", "ret"])
    if r.empty:
        return {"configs_traded": 0, "tested": 0, "survive": 0}
    g = r.groupby("config_id").ret
    tested = [(c, t_test(v)["p"]) for c, v in g if len(v) >= 5]
    keep = bh([p for _, p in tested], FDR_CONFIGS)
    return {"configs_traded": int(g.ngroups), "tested": len(tested), "survive": int(sum(keep)),
            "traded_without_evidence": int(g.ngroups - sum(keep))}


def bias_audit(ev: dict, ledger_ok: dict, approvals: list[dict], research: dict | None) -> list[dict]:
    inc, chl, hist = ev["incumbent"], ev["challenger"], ev["history"]
    rt = inc["rt"]
    A = []

    def add(name, risk, guard, status, evidence):
        A.append({"bias": name, "risk": risk, "guard": guard, "status": status, "evidence": evidence})

    la = _lookahead(inc["account"]) if inc["account"] else {"checked": False}
    add("Look-ahead", "a decision uses a price that was not yet known",
        "decisions on completed candles only; fills no earlier than the next bar; late minutes become catch-up and never trade",
        "not measurable yet" if not la.get("checked") else "detected" if (la["early_fills"] or la["catch_up_orders"]) else "guarded",
        "no timing columns in this ledger" if not la.get("checked") else
        f"{la['early_fills']} early fill(s), {la['catch_up_orders']} buy(s) decided on catch-up minutes; {la['late_bars_excluded']} late bar(s) excluded")
    base = Path(__file__).resolve().parent
    leaks = [f for f in TRADING_SOURCES if (base / f).exists() and re.search(r"opportunities|research_db|signal_outcomes|screening\.integrity|import integrity|benchmarks",
                                                                             (base / f).read_text())]
    add("Hindsight", "outcomes known only later leak into decisions",
        "rejected and missed opportunities are scored only in a separate research store that no trading module reads",
        "detected" if leaks else "guarded", f"trading modules referencing research outcomes: {', '.join(leaks) or 'none'} (source scan)")
    pit = inc["account"] and la.get("checked")
    add("Point-in-time data", "revised or late data treated as if it had been available",
        "each bar keeps its arrival time; late bars are flagged and excluded; fundamentals are snapshotted daily",
        "guarded" if pit else "not measurable yet", "arrival, decision and earliest-fill times recorded per minute" if pit else "no ledger yet")
    syms = len((_meta(inc["account"]) if inc["account"] else {}).get("symbols") or [])
    add("Survivorship", "testing only on stocks that survived to today",
        "none yet for history: the watchlist is today's large caps; historical replays on it overstate results",
        "exposed", f"{syms} watchlist stocks chosen today; delisted or demoted stocks are absent from the daily history")
    refused = research.get("refused_rows") if research else None
    add("Selection", "judging only the trades taken",
        "every refused signal and missed move is recorded with its reason and scored in the research store",
        "guarded" if refused else "not measurable yet", f"{refused or 0:,} refused or missed setups scored so far")
    changes = [(v, n) for v, n in hist["versions"]]
    early = [f"{v} after {n} session(s)" for v, n in changes[1:] if n < MIN_SESSIONS]
    add("Recency", "changing rules after a few recent days",
        f"a rule change needs {MIN_SESSIONS} confirmatory sessions and the operator's approval",
        "detected" if early else "guarded" if len(changes) > 1 else "not measurable yet",
        ("rules changed with little evidence: " + "; ".join(early)) if early else f"{len(changes)} rules version(s) in the ledger")
    ce = ev["configs"]
    add("Data snooping / multiple testing", "with thousands of configurations, some look good by chance",
        f"Benjamini-Hochberg at {FDR_CONFIGS:.0%} over configurations with 5+ round trips, and at {ALPHA} over the hypotheses",
        "not measurable yet" if not ce["tested"] else "detected" if ce["survive"] == 0 else "guarded",
        f"{ce['configs_traded']} configurations traded, {ce['tested']} have 5+ round trips, {ce['survive']} survive correction"
        + (f"; research loop trials so far: {research['trials']:,}" if research and research.get("trials") is not None else ""))
    add("Overfitting", "rules tuned to the data they are judged on",
        "pre-registered hypotheses; only sessions after registration count; exploratory results are labelled and never decide",
        "guarded" if ev["hypotheses_registered"] else "not measurable yet",
        f"{ev['hypotheses_registered']} hypotheses pre-registered with falsification criteria in the hash-chained ledger")
    add("Confirmation", "looking only for evidence that agrees",
        "every hypothesis runs the same contradicting checks (best day/stock removed, doubled costs, delay, placebo, beta, regime)",
        "guarded", f"{len(ev['critic'])} contradicting checks on the incumbent; {sum(not c['survives'] for c in ev['critic'])} contradict it")
    add("Anchoring", "judging a change only against the previous version",
        "promotion requires beating cash, NIFTY 50 and buy and hold as well as the incumbent", "guarded",
        "promotion gates list every benchmark (see eligibility)")
    if len(rt) and rt.score.notna().any():
        q = rt.assign(good=rt.score.fillna(0) > 0, won=rt.net > 0)
        mx = {f"{'positive' if g else 'no positive'} expected edge, {'won' if w else 'lost'}": int(((q.good == g) & (q.won == w)).sum())
              for g in (True, False) for w in (True, False)}
        add("Outcome", "calling a decision good because it made money",
            "decision quality = expected edge recorded at decision time, reported separately from the result", "guarded",
            "; ".join(f"{k}: {v}" for k, v in mx.items()))
    else:
        add("Outcome", "calling a decision good because it made money",
            "decision quality = expected edge recorded at decision time, reported separately from the result", "not measurable yet",
            "no closed round trip with a recorded decision score yet")
    rho = spearman(rt.score, rt.ret) if len(rt) else None
    add("Overconfidence / calibration", "trusting a score that does not predict outcomes",
        f"the allocator's score at decision is ranked against the realised result once {MIN_TRADES} round trips exist",
        "not measurable yet" if len(rt) < MIN_TRADES or rho is None else "detected" if rho <= 0 else "guarded",
        f"rank correlation of score with net return: {rho:+.2f} over {len(rt)} round trips" if rho is not None
        else f"{len(rt)} round trip(s) with a score")
    unapproved = [v for v, _ in changes[1:] if not any(a.get("subject") == f"rules {v}" and a.get("decision") == "approve" for a in approvals)]
    add("Automation", "accepting an AI's change because AIs agree",
        "AI agreement is never evidence; a rule change needs a recorded operator decision in the ledger",
        "detected" if unapproved else "guarded",
        ("rules versions without a recorded operator approval in the evidence ledger: " + ", ".join(unapproved)) if unapproved
        else "every rules change has a recorded operator decision")
    return A


# ================================================================================================ 7. assemble
def _evidence(exp_dir: Path, data_dir: Path | None) -> dict:
    from nseql.screening.benchmarks import compare
    acc = accounts(exp_dir)
    cur = acc["incumbent"][-1] if acc["incumbent"] else None
    chl_acc = next((c for c in reversed(acc["challenger"]) if cur and c.name.startswith(cur.name)), None)
    # the incumbent's evidence spans every incumbent account (rules versions in sequence)
    daily = pd.concat([daily_returns(a) for a in acc["incumbent"]]) if acc["incumbent"] else pd.Series(dtype=float)
    daily = daily[~daily.index.duplicated(keep="last")].sort_index()
    rts = [round_trips(a) for a in acc["incumbent"]]
    rt_all = pd.concat([r for r in rts if len(r)], ignore_index=True) if any(len(r) for r in rts) else pd.DataFrame(columns=RT_COLS)
    bench, bench_daily = None, {}
    if cur is not None:
        try:
            bench = compare(cur, data_dir)
            for k, v in bench.get("series", {}).items():
                if k not in ("account (after costs)", "cash"):
                    bench_daily[k] = pd.Series(v["daily"], index=bench["sessions"], dtype=float)
        except Exception:       # noqa: BLE001 - a benchmark failure leaves those hypotheses unmeasurable
            bench = None
    versions, seen = [], {}
    for a in acc["incumbent"]:
        seen.setdefault(rules_version(a), 0)
        seen[rules_version(a)] += len(daily_returns(a))
    versions = list(seen.items())
    chl = None
    if chl_acc is not None:
        cd = daily_returns(chl_acc)
        chl = {"account": chl_acc, "daily": cd, "rt": round_trips(chl_acc)}
    inc = {"account": cur, "daily": daily, "rt": rt_all, "rt_current": round_trips(cur) if cur else rt_all.iloc[:0], "bench_daily": bench_daily}
    return {"incumbent": inc, "challenger": chl, "bench": bench, "history": {"versions": versions}, "incumbent_accounts": acc["incumbent"],
            "configs": _config_evidence(rts), "accounts": {k: [p.name for p in v] for k, v in acc.items()}}


def _peak_drawdown(account: Path) -> float | None:
    con = _ro(account)
    try:
        eq = [r[0] for r in con.execute("SELECT equity_inr FROM cycles WHERE equity_inr IS NOT NULL AND mode != 'warmup' ORDER BY minute")]
    finally:
        con.close()
    m = _meta(account)
    start = float(m.get("start_cash") or m.get("capital") or (eq[0] if eq else 0))
    if not eq or not start:
        return None
    e = np.concatenate([[start], np.asarray(eq, float)])
    return float((e[-1] / np.maximum.accumulate(e)[-1]) - 1)


def report(exp_dir: Path, data_dir: Path | None = None, research: dict | None = None, register: bool = True) -> dict:
    """The Evidence Integrity report. Deterministic for the same ledgers."""
    exp_dir = Path(exp_dir)
    ledger = Ledger(exp_dir)
    if register:
        register_seed_hypotheses(ledger)
    chain = ledger.verify()
    from nseql.screening.evidence_guard import verify_anchors
    anchors = verify_anchors(exp_dir, ledger)
    chain = {**chain, "anchors": anchors}
    crit = check_criteria(ledger, register=register)
    seals = verify_digests(ledger, exp_dir)
    hyps = {e["subject"]: e["body"] for e in ledger.entries("hypothesis")}
    approvals = [{"subject": e["subject"], **e["body"]} for e in ledger.entries("decision")]
    ev = _evidence(exp_dir, data_dir)
    inc = ev["incumbent"]
    ev["critic"] = critic(inc["rt"], inc["daily"], ev["bench"])
    ev["hypotheses_registered"] = len(hyps)
    results = [evaluate(h, ev) for h in hyps.values()]
    keep = bh([r.get("p") if r.get("enough") else None for r in results], ALPHA)
    for r, k in zip(results, keep):
        _status(r, k)
        r.pop("_hi", None)
        r["supporting"] = [] if r["status"] != "survived" else [r["reason"]]
        r["contradicting"] = [c for c in ev["critic"] if not c["survives"]] if r["id"] in ("H-EDGE", "H-SKILL", "H-INDEX", "H-SIMPLE") else []
    st_inc = stress(inc["account"], inc["rt"]) if inc["account"] is not None and len(inc["rt"]) else {"n": 0}
    pl_inc = placebo(inc["account"], inc["rt"]) if inc["account"] is not None and len(inc["rt"]) >= 2 else {"n": len(inc["rt"]), "p": None}
    ph = page_hinkley(inc["rt_current"].net.to_numpy()) if len(inc["rt_current"]) else {"alarm": False, "n": 0, "decided": False}
    regime = []
    if len(inc["rt"]):
        for k, g in inc["rt"].groupby(inc["rt"].regime.fillna("unknown")):
            regime.append({"regime": k, "round_trips": len(g), "net_inr": round(float(g.net.sum()), 2), "mean_ret": round(float(g.ret.mean()), 5),
                           "share_won": round(float((g.net > 0).mean()), 3)})
    audit = bias_audit(ev, chain, approvals, research)
    recon = {a.name: cash_reconciliation(a) for a in ([inc["account"]] if inc["account"] is not None else []) + ([ev["challenger"]["account"]] if ev["challenger"] else [])}
    audit.append({"bias": "Evaluation manipulation (Goodhart, reward hacking)", "risk": "the evaluated system loosens its own test or benchmarks",
                  "guard": "the evaluation criteria (thresholds, benchmarks, hypotheses, stop rules) are hashed into the ledger; a change is "
                           "flagged and blocks promotion until the operator approves that exact version",
                  "status": "detected" if crit["changed"] and not crit["approved"] else "guarded",
                  "evidence": (f"criteria changed since registration ({crit['registered'][:12]} -> {crit['hash'][:12]}), not approved"
                               if crit["changed"] and not crit["approved"] else f"criteria {crit['hash'][:12]} as registered"
                               + (" (change approved by the operator)" if crit["changed"] else ""))})
    from nseql.screening.concept_study import consistency as _cs_consistency
    cons = _cs_consistency(exp_dir)
    audit.append({"bias": "Research record consistency (memory poisoning)", "risk": "a research verdict edited outside the protocol",
                  "guard": "every concept verdict in the research store must equal the evidence ledger's record for it",
                  "status": "detected" if cons["mismatches"] else "guarded" if cons["checked"] else "not measurable yet",
                  "evidence": ("; ".join(f"{m['concept']}: store says '{m['research_store']}', ledger says '{m['ledger']}'" for m in cons["mismatches"][:3])
                               or f"{cons['checked']} verdict(s) match the ledger")})
    bad_cash = [k for k, v in recon.items() if v.get("checked") and not v["ok"]]
    audit.append({"bias": "Accounting integrity (hidden or reclassified trades)", "risk": "losing trades removed or edited; cash that does not add up",
                  "guard": "each closed day's fills are sealed by hash in the evidence ledger; cash is recomputed from every fill",
                  "status": "detected" if bad_cash or not seals["ok"] else "guarded" if any(v.get("checked") for v in recon.values()) else "not measurable yet",
                  "evidence": "; ".join([f"{k}: cash differs from the fills by Rs {recon[k]['difference_inr']:,.2f}" for k in bad_cash]
                                        + [f"{b['day']}: {b['problem']}" for b in seals["altered"]])
                              or f"cash reconciles with every fill ({', '.join(f'{k}: {v.get('fills', 0)} fills' for k, v in recon.items() if v.get('checked')) or 'no account'}); "
                                 f"{seals['sealed_days']} sealed day(s) unchanged"})
    # predeclared suspension rules
    trig = []
    la = _lookahead(inc["account"]) if inc["account"] is not None else {"checked": False}
    if la.get("checked") and (la["early_fills"] or la["catch_up_orders"]):
        trig.append({"rule": "S1", "detail": f"{la['early_fills']} early fill(s), {la['catch_up_orders']} catch-up buy(s)"})
    if bad_cash or not seals["ok"]:
        trig.append({"rule": "S5", "detail": "; ".join([f"{k} cash off by Rs {recon[k]['difference_inr']:,.2f}" for k in bad_cash]
                                                       + [b["day"] for b in seals["altered"]])})
    if not chain["ok"]:
        trig.append({"rule": "S2", "detail": f"chain broken at entry {chain['broken_at']}"})
    elif not anchors["ok"]:
        trig.append({"rule": "S2", "detail": "; ".join(p_["problem"] for p_ in anchors["problems"][:3])})
    dd = _peak_drawdown(inc["account"]) if inc["account"] is not None else None
    if dd is not None and dd <= -DRAWDOWN_SUSPEND:
        trig.append({"rule": "S3", "detail": f"{dd:.2%} below the account's peak"})
    if ph.get("alarm") and (pl_inc.get("p") is not None and pl_inc["p"] > 0.5):
        trig.append({"rule": "S4", "detail": f"Page-Hinkley alarm at round trip {ph['at']}; placebo p = {pl_inc['p']}"})
    for t in trig:
        t["id"] = f"{t['rule']}:{inc['account'].name if inc['account'] else '-'}"
        t["text"] = SUSPEND[t["rule"]]
    acked = {e["body"].get("trigger") for e in ledger.entries("acknowledged")}
    active = [t for t in trig if t["id"] not in acked]
    # promotion eligibility of the challenger: every required benchmark must EXIST for the same sessions (a missing one
    # blocks; it never counts as zero), comparisons need a block-bootstrap interval above an economic threshold, and the
    # operator's approval must be bound to this exact candidate (candidate_fingerprint).
    from nseql.screening.contest import block_bootstrap
    hc = next((r for r in results if r["id"] == "H-CHALLENGER"), None)
    chl = ev["challenger"]
    st_ch = stress(chl["account"], chl["rt"]) if chl and len(chl["rt"]) else {"n": 0}
    pl_ch = placebo(chl["account"], chl["rt"]) if chl and len(chl["rt"]) >= 2 else {"p": None}
    bench_cmp = {}
    if chl is not None and len(chl["daily"]):
        cd = chl["daily"]
        expo = _exposure(chl["account"])
        refs = {"cash": pd.Series(0.0, index=cd.index)}
        for k in ("NIFTY 50", "buy and hold watchlist", "trend rule (above 20-day average)"):
            if k in inc["bench_daily"]:
                refs[k] = inc["bench_daily"][k]
        if "buy and hold watchlist" in refs and expo is not None:
            refs["buy and hold at the challenger's exposure"] = refs["buy and hold watchlist"] * expo
        for k in REQUIRED_BENCHMARKS:
            if k not in refs:
                bench_cmp[k] = {"available": False, "pass": False, "reading": "benchmark not available for these sessions: promotion blocked"}
                continue
            j = pd.concat([cd, refs[k]], axis=1, keys=["c", "b"]).dropna()
            if len(j) < len(cd):
                bench_cmp[k] = {"available": False, "pass": False, "reading": f"benchmark covers {len(j)} of {len(cd)} sessions: promotion blocked"}
                continue
            ci = block_bootstrap((j.c - j.b).to_numpy())
            bench_cmp[k] = {"available": True, "sessions": int(len(j)), "mean_excess": ci["mean"], "low": ci["low"], "high": ci["high"],
                            # an interval from fewer sessions than the minimum is not evidence: with n <= the bootstrap block
                            # length every resample is the whole series and the interval collapses to the mean
                            "pass": bool(len(j) >= MIN_SESSIONS and ci["low"] is not None and ci["low"] > 0 and ci["mean"] >= ECONOMIC_EXCESS),
                            **({"reading": f"{len(j)} of {MIN_SESSIONS} sessions: too few for an interval"} if len(j) < MIN_SESSIONS else {})}
    fp = candidate_fingerprint(chl, exp_dir) if chl is not None else None
    appr = [a for a in approvals if fp and a.get("subject") == f"challenger {fp['fingerprint'][:16]}"
            and a.get("candidate_fingerprint", fp["fingerprint"]) == fp["fingerprint"]]
    approved = bool(appr and appr[-1].get("decision") == "approve")
    gates = [
        {"gate": "falsification criteria registered before the test data", "pass": hc is not None},
        {"gate": f"{MIN_SESSIONS} confirmatory forward sessions (a minimum, not a guarantee of sufficiency)", "pass": bool(hc and hc.get("enough"))},
        {"gate": "beats the incumbent after costs (survives Benjamini-Hochberg)", "pass": bool(hc and hc["status"] == "survived")},
        *[{"gate": f"beats {k}: interval above zero and at least {ECONOMIC_EXCESS:.2%} a day", "pass": v["pass"], "detail": v.get("reading")}
          for k, v in (bench_cmp.items() if bench_cmp else [(k, {"pass": False, "reading": "no challenger evidence"}) for k in REQUIRED_BENCHMARKS])],
        {"gate": "survives doubled costs and a one-minute delay", "pass": bool(st_ch.get("survives_costs") and st_ch.get("survives_delay"))},
        {"gate": f"beats random entries (placebo p < {ALPHA})", "pass": bool(pl_ch.get("p") is not None and pl_ch["p"] < ALPHA)},
        {"gate": "no integrity violation and no active suspension", "pass": not trig},
        {"gate": "evaluation criteria unchanged since registration (or the change approved by the operator)", "pass": crit["approved"]},
        {"gate": "no performance-deterioration alarm", "pass": not ph.get("alarm")},
        {"gate": "operator approval bound to this exact candidate (fingerprint)", "pass": approved},
    ]
    eligible_for_review = all(g["pass"] for g in gates[:-1])
    edge = next((r for r in results if r["id"] == "H-EDGE"), {})
    verdict = ("abstain" if edge.get("status") in (None, "abstain", "not measurable yet") else
               "no edge shown" if edge["status"] == "falsified" else "edge not falsified")
    weaknesses = [f"{a['bias']}: {a['evidence']}" for a in audit if a["status"] in ("detected", "exposed")]
    weaknesses += [f"{c['check']}: {c['detail']}" for c in ev["critic"] if not c["survives"]]
    if len(inc["rt"]) and inc["rt"].score.notna().any():
        share = float((inc["rt"].score.fillna(0) > 0).mean())
        if share < 0.5:
            weaknesses.append(f"decision quality: only {share:.0%} of round trips had a positive expected edge (allocator score after costs) "
                              "when they were taken; the rest were taken without evidence of an edge")
    if st_inc.get("n") and not st_inc.get("survives_costs"):
        weaknesses.append(f"doubled costs: Rs {st_inc['double_costs_inr']:,.0f} over {st_inc['n']} round trips")
    return {
        "version": VERSION, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "objective": "Not to prove the bot works: to find out whether it works, when, and when it must stop.",
        "verdict": verdict,
        "verdict_plain": {"abstain": "Abstain: there is not enough confirmatory evidence to say the bot has an edge. Paper trading continues only to collect it.",
                          "no edge shown": "The edge hypothesis was falsified on confirmatory data: the bot has not shown it beats cash after costs.",
                          "edge not falsified": "The edge hypothesis survived its pre-registered test. That is not proof; checks continue."}[verdict],
        "research_protocols": [{"key": e["subject"], **{k: e["body"].get(k) for k in ("registered_at", "dev_until", "holdout_from", "embargo",
                                "data_fingerprint_at_registration", "definitions_hash", "dataset", "development_until", "contaminated_from")},
                                "holdout_accesses": sum(1 for a in ledger.entries("holdout_access") if a["subject"] == e["subject"]),
                                "holdout_reused": any(a["body"].get("reused_by_other_definitions") for a in ledger.entries("holdout_access")
                                                      if a["subject"] == e["subject"])} for e in ledger.entries("protocol")],
        "ledger": chain, "accounts": ev["accounts"], "criteria": crit, "financial": {"reconciliation": recon, "sealed": seals},
        "hypotheses": results, "biases": audit, "critic": ev["critic"],
        "validation": {"stress": st_inc, "placebo": pl_inc, "drift": ph, "regimes": regime, "configs": ev["configs"],
                       "benchmarks": (ev["bench"] or {}).get("plain"),
                       "sessions": int(len(inc["daily"])), "round_trips": int(len(inc["rt"]))},
        "failed": [r for r in results if r["status"] == "falsified"],
        "weaknesses": weaknesses,
        "promotion": {"candidate": chl["account"].name if chl else None, "gates": gates, "benchmarks": bench_cmp,
                      "candidate_fingerprint": fp, "eligible_for_operator_review": eligible_for_review,
                      "approved": approved and eligible_for_review, "deployed": False, "promoted": False,
                      "states": "eligible for operator review -> approved (operator, bound to the fingerprint) -> deployed (a reviewed code "
                                "change by the operator); any change to the candidate voids an approval",
                      "note": "Even when every gate passes, only the operator can approve; nothing here changes a rule."},
        "suspension": {"rules": SUSPEND, "triggered": trig, "active": active,
                       "action": "kill switch engaged (no new buys; exits continue); only the operator can reset it"},
        "decisions": approvals,
        "settings": {"min_sessions": MIN_SESSIONS, "min_trades": MIN_TRADES, "alpha": ALPHA, "fdr_configs": FDR_CONFIGS,
                     "placebo_draws": PLACEBO_DRAWS, "seed": SEED, "cost_multiplier": COST_MULT, "delay_minutes": DELAY_MIN},
    }


def enforce(rep: dict, kill, ledger: Ledger | None = None) -> list[str]:
    """Engage the kill switch for each active (unacknowledged) suspension trigger. Only ever tightens."""
    done = []
    for t in rep.get("suspension", {}).get("active", []):
        if not kill.engaged:
            kill.engage(f"integrity suspension {t['rule']}: {t['text']} ({t['detail']})", datetime.now())
        if ledger is not None and not any(e["body"].get("trigger") == t["id"] for e in ledger.entries("suspension")):
            from nseql.screening.evidence_guard import GuardedLedger
            ledger = ledger if isinstance(ledger, GuardedLedger) else GuardedLedger(ledger, "evaluation")
            ledger.append("suspension", t["rule"], {"trigger": t["id"], "detail": t["detail"], "rule": t["text"]})
        done.append(t["id"])
    return done


def acknowledge(ledger: Ledger, rep: dict, operator: str, reason: str) -> list[str]:
    """Called when the operator resets the kill switch: current triggers are acknowledged so they do not re-engage it.
    A new trigger (a new account, a new rule) engages it again."""
    out = []
    for t in rep.get("suspension", {}).get("active", []):
        ledger.append("acknowledged", t["rule"], {"trigger": t["id"], "operator": operator, "reason": reason})
        out.append(t["id"])
    return out


def record(ledger: Ledger, rep: dict, day: str) -> int:
    """Append each hypothesis result to the immutable ledger when it changed (status or amount of confirmatory evidence)
    since the last record: passed and failed results alike are kept for good."""
    last = {}
    for e in ledger.entries("result"):
        last[e["subject"]] = e["body"]
    n = 0
    for h in rep["hypotheses"]:
        c = h.get("confirmatory") or {}
        body = {"day": day, "status": h["status"], "reason": h.get("reason"), "p": h.get("p"),
                "evidence": c.get("sessions", c.get("round_trips")), "version": rep["version"]}
        prev = last.get(h["id"])
        if prev is None or prev.get("status") != body["status"] or prev.get("evidence") != body["evidence"]:
            ledger.append("result", h["id"], body)
            n += 1
    return n

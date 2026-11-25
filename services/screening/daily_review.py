"""Automatic daily review (operator, 2026-11-24: "I dont know why are you asking me to run all these").

After each session closes, the lab itself runs the analyses that were previously typed by hand, on the live paper
account's own stored records, and saves them per day:

1. loss study     why the day's trades lost: entry context of every round trip, losers vs winners (loss_study.py)
2. day backtest   every registered strategy on its own with its own Rs 1,00,000, plus the challenger (day_backtest.py)
3. Monte Carlo    the day's closed trades reshuffled 1,000 times (seeded): how bad the drawdown could have been
                  with the same trades in another order (robustness.monte_carlo)
4. Fundamental agent  company results, news tone, events, crowd emotion and behavioural patterns as of the close,
                  saved as a daily snapshot (agents/fundamental.py; observation only, see its placebo test)

The per-trade root cause (rca.py) is already written by the scheduler at 15:30; the review only summarises it.

Each step runs on its own: a failing step is recorded with its error and the others still run. Nothing here trades,
changes rules or touches the account's ledger (read-only connections; replays run in scratch folders under the
review's own directory). The headline sentences are built only from the numbers in the saved results; nothing is
invented. One day is one sample: the review says so.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd

STEPS = ("trade_report", "benchmarks", "opportunities", "loss_study", "day_backtest", "monte_carlo", "fundamental_agent", "integrity")
VERSION = "1.0"


def review_root(exp_dir: Path) -> Path:
    return Path(exp_dir) / "daily_review"


def review_dir(exp_dir: Path, account: str, day: str) -> Path:
    return review_root(exp_dir) / account / day


def live_accounts(exp_dir: Path) -> list[Path]:
    """Incumbent live paper accounts (a live.json without a challenger key), oldest first."""
    base = Path(exp_dir) / "screening"
    out = []
    for d in sorted(base.glob("*/live.json")) if base.exists() else []:
        try:
            m = json.loads(d.read_text())
        except (OSError, ValueError):
            continue
        if not m.get("challenger") and (d.parent / "screening.sqlite").exists():
            out.append(d.parent)
    return sorted(out, key=lambda p: (p / "live.json").stat().st_mtime)


def closed_sessions(account: Path) -> list[str]:
    """Days on which the account processed the 15:30 minute at or after its live start (the session closed with data
    while the account was paper trading; warm-up history and holidays do not count)."""
    try:
        live_from = json.loads((Path(account) / "live.json").read_text()).get("live_from") or ""
    except (OSError, ValueError):
        live_from = ""
    con = sqlite3.connect(f"file:{Path(account) / 'screening.sqlite'}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT DISTINCT substr(minute,1,10) FROM cycles WHERE status='processed' AND substr(minute,12,5)='15:30' "
                           "AND minute >= ? ORDER BY 1", (live_from,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [r[0] for r in rows]


def status(exp_dir: Path, account: str, day: str) -> dict | None:
    f = review_dir(exp_dir, account, day) / "status.json"
    try:
        return json.loads(f.read_text()) if f.exists() else None
    except ValueError:
        return None


def day_trades(account: Path, day: str) -> list[float]:
    """The day's closed round trips (net after both legs' charges), in exit order."""
    from nseql.screening.metrics import _next_day
    con = sqlite3.connect(f"file:{Path(account) / 'screening.sqlite'}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT realized_paise FROM paper_orders WHERE side='SELL' AND status IN ('filled','partial') "
                           "AND fill_ts >= ? AND fill_ts < ? ORDER BY fill_ts", (day, _next_day(day))).fetchall()
    finally:
        con.close()
    return [float(r[0] or 0) / 100 for r in rows]


def _monte_carlo(account: Path, day: str) -> dict:
    from nseql.screening.robustness import monte_carlo
    meta = json.loads((Path(account) / "live.json").read_text())
    capital = float(meta.get("start_cash") or meta.get("capital") or 100_000)
    rep = {"account": Path(account).name, "day": day, "capital_inr": capital, **monte_carlo(day_trades(account, day), capital)}
    return rep


def _fundamental(account: Path, day: str, exp_dir: Path) -> dict:
    """The Fundamental Analysis Agent's dossier as of this session's close, saved as a snapshot (evidence for its placebo
    test). Observation only."""
    from nseql.agents import fundamental as FA
    from nseql.config import get_settings
    syms = json.loads((Path(account) / "live.json").read_text()).get("symbols") or []
    d = FA.analyse(syms, pd.Timestamp(f"{day}T15:35"), FA.default_sources(exp_dir, get_settings().data_dir, account))
    (Path(exp_dir) / "fundamental").mkdir(parents=True, exist_ok=True)
    FA.save(d, Path(exp_dir) / "fundamental" / "snapshots.sqlite")
    return d


def _integrity(exp_dir: Path, day: str) -> dict:
    """Evidence integrity (integrity.py): try to disprove the hypotheses; results go to the immutable ledger. Suspension
    triggers are written in the report; the API process engages the kill switch for them (this step runs in a subprocess)."""
    from nseql.config import get_settings
    from nseql.screening import integrity as I
    rep = I.report(exp_dir, get_settings().data_dir, research=_research_counts(exp_dir))
    rep["recorded"] = I.record(I.Ledger(exp_dir), rep, day)
    return rep


def _research_counts(exp_dir: Path) -> dict:
    out: dict = {"refused_rows": None, "trials": None}
    f = Path(exp_dir) / "research" / "opportunities.sqlite"
    if f.exists():
        con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        try:
            out["refused_rows"] = con.execute("SELECT (SELECT COUNT(*) FROM signal_outcomes WHERE status != 'accepted') + (SELECT COUNT(*) FROM missed)").fetchone()[0]
        except sqlite3.Error:
            pass
        finally:
            con.close()
    reg = Path(exp_dir) / "registry.sqlite"
    if reg.exists():
        con = sqlite3.connect(f"file:{reg}?mode=ro", uri=True)
        try:
            out["trials"] = con.execute("SELECT COUNT(*) FROM runs WHERE partition != 'sealed'").fetchone()[0]
        except sqlite3.Error:
            pass
        finally:
            con.close()
    return out


def _write(f: Path, obj) -> None:
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    tmp.replace(f)


def run(account: Path, day: str, exp_dir: Path, steps: tuple[str, ...] = STEPS, force: bool = False) -> dict:
    """Run the review for one closed session of one account. Re-running a finished review is a no-op unless `force`."""
    from nseql.screening import day_backtest, loss_study
    account = Path(account)
    out = review_dir(exp_dir, account.name, day)
    st = status(exp_dir, account.name, day)
    if st and st.get("state") == "done" and not force:
        return json.loads((out / "review.json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    st = {"account": account.name, "day": day, "version": VERSION, "state": "running", "started_at": datetime.now().isoformat(timespec="seconds"),
          "steps": {s: {"state": "waiting"} for s in steps}}
    _write(out / "status.json", st)
    results: dict = {}
    for s in steps:
        st["steps"][s] = {"state": "running"}
        _write(out / "status.json", st)
        t0 = time.time()
        try:
            if s == "trade_report":
                from nseql.screening.daily_report import run as _tr
                results[s] = _tr(account, day)
                _write(out / "trade_report.json", results[s])
            elif s == "benchmarks":
                from nseql.config import get_settings
                from nseql.screening.benchmarks import compare
                results[s] = compare(account, get_settings().data_dir)
                _write(out / "benchmarks.json", results[s])
            elif s == "opportunities":
                from nseql.screening.opportunities import run as _op
                results[s] = _op(account, day, exp_dir)
                _write(out / "opportunities.json", results[s])
            elif s == "loss_study":
                shutil.rmtree(out / "loss_study", ignore_errors=True)
                results[s] = loss_study.run(account, day, out / "loss_study")
            elif s == "day_backtest":
                shutil.rmtree(out / "day_backtest", ignore_errors=True)
                results[s] = day_backtest.run(account, day, out / "day_backtest")
            elif s == "monte_carlo":
                results[s] = _monte_carlo(account, day)
                _write(out / "monte_carlo.json", results[s])
            elif s == "fundamental_agent":
                results[s] = _fundamental(account, day, exp_dir)
                _write(out / "fundamental_agent.json", results[s])
            elif s == "integrity":
                results[s] = _integrity(exp_dir, day)
                _write(out / "integrity.json", results[s])
            st["steps"][s] = {"state": "done", "seconds": round(time.time() - t0, 1)}
        except Exception as e:      # noqa: BLE001 - one failing analysis never stops the others
            st["steps"][s] = {"state": "failed", "seconds": round(time.time() - t0, 1), "error": f"{type(e).__name__}: {e}",
                              "trace": traceback.format_exc(limit=4)}
        _write(out / "status.json", st)
    for s in ("loss_study", "day_backtest"):                  # scratch replay ledgers are large; keep only the reports
        for sub in (out / s).glob("*") if (out / s).exists() else []:
            if sub.is_dir():
                shutil.rmtree(sub, ignore_errors=True)
            elif sub.suffix != ".json":
                sub.unlink(missing_ok=True)
    rev = build(account.name, day, results, st)
    _write(out / "review.json", rev)
    st["state"] = "done" if all(v["state"] == "done" for v in st["steps"].values()) else "partial"
    st["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _write(out / "status.json", st)
    return rev


def _inr(v: float) -> str:
    return f"{'-' if v < 0 else ''}Rs {abs(v):,.0f}"


def build(account: str, day: str, r: dict, st: dict) -> dict:
    """The review document: plain sentences first, each derived from a saved number, then the compact results."""
    ls, bt, mc = r.get("loss_study"), r.get("day_backtest"), r.get("monte_carlo")
    head: list[str] = []
    tr = r.get("trade_report")
    if tr:
        head.append("Today: " + tr["plain"])
        for x in tr["next_time"][:3]:
            head.append(f"Next time ({x['trades']} trades, {x['lost_inr']:,.0f} INR lost): {x['next_time']}")
    bm = r.get("benchmarks")
    if bm and bm.get("plain"):
        head.append("Versus benchmarks: " + bm["plain"])
    op = r.get("opportunities")
    if op:
        head.extend(op.get("plain", [])[:4])
    rec = (ls or {}).get("reconciliation") or {}
    if ls and not rec.get("reconciled"):
        head.append(f"Not published: the study did not reconcile with the paper ledger ({rec.get('analysed_round_trips')} analysed vs "
                    f"{rec.get('ledger_round_trips')} ledger round trips; {rec.get('mismatched')} mismatched). The tables below are withheld.")
        ls = None
    if ls:
        head.append(f"The bot closed {ls['trades']} round trips, net {_inr(ls['net_inr'])} after charges; "
                    f"{ls['win_rate']:.0%} were winners, average {ls['mean_inr']:+.2f} INR per trade.")
        fs = ls.get("findings") or []
        if fs:
            head.extend(f["plain"] for f in fs[:3])
        else:
            head.append("No single factor separated the losing trades from the winners clearly on this day.")
        cells = [c for tf in ls.get("two_factor", []) for c in tf.get("cells", []) if c.get("finding")]
        if cells:
            c = min(cells, key=lambda c: c["mean_inr"])
            head.append(f"Worst combination: '{c['a']}' with '{c['b']}' ({c['trades']} trades, {c['mean_inr']:+.2f} INR per trade).")
    elif mc and mc.get("trades") is not None:
        head.append(f"The bot closed {mc['trades']} round trips" + (f", net {_inr(mc['net_pnl_inr'])} after charges." if "net_pnl_inr" in mc else "."))
    if bt:
        sp = bt.get("share_positive")
        if sp is not None:
            head.append(f"Run one by one, {bt['traded']:,} of {bt['strategies']:,} strategies traded and {sp:.0%} of those made money "
                        f"(about 50% would by luck alone).")
        typed = [g for g in bt.get("by_family", []) if g["trades"] >= 20 and g["net_per_trade"] is not None]
        if len(typed) >= 2:
            lo, hi = min(typed, key=lambda g: g["net_per_trade"]), max(typed, key=lambda g: g["net_per_trade"])
            head.append(f"Weakest strategy type: {lo['group']} ({lo['net_per_trade']:+.2f} INR per trade); strongest: {hi['group']} "
                        f"({hi['net_per_trade']:+.2f} INR per trade).")
        ch = bt.get("challenger")
        if ch:
            head.append(f"The challenger on the same day: {ch['trades']} trades, net {_inr(ch['net_inr'])}.")
    if mc and mc.get("plain"):
        head.append("Trade-order luck: " + mc["plain"] + ".")
    fa = r.get("fundamental_agent")
    if fa:
        head.append("Fundamental agent (observation only): " + fa["market"]["emotion"].get("plain", ""))
        lean = {k: [s for s, x in fa["stocks"].items() if x["stance"].startswith(k)] for k in ("supportive", "cautious")}
        due = [s for s, x in fa["stocks"].items() if "results due" in x["stance"]]
        bits = [f"{k}: {', '.join(v[:6])}{' ...' if len(v) > 6 else ''}" for k, v in lean.items() if v] + ([f"results due: {', '.join(due)}"] if due else [])
        if bits:
            head.append("Stocks: " + "; ".join(bits) + ".")
    ig = r.get("integrity")
    if ig:
        head.append("Evidence integrity: " + ig["verdict_plain"])
        if ig["suspension"]["active"]:
            head.append("Suspension rule fired: " + "; ".join(f"{t['rule']} {t['detail']}" for t in ig["suspension"]["active"]) + ".")
    failed = [k for k, v in st["steps"].items() if v["state"] == "failed"]
    if failed:
        head.append("Not available: " + ", ".join(failed).replace("_", " ") + " (the error is shown below).")
    compact_ls = None
    if ls:
        compact_ls = {k: ls.get(k) for k in ("trades", "net_inr", "mean_inr", "win_rate", "findings", "factors", "two_factor",
                                             "reconciliation", "journal_coverage", "source")}
    compact_bt = None
    if bt:
        compact_bt = {k: bt.get(k) for k in ("strategies", "symbols", "traded", "trades", "net_inr", "charges_inr", "positive", "share_positive",
                                             "luck_baseline", "by_family", "by_timeframe", "by_stop", "best_10", "worst_10", "challenger", "execution")}
    return {"account": account, "day": day, "version": VERSION, "generated_at": datetime.now().isoformat(timespec="seconds"),
            "headline": head, "steps": st["steps"], "loss_study": compact_ls, "day_backtest": compact_bt, "monte_carlo": mc,
            "fundamental_agent": r.get("fundamental_agent"), "trade_report": tr, "benchmarks": bm, "opportunities": op,
            "integrity": ({k: r["integrity"][k] for k in ("verdict", "verdict_plain", "weaknesses", "suspension", "recorded")} if r.get("integrity") else None),
            "caution": "One day is one sample. These findings are ideas to test on later days, not proof; no rule is changed by this review."}


class ReviewQueue:
    """Runs reviews one at a time in the background, in a separate low-priority process, never during market hours.

    `busy()` returns True while the market session is on (the review waits; the live bot keeps the CPU). The default
    runner starts `python -m nseql.cli daily-review` so a crash or a memory spike in an analysis cannot touch the lab.
    """

    def __init__(self, exp_dir: Path, busy=lambda: False, runner=None, log=print, poll_s: float = 60.0, after=None):
        import threading
        self.exp_dir, self.busy, self.log, self.poll_s = Path(exp_dir), busy, log, poll_s
        self.runner = runner or self._subprocess
        self.after = after                       # called after each review (e.g. the integrity suspension check)
        self.pending: list[tuple[str, str]] = []
        self.current: tuple[str, str] | None = None
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None

    def submit(self, account: str, day: str, force: bool = False) -> str:
        """Queue a review. Returns 'done', 'queued' or 'running' (never queues twice)."""
        key = (account, day)
        with self._cv:
            if key == self.current:
                return "running"
            if key in self.pending:
                return "queued"
            st = status(self.exp_dir, account, day)
            if st and st.get("state") == "done" and not force:
                return "done"
            if force:
                f = review_dir(self.exp_dir, account, day) / "status.json"
                if f.exists():
                    f.unlink()
            self.pending.append(key)
            self._cv.notify()
        self._ensure()
        return "queued"

    def catch_up(self, last_n: int = 5) -> list[tuple[str, str]]:
        """Queue the latest closed sessions of every live account that have no finished review (e.g. after a restart)."""
        queued = []
        for acc in live_accounts(self.exp_dir):
            for day in closed_sessions(acc)[-last_n:]:
                st = status(self.exp_dir, acc.name, day)
                if not st or st.get("state") not in ("done", "partial"):
                    if self.submit(acc.name, day) == "queued":
                        queued.append((acc.name, day))
        return queued

    def state(self, account: str, day: str) -> str:
        with self._cv:
            if (account, day) == self.current:
                return "running"
            if (account, day) in self.pending:
                return "queued"
        st = status(self.exp_dir, account, day)
        if st and st.get("state") == "running":          # a status left 'running' by a process that is gone
            return "interrupted"
        return st["state"] if st else "none"

    def _ensure(self) -> None:
        import threading
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="daily-review", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self.pending:
                    if not self._cv.wait(timeout=600):
                        return
            while self.busy():
                time.sleep(self.poll_s)
            with self._cv:
                if not self.pending:
                    continue
                self.current = self.pending.pop(0)
            acc, day = self.current
            self.log(f"daily review {day} ({acc}): started")
            try:
                ok = self.runner(acc, day)
                self.log(f"daily review {day} ({acc}): {'finished' if ok else 'finished with errors (see the Daily Review page)'}")
            except Exception as e:      # noqa: BLE001
                self.log(f"daily review {day} ({acc}): could not run: {type(e).__name__}: {e}")
            finally:
                with self._cv:
                    self.current = None
            if self.after is not None:
                try:
                    self.after()
                except Exception as e:      # noqa: BLE001
                    self.log(f"after-review check failed: {type(e).__name__}: {e}")

    def _subprocess(self, account: str, day: str) -> bool:
        import os
        import subprocess
        import sys
        out = review_dir(self.exp_dir, account, day)
        out.mkdir(parents=True, exist_ok=True)
        flags = 0x00004000 if os.name == "nt" else 0            # BELOW_NORMAL_PRIORITY_CLASS on Windows
        with open(out / "review.log", "w") as fh:
            p = subprocess.Popen([sys.executable, "-m", "nseql.cli", "daily-review", "--account", account, "--day", day],
                                 stdout=fh, stderr=subprocess.STDOUT, creationflags=flags,
                                 preexec_fn=(lambda: os.nice(10)) if os.name != "nt" else None)
            return p.wait() == 0

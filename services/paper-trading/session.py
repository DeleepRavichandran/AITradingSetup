"""Paper-trading session.

Runs strategies against a stream of bars using THE SAME engine step, execution simulator, cost
model, margin model and ledger as the backtester — only the source of bars differs. Orders are
simulated; nothing is sent to a broker.

Safety behaviour (acceptance test #15): a stale feed, a feed error, a breached session loss
limit or a failed reconciliation engages the kill switch, cancels all working orders and stops
the session from accepting new orders until a named human resets it.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime

import numpy as np
import pandas as pd

from nseql.analytics.metrics import compute_metrics
from nseql.backtester.engine import Allocation, BacktestEngine
from nseql.backtester.execution import ExecutionConfig
from nseql.data.portal import FIELDS, Bar
from nseql.instruments.models import Instrument
from nseql.instruments.rules import ChargeSchedule
from nseql.risk.limits import KillSwitch, RiskLimits
from nseql.strategies.api import Strategy


class StreamPortal:
    """Point-in-time store that grows as bars arrive. Same read interface as DataPortal."""

    def __init__(self, instruments: dict[str, Instrument], actions=None, interval: str = "1d"):
        self.instruments, self.actions, self.interval = instruments, list(actions or []), interval
        self._a: dict[str, dict[str, list]] = {}
        self._now: pd.Timestamp | None = None
        self._current: dict[str, Bar] = {}
        self.timeline = np.array([], dtype="int64")

    @property
    def symbols(self) -> list[str]:
        return list(self.instruments)

    def push(self, t: pd.Timestamp, bars: dict[str, Bar]) -> None:
        if self._now is not None and t <= self._now:
            raise ValueError("bars must arrive in strictly increasing time order")
        self._now = t
        for s, b in bars.items():
            a = self._a.setdefault(s, {"ts": [], **{f: [] for f in FIELDS}})
            a["ts"].append(t)
            for f in FIELDS:
                a[f].append(getattr(b, f))
        self._current = dict(bars)

    def _factor(self, symbol: str, ts: list) -> np.ndarray:
        g = np.ones(len(ts))
        for ca in self.actions:
            if ca.symbol == symbol and ca.kind in ("split", "bonus") and self._now is not None and ca.ex_date <= self._now.date():
                g[np.array([t.date() < ca.ex_date for t in ts])] /= ca.ratio
        return g

    def history(self, symbol, field="close", n=None, adjusted=True):
        a = self._a.get(symbol)
        if not a:
            return np.empty(0)
        out = np.asarray(a[field][-n:] if n else a[field], dtype=float)
        if adjusted and field != "oi" and self.actions:
            g = self._factor(symbol, a["ts"][-len(out):])
            out = out / g if field == "volume" else out * g
        return out

    def current(self, symbol):
        return self._current.get(symbol)

    def last(self, symbol, field="close"):
        a = self._a.get(symbol)
        return float(a[field][-1]) if a else None

    def last_ts(self, symbol):
        a = self._a.get(symbol)
        return a["ts"][-1] if a else None

    def bars_seen(self, symbol):
        a = self._a.get(symbol)
        return len(a["ts"]) if a else 0

    def listed_now(self, d):
        return [s for s in self._a if self.instruments[s].listed_on(d)]


class ManualBook(Strategy):
    """A book whose orders come from a person (the paper-trading terminal), not from code."""
    name = "manual"
    family = "manual"

    def __init__(self):
        super().__init__()
        self.queue: deque = deque()
        self.results: list[dict] = []

    def on_bar(self, ctx):
        while self.queue:
            o = self.queue.popleft()
            oid = ctx.order(o["symbol"], o["qty"], type=o.get("type", "MARKET"), limit=o.get("limit"), stop=o.get("stop"),
                            product=o.get("product"), tif=o.get("tif", "DAY"), tag=o.get("tag", "manual"))
            self.results.append({"request": o, "order_id": oid})


class _SessionGate:
    """What the engine consults before accepting an order: the global kill switch, this session's own
    halt (permanent), and the warm-up phase (history is being fed; nothing may trade yet)."""

    def __init__(self, global_kill: KillSwitch):
        self.global_kill = global_kill
        self.halted = ""
        self.warmup = False

    @property
    def engaged(self) -> bool:
        return bool(self.halted) or self.warmup or self.global_kill.engaged

    @property
    def reason(self) -> str:
        return self.halted or ("WARMUP: history is loading, trading has not started" if self.warmup else self.global_kill.reason)


def bars_to_json(bars: dict[str, Bar]) -> dict:
    return {s: [b.open, b.high, b.low, b.close, b.volume, b.oi] for s, b in bars.items()}


def bars_from_json(t: pd.Timestamp, d: dict) -> dict[str, Bar]:
    return {s: Bar(s, t, float(v[0]), float(v[1]), float(v[2]), float(v[3]), int(v[4]), int(v[5])) for s, v in d.items()}


class PaperSession:
    def __init__(self, feed, allocations: list[Allocation], *, kill: KillSwitch | None = None,
                 exec_cfg: ExecutionConfig | None = None, risk: RiskLimits | None = None, max_session_loss_pct: float = 0.05,
                 stale_after_seconds: float = 120.0, notify=None, label: str = "", journal=None):
        self.feed = feed
        self.kill = kill or KillSwitch()
        self.gate = _SessionGate(self.kill)
        self.journal = journal or (lambda record: None)   # write-ahead log of every input (see paper.store)
        self.replaying = False                            # True while being rebuilt from the journal
        self.portal = StreamPortal(feed.instruments, getattr(feed, "actions", []), getattr(feed, "interval", "1d"))
        self.manual = next((a.strategy for a in allocations if isinstance(a.strategy, ManualBook)), None)
        self.engine = BacktestEngine(self.portal, allocations, exec_cfg=exec_cfg, risk=risk, kill_switch=self.gate,
                                     charges=ChargeSchedule(allow_extrapolation=True))
        self.capital = sum(a.capital for a in allocations)
        self.max_session_loss_pct = max_session_loss_pct
        self.stale_after = stale_after_seconds
        self.notify = notify or (lambda event, text: None)
        self.label = label
        self.k = 0
        self.started = False
        self.halted_reason: str | None = None
        self.last_bar_wallclock: datetime | None = None
        self.curve: list[tuple[pd.Timestamp, int]] = []
        self.created = datetime.now().isoformat(timespec="seconds")

    # ---------------------------------------------------------------- data in
    def on_bars(self, t: pd.Timestamp, bars: dict[str, Bar], session_end: bool, wallclock: datetime | None = None,
                warmup: bool = False) -> None:
        if not self.replaying:      # journal BEFORE processing: a crash mid-step replays the step on restart
            self.journal({"t": "bar", "ts": str(t), "end": bool(session_end), "warm": bool(warmup), "bars": bars_to_json(bars)})
        self.gate.warmup = warmup
        if not self.started:
            self.engine.begin(t)
            self.started = True
        self.portal.push(t, bars)
        self.engine.step(self.k, t, bars, session_end)
        self.k += 1
        self.last_bar_wallclock = wallclock or datetime.now()
        eq = self.engine.account.equity()
        self.curve.append((t, eq))
        self.gate.warmup = False
        if self.replaying:
            return                   # halts are restored from their own journal records, in order
        if eq < self.capital * 100 * (1 - self.max_session_loss_pct):
            self.halt(f"session loss limit breached: equity {eq / 100:,.0f} vs capital {self.capital:,.0f}", t)
        elif not self.engine.account.reconcile()["ok"]:
            self.halt("ledger reconciliation failed", t)

    def advance(self, n: int = 1) -> int:
        """Pull up to n steps from the feed. Returns the number processed."""
        done = 0
        for _ in range(n):
            try:
                nxt = self.feed.next()
            except Exception as e:      # any feed failure is a reason to stop trading, not to guess
                self.on_feed_error(e)
                break
            if nxt is None:
                break
            self.on_bars(*nxt)
            done += 1
        return done

    # ---------------------------------------------------------------- safety
    def halt(self, reason: str, t=None) -> None:
        if self.halted_reason:
            return
        self.halted_reason = reason
        self.gate.halted = reason                 # this session never trades again, even after a kill-switch reset
        if not self.replaying:
            self.journal({"t": "halt", "reason": reason})
            self.kill.engage(reason, t or datetime.now())
        for o in self.engine._active():
            self.engine._cancel(o.id, f"halt: {reason}")
        self.engine._event("HALT", reason=reason)
        if not self.replaying:
            self.notify("halt", f"Paper session {self.label or ''} HALTED: {reason}. Working orders cancelled; positions left as they are.")

    def on_feed_error(self, exc: Exception) -> None:
        self.halt(f"market-data feed failure: {type(exc).__name__}: {exc}")

    def check_feed(self, now: datetime) -> bool:
        """Call periodically while the market is open. Halts and returns False if the feed is stale."""
        if self.last_bar_wallclock is None or (now - self.last_bar_wallclock).total_seconds() > self.stale_after:
            self.halt(f"stale market-data feed (no bar for more than {self.stale_after:.0f}s)")
            return False
        return True

    # ---------------------------------------------------------------- orders & state
    def submit_manual(self, order: dict) -> dict:
        if self.manual is None:
            raise ValueError("session has no manual book")
        if self.gate.engaged:
            return {"status": "REJECTED", "reason": f"KILL_SWITCH: {self.gate.reason}"}
        if not self.replaying:
            self.journal({"t": "order", "order": order})
        self.manual.queue.append(order)
        return {"status": "QUEUED", "note": "will be submitted at the next bar and can fill from the bar after that"}

    def apply_record(self, rec: dict) -> None:
        """Re-apply one journal record. Used only while rebuilding a session after a restart."""
        if rec["t"] == "bar":
            t = pd.Timestamp(rec["ts"])
            self.on_bars(t, bars_from_json(t, rec["bars"]), rec["end"], warmup=rec.get("warm", False))
        elif rec["t"] == "order" and self.manual is not None:
            self.manual.queue.append(rec["order"])
        elif rec["t"] == "halt":
            self.halt(rec["reason"])

    def positions_from_fills(self) -> dict[tuple[str, str], int]:
        """Positions rebuilt independently from the fill tape (adjusted for splits by the ledger's own events)."""
        pos: dict[tuple[str, str], int] = {}
        for f in self.engine.account.fills:
            pos[(f.book, f.symbol)] = pos.get((f.book, f.symbol), 0) + f.side * f.qty
        return {k: v for k, v in pos.items() if v}

    def reconcile(self) -> dict:
        acct = self.engine.account
        book = {(b.id, s): p.qty for b in acct.books.values() for s, p in b.positions.items()}
        tape = self.positions_from_fills()
        split_syms = {e["symbol"] for e in self.engine.events if e["type"] == "CORPORATE_ACTION" and e["action"] != "dividend"}
        diffs = [{"book": k[0], "symbol": k[1], "ledger": book.get(k, 0), "fill_tape": tape.get(k, 0)}
                 for k in sorted(set(book) | set(tape)) if book.get(k, 0) != tape.get(k, 0) and k[1] not in split_syms]
        rec = acct.reconcile()
        return {"ok": rec["ok"] and not diffs, "ledger_checks": rec, "position_breaks": diffs, "positions": len(book),
                "fills": len(acct.fills)}

    def state(self) -> dict:
        acct = self.engine.account
        ts = np.array([t.value for t, _ in self.curve], dtype="int64")
        eq = np.array([e for _, e in self.curve], dtype="int64")
        trades = [t.to_dict() for t in acct.trades]
        m = compute_metrics(ts, eq, trades, initial_capital=self.capital,
                            total_charges=sum(b.charges for b in acct.books.values()) / 100) if len(ts) else {}
        return {
            "label": self.label, "created": self.created, "feed": getattr(self.feed, "kind", "?"),
            "feed_note": "REPLAY of stored data — not a live market" if getattr(self.feed, "kind", "") == "replay" else "",
            "synthetic": getattr(getattr(self.feed, "ds", None), "synthetic", None),
            "now": str(self.curve[-1][0]) if self.curve else None, "steps": self.k,
            "remaining": getattr(self.feed, "remaining", None), "halted": self.halted_reason,
            "kill_switch": {"engaged": self.gate.engaged, "reason": self.gate.reason},
            "equity": acct.equity() / 100 if self.started else self.capital, "cash": acct.cash / 100 if self.started else self.capital,
            "capital": self.capital, "metrics": m,
            "positions": [{"book": b.id, "symbol": p.symbol, "qty": p.qty, "avg_price": p.avg_price,
                           "mark": acct.marks.get(p.symbol, 0) / 100, "unrealized": acct.unrealized(p) / 100}
                          for b in acct.books.values() for p in b.positions.values()],
            "open_orders": [o.to_dict() for o in self.engine._active()],
            "orders": [o.to_dict() for o in self.engine.orders[-200:]],
            "fills": [f.to_dict() for f in acct.fills[-200:]],
            "events": self.engine.events[-200:],
            "curve": {"ts": [str(t) for t, _ in self.curve[-2000:]], "equity": [e / 100 for _, e in self.curve[-2000:]]},
            "reconciliation": self.reconcile() if self.started else None,
        }

    def drift(self, expected_daily_mean: float, expected_daily_std: float) -> dict:
        """Forward-performance drift: z-score of realised mean daily return versus the backtest's."""
        if len(self.curve) < 10 or expected_daily_std <= 0:
            return {"status": "insufficient data", "sessions": len(self.curve)}
        s = pd.Series([e for _, e in self.curve], index=[t for t, _ in self.curve], dtype=float)
        r = s.groupby(s.index.normalize()).last().pct_change().dropna()
        if len(r) < 5:
            return {"status": "insufficient data", "sessions": int(len(r))}
        z = (r.mean() - expected_daily_mean) / (expected_daily_std / np.sqrt(len(r)))
        return {"status": "DRIFT" if z < -2 else "ok", "z": float(z), "sessions": int(len(r)), "realised_mean": float(r.mean()),
                "expected_mean": expected_daily_mean}

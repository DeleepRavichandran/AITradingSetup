"""The master one-minute scheduler for the screening loop.

`run_cycle(now)` is the whole behaviour; the background thread only calls it a few seconds after every
minute boundary while the market is open. One cycle:

  1. pulls new 1-minute bars from the feed (if one is attached) into the durable event log
     (duplicates ignored, conflicting copies quarantined, bars for already-processed minutes marked late);
  2. finds every minute with data that is complete (its close + `grace_seconds` has passed) and not yet
     processed, and processes them IN ORDER: a scheduler that was paused or restarted catches up;
  3. records session minutes that had no data at all as `no_data` cycles (a feed gap, visible on the
     dashboard) - they are never silently skipped;
  4. checkpoints the engine state every `checkpoint_every` minutes and at the end of the session.

Recovery: on start the last checkpoint is loaded and every logged minute after it is replayed through
the same deterministic engine. Ledger writes use deterministic keys, so minutes that were already
written before a crash are not written twice.

Feeds: `ReplayMinuteFeed` (stored bars replayed as if live) and `nseql.screening.feeds.BrokerMinutePoller`
(completed 1-minute candles polled from Angel One; mock-tested, NOT yet verified against the real service).
No WebSocket/tick streaming adapter exists (KiteTicker and Angel One SmartStream are not implemented).
With no feed, the scheduler processes bars pushed into the event log (`ingest`). Every status response
carries a provenance block (nseql.screening.provenance) saying which of these the data is.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from nseql.instruments.calendar import IST, TradingCalendar
from nseql.screening.engine import ScreeningEngine
from nseql.screening.execution import ExecConfig, PortfolioRules
from nseql.screening.features import SESSION_MINUTES, SESSION_OPEN_MIN, minute_of_session
from nseql.screening.registry import FAMILIES, HORIZON, TF_NAMES, build
from nseql.screening.store import ScreeningStore, _iso


def _in_session(ts) -> bool:
    m = minute_of_session(pd.Timestamp(ts))
    return 1 <= m <= SESSION_MINUTES

class ReplayMinuteFeed:
    """Serves stored 1-minute bars as if they were arriving live: fetch(now) returns every bar whose close
    time is <= now and that has not been returned yet. A REPLAY, labelled as such everywhere."""
    kind = "replay"

    def __init__(self, frames: dict[str, pd.DataFrame], label: str = "replay", synthetic: bool = False):
        self.label, self.synthetic = label, synthetic
        rows = []
        for s, df in frames.items():
            d = df[["ts", "open", "high", "low", "close", "volume"]].copy()
            d["symbol"] = s
            rows.append(d)
        self.df = pd.concat(rows).sort_values(["ts", "symbol"]).reset_index(drop=True) if rows else pd.DataFrame()
        self._k = 0
        self._ts = self.df["ts"].to_numpy() if len(self.df) else np.array([], dtype="datetime64[ns]")

    def fetch(self, now) -> list[dict]:
        j = int(np.searchsorted(self._ts, np.datetime64(pd.Timestamp(now)), side="right"))
        out = self.df.iloc[self._k:j].to_dict("records")
        self._k = max(self._k, j)
        return out

    def minutes(self) -> list[pd.Timestamp]:
        return [pd.Timestamp(x) for x in np.unique(self._ts)]


class MinuteScheduler:
    def __init__(self, root: Path, symbols: list[str], n_configs: int | None = None, *, calendar: TradingCalendar | None = None,
                 feed=None, kill=None, checkpoint_every: int = 15, grace_seconds: float = 5.0, mode: str = "replay",
                 clock: Callable[[], datetime] | None = None, execution: str = "realistic", exec_cfg: ExecConfig | None = None,
                 portfolio: PortfolioRules | None | bool = True, live_from=None, experiments_dir: Path | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        self.reg = build(symbols, n_configs)
        if execution not in ("realistic", "simple"):
            raise ValueError("execution must be 'realistic' or 'simple'")
        if execution == "simple":
            exec_cfg, portfolio = None, None
        else:
            exec_cfg = exec_cfg or ExecConfig()
            portfolio = PortfolioRules() if portfolio is True else (portfolio or None)
        self.engine = ScreeningEngine(self.reg, exec_cfg, portfolio)
        self.store = ScreeningStore(self.root / "screening.sqlite")
        r = self.reg
        self.store.register_configs([(str(r.ids[i]), i, r.symbols[r.sym[i]], TF_NAMES[r.tf[i]], HORIZON[TF_NAMES[r.tf[i]]],
                                      FAMILIES[r.fam[i]], json.dumps(r.params[i]), float(r.stop[i])) for i in range(len(r))])
        self.calendar = calendar or TradingCalendar()
        self.feed, self.kill, self.mode = feed, kill, mode
        self.experiments_dir = experiments_dir
        self.live_from = _iso(live_from) if live_from is not None else None   # minutes before this: no paper orders (warm-up)
        self.checkpoint_every, self.grace = checkpoint_every, grace_seconds
        self.clock = clock or (lambda: datetime.now(IST).replace(tzinfo=None))
        self.ckpt = self.root / "checkpoint.npz"
        self._since_ckpt = 0
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_cycle: dict | None = None
        self.overruns = 0
        self.startup_seconds = round(time.perf_counter() - t0, 3)
        self.recovery = self._recover()

    # ------------------------------------------------------------------ state
    @property
    def watermark(self) -> str | None:
        lm = self.engine.last_minute
        return None if lm < 0 else _iso(pd.Timestamp(lm * 60_000_000_000))

    def _recover(self) -> dict:
        t0 = time.perf_counter()
        if not self.ckpt.exists():
            return {"checkpoint": None, "seconds": 0.0}
        with np.load(self.ckpt, allow_pickle=False) as z:
            self.engine.load({k: z[k] for k in z.files})
        return {"checkpoint": self.watermark, "seconds": round(time.perf_counter() - t0, 3)}

    def checkpoint(self) -> None:
        tmp = self.ckpt.with_suffix(".tmp.npz")
        np.savez(tmp, **self.engine.state())
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, self.ckpt)
        self._since_ckpt = 0

    # ------------------------------------------------------------------ input
    def ingest(self, bars: list[dict], source: str = "push") -> dict:
        """Store 1-minute bars. Bars outside the 09:16-15:30 continuous session (the 09:00-09:08 pre-open
        auction that some brokers return, or after-close prints) are not trading minutes: counted, not stored."""
        ok = [b for b in bars if _in_session(b["ts"])]
        out = self.store.add_events(ok, processed_upto=self.watermark, source=source)
        if len(ok) != len(bars):
            out["outside_session"] = len(bars) - len(ok)
        return out

    # ------------------------------------------------------------------ one cycle
    def run_cycle(self, now=None) -> dict:
        now = pd.Timestamp(now if now is not None else self.clock())
        with self.lock:
            got = self.ingest(self.feed.fetch(now), getattr(self.feed, "label", "feed")) if self.feed is not None else {}
            upto = _iso(now - pd.Timedelta(seconds=self.grace))
            before = self.watermark
            minutes = [m for m in self.store.pending_minutes(before, upto) if _in_session(m)]   # older stores may hold pre-open bars
            gaps = self._gaps(before, minutes[-1] if minutes else None)
            for g in gaps:
                self.store.record({"minute": g, "processed_at": time.time(), "latency_ms": 0.0, "lag_s": None, "mode": self.mode,
                                   "status": "no_data", "symbols_with_data": 0, "symbols_expected": len(self.reg.symbols), "closed": "[]",
                                   "eligible": 0, "evaluated": 0, "skipped_pending": 0, "stale_skips": 0, "counts": "{}", "fills": 0}, [], [], [])
            done = [self._process(m, now) for m in minutes]
            if done and self._since_ckpt:
                last = pd.Timestamp(minutes[-1])
                if self._since_ckpt >= self.checkpoint_every or (last.hour * 60 + last.minute - SESSION_OPEN_MIN) >= SESSION_MINUTES:
                    self.checkpoint()
            self.last_cycle = {"at": str(now), "ingested": got, "processed": len(done), "gaps": len(gaps),
                               "latency_ms": [d["latency_ms"] for d in done], "watermark": self.watermark}
            return self.last_cycle

    def _gaps(self, after: str | None, until: str | None) -> list[str]:
        """Session minutes strictly between the last processed minute and the next one with data, on
        session days, that have no data at all."""
        if after is None or until is None:
            return []
        a, b = pd.Timestamp(after), pd.Timestamp(until)
        out, day = [], a.normalize()
        while day <= b.normalize():
            if self.calendar.is_session(day.date()):
                for k in range(1, SESSION_MINUTES + 1):
                    ts = day + pd.Timedelta(minutes=SESSION_OPEN_MIN + k)
                    if a < ts < b:
                        out.append(_iso(ts))
            day += pd.Timedelta(days=1)
        return out

    def _process(self, minute: str, now: pd.Timestamp) -> dict:
        rows = self.store.bars_at(minute)
        r = self.reg
        known = [x for x in rows if x["symbol"] in self.engine.fe.index]
        sy = [x["symbol"] for x in known]
        cols = [np.array([x[k] for x in known], dtype=float) for k in ("open", "high", "low", "close", "volume")]
        warm = self.live_from is not None and minute < self.live_from
        allow = not (self.kill is not None and self.kill.engaged) and not warm
        t0 = time.perf_counter()
        res = self.engine.step(pd.Timestamp(minute), sy, *cols, allow_orders=allow,
                               block_reason="warm-up history (before live start)" if warm else "kill switch engaged")
        lat = (time.perf_counter() - t0) * 1000
        if res.status == "duplicate":
            return {"minute": minute, "status": "duplicate", "latency_ms": lat}
        hz = lambda i: "intraday" if r.intraday[i] else "swing"
        iso_em = lambda em: _iso(pd.Timestamp(int(em) * 60_000_000_000))
        okey = lambda i, decided, side: f"{r.ids[i]}|{iso_em(decided)}|{side}"
        alloc = {o["config"]: o for o in res.orders}
        sig_alloc = lambda i: (alloc.get(i, {}).get("alloc", "rejected" if i in alloc else None), alloc.get(i, {}).get("alloc_reason") or alloc.get(i, {}).get("reason"),
                               alloc.get(i, {}).get("score"))
        signals = [(str(r.ids[i]), minute, hz(i), r.symbols[r.sym[i]], d, p, why, *sig_alloc(i)) for i, d, p, why in res.signals]
        orders = [(okey(o["config"], o["decided"], o["side"]), str(r.ids[o["config"]]), r.symbols[r.sym[o["config"]]], o["side"],
                   hz(o["config"]), iso_em(o["decided"]), o["status"], o["qty"], o["reason"]) for o in res.orders if o["status"] != "rejected"]
        # a refused BUY is already in `signals` (what the strategy wanted); store only counts per reason, not a row each
        refused: dict[str, int] = {}
        for o in res.orders:
            if o["status"] == "rejected":
                refused[o["reason"]] = refused.get(o["reason"], 0) + 1
        fills = [(f"{okey(f['config'], f['decided'], f['side'])}|{minute}", okey(f["config"], f["decided"], f["side"]), str(r.ids[f["config"]]),
                  r.symbols[r.sym[f["config"]]], f["side"], minute, f["qty"], f["price"], f["charges_paise"], f["realized_paise"]) for f in res.fills]
        done = [(d["status"], iso_em(d["last_fill"]) if d["last_fill"] is not None else None, d["filled_qty"], d["avg_price"], d["product"],
                 d["charges_paise"], d["realized_paise"], d["reason"], okey(d["config"], d["decided"], d["side"])) for d in res.done]
        events = [(ev["ts"], ev["event"], json.dumps(ev)) for ev in res.events]
        x = self.engine.x
        lag = (now - pd.Timestamp(minute)).total_seconds() + lat / 1000
        cyc = {"minute": minute, "processed_at": time.time(), "latency_ms": round(lat, 3), "lag_s": round(lag, 3), "mode": "warmup" if warm else self.mode,
               "status": "processed", "symbols_with_data": res.symbols_with_data, "symbols_expected": len(r.symbols),
               "closed": json.dumps(res.closed), "eligible": res.eligible, "evaluated": res.evaluated, "skipped_pending": res.skipped_pending,
               "stale_skips": res.stale_skips, "counts": json.dumps(res.counts), "fills": len(fills),
               "equity_inr": x.equity() / 100 if x.rules else None, "cash_inr": x.cash / 100 if x.rules else None, "halted": int(x.halted_day)}
        self.store.record(cyc, signals, orders, fills, done, events, [(minute, k, v) for k, v in refused.items()])
        self._since_ckpt += 1
        return cyc

    # ------------------------------------------------------------------ thread
    def start(self) -> None:
        """Run a cycle a few seconds after every minute boundary while the session is open."""
        if self._thread is not None:
            return

        def loop():
            while not self._stop.is_set():
                now = self.clock()
                nxt = (now.replace(second=0, microsecond=0) + timedelta(minutes=1, seconds=self.grace))
                if self._stop.wait(max(0.0, (nxt - now).total_seconds())):
                    break
                now = self.clock()
                if (now - nxt).total_seconds() > 60:
                    self.overruns += 1
                m = now.hour * 60 + now.minute - SESSION_OPEN_MIN
                if self.calendar.is_session(now.date()) and 1 <= m <= SESSION_MINUTES + 1:
                    try:
                        self.run_cycle(now)
                    except Exception as e:          # keep the clock alive; the error is visible in status()
                        self.last_cycle = {"at": str(now), "error": f"{type(e).__name__}: {e}"}
        self._thread = threading.Thread(target=loop, name="screening-minute", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def provenance(self, now=None) -> dict:
        from nseql.screening.provenance import classify
        now = pd.Timestamp(now if now is not None else self.clock())
        return classify(self.feed, synthetic=getattr(self.feed, "synthetic", None), last_bar=self.watermark, now=now,
                        market_open=self.calendar.is_open(now.to_pydatetime()), experiments_dir=self.experiments_dir)

    def feed_health(self, now=None) -> dict | None:
        if not hasattr(self.feed, "health"):
            return None
        now = pd.Timestamp(now if now is not None else self.clock())
        return self.feed.health(now, market_open=self.calendar.is_open(now.to_pydatetime()))

    def status(self) -> dict:
        return {"data_source": self.provenance(), "feed_health": self.feed_health(), "mode": self.mode,
                "feed": getattr(self.feed, "label", None) or "none attached",
                "feed_status": getattr(self.feed, "status", None), "feed_errors": dict(list(getattr(self.feed, "errors", {}).items())[:20]),
                "live_from": self.live_from,
                "running": self._thread is not None and self._thread.is_alive(), "registered": len(self.reg), "symbols": len(self.reg.symbols),
                "watermark": self.watermark, "last_cycle": self.last_cycle, "overruns": self.overruns, "recovery": self.recovery,
                "startup_seconds": self.startup_seconds, "paper_orders_enabled": not (self.kill is not None and self.kill.engaged),
                "portfolio": self.engine.x.snapshot(),
                "live_trading": False, **self.store.counts()}

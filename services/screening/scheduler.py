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

Real-time operation needs a licensed live 1-minute feed. NONE is attached by default: the KiteTicker
adapter is NOT IMPLEMENTED. With no feed, the scheduler processes bars that are pushed into the event
log (`ingest`), e.g. a replay of a stored dataset.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from nseql.instruments.calendar import IST, TradingCalendar
from nseql.screening.engine import ScreeningEngine
from nseql.screening.features import SESSION_MINUTES, SESSION_OPEN_MIN
from nseql.screening.registry import FAMILIES, HORIZON, TF_NAMES, build
from nseql.screening.store import ScreeningStore, _iso


class ReplayMinuteFeed:
    """Serves stored 1-minute bars as if they were arriving live: fetch(now) returns every bar whose close
    time is <= now and that has not been returned yet. A REPLAY, labelled as such everywhere."""
    kind = "replay"

    def __init__(self, frames: dict[str, pd.DataFrame], label: str = "replay"):
        self.label = label
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
                 clock: Callable[[], datetime] | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        self.reg = build(symbols, n_configs)
        self.engine = ScreeningEngine(self.reg)
        self.store = ScreeningStore(self.root / "screening.sqlite")
        r = self.reg
        self.store.register_configs([(str(r.ids[i]), i, r.symbols[r.sym[i]], TF_NAMES[r.tf[i]], HORIZON[TF_NAMES[r.tf[i]]],
                                      FAMILIES[r.fam[i]], json.dumps(r.params[i]), float(r.stop[i])) for i in range(len(r))])
        self.calendar = calendar or TradingCalendar()
        self.feed, self.kill, self.mode = feed, kill, mode
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
        return self.store.add_events(bars, processed_upto=self.watermark, source=source)

    # ------------------------------------------------------------------ one cycle
    def run_cycle(self, now=None) -> dict:
        now = pd.Timestamp(now if now is not None else self.clock())
        with self.lock:
            got = self.ingest(self.feed.fetch(now), getattr(self.feed, "label", "feed")) if self.feed is not None else {}
            upto = _iso(now - pd.Timedelta(seconds=self.grace))
            before = self.watermark
            minutes = self.store.pending_minutes(before, upto)
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
        allow = not (self.kill is not None and self.kill.engaged)
        t0 = time.perf_counter()
        res = self.engine.step(pd.Timestamp(minute), sy, *cols, allow_orders=allow)
        lat = (time.perf_counter() - t0) * 1000
        if res.status == "duplicate":
            return {"minute": minute, "status": "duplicate", "latency_ms": lat}
        hz = lambda i: "intraday" if r.intraday[i] else "swing"
        signals = [(str(r.ids[i]), minute, hz(i), r.symbols[r.sym[i]], d, p, why) for i, d, p, why in res.signals]
        orders = [(f"{r.ids[i]}|{minute}|{'BUY' if d == 'BUY' else 'SELL'}", str(r.ids[i]), r.symbols[r.sym[i]],
                   "BUY" if d == "BUY" else "SELL", hz(i), minute) for i, d, _, _ in res.signals] if allow else []
        fills = []
        for f in res.fills:
            i = f["config"]
            key = f"{r.ids[i]}|{_iso(pd.Timestamp(f['decided'] * 60_000_000_000))}|{f['side']}"
            fills.append((f["status"], minute, f.get("qty"), f.get("price"), f.get("product"), f.get("charges_paise"), f.get("realized_paise"), key))
        lag = (now - pd.Timestamp(minute)).total_seconds() + lat / 1000
        cyc = {"minute": minute, "processed_at": time.time(), "latency_ms": round(lat, 3), "lag_s": round(lag, 3), "mode": self.mode,
               "status": "processed", "symbols_with_data": res.symbols_with_data, "symbols_expected": len(r.symbols),
               "closed": json.dumps(res.closed), "eligible": res.eligible, "evaluated": res.evaluated, "skipped_pending": res.skipped_pending,
               "stale_skips": res.stale_skips, "counts": json.dumps(res.counts), "fills": len(fills)}
        self.store.record(cyc, signals, orders, fills)
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

    def status(self) -> dict:
        return {"mode": self.mode, "feed": getattr(self.feed, "label", None) or "none attached (live 1-minute feed NOT IMPLEMENTED)",
                "running": self._thread is not None and self._thread.is_alive(), "registered": len(self.reg), "symbols": len(self.reg.symbols),
                "watermark": self.watermark, "last_cycle": self.last_cycle, "overruns": self.overruns, "recovery": self.recovery,
                "startup_seconds": self.startup_seconds, "paper_orders_enabled": not (self.kill is not None and self.kill.engaged),
                "live_trading": False, **self.store.counts()}

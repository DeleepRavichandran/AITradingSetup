"""Unattended operation of paper sessions.

`PaperScheduler.tick(now)` is the whole behaviour; the background thread only calls it on a timer.
On each tick, for every open, un-halted session:

  * kite_daily feed — poll for completed daily candles (catching up on any missed days), step the
    session once per day, then send one end-of-day summary. A failed poll (for example the daily
    Kite login has not been done) is reported once per day and retried on later ticks; it does not
    halt the session, because a late daily candle is still processed correctly and in order.
  * replay feed with `auto_bars` set — advance that many bars per tick (unattended demo runs).

The scheduler never places real orders and has no access to the execution service.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable

import pandas as pd

from nseql.instruments.calendar import IST, TradingCalendar
from nseql.paper.feed import FeedError


class PaperScheduler:
    def __init__(self, sessions: Callable[[], dict], *, notify: Callable[[str, str], None], calendar: TradingCalendar | None = None,
                 meta: Callable[[str], dict] | None = None, lock: threading.Lock | None = None, poll_minutes: int = 15,
                 clock: Callable[[], datetime] | None = None):
        self.sessions, self.notify = sessions, notify
        self.calendar = calendar or TradingCalendar()
        self.meta = meta or (lambda sid: {})
        self.lock = lock or threading.Lock()
        self.poll_minutes = poll_minutes
        self.clock = clock or (lambda: datetime.now(IST).replace(tzinfo=None))
        self.last_tick: datetime | None = None
        self.last_error: dict[str, str] = {}
        self._warned: dict[str, object] = {}
        self._last_poll: dict[str, datetime] = {}
        self.log: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ one tick
    def tick(self, now: datetime | None = None) -> list[dict]:
        now = now or self.clock()
        self.last_tick = now
        done = []
        with self.lock:
            for sid, s in list(self.sessions().items()):
                if s.halted_reason:
                    continue
                kind = getattr(s.feed, "kind", "")
                try:
                    if kind in ("kite_daily", "angel_daily"):
                        done += self._daily(sid, s, now)
                    elif kind == "replay":
                        n = int(self.meta(sid).get("auto_bars") or 0)
                        if n and s.advance(n):
                            done.append({"session": sid, "action": "advanced", "bars": n})
                except Exception as e:       # one session's failure must not stop the others
                    self.last_error[sid] = f"{type(e).__name__}: {e}"
        self.log = (self.log + [{"at": str(now), **d} for d in done])[-200:]
        return done

    def _daily(self, sid: str, s, now: datetime) -> list[dict]:
        if not self.calendar.is_session(now.date()) and getattr(s.feed, "caught_up", lambda n: False)(now):
            return []                     # weekend or listed holiday and nothing to catch up: no poll, no login nag
        if self.calendar.years and not self.calendar.covers(now.date()) and self._warned.get(("calendar", now.year)) is None:
            self._warned[("calendar", now.year)] = True
            self.notify("calendar", f"No NSE holiday list loaded for {now.year}: the scheduler will treat holidays as trading days. "
                                    "Load it with `nseql holidays <file.csv> --source ...`.")
        last = self._last_poll.get(sid)
        if last is not None and (now - last).total_seconds() < self.poll_minutes * 60:
            return []
        self._last_poll[sid] = now
        try:
            steps = s.feed.poll(now)
        except FeedError as e:
            self.last_error[sid] = str(e)
            if self._warned.get(sid) != now.date():        # tell the operator once a day, then keep retrying quietly
                self._warned[sid] = now.date()
                self.notify("feed", f"Paper session {s.label or sid}: could not fetch today's candles ({e}). "
                                    "It will catch up automatically once the Kite login is done.")
            return []
        self.last_error.pop(sid, None)
        out, live = [], 0
        for t, bars, end, warm in steps:
            before = s.engine.account.equity() if s.started else int(s.capital * 100)
            s.on_bars(pd.Timestamp(t), bars, end, wallclock=now, warmup=warm)
            if not warm:
                live += 1
                st = s.state()
                day_pnl = (s.engine.account.equity() - before) / 100
                fills = [f for f in st["fills"] if f["ts"][:10] == str(t)[:10]]
                self.notify("daily", f"Paper {s.label or sid} {str(t)[:10]}: equity Rs {st['equity']:,.0f} (day {day_pnl:+,.0f}), "
                                     f"{len(fills)} fill(s), {len(st['positions'])} position(s), "
                                     f"reconciliation {'ok' if st['reconciliation']['ok'] else 'BREAK'}. Simulated orders only.")
            if s.halted_reason:
                break
        if steps:
            out.append({"session": sid, "action": "daily", "days": len(steps), "live_days": live})
        return out

    # ------------------------------------------------------------------ thread
    def start(self, tick_seconds: float = 60.0) -> None:
        if self._thread is not None:
            return

        def loop():
            while not self._stop.wait(tick_seconds):
                try:
                    self.tick()
                except Exception as e:
                    self.last_error["scheduler"] = f"{type(e).__name__}: {e}"
        self._thread = threading.Thread(target=loop, name="paper-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self, now: datetime | None = None) -> dict:
        now = now or self.clock()
        return {"running": self._thread is not None and self._thread.is_alive(), "last_tick": str(self.last_tick) if self.last_tick else None,
                "market_open_now": self.calendar.is_open(now), "calendar": self.calendar.source,
                "today_is_session": self.calendar.is_session(now.date()),
                "calendar_covers_this_year": self.calendar.covers(now.date()),
                "holiday_name": self.calendar.names.get(now.date()), "poll_minutes": self.poll_minutes,
                "errors": dict(self.last_error), "recent": self.log[-20:]}

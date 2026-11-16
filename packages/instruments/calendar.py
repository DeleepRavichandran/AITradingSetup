"""Trading sessions and calendars.

All timestamps in the platform are exchange-local (Asia/Kolkata), stored tz-naive.
Adapters are responsible for converting vendor timestamps into this convention.
A bar's timestamp is the time the bar CLOSES — the first instant its contents are known.

Backtests derive the trading calendar from the dataset itself (the set of dates that
have bars), which is point-in-time correct by construction. For live/paper use an
explicit holiday list must be supplied by the operator from the NSE circular; this
module deliberately ships NO built-in holiday table so it cannot be silently wrong.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
MIS_SQUARE_OFF = time(15, 20)

INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 375, "1w": 1875}


def is_intraday(interval: str) -> bool:
    return interval not in ("1d", "1w")


@dataclass
class TradingCalendar:
    holidays: set[date] = field(default_factory=set)
    source: str = "none (weekends only — supply NSE holiday list)"

    @staticmethod
    def from_file(path: str | Path) -> "TradingCalendar":
        raw = json.loads(Path(path).read_text())
        return TradingCalendar({date.fromisoformat(d) for d in raw["holidays"]}, raw.get("source", str(path)))

    def is_session(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def is_open(self, now: datetime) -> bool:
        local = now.astimezone(IST) if now.tzinfo else now
        return self.is_session(local.date()) and SESSION_OPEN <= local.time() < SESSION_CLOSE

    def sessions(self, start: date, end: date) -> list[date]:
        out, d = [], start
        while d <= end:
            if self.is_session(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def missing_sessions(self, observed: set[date], start: date, end: date) -> list[date]:
        return [d for d in self.sessions(start, end) if d not in observed]

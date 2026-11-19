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

    years: set[int] = field(default_factory=set)          # calendar years the holiday list is complete for
    names: dict[date, str] = field(default_factory=dict)

    @staticmethod
    def from_file(path: str | Path) -> "TradingCalendar":
        raw = json.loads(Path(path).read_text())
        hol = {date.fromisoformat(d) for d in raw["holidays"]}
        years = set(raw.get("years") or {d.year for d in hol})
        names = {date.fromisoformat(k): v for k, v in (raw.get("names") or {}).items()}
        return TradingCalendar(hol, raw.get("source", str(path)), years, names)

    def to_file(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps({"source": self.source, "years": sorted(self.years),
                                          "holidays": sorted(d.isoformat() for d in self.holidays),
                                          "names": {k.isoformat(): v for k, v in sorted(self.names.items())}}, indent=1))

    def covers(self, d: date) -> bool:
        """True if the holiday list is known to be complete for d's year."""
        return d.year in self.years

    def merged(self, other: "TradingCalendar") -> "TradingCalendar":
        """Combine two lists; a year supplied by `other` replaces that year here (a revised circular wins)."""
        keep = {d for d in self.holidays if d.year not in other.years}
        names = {d: n for d, n in self.names.items() if d.year not in other.years} | other.names
        src = other.source if not self.years - other.years else f"{self.source}; {other.source}"
        return TradingCalendar(keep | other.holidays, src, self.years | other.years, names)

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


_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y")


def _parse_day(s: str) -> date:
    s = s.strip()
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    raise ValueError(f"unrecognised date {s!r}; use YYYY-MM-DD or the NSE style 26-Jan-2026")


def holidays_from_csv(path: str | Path, *, source: str, years: set[int] | None = None) -> TradingCalendar:
    """Read a trading-holiday list as published by NSE (copy the table from the exchange circular
    into a CSV). Needs a column whose header contains "date"; a header containing "description",
    "holiday" or "occasion" (other than the date column) is kept as the holiday's name.

    `years` are the years the list is COMPLETE for; by default every year that appears in it. Weekend
    rows are accepted and ignored (NSE lists them for information). Nothing is fetched from the web.
    """
    import csv
    if not source.strip():
        raise ValueError("give the source, e.g. 'NSE circular NSE/CMTR/12345 dated 2025-12-12'")
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ValueError(f"{path} is empty")
    head = [h.strip().lower() for h in rows[0]]
    dcol = next((i for i, h in enumerate(head) if "date" in h), None)
    if dcol is None:
        raise ValueError(f"no column with 'date' in its header in {path}; found {rows[0]}")
    ncol = next((i for i, h in enumerate(head) if i != dcol and any(k in h for k in ("description", "holiday", "occasion", "name"))), None)
    hol, names = set(), {}
    for r in rows[1:]:
        if len(r) <= dcol or not r[dcol].strip():
            continue
        d = _parse_day(r[dcol])
        if d.weekday() >= 5:
            continue
        hol.add(d)
        if ncol is not None and len(r) > ncol and r[ncol].strip():
            names[d] = r[ncol].strip()
    if not hol:
        raise ValueError(f"no weekday holidays found in {path}")
    return TradingCalendar(hol, source.strip(), set(years) if years else {d.year for d in hol}, names)


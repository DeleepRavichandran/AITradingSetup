"""NSE holiday list: loading, the scheduler's behaviour on closed days, and incomplete daily candles."""
from datetime import date, datetime

import pandas as pd
import pytest
from nseql.instruments.calendar import TradingCalendar, holidays_from_csv

from tests.integration.test_paper_persistence import TOKENS, FakeKite, daily_session

NSE_CSV = """Sr. No,Date,Day,Description
1,26-Jan-2026,Monday,Republic Day
2,03-Mar-2026,Tuesday,Holi
3,14-Nov-2026,Saturday,Diwali Laxmi Pujan (Muhurat trading)
4,24-Nov-2026,Tuesday,Guru Nanak Jayanti
"""


def test_nse_style_csv_is_parsed_weekends_dropped_and_names_kept(tmp_path):
    p = tmp_path / "h.csv"
    p.write_text(NSE_CSV)
    cal = holidays_from_csv(p, source="NSE circular (test)")
    assert cal.holidays == {date(2026, 1, 26), date(2026, 3, 3), date(2026, 11, 24)}
    assert cal.names[date(2026, 11, 24)] == "Guru Nanak Jayanti"
    assert cal.years == {2026} and cal.covers(date(2026, 5, 1)) and not cal.covers(date(2027, 1, 1))
    assert not cal.is_session(date(2026, 11, 24)) and cal.is_session(date(2026, 11, 23))
    cal.to_file(tmp_path / "h.json")
    back = TradingCalendar.from_file(tmp_path / "h.json")
    assert (back.holidays, back.years, back.names) == (cal.holidays, cal.years, cal.names)


def test_csv_without_source_or_date_column_is_refused(tmp_path):
    p = tmp_path / "h.csv"
    p.write_text(NSE_CSV)
    with pytest.raises(ValueError, match="source"):
        holidays_from_csv(p, source=" ")
    p.write_text("when,what\n26-Jan-2026,Republic Day\n")
    with pytest.raises(ValueError, match="date"):
        holidays_from_csv(p, source="x")
    p.write_text("Date\n31-31-2026\n")
    with pytest.raises(ValueError, match="unrecognised date"):
        holidays_from_csv(p, source="x")


def test_a_revised_list_for_a_year_replaces_that_year_only():
    a = TradingCalendar({date(2026, 1, 26), date(2027, 1, 26)}, "old", {2026, 2027})
    b = TradingCalendar({date(2026, 3, 3)}, "revised 2026", {2026})
    m = a.merged(b)
    assert m.holidays == {date(2026, 3, 3), date(2027, 1, 26)} and m.years == {2026, 2027}


def _cal():
    return TradingCalendar({date(2026, 11, 24)}, "test list", {2026}, {date(2026, 11, 24): "Guru Nanak Jayanti"})


def _days():
    return [d for d in pd.bdate_range("2026-06-01", "2026-11-30") if d.date() != date(2026, 11, 24)]


def test_no_poll_and_no_login_nag_on_a_holiday_or_weekend_once_caught_up():
    api, sent = FakeKite(_days()), []
    ps, sched = daily_session(api, "2026-11-16", sent)
    sched.calendar = ps.feed.calendar = _cal()
    sched.tick(datetime(2026, 11, 23, 16, 0))                     # Monday close: caught up to the 23rd
    calls = api.calls
    api.fail = True                                               # login expired over the holiday
    sched.tick(datetime(2026, 11, 24, 16, 0))                     # Tuesday = holiday
    assert api.calls == calls and not [e for e, _ in sent if e == "feed"]
    assert sched.status(datetime(2026, 11, 24, 16, 0))["holiday_name"] == "Guru Nanak Jayanti"
    api.fail = False
    sched.tick(datetime(2026, 11, 25, 16, 0))                     # next session proceeds normally
    assert ps.curve[-1][0].date() == date(2026, 11, 25)
    assert ps.feed.gaps == []                                     # the holiday is not reported as missing data


def test_without_a_holiday_list_the_closed_day_is_polled_and_flagged():
    api, sent = FakeKite(_days()), []
    ps, sched = daily_session(api, "2026-11-16", sent)
    sched.tick(datetime(2026, 11, 23, 16, 0))
    calls = api.calls
    sched.tick(datetime(2026, 11, 24, 16, 0))
    assert api.calls > calls                                       # weekends-only calendar: Tuesday looks like a session
    ps.feed.calendar = TradingCalendar()                           # weekends-only calendar: the 24th is an unexplained gap
    sched.tick(datetime(2026, 11, 25, 16, 0))
    assert {"date": "2026-11-24", "kind": "no_candles_for_session"}.items() <= ps.feed.gaps[0].items()


def test_scheduler_warns_once_when_the_list_does_not_cover_this_year():
    api, sent = FakeKite(pd.bdate_range("2026-06-01", "2027-01-15")), []
    ps, sched = daily_session(api, "2026-12-28", sent)
    sched.calendar = ps.feed.calendar = _cal()
    sched.tick(datetime(2027, 1, 4, 16, 0))
    sched.tick(datetime(2027, 1, 5, 16, 0))
    warn = [t for e, t in sent if e == "calendar"]
    assert len(warn) == 1 and "2027" in warn[0]
    assert sched.status(datetime(2027, 1, 5, 16, 0))["calendar_covers_this_year"] is False


class PartialKite(FakeKite):
    """BBB's candle for `late_day` is only published once the clock passes `ready`."""

    def __init__(self, days, late_day, ready):
        super().__init__(days)
        self.late_day, self.ready, self.now = late_day, ready, None

    def candles(self, token, interval, start, end):
        df = super().candles(token, interval, start, end)
        if token == 102 and (self.now is None or self.now < self.ready):
            df = df[df["ts"].dt.date != self.late_day]
        return df


def test_today_is_held_back_until_every_candle_exists():
    api, sent = PartialKite(pd.bdate_range("2026-06-01", "2026-11-30"), date(2026, 11, 17), datetime(2026, 11, 17, 16, 30)), []
    ps, sched = daily_session(api, "2026-11-16", sent)
    api.now = datetime(2026, 11, 17, 16, 0)
    sched.tick(datetime(2026, 11, 17, 16, 0))
    assert ps.curve[-1][0].date() == date(2026, 11, 16) and ps.feed.status.startswith("waiting for 1")
    api.now = datetime(2026, 11, 17, 16, 30)
    sched.tick(datetime(2026, 11, 17, 16, 30))
    assert ps.curve[-1][0].date() == date(2026, 11, 17) and ps.feed.gaps == []


def test_a_candle_still_missing_next_day_is_delivered_with_the_gap_recorded():
    api, sent = PartialKite(pd.bdate_range("2026-06-01", "2026-11-30"), date(2026, 11, 17), datetime(2099, 1, 1)), []
    ps, sched = daily_session(api, "2026-11-16", sent)
    sched.tick(datetime(2026, 11, 17, 16, 0))
    sched.tick(datetime(2026, 11, 18, 16, 0))
    assert [t.date() for t, _ in ps.curve[-3:]] == [date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)]
    assert ps.feed.gaps == [{"date": "2026-11-17", "kind": "symbols_missing", "symbols": ["BBB"]}]
    assert ps.reconcile()["ok"]


def test_cli_loads_and_shows_the_list(tmp_path, monkeypatch, capsys):
    from nseql import config
    from nseql.cli import main
    monkeypatch.setenv("NSEQL_EXPERIMENTS_DIR", str(tmp_path / "exp"))
    config.get_settings.cache_clear() if hasattr(config.get_settings, "cache_clear") else None
    p = tmp_path / "h.csv"
    p.write_text(NSE_CSV)
    assert main(["holidays", str(p)]) == 2                         # source is mandatory
    assert main(["holidays", str(p), "--source", "NSE circular test"]) == 0
    assert TradingCalendar.from_file(tmp_path / "exp" / "holidays.json").holidays == {date(2026, 1, 26), date(2026, 3, 3), date(2026, 11, 24)}
    capsys.readouterr()
    assert main(["holidays", "--show"]) == 0
    assert "Guru Nanak Jayanti" in capsys.readouterr().out


assert TOKENS                                                      # shared fixture data from the persistence tests

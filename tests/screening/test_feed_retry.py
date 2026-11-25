"""Live feed timing (2026-11-25 on the laptop: 8 of 18 minutes became catch-up because a candle that was a few seconds late
was only asked for again a whole minute later)."""
import pandas as pd

from nseql.screening.scheduler import MinuteScheduler
from tests.screening.scripted import bar

SYMS = [f"S{k}" for k in range(10)]
DAY = "2026-11-16"


class LateFeed:
    """Candles for minute M arrive for half the symbols at M+5 s and for the rest only at M+15 s."""
    label = "test feed"

    def __init__(self):
        self.calls = []

    def fetch(self, now, need=None):
        now = pd.Timestamp(now)
        self.calls.append((now, need))
        m = now.floor("min")
        secs = (now - m).total_seconds()
        ready = SYMS if secs >= 15 else SYMS[:5]
        if need is not None:
            ready = [s for s in ready if s not in self.sent.get(m, set())]
        out = [bar(s, m, 100.0 + k * 0.01) for k, s in enumerate(ready) if s not in self.sent.get(m, set())]
        self.sent.setdefault(m, set()).update(b["symbol"] for b in out)
        return out

    sent: dict = {}


def test_held_minute_is_completed_by_a_retry_and_traded_live(tmp_path):
    LateFeed.sent = {}
    feed = LateFeed()
    sch = MinuteScheduler(tmp_path / "a", SYMS, mode="live", feed=feed)
    t = pd.Timestamp(f"{DAY}T09:20:00")
    sch.run_cycle(t + pd.Timedelta(seconds=5), hold=True)          # half the candles: held, not processed
    assert sch.held == {"minute": f"{DAY}T09:20:00", "have": 5, "of": 10, "waited_s": 5.0} and sch.watermark is None
    sch.run_cycle(t + pd.Timedelta(seconds=15), retry=True, hold=True)   # the rest arrive: processed as a LIVE minute
    row = sch.store.db.execute("SELECT mode, symbols_with_data, lag_s FROM cycles WHERE minute=?", (f"{DAY}T09:20:00",)).fetchone()
    assert tuple(row)[:2] == ("live", 10) and row[2] < 20
    assert sch.store.db.execute("SELECT COUNT(*) FROM events WHERE late=1").fetchone()[0] == 0
    assert feed.calls[-1][1] == t                                 # the retry asked only for the missing minute


def test_without_coverage_the_hold_ends_before_the_decision_limit(tmp_path):
    class Half(LateFeed):
        def fetch(self, now, need=None):
            m = pd.Timestamp(now).floor("min")
            out = [bar(s, m, 100.0) for s in SYMS[:5] if s not in self.sent.get(m, set())]
            self.sent.setdefault(m, set()).update(b["symbol"] for b in out)
            return out
    Half.sent = {}
    sch = MinuteScheduler(tmp_path / "b", SYMS, mode="live", feed=Half())
    t = pd.Timestamp(f"{DAY}T09:20:00")
    sch.run_cycle(t + pd.Timedelta(seconds=5), hold=True)
    assert sch.watermark is None
    sch.run_cycle(t + pd.Timedelta(seconds=46), retry=True, hold=True)   # past the 45 s hold: processed with what it has, still live
    row = sch.store.db.execute("SELECT mode, symbols_with_data FROM cycles WHERE minute=?", (f"{DAY}T09:20:00",)).fetchone()
    assert tuple(row) == ("live", 5)


def test_replay_mode_never_holds(tmp_path):
    LateFeed.sent = {}
    sch = MinuteScheduler(tmp_path / "c", SYMS, mode="replay", feed=LateFeed())
    sch.run_cycle(pd.Timestamp(f"{DAY}T09:20:05"), hold=True)
    assert sch.watermark == f"{DAY}T09:20:00" and sch.held is None

"""News watcher: tags headlines to stocks and themes, stores each once with first-seen time, survives feed
outages, records market pulses from the screening loop; Vega answers news questions from the records."""
from datetime import datetime

import pandas as pd
from nseql.data.news_watch import NewsWatch, tag
from nseql.research import copilot
from nseql.screening.scheduler import MinuteScheduler
from nseql.screening.synthetic import minute_stream, symbols


class Desk:
    def __init__(self):
        self.items, self.fail = [], False

    def headlines(self, limit=30, topic=None, refresh=False):
        if self.fail:
            raise RuntimeError("feeds down")
        return {"items": self.items, "errors": {"Livemint Markets": "timeout"}}


def test_tagging_finds_stocks_and_themes_without_false_hits():
    s, t = tag("Reliance shares jump after RBI holds repo rate; crude slips", ["RELIANCE", "TCS", "ITC"])
    assert s == ["RELIANCE"] and {"rates & central banks", "commodities"} <= set(t)
    s, _ = tag("Bitcoin hits a record as traders cheer", ["ITC", "TCS"])          # 'itc' inside 'Bitcoin' must not match
    assert s == []
    assert tag("Infosys and TCS beat estimates in Q2 results", ["INFY", "TCS"])[0] == ["INFY", "TCS"]


def test_headlines_are_stored_once_and_outages_do_not_stop_the_watcher(tmp_path):
    d = Desk()
    clock = [datetime(2026, 11, 23, 9, 30)]
    w = NewsWatch(tmp_path, d, symbols=lambda: ["HDFCBANK", "SBIN"], clock=lambda: clock[0])
    d.items = [{"title": "HDFC Bank and SBI lead banks higher", "link": "https://x/1", "source": "ET", "topic": "india", "published": "2026-11-23T03:55:00Z"}]
    assert w.poll_news() == 1
    clock[0] = datetime(2026, 11, 23, 9, 35)
    d.items.append({"title": "Fed signals a pause; Wall Street rallies", "link": "https://x/2", "source": "BBC", "topic": "world", "published": None})
    assert w.poll_news() == 1                                            # the first one is not stored again
    rows = w.recent(10)
    assert [r["first_seen"] for r in rows] == ["2026-11-23T09:35:00", "2026-11-23T09:30:00"]
    assert rows[1]["symbols"] == ["HDFCBANK", "SBIN"] and "global markets" in rows[0]["themes"]
    assert w.recent(10, symbol="SBIN")[0]["link"] == "https://x/1" and w.counts("2026-11-23T00:00:00")["by_symbol"] == {"HDFCBANK": 1, "SBIN": 1}
    d.fail = True
    w._next_news = w._next_pulse = 0.0
    w.tick(1.0)                                                          # an outage is recorded, not raised
    assert "feeds down" in w.status()["errors"]["watcher"] and w.status()["headlines_stored"] == 2


def test_market_pulse_from_the_screening_loop_and_vega_news_answer(tmp_path):
    stream = minute_stream(4, ["2026-11-16"], seed=5, missing=0)
    sch = MinuteScheduler(tmp_path / "run", symbols(4))
    for ts, bars in stream[:120]:
        sch.ingest(bars)
        sch.run_cycle(ts + pd.Timedelta(seconds=6))
    d = Desk()
    d.items = [{"title": "SYN001 surges on order win", "link": "https://x/3", "source": "ET", "topic": "india", "published": None}]
    w = NewsWatch(tmp_path, d, symbols=lambda: sch.reg.symbols, scheduler=lambda: sch, market_open=lambda: True,
                  clock=lambda: datetime(2026, 11, 16, 11, 16))
    w.poll_news()
    p = w.pulse()
    assert p["session"] == "2026-11-16" and p["breadth"]["symbols"] == 4 and p["breadth"]["up"] + p["breadth"]["down"] <= 4
    assert len(p["top_gainers"]) == 3 and p["news_since_open"]["by_symbol"] == {"SYN001": 1} and w.pulses(1)[0]["at"] == p["at"]

    class Ctx:
        news = staticmethod(lambda: w)
    a = copilot.answer("What is in the news for my stocks?", Ctx)
    assert a["intent"] == "news" and "SYN001" in a["answer"] and "no strategy trades on them" in a["answer"]

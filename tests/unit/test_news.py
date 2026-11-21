"""News desk: RSS and Atom parsing, de-duplication, ordering, failure isolation, caching."""
import httpx

from nseql.data.news import NewsDesk, parse_feed

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Nifty ends higher as banks rally</title><link>https://ex.com/a</link><pubDate>Fri, 20 Nov 2026 10:30:00 GMT</pubDate></item>
<item><title>Oil &amp; gold steady</title><link>https://ex.com/b</link><pubDate>Fri, 20 Nov 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Nifty ends higher as banks rally!</title><link href="https://other.com/x"/><updated>2026-11-20T11:00:00Z</updated></entry>
<entry><title>Fed holds rates</title><link href="https://other.com/y"/><updated>2026-11-20T12:00:00Z</updated></entry>
</feed>"""


def test_parse_rss_and_atom_with_ist_times():
    r = parse_feed(RSS, "A", "india")
    assert r[0]["title"] == "Nifty ends higher as banks rally" and str(r[0]["published"]) == "2026-11-20 16:00:00"
    assert r[1]["title"] == "Oil & gold steady"
    a = parse_feed(ATOM, "B", "world")
    assert a[1]["link"] == "https://other.com/y" and str(a[1]["published"]) == "2026-11-20 17:30:00"


def test_desk_merges_dedupes_sorts_and_survives_a_dead_feed(tmp_path):
    (tmp_path / "news_feeds.txt").write_text("A | https://a.test/rss | india\nB | https://b.test/atom | world\nDead | https://dead.test/x\n")
    calls = []

    def handler(req):
        calls.append(req.url.host)
        if req.url.host == "dead.test":
            return httpx.Response(503)
        return httpx.Response(200, content=RSS if req.url.host == "a.test" else ATOM)
    desk = NewsDesk(tmp_path, transport=httpx.MockTransport(handler))
    out = desk.headlines()
    titles = [x["title"] for x in out["items"]]
    assert titles == ["Fed holds rates", "Nifty ends higher as banks rally", "Oil & gold steady"]   # duplicate story merged, newest first
    assert "Dead" in out["errors"] and out["feeds"] == ["A", "B", "Dead"]
    n = len(calls)
    desk.headlines()
    assert calls[n:] == []                                                 # cached for 10 minutes, the dead feed included
    assert [x["title"] for x in desk.headlines(topic="world")["items"]] == ["Fed holds rates"]

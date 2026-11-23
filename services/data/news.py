"""World and market headlines from public RSS/Atom feeds, for reading on the Command Deck.

Only headline, link, source and publication time are kept, and each headline links back to its
publisher. Headlines are shown to the operator; they are NOT used by any strategy or backtest
(a headline needs a verified point-in-time timestamp and a licence for that).

Feeds come from experiments/news_feeds.txt if present (one "Name | URL" per line), otherwise the
defaults below. Each feed is fetched at most every 10 minutes. A feed that fails is reported and
skipped; it never blocks the others. Check each publisher's terms; RSS feeds are generally offered
for personal reading.
"""
from __future__ import annotations

import email.utils
import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pandas as pd

DEFAULT_FEEDS = [
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "world"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml", "business"),
    ("Google News: Indian markets", "https://news.google.com/rss/search?q=Nifty+OR+Sensex+OR+NSE&hl=en-IN&gl=IN&ceid=IN:en", "india"),
    ("Google News: world economy", "https://news.google.com/rss/search?q=global+economy+markets&hl=en-IN&gl=IN&ceid=IN:en", "world"),
    ("Economic Times Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", "india"),
    ("Livemint Markets", "https://www.livemint.com/rss/markets", "india"),
]
TTL_SECONDS = 600
_TAG = re.compile(r"<[^>]+>")


def _text(el, *names) -> str:
    for n in names:
        x = el.find(n)
        if x is not None and (x.text or "").strip():
            return html.unescape(_TAG.sub("", x.text)).strip()
    return ""


def _when(s: str) -> pd.Timestamp | None:
    if not s:
        return None
    try:
        return pd.Timestamp(email.utils.parsedate_to_datetime(s)).tz_convert("Asia/Kolkata").tz_localize(None)
    except (TypeError, ValueError, IndexError):
        try:
            t = pd.Timestamp(s)
            return (t.tz_convert("Asia/Kolkata").tz_localize(None) if t.tzinfo else t)
        except ValueError:
            return None


def parse_feed(xml: bytes | str, source: str, topic: str) -> list[dict]:
    """RSS 2.0 or Atom -> [{title, link, source, topic, published}]."""
    root = ET.fromstring(xml)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for it in root.iter("item"):
        title, link = _text(it, "title"), _text(it, "link")
        if title and link:
            out.append({"title": title, "link": link, "source": source, "topic": topic, "published": _when(_text(it, "pubDate", "date"))})
    for it in root.iter("{http://www.w3.org/2005/Atom}entry"):
        title = _text(it, "{http://www.w3.org/2005/Atom}title")
        le = it.find("a:link", ns)
        link = le.get("href", "") if le is not None else ""
        if title and link:
            out.append({"title": title, "link": link, "source": source, "topic": topic,
                        "published": _when(_text(it, "{http://www.w3.org/2005/Atom}updated", "{http://www.w3.org/2005/Atom}published"))})
    return out


def load_feeds(experiments_dir: Path | None) -> list[tuple[str, str, str]]:
    f = Path(experiments_dir) / "news_feeds.txt" if experiments_dir else None
    if f and f.exists():
        feeds = []
        for line in f.read_text().splitlines():
            line = line.split("#")[0].strip()
            if "|" in line:
                parts = [p.strip() for p in line.split("|")]
                feeds.append((parts[0], parts[1], parts[2] if len(parts) > 2 else "general"))
        if feeds:
            return feeds
    return list(DEFAULT_FEEDS)


class NewsDesk:
    def __init__(self, experiments_dir: Path | None = None, transport: httpx.BaseTransport | None = None, clock=time.time):
        self.dir, self.transport, self.clock = experiments_dir, transport, clock
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self.errors: dict[str, str] = {}
        self._lock = threading.Lock()

    def _fetch(self, name: str, url: str, topic: str) -> list[dict]:
        hit = self._cache.get(url)
        if hit and self.clock() - hit[0] < TTL_SECONDS:
            return hit[1]
        try:
            with httpx.Client(transport=self.transport, timeout=10.0, follow_redirects=True,
                              headers={"User-Agent": "NSE-AI-Quant-Lab/0.1 (personal news reader)"}) as c:
                r = c.get(url)
                r.raise_for_status()
            items = parse_feed(r.content, name, topic)
            self.errors.pop(name, None)
        except Exception as e:                        # one bad feed never blocks the rest
            self.errors[name] = f"{type(e).__name__}: {str(e)[:120]}"
            items = hit[1] if hit else []
        with self._lock:
            self._cache[url] = (self.clock(), items)
        return items

    def headlines(self, limit: int = 30, topic: str | None = None, refresh: bool = False) -> dict:
        feeds = load_feeds(self.dir)
        if refresh:
            self._cache.clear()
        with ThreadPoolExecutor(max_workers=6) as ex:
            batches = list(ex.map(lambda f: self._fetch(*f), feeds))
        seen, items = set(), []
        for it in (x for b in batches for x in b):
            key = re.sub(r"\W+", " ", it["title"].lower()).strip()[:90]
            if key in seen:                      # the same story from another feed
                continue
            seen.add(key)
            if not topic or it["topic"] == topic:
                items.append(it)
        items.sort(key=lambda x: x["published"] or pd.Timestamp.min, reverse=True)
        return {"items": [{**x, "published": str(x["published"]) if x["published"] is not None else None} for x in items[:limit]],
                "feeds": [f[0] for f in feeds], "errors": dict(self.errors), "cached_for_seconds": TTL_SECONDS,
                "note": "Headlines from public RSS feeds, for reading. Not used by any strategy."}

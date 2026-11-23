"""News and market watcher: records what the lab saw, when it saw it, for later evaluation.

* Every `news_every` seconds (default 300) it refreshes the RSS feeds (services/data/news.py), stores each
  new headline ONCE with the time it was first seen here (`first_seen`), and tags it with watchlist stocks
  and with global / national topics (keyword rules below; transparent, not a language model).
* Every `pulse_every` seconds (default 900) while the market is open it stores a market pulse from the
  screening loop's own data: breadth since the open, biggest movers, paper equity, today's signals by
  rule family.

Headlines are NOT used by any strategy. They are recorded point-in-time so that, once enough history
exists, it can be measured whether news would have improved decisions without using information that
arrived later. Feeds are public RSS published for syndication; headlines are stored for this lab only.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SCHEMA = """
CREATE TABLE IF NOT EXISTS headlines (id TEXT PRIMARY KEY, first_seen TEXT, published TEXT, source TEXT, topic TEXT, title TEXT,
  link TEXT, symbols TEXT, themes TEXT);
CREATE INDEX IF NOT EXISTS ix_head_seen ON headlines(first_seen);
CREATE TABLE IF NOT EXISTS pulses (at TEXT PRIMARY KEY, data TEXT);
"""
ALIASES = {"RELIANCE": ["reliance"], "HDFCBANK": ["hdfc bank"], "ICICIBANK": ["icici bank", "icici"], "INFY": ["infosys", "infy"],
           "TCS": ["tcs", "tata consultancy"], "BHARTIARTL": ["bharti airtel", "airtel"], "SBIN": ["sbi", "state bank of india"],
           "ITC": ["itc"], "LT": ["larsen", "l&t"], "KOTAKBANK": ["kotak"], "AXISBANK": ["axis bank"], "HINDUNILVR": ["hindustan unilever", "hul"],
           "BAJFINANCE": ["bajaj finance"], "MARUTI": ["maruti"], "SUNPHARMA": ["sun pharma"], "TITAN": ["titan"],
           "ULTRACEMCO": ["ultratech"], "NTPC": ["ntpc"], "TMPV": ["tata motors"], "ASIANPAINT": ["asian paints"],
           "NIFTY50": ["nifty", "sensex"]}
THEMES = {
    "rates & central banks": ["rbi", "repo rate", "fed ", "federal reserve", "interest rate", "rate cut", "rate hike", "ecb", "powell", "monetary policy"],
    "inflation & growth": ["inflation", "cpi", "gdp", "recession", "slowdown", "jobs report", "payrolls", "pmi"],
    "currency": ["rupee", "dollar index", "usd/inr", "forex"],
    "commodities": ["crude", "oil price", "brent", "gold", "opec"],
    "geopolitics": ["war", "sanction", "tariff", "trade deal", "conflict", "missile", "election"],
    "flows": ["fii", "fpi", "dii", "foreign investors", "outflow", "inflow"],
    "earnings": ["results", "earnings", "profit", "quarterly", "q1 ", "q2 ", "q3 ", "q4 ", "guidance"],
    "india markets": ["nifty", "sensex", "nse", "bse", "sebi"],
    "global markets": ["wall street", "s&p 500", "nasdaq", "dow jones", "nikkei", "hang seng"],
}


def tag(title: str, symbols: list[str]) -> tuple[list[str], list[str]]:
    t = f" {title.lower()} "
    syms = [s for s in symbols if any(re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", t) for a in ALIASES.get(s, [s.lower()]))]
    themes = [k for k, words in THEMES.items() if any(w in t for w in words)]
    return syms, themes


class NewsWatch:
    def __init__(self, experiments_dir: Path, desk, *, symbols=list, scheduler=lambda: None, market_open=lambda: False,
                 news_every: float = 300, pulse_every: float = 900, clock=lambda: datetime.now(IST).replace(tzinfo=None)):
        d = Path(experiments_dir) / "news"
        d.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(d / "watch.sqlite"), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.desk, self.symbols, self.scheduler, self.market_open, self.clock = desk, symbols, scheduler, market_open, clock
        self.news_every, self.pulse_every = news_every, pulse_every
        self.last_news = self.last_pulse = None
        self.errors: dict = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ news
    def poll_news(self) -> int:
        out = self.desk.headlines(limit=500, refresh=True)
        self.errors = dict(out.get("errors") or {})
        now = self.clock().isoformat(timespec="seconds")
        syms = list(self.symbols() or [])
        new = 0
        with self.lock:
            for h in out.get("items", []):
                key = hashlib.sha256(((h.get("link") or "") + "|" + (h.get("title") or "")).encode()).hexdigest()[:20]
                s, th = tag(h.get("title") or "", syms)
                cur = self.db.execute("INSERT OR IGNORE INTO headlines VALUES (?,?,?,?,?,?,?,?,?)",
                                      (key, now, h.get("published"), h.get("source"), h.get("topic"), h.get("title"), h.get("link"),
                                       json.dumps(s), json.dumps(th)))
                new += cur.rowcount
        self.last_news = {"at": now, "new": new, "feeds_failed": len(self.errors)}
        return new

    def recent(self, limit: int = 50, symbol: str = "", theme: str = "") -> list[dict]:
        q, a = "SELECT * FROM headlines", []
        w = []
        if symbol:
            w.append("symbols LIKE ?")
            a.append(f'%"{symbol}"%')
        if theme:
            w.append("themes LIKE ?")
            a.append(f'%"{theme}"%')
        if w:
            q += " WHERE " + " AND ".join(w)
        with self.lock:
            rows = [dict(r) for r in self.db.execute(q + " ORDER BY first_seen DESC, published DESC LIMIT ?", [*a, limit])]
        for r in rows:
            r["symbols"], r["themes"] = json.loads(r["symbols"]), json.loads(r["themes"])
        return rows

    def counts(self, since: str) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT symbols, themes FROM headlines WHERE first_seen >= ?", (since,)).fetchall()
        sym: dict[str, int] = {}
        th: dict[str, int] = {}
        for r in rows:
            for s in json.loads(r[0]):
                sym[s] = sym.get(s, 0) + 1
            for t in json.loads(r[1]):
                th[t] = th.get(t, 0) + 1
        return {"headlines": len(rows), "by_symbol": dict(sorted(sym.items(), key=lambda x: -x[1])), "by_theme": dict(sorted(th.items(), key=lambda x: -x[1]))}

    # ------------------------------------------------------------------ market pulse
    def pulse(self) -> dict | None:
        sch = self.scheduler()
        if sch is None:
            return None
        db = sch.store.db
        with sch.store._lock:
            day = db.execute("SELECT MAX(substr(ts,1,10)) FROM events WHERE late=0").fetchone()[0]
            if not day:
                return None
            rows = db.execute("SELECT symbol, MIN(ts), MAX(ts) FROM events WHERE substr(ts,1,10)=? AND late=0 GROUP BY symbol", (day,)).fetchall()
            moves = []
            for s, t0, t1 in rows:
                o = db.execute("SELECT open FROM events WHERE symbol=? AND ts=?", (s, t0)).fetchone()[0]
                c = db.execute("SELECT close FROM events WHERE symbol=? AND ts=?", (s, t1)).fetchone()[0]
                if o:
                    moves.append((s, round((c / o - 1) * 100, 3)))
            fam = db.execute("SELECT c.family, COUNT(*) FROM signals s JOIN configs c ON c.id = s.config_id WHERE substr(s.bar_ts,1,10)=? "
                             "AND s.decision='BUY' GROUP BY c.family", (day,)).fetchall()
            real = db.execute("SELECT COALESCE(SUM(realized_paise),0) FROM paper_orders WHERE substr(fill_ts,1,10)=?", (day,)).fetchone()[0]
        moves.sort(key=lambda x: x[1])
        snap = sch.engine.x.snapshot()
        p = {"at": self.clock().isoformat(timespec="seconds"), "session": day, "data": sch.provenance().get("label"),
             "breadth": {"up": sum(m[1] > 0 for m in moves), "down": sum(m[1] < 0 for m in moves), "symbols": len(moves)},
             "top_gainers": moves[::-1][:3], "top_losers": moves[:3], "paper_equity_inr": snap["equity_inr"], "open_positions": snap["open_positions"],
             "realised_today_inr": real / 100, "buy_signals_by_family": dict(fam),
             "news_since_open": self.counts(f"{day}T09:00:00")}
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO pulses VALUES (?,?)", (p["at"], json.dumps(p)))
        self.last_pulse = p
        return p

    def pulses(self, limit: int = 30) -> list[dict]:
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM pulses ORDER BY at DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------------ thread
    def tick(self, now: float) -> None:
        if self._next_news <= now:
            self._next_news = now + self.news_every
            try:
                self.poll_news()
            except Exception as e:  # noqa: BLE001 - a feed outage must never stop the watcher
                self.errors["watcher"] = f"{type(e).__name__}: {e}"
        if self._next_pulse <= now and self.market_open():
            self._next_pulse = now + self.pulse_every
            try:
                self.pulse()
            except Exception as e:  # noqa: BLE001
                self.errors["pulse"] = f"{type(e).__name__}: {e}"

    def start(self) -> None:
        self._next_news = self._next_pulse = 0.0

        def run():
            while not self._stop.is_set():
                self.tick(time.monotonic())
                self._stop.wait(20)
        self._thread = threading.Thread(target=run, name="news-watch", daemon=True)
        self._thread.start()

    def status(self) -> dict:
        with self.lock:
            n = self.db.execute("SELECT COUNT(*), MIN(first_seen) FROM headlines").fetchone()
        return {"running": bool(self._thread and self._thread.is_alive()), "headlines_stored": n[0], "since": n[1],
                "last_news_poll": self.last_news, "last_pulse": self.last_pulse, "errors": self.errors,
                "news_every_s": self.news_every, "pulse_every_s": self.pulse_every,
                "use": "recorded with first-seen times for later evaluation; no strategy uses news"}

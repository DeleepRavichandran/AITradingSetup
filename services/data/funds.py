"""Mutual-fund NAV data: AMFI file parsing and a local store.

Data contract: one NAV per scheme per business day, as published by AMFI (Association of Mutual Funds in
India). Mutual funds have no intraday price; every analytic here uses daily NAVs only.

Accepted files (AMFI's published text layouts, ';'-separated):
  * daily "NAVAll.txt":
      Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date
    interleaved with fund-house and category heading lines;
  * NAV history report:
      Scheme Code;Scheme Name;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Net Asset Value;Repurchase Price;Sale Price;Date
Dates are dd-Mon-yyyy. Rows with a non-numeric NAV ("N.A.") are skipped and counted.

STATUS: the parser is tested against these documented layouts. No real AMFI file has been downloaded
from this development environment (the host is not reachable here); the first real import should be
checked against the AMFI website for a few schemes.
Licensing: AMFI publishes these files for public download. Import files you downloaded; do not
redistribute them.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS schemes (code TEXT PRIMARY KEY, name TEXT, isin_growth TEXT, isin_reinvest TEXT, amc TEXT, category TEXT,
  plan TEXT, option TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS navs (code TEXT NOT NULL, date TEXT NOT NULL, nav REAL NOT NULL, source TEXT, PRIMARY KEY (code, date));
CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY, at TEXT, file TEXT, sha256 TEXT, rows INTEGER, skipped INTEGER, schemes INTEGER, layout TEXT);
"""
CATEGORY = re.compile(r"^(Open|Close|Interval) Ended Schemes\s*\((.+)\)\s*$", re.IGNORECASE)


def _date(s: str) -> date:
    return datetime.strptime(s.strip(), "%d-%b-%Y").date()


def _num(s: str) -> float | None:
    try:
        v = float(s.strip().replace(",", ""))
        return v if v > 0 else None
    except ValueError:
        return None


def _plan(name: str) -> tuple[str, str]:
    n = name.lower()
    plan = "direct" if "direct" in n else "regular"
    option = "growth" if "growth" in n else ("idcw" if ("idcw" in n or "dividend" in n) else "other")
    return plan, option


def parse_amfi(text: str) -> dict:
    """-> {"layout", "schemes": {code: {...}}, "navs": [(code, date, nav)], "skipped"}"""
    lines = [l.strip() for l in text.splitlines()]
    header = next((l for l in lines if l.lower().startswith("scheme code")), None)
    if header is None:
        raise ValueError("not an AMFI NAV file: no 'Scheme Code' header line")
    cols = [c.strip().lower() for c in header.split(";")]
    layout = "history" if "repurchase price" in cols else "daily"
    ci = {k: next(i for i, c in enumerate(cols) if c.startswith(k)) for k in ("scheme code", "scheme name", "net asset value", "date")}
    isin = [i for i, c in enumerate(cols) if c.startswith("isin")]
    schemes, navs, skipped = {}, [], 0
    category, amc = None, None
    for l in lines:
        if not l or l == header:
            continue
        parts = l.split(";")
        if len(parts) < len(cols):
            m = CATEGORY.match(l)
            if m:
                category = m.group(2).strip()
            else:
                amc = l.strip()                       # fund-house heading line
            continue
        code = parts[ci["scheme code"]].strip()
        if not code.isdigit():
            continue
        nav = _num(parts[ci["net asset value"]])
        try:
            d = _date(parts[ci["date"]])
        except ValueError:
            skipped += 1
            continue
        if nav is None:
            skipped += 1
            continue
        name = parts[ci["scheme name"]].strip()
        plan, option = _plan(name)
        schemes[code] = {"code": code, "name": name, "isin_growth": parts[isin[0]].strip() if isin else None,
                         "isin_reinvest": parts[isin[1]].strip() if len(isin) > 1 else None, "amc": amc, "category": category,
                         "plan": plan, "option": option}
        navs.append((code, d, nav))
    return {"layout": layout, "schemes": schemes, "navs": navs, "skipped": skipped}


class FundStore:
    def __init__(self, data_dir: Path):
        p = Path(data_dir) / "funds"
        p.mkdir(parents=True, exist_ok=True)
        self.path = p / "funds.sqlite"
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._lock = threading.RLock()

    def import_text(self, text: str, *, file: str = "upload", source: str = "AMFI") -> dict:
        parsed = parse_amfi(text)
        sha = hashlib.sha256(text.encode()).hexdigest()
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            cur = self.db.cursor()
            cur.execute("BEGIN")
            for s in parsed["schemes"].values():
                cur.execute("INSERT INTO schemes VALUES (:code,:name,:isin_growth,:isin_reinvest,:amc,:category,:plan,:option,:at) "
                            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, isin_growth=excluded.isin_growth, isin_reinvest=excluded.isin_reinvest, "
                            "amc=COALESCE(excluded.amc, schemes.amc), category=COALESCE(excluded.category, schemes.category), plan=excluded.plan, "
                            "option=excluded.option, updated_at=excluded.updated_at", {**s, "at": now})
            before = self.db.execute("SELECT COUNT(*) FROM navs").fetchone()[0]
            cur.executemany("INSERT OR IGNORE INTO navs VALUES (?,?,?,?)", [(c, d.isoformat(), v, source) for c, d, v in parsed["navs"]])
            added = self.db.execute("SELECT COUNT(*) FROM navs").fetchone()[0] - before
            cur.execute("INSERT OR REPLACE INTO imports VALUES (?,?,?,?,?,?,?,?)",
                        (sha[:16], now, file, sha, len(parsed["navs"]), parsed["skipped"], len(parsed["schemes"]), parsed["layout"]))
            cur.execute("COMMIT")
        return {"layout": parsed["layout"], "schemes": len(parsed["schemes"]), "rows": len(parsed["navs"]), "new_navs": added,
                "duplicates": len(parsed["navs"]) - added, "skipped": parsed["skipped"], "sha256": sha}

    def status(self) -> dict:
        with self._lock:
            q = lambda s: self.db.execute(s).fetchone()
            span = q("SELECT MIN(date), MAX(date) FROM navs")
            return {"schemes": q("SELECT COUNT(*) FROM schemes")[0], "navs": q("SELECT COUNT(*) FROM navs")[0], "first": span[0], "last": span[1],
                    "imports": [dict(r) for r in self.db.execute("SELECT * FROM imports ORDER BY at DESC LIMIT 10")]}

    def search(self, q: str = "", category: str = "", plan: str = "", limit: int = 50, offset: int = 0) -> dict:
        where, args = [], []
        if q:
            where.append("(name LIKE ? OR code = ?)")
            args += [f"%{q}%", q]
        if category:
            where.append("category = ?")
            args.append(category)
        if plan:
            where.append("plan = ?")
            args.append(plan)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self.db.execute(f"SELECT COUNT(*) FROM schemes{w}", args).fetchone()[0]
            rows = [dict(r) for r in self.db.execute(
                f"SELECT s.*, (SELECT COUNT(*) FROM navs n WHERE n.code=s.code) AS nav_points, (SELECT MAX(date) FROM navs n WHERE n.code=s.code) AS last_date "
                f"FROM schemes s{w} ORDER BY name LIMIT ? OFFSET ?", [*args, limit, offset])]
            cats = [r[0] for r in self.db.execute("SELECT DISTINCT category FROM schemes WHERE category IS NOT NULL ORDER BY 1")]
        return {"total": total, "rows": rows, "categories": cats}

    def scheme(self, code: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM schemes WHERE code=?", (code,)).fetchone()
            return dict(r) if r else None

    def series(self, code: str, start: str | None = None, end: str | None = None) -> tuple[list[date], list[float]]:
        q, args = "SELECT date, nav FROM navs WHERE code=?", [code]
        if start:
            q += " AND date >= ?"
            args.append(start)
        if end:
            q += " AND date <= ?"
            args.append(end)
        with self._lock:
            rows = self.db.execute(q + " ORDER BY date", args).fetchall()
        return [date.fromisoformat(r[0]) for r in rows], [r[1] for r in rows]

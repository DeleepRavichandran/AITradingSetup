"""Company financial reports (quarterly results), point in time.

Angel One and Kite serve prices, not financial statements, so results come from a CSV the operator downloads (free):
Screener.in "Quarterly Results" or the exchange's financial-results filings (NSE / BSE corporate announcements).
See docs/FUNDAMENTALS.md and docs/templates/financials_template.csv.

Every row carries `published_at`: the moment the result became public (the exchange filing time). A query "as of"
a time only sees results published BEFORE it, so research and the agent can never read a result early. A row without
a filing time is refused; guessing it would leak the future.

Columns (amounts in Rs crore; blank = unknown, never zero):
  symbol, period_end (YYYY-MM-DD, quarter end), published_at (YYYY-MM-DD HH:MM, IST filing time),
  revenue_cr, operating_profit_cr, net_profit_cr, eps, total_debt_cr, equity_cr, source
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pandas as pd

NUMS = ("revenue_cr", "operating_profit_cr", "net_profit_cr", "eps", "total_debt_cr", "equity_cr")
SCHEMA = """
CREATE TABLE IF NOT EXISTS results (symbol TEXT NOT NULL, period_end TEXT NOT NULL, published_at TEXT NOT NULL, revenue_cr REAL,
  operating_profit_cr REAL, net_profit_cr REAL, eps REAL, total_debt_cr REAL, equity_cr REAL, source TEXT, imported_at TEXT,
  PRIMARY KEY (symbol, period_end));
CREATE INDEX IF NOT EXISTS ix_results_pub ON results(symbol, published_at);
"""


class FinancialsError(ValueError):
    pass


class Financials:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    @staticmethod
    def _num(v):
        v = (v or "").strip().replace(",", "")
        if v in ("", "-", "NA", "N/A"):
            return None
        return float(v)

    def import_csv(self, path: Path, source: str) -> dict:
        """Add or correct rows. A correction of an existing quarter keeps the ORIGINAL published_at unless the new one is
        later (a restatement is public only from its own filing time). Returns counts; raises on the first bad row."""
        n_new, n_upd = 0, 0
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        for k, r in enumerate(rows, 2):
            sym, pe, pub = (r.get("symbol") or "").strip().upper(), (r.get("period_end") or "").strip(), (r.get("published_at") or "").strip()
            if not sym or not pe or not pub:
                raise FinancialsError(f"line {k}: symbol, period_end and published_at are required (published_at = the filing time)")
            try:
                pe_t, pub_t = pd.Timestamp(pe), pd.Timestamp(pub)
            except ValueError as e:
                raise FinancialsError(f"line {k}: bad date: {e}") from None
            if pub_t < pe_t:
                raise FinancialsError(f"line {k}: published_at {pub} is before the quarter ended ({pe})")
            try:
                vals = [self._num(r.get(c)) for c in NUMS]
            except ValueError as e:
                raise FinancialsError(f"line {k}: not a number: {e}") from None
            old = self.db.execute("SELECT published_at FROM results WHERE symbol=? AND period_end=?", (sym, pe_t.date().isoformat())).fetchone()
            pub_s = pub_t.isoformat(timespec="minutes")
            if old is not None and old[0] > pub_s:
                pub_s = old[0]
            self.db.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                            (sym, pe_t.date().isoformat(), pub_s if old is None else max(old[0], pub_s), *vals, r.get("source") or source))
            n_new += old is None
            n_upd += old is not None
        self.db.commit()
        return {"added": n_new, "updated": n_upd, "rows": len(rows)}

    def as_of(self, symbol: str, ts, quarters: int = 8) -> list[dict]:
        """Results of `symbol` published strictly before `ts`, newest quarter first."""
        t = pd.Timestamp(ts).isoformat(timespec="minutes")
        rows = self.db.execute("SELECT * FROM results WHERE symbol=? AND published_at < ? ORDER BY period_end DESC LIMIT ?",
                               (symbol.upper(), t, quarters)).fetchall()
        return [dict(r) for r in rows]

    def symbols(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT symbol FROM results ORDER BY 1")]

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM results").fetchone()[0])

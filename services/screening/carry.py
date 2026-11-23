"""Carry a live paper account forward when the trading rules change (operator, 2026-11-23: "continue with
same money"). A new rules version needs a new account folder (the configuration registry or rules differ,
so the engine checkpoint cannot be reused), but the MONEY and the TRACK RECORD continue:

* cash: the previous account's last recorded cash. Allowed only when it holds no open positions (all
  intraday positions are squared off by 15:15; anything still held is reported and nothing is carried).
* original capital: kept, so "since the start" keeps counting from the first account's starting money.
* forward evidence (the allocator's per-strategy record of completed round trips, net of charges): rebuilt
  from the previous account's ledger for every strategy id that still exists. Only realised past results;
  nothing from the future.

The previous account is the most recent folder in the same capital series (same name up to the rules
suffix) that has any paper fills; folders that never traded are skipped.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd


class CarryError(RuntimeError):
    pass


def series(name: str) -> str:
    """live_angelone_100000_r2_v12 -> live_angelone_100000 (the capital series)."""
    parts = name.split("_")
    while parts and (parts[-1].startswith("r") and parts[-1][1:].isdigit() or parts[-1].startswith("v") and parts[-1][1:].isdigit()):
        parts.pop()
    return "_".join(parts)


def _fills(root: Path) -> int:
    db = root / "screening.sqlite"
    if not db.exists():
        return 0
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def find_previous(screening_dir: Path, name: str) -> Path | None:
    s = series(name)
    best, best_t = None, -1.0
    for d in Path(screening_dir).iterdir():
        if not d.is_dir() or d.name == name or series(d.name) != s or not (d / "live.json").exists():
            continue
        if _fills(d) == 0:
            continue
        t = (d / "screening.sqlite").stat().st_mtime
        if t > best_t:
            best, best_t = d, t
    return best


def carry_state(prev: Path) -> dict:
    meta = json.loads((prev / "live.json").read_text())
    con = sqlite3.connect(f"file:{prev / 'screening.sqlite'}?mode=ro", uri=True)
    try:
        open_ = con.execute("SELECT symbol, SUM(CASE WHEN side='BUY' THEN qty ELSE -qty END) q FROM paper_fills GROUP BY config_id, symbol "
                            "HAVING q != 0").fetchall()
        if open_:
            held = sorted({s for s, _ in open_})
            raise CarryError(f"previous account {prev.name} still holds positions in {', '.join(held)}; nothing carried. "
                             "Restart after they are sold, or set NSEQL_LIVE_RUN to start a separate fresh account.")
        row = con.execute("SELECT minute, cash_inr, equity_inr FROM cycles WHERE cash_inr IS NOT NULL ORDER BY minute DESC LIMIT 1").fetchone()
        if not row:
            raise CarryError(f"previous account {prev.name} has no recorded cash")
        rt = pd.read_sql_query(
            "SELECT b.config_id, b.price AS entry_px, s.qty, s.realized_paise FROM paper_orders s "
            "JOIN paper_orders b ON b.config_id = s.config_id AND b.side = 'BUY' AND b.status IN ('filled','partial') "
            "AND b.fill_ts = (SELECT MAX(fill_ts) FROM paper_orders x WHERE x.config_id = s.config_id AND x.side = 'BUY' "
            "AND x.status IN ('filled','partial') AND x.fill_ts <= s.fill_ts) "
            "WHERE s.side = 'SELL' AND s.status IN ('filled','partial')", con)
    finally:
        con.close()
    ev: dict[str, list[float]] = {}
    for r in rt.itertuples():
        basis = float(r.entry_px) * int(r.qty) * 100
        if basis <= 0:
            continue
        ret = float(r.realized_paise or 0) / basis
        e = ev.setdefault(r.config_id, [0.0, 0.0, 0.0])
        e[0] += 1
        e[1] += 1.0 if ret > 0 else 0.0
        e[2] += ret
    return {"from_account": prev.name, "as_of": row[0], "cash_inr": round(float(row[1]), 2),
            "original_capital": float(meta.get("original_capital") or meta.get("capital") or 0) or None,
            "evidence": ev, "round_trips": int(sum(v[0] for v in ev.values()))}


def apply_evidence(sch, evidence: dict) -> int:
    """Seed the allocator's forward-evidence book for strategy ids that exist in this registry."""
    a = sch.engine.x.alloc
    idx = {str(i): k for k, i in enumerate(sch.reg.ids)}
    n = 0
    for cid, (cnt, wins, s) in evidence.items():
        k = idx.get(cid)
        if k is None:
            continue
        a.ev_n[k] += cnt
        a.ev_w[k] += wins
        a.ev_sum[k] += s
        n += 1
    return n

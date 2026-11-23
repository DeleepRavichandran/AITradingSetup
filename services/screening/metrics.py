"""One definition for every headline number on the dashboard (review 2026-11-24).

Each figure states its record, time window and denominator. Pages read these; they never recompute them.

Account (paper, from the screening ledger)
  equity_now          engine equity now (cash + held shares at the last 1-minute close)         rupees
  starting_capital    the account series' ORIGINAL money (live.json original_capital; a rules change
                      continues the same money and keeps the original)                            rupees
  day_open_equity     last recorded equity before today's first processed minute; if the account
                      has no earlier minute: its start cash                                       rupees
  pnl_today           equity_now - day_open_equity          window: today's session (IST)
  pnl_since_start     equity_now - starting_capital         window: since the series began
  return_since_start  pnl_since_start / starting_capital    denominator: starting_capital
  exposure            gross held value / equity_now          denominator: equity_now

Session decisions (BUY signals of one session, from the `signals` table, by RECORDED allocation reason)
  warm_up             decided on historical bars before the account's live start (never tradable)
  eligible            BUY signals at or after the live start: the paper-tradable population
    accepted          eligible and given an order
    deferred          eligible but a higher-ranked signal used the room this minute
    rejected          eligible and refused; split by recorded reason category
  orders              BUY orders decided that session (paper_orders)
  fills               fills executed that session (paper_fills, both sides)
Counts for a session use the same IST date on every table.
"""
from __future__ import annotations

import json
from pathlib import Path

CATEGORIES = (  # (category, reason prefix) in priority order; recorded reasons only
    ("warm-up history", "warm-up history"),
    ("kill switch", "kill switch"),
    ("daily loss limit", "daily loss limit"),
    ("cash / position / exposure limits", ("not enough free cash", "max ", "symbol exposure", "gross exposure")),
    ("churn limits", ("cool-down", "daily cap", "one at a time")),
    ("losing-strategy gates", ("expected net return", "strategy type losing")),
    ("size or liquidity", ("price above trade size", "no liquidity")),
    ("higher-ranked signal first", ("redundant",)),
)


def reason_category(reason: str | None) -> str:
    r = (reason or "").lower()
    if "after higher-ranked signals this minute" in r or "after earlier signals this minute" in r:
        return "higher-ranked signal first"
    for cat, pre in CATEGORIES:
        if any(r.startswith(p) for p in ((pre,) if isinstance(pre, str) else pre)):
            return cat
    return "other" if r else "not recorded"


def account_meta(root: Path) -> dict:
    f = Path(root) / "live.json"
    return json.loads(f.read_text()) if f.exists() else {}


def account_figures(sch, meta: dict | None, day: str) -> dict:
    """Account figures with their windows (see the module docstring)."""
    meta = meta or {}
    x = sch.engine.x
    snap = x.snapshot()
    db = sch.store.db
    with sch.store._lock:
        before = db.execute("SELECT equity_inr FROM cycles WHERE substr(minute,1,10) < ? AND equity_inr IS NOT NULL ORDER BY minute DESC LIMIT 1",
                            (day,)).fetchone()
        first = db.execute("SELECT MIN(minute) FROM cycles WHERE equity_inr IS NOT NULL").fetchone()[0]
    eq = float(snap["equity_inr"])
    rules_cap = float(x.rules.capital) if x.rules else None
    start_cash = float(meta.get("start_cash") or rules_cap or eq)
    original = float(meta.get("original_capital") or rules_cap or start_cash)
    day_open = float(before[0]) if before else start_cash
    gross = float(snap["gross_exposure_inr"])
    return {
        "equity_now": round(eq, 2), "starting_capital": original, "account_start_cash": start_cash, "day_open_equity": round(day_open, 2),
        "pnl_today": round(eq - day_open, 2), "pnl_since_start": round(eq - original, 2),
        "return_since_start": round(eq / original - 1, 6) if original else None, "exposure": round(gross / eq, 6) if eq else None,
        "cash_free": round(snap["cash_inr"] - snap["reserved_inr"], 2), "invested": round(gross, 2),
        "windows": {"pnl_today": f"{day} (IST session): equity now minus {'the last equity before ' + day if before else 'the account start cash'}",
                    "pnl_since_start": f"since the account series began{' (continued from ' + str((meta.get('carried_from') or {}).get('from_account')) + ')' if meta.get('carried_from') else ''}: equity now minus the original capital",
                    "first_recorded_minute": first},
    }


def session_decisions(db, day: str, live_from: str | None) -> dict:
    """BUY decisions of one IST session by recorded allocation reason, with orders and fills of the same session."""
    rows = db.execute("SELECT bar_ts, alloc_status, alloc_reason FROM signals WHERE decision='BUY' AND substr(bar_ts,1,10) = ?", (day,)).fetchall()
    warm, eligible, acc, dfr = 0, 0, 0, 0
    rej: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for ts, status, why in rows:
        cat = reason_category(why) if status in ("rejected", "deferred") else None
        if (live_from and str(ts) < live_from) or cat == "warm-up history":
            warm += 1
            continue
        eligible += 1
        if status == "accepted":
            acc += 1
        elif status == "deferred":
            dfr += 1
            reasons[str(why)] = reasons.get(str(why), 0) + 1
        else:
            c = cat or "not recorded"
            rej[c] = rej.get(c, 0) + 1
            reasons[str(why)] = reasons.get(str(why), 0) + 1
    orders = db.execute("SELECT COUNT(*) FROM paper_orders WHERE side='BUY' AND substr(decided_at,1,10) = ?", (day,)).fetchone()[0]
    fills = db.execute("SELECT COUNT(*), COALESCE(SUM(side='BUY'),0), COALESCE(SUM(side='SELL'),0) FROM paper_fills WHERE substr(ts,1,10) = ?",
                       (day,)).fetchone()
    last = db.execute("SELECT MAX(minute) FROM cycles WHERE status='processed' AND substr(minute,1,10) = ?", (day,)).fetchone()[0]
    return {"day": day, "warm_up": warm, "eligible": eligible, "accepted": acc, "deferred": dfr,
            "rejected": sum(rej.values()), "rejected_by_category": dict(sorted(rej.items(), key=lambda kv: -kv[1])),
            "top_reasons": [{"reason": k, "n": v, "category": reason_category(k)} for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:5]],
            "orders": int(orders), "fills": int(fills[0]), "buy_fills": int(fills[1]), "sell_fills": int(fills[2]),
            "last_processed_minute": last,
            "definitions": "BUY signals of this IST session by recorded allocation reason; warm-up = decided on historical bars before the "
                           "live start; eligible = at or after it; orders and fills of the same session"}


def category_sql(category: str, column: str = "alloc_reason") -> tuple[str, list]:
    """SQL clause selecting rows whose recorded reason falls in `category` (for ledger drill-downs)."""
    if category == "higher-ranked signal first":
        return (f"({column} LIKE ? OR {column} LIKE ? OR {column} LIKE ?)", ["redundant%", "%after higher-ranked signals this minute",
                                                                               "%after earlier signals this minute"])
    for cat, pre in CATEGORIES:
        if cat == category:
            pres = (pre,) if isinstance(pre, str) else pre
            return ("(" + " OR ".join(f"{column} LIKE ?" for _ in pres) + ")", [p_ + "%" for p_ in pres])
    raise ValueError(f"unknown reason category {category!r}")

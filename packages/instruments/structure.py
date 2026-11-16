"""Market-structure timeline: dated regime changes a backtest must respect."""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache

from nseql.instruments.rules import RULES_DIR


@lru_cache(maxsize=1)
def timeline() -> list[dict]:
    raw = json.loads((RULES_DIR / "market_structure_in.json").read_text())
    return sorted(raw["events"], key=lambda e: e["date"])


def events_between(start: date, end: date, categories: set[str] | None = None) -> list[dict]:
    return [e for e in timeline() if start.isoformat() <= e["date"] <= end.isoformat()
            and (categories is None or e["category"] in categories)]


def window_warnings(kinds: set[str], start: date, end: date) -> list[str]:
    """Human-readable warnings for a backtest window: instruments that did not exist yet, and
    structural breaks inside the window that make early and late results non-comparable."""
    out = []
    for e in timeline():
        for k in e.get("min_start_for", []):
            if k in kinds and start.isoformat() < e["date"]:
                out.append(f"window starts {start} before '{e['title']}' ({e['date']}): {e['effect']}")
    for e in events_between(start, end):
        if not e.get("min_start_for"):
            tag = "" if e.get("verified") else " [unverified date]"
            out.append(f"structural break {e['date']}{tag}: {e['title']} — {e['effect']}")
    return out

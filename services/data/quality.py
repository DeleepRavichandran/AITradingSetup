"""Data-quality validation. Checks are reported, never silently "fixed"."""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from nseql.data.store import Dataset
from nseql.instruments.calendar import TradingCalendar


def check_bars(df: pd.DataFrame, symbol: str, *, split_dates: set | None = None, jump: float = 0.25) -> list[dict]:
    issues: list[dict] = []
    add = lambda sev, code, n, detail="": issues.append({"symbol": symbol, "severity": sev, "check": code,
                                                         "count": int(n), "detail": detail}) if n else None
    if len(df) == 0:
        add("error", "empty", 1)
        return issues
    o, h, l, c, v = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume"))
    ts = df["ts"]
    add("error", "duplicate_timestamp", ts.duplicated().sum())
    add("error", "non_monotonic_timestamp", (ts.diff().dropna() <= pd.Timedelta(0)).sum())
    add("error", "non_positive_price", ((o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)).sum())
    add("error", "ohlc_inconsistent", ((h < np.maximum(o, c) - 1e-9) | (l > np.minimum(o, c) + 1e-9) | (h < l)).sum())
    add("error", "negative_volume", (v < 0).sum())
    add("error", "nan_values", int(df[["open", "high", "low", "close"]].isna().sum().sum()))
    add("info", "zero_volume_bars", (v == 0).sum())
    if len(c) > 1:
        r = np.abs(c[1:] / c[:-1] - 1)
        dates = ts.dt.date.to_numpy()[1:]
        unexplained = [str(d) for d, x in zip(dates, r) if x > jump and d not in (split_dates or set())]
        add("warning", "unexplained_price_jump", len(unexplained),
            f">{jump:.0%} close-to-close without a corporate action: {unexplained[:5]}")
        same = (np.diff(c) == 0) & (v[1:] > 0)
        run = longest = 0
        for s in same:
            run = run + 1 if s else 0
            longest = max(longest, run)
        add("warning", "stale_price_run", longest if longest >= 5 else 0, f"{longest} consecutive unchanged closes")
    return issues


def dataset_quality(ds: Dataset, interval: str = "1d", calendar: TradingCalendar | None = None,
                    symbols: list[str] | None = None, max_symbols: int = 400) -> dict:
    """Coverage and quality report for one interval of a dataset."""
    syms = symbols or ds.symbols(interval)
    truncated = len(syms) > max_symbols
    splits: dict[str, set] = {}
    for ca in ds.corporate_actions():
        if ca.kind in ("split", "bonus"):
            splits.setdefault(ca.symbol, set()).add(ca.ex_date)
    all_dates: set = set()
    frames = {}
    for s in syms[:max_symbols]:
        frames[s] = ds.bars(s, interval)
        all_dates |= set(frames[s]["ts"].dt.date)
    issues, coverage = [], []
    sessions = sorted(all_dates)
    cal_missing = []
    if calendar and sessions:
        cal_missing = [str(d) for d in calendar.missing_sessions(all_dates, sessions[0], sessions[-1])]
    for s, df in frames.items():
        issues += check_bars(df, s, split_dates=splits.get(s))
        inst = ds.instruments.get(s)
        d = set(df["ts"].dt.date)
        if len(df):
            lo, hi = min(d), max(d)
            expected = [x for x in sessions if lo <= x <= hi]
            gaps = [str(x) for x in expected if x not in d]
            # a derivative or an index with no bar on a session it was listed is a data gap; an
            # illiquid option may legitimately not trade, so it is reported as info there
            if gaps:
                issues.append({"symbol": s, "severity": "info" if inst and inst.is_option else "warning",
                               "check": "missing_sessions", "count": len(gaps), "detail": f"first: {gaps[:5]}"})
            coverage.append({"symbol": s, "kind": inst.kind.value if inst else "?", "first": str(df["ts"].iloc[0]),
                             "last": str(df["ts"].iloc[-1]), "bars": int(len(df)), "missing_sessions": len(gaps),
                             "zero_volume_bars": int((df["volume"] == 0).sum())})
    counts = {"error": 0, "warning": 0, "info": 0}
    for i in issues:
        counts[i["severity"]] += 1
    return {"dataset_id": ds.id, "interval": interval, "synthetic": ds.synthetic, "symbols_checked": len(frames),
            "symbols_total": len(syms), "truncated": truncated, "sessions": len(sessions),
            "first_session": str(sessions[0]) if sessions else None, "last_session": str(sessions[-1]) if sessions else None,
            "calendar_source": calendar.source if calendar else "derived from data (no external calendar supplied)",
            "sessions_missing_vs_calendar": cal_missing[:50], "issue_counts": counts, "issues": issues[:500],
            "coverage": coverage, "integrity_failures": ds.verify(),
            "generated_at": datetime.now().isoformat(timespec="seconds")}


def feed_is_stale(last_tick: datetime | None, now: datetime, max_age_seconds: float) -> bool:
    """Stale-feed monitor used by the paper/live path."""
    return last_tick is None or (now - last_tick).total_seconds() > max_age_seconds

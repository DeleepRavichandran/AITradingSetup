"""Real-market data audit (docs/VALIDATION.md, "real NSE data"): what the live Angel One feed actually delivered.

Per live account and session: first bar time (the first completed regular candle is stamped 09:16), bars per stock
(375 expected for a full session), missing minutes, late bars (arrived after their minute was processed), conflicting
re-sent bars, quarantined impossible bars, and the arrival delay of each bar after its candle closed. Plus the historical
minute dataset's coverage. Read-only. This is the evidence about data availability; the screening benchmark is not.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

FULL_SESSION_BARS = 375
COMMANDS = [
    "python -m nseql.cli angel-check                     # login, instrument resolution, one candle",
    "python -m nseql.cli angel-verify                    # adapter checks against the real service",
    "python -m nseql.cli history-validate --months 12    # import 12 months of 1-minute history and replay the live rules",
]


def audit_account(account: Path, last_sessions: int = 10) -> dict:
    con = sqlite3.connect(f"file:{Path(account) / 'screening.sqlite'}?mode=ro", uri=True)
    try:
        ev = pd.read_sql_query("SELECT symbol, ts, received_at, late FROM events", con)
        conf = pd.read_sql_query("SELECT symbol, ts FROM event_conflicts", con)
        try:
            quar = pd.read_sql_query("SELECT symbol, ts, reason FROM event_quarantine", con)
        except Exception:       # noqa: BLE001 - older ledgers
            quar = pd.DataFrame(columns=["symbol", "ts", "reason"])
    finally:
        con.close()
    if ev.empty:
        return {"account": Path(account).name, "sessions": []}
    ev["ts"] = pd.to_datetime(ev.ts)
    ev["day"] = ev.ts.dt.strftime("%Y-%m-%d")
    # arrival delay: received (epoch, local clock) minus the candle's close in IST (stored naive)
    close_epoch = ev.ts.dt.tz_localize("Asia/Kolkata").astype("int64") / 1e9
    ev["arrival_s"] = ev.received_at - close_epoch
    rows = []
    for d, g in sorted(ev.groupby("day"), key=lambda x: x[0])[-last_sessions:]:
        live = g[g.late == 0]
        per = live.groupby("symbol").size()
        lag = g.arrival_s[(g.arrival_s > -5) & (g.arrival_s < 3600)]
        rows.append({"day": d, "first_bar": live.ts.min().strftime("%H:%M") if len(live) else None,
                     "last_bar": live.ts.max().strftime("%H:%M") if len(live) else None, "symbols": int(per.size),
                     "bars_median": int(per.median()) if len(per) else 0, "bars_min": int(per.min()) if len(per) else 0,
                     "missing_bars": int(max(0, FULL_SESSION_BARS * per.size - per.sum())),
                     "late_bars": int((g.late == 1).sum()), "conflicting_resends": int((conf.ts.str[:10] == d).sum()),
                     "quarantined": int((quar.ts.str[:10] == d).sum()) if len(quar) else 0,
                     "arrival_p50_s": None if lag.empty else round(float(np.median(lag)), 1),
                     "arrival_p95_s": None if lag.empty else round(float(np.quantile(lag, 0.95)), 1),
                     "first_bar_ok": bool(len(live) and live.ts.min().strftime("%H:%M") == "09:16")})
    return {"account": Path(account).name, "sessions": rows,
            "notes": ["arrival delay uses this machine's clock at receipt; replayed history has no meaningful arrival time",
                      "a half-day or holiday session shows fewer bars by design"]}


def audit(exp_dir: Path, data_dir: Path | None = None) -> dict:
    from nseql.screening.integrity import accounts
    acc = accounts(exp_dir)
    out = {"live": [audit_account(a) for a in acc["incumbent"][-1:]], "commands": COMMANDS, "history": None}
    if data_dir is not None and (Path(data_dir) / "angel_minute").exists():
        from nseql.data.store import Dataset
        from nseql.screening.history_validation import coverage
        out["history"] = coverage(Dataset(Path(data_dir) / "angel_minute"))
    else:
        out["history_status"] = ("no historical minute dataset on this machine yet: the laptop's weekly history-validate job "
                                 "imports it (Angel One credentials stay on the laptop)")
    return out


def update_status(exp_dir: Path) -> dict:
    """The laptop's daily self-update, from the last line of experiments/logs/update.log (written by daily_update.ps1).
    2026-11-26: the updater refused for two days because of a staged edit to a doc ("NOT updated: local changes") and
    nothing on the dashboard said so; the lab traded on rules two versions old. A refusal or rollback is now 'blocked'."""
    f = Path(exp_dir) / "logs" / "update.log"
    if not f.exists():
        return {"status": "not configured", "detail": "no update log (the daily self-update is not installed on this machine)"}
    lines = [ln.strip().lstrip("﻿") for ln in f.read_text(encoding="utf-8", errors="replace").splitlines()]
    dated = [ln for ln in lines if len(ln) > 20 and ln[:4].isdigit() and ln[4] == "-" and ln[10] == " "]
    if not dated:
        return {"status": "unverified", "detail": "update log has no dated entry yet"}
    last = dated[-1]
    at, msg = last[:19], last[20:]
    bad = ("NOT updated", "rolling back", "ROLLBACK", "unhealthy")
    if any(b in msg for b in bad):
        fix = (" Fix: on the laptop run 'git status --short' in the lab folder; set the change aside with 'git stash', then run "
               "scripts\\windows\\daily_update.ps1." if "local changes" in msg else "")
        return {"status": "blocked", "detail": f"{at}: {msg}.{fix}", "at": at}
    return {"status": "ready", "detail": f"{at}: {msg}", "at": at}

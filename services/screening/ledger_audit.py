"""Read-only audit of an existing paper ledger for traces of the measurement bugs fixed on 2026-11-24, and of execution
assumptions changed on 2026-11-25 (X: version, X3: cash below zero, X4: retrospective decisions traded).

Ledgers are never modified. The audit writes LIMITATIONS.json beside the ledger, so that reports made from that
ledger before the fixes can be read with their limitations:

  F1  stop-limit buys priced below their trigger           backtester only (the minute loop uses no stop-limit orders)
  F2  failed ledger write skipped a minute                  -> minutes with stored bars but no cycle row at all
  F3  a replay replaced the live account on the dashboard   display only; no ledger effect (nothing to detect)
  F4  catch-up marked received bars as no_data              -> 'no_data' cycle rows for minutes that have stored bars;
                                                               their equity/cash points are missing from curves
  F5  a second migration dropped inherited evidence         -> account continued from an account that itself continued
  F6  overnight exits lost entry time and buy charges       -> SELL fills whose BUY filled on an earlier date: daily
                                                               reports / RCA for those exit days, made before the fix
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def audit(account: Path) -> dict:
    account = Path(account)
    con = sqlite3.connect(f"file:{account / 'screening.sqlite'}?mode=ro", uri=True)
    try:
        q = lambda sql: con.execute(sql).fetchall()
        f4 = q("SELECT c.minute FROM cycles c WHERE c.status = 'no_data' AND EXISTS (SELECT 1 FROM events e WHERE e.ts = c.minute AND e.late = 0) "
               "ORDER BY 1")
        f2 = q("SELECT DISTINCT e.ts FROM events e WHERE e.late = 0 AND substr(e.ts, 12, 5) BETWEEN '09:16' AND '15:30' "
               "AND e.ts <= (SELECT MAX(minute) FROM cycles) AND NOT EXISTS (SELECT 1 FROM cycles c WHERE c.minute = e.ts) ORDER BY 1")
        f6 = q("SELECT DISTINCT substr(s.fill_ts, 1, 10) FROM paper_orders s WHERE s.side = 'SELL' AND s.status IN ('filled','partial') "
               "AND (SELECT substr(MAX(b.fill_ts), 1, 10) FROM paper_orders b WHERE b.config_id = s.config_id AND b.side = 'BUY' "
               "AND b.status IN ('filled','partial') AND b.fill_ts <= s.fill_ts) < substr(s.fill_ts, 1, 10) ORDER BY 1")
        x3 = q("SELECT DISTINCT substr(minute,1,10) FROM cycles WHERE cash_inr < 0 ORDER BY 1")
        x4 = q("SELECT DISTINCT substr(c.minute,1,10) FROM cycles c WHERE c.mode = 'live' AND c.lag_s > 55 AND EXISTS "
               "(SELECT 1 FROM paper_orders o WHERE o.decided_at = c.minute) ORDER BY 1")
    finally:
        con.close()
    meta = json.loads((account / "live.json").read_text()) if (account / "live.json").exists() else {}
    src = (meta.get("carried_from") or {}).get("from_account")
    chained = bool(src) and (account.parent / src / "live.json").exists() and bool(
        json.loads((account.parent / src / "live.json").read_text()).get("carried_from"))
    days = lambda rows: sorted({str(r[0])[:10] for r in rows})
    findings = []
    if f2:
        findings.append({"id": "F2", "minutes": len(f2), "days": days(f2),
                         "limitation": "minutes with stored bars were never processed: trades in them are missing from this ledger"})
    if f4:
        findings.append({"id": "F4", "minutes": len(f4), "days": days(f4),
                         "limitation": "minutes with bars recorded as no_data: equity/cash points missing from curves; data-quality counts overstated"})
    if f6:
        findings.append({"id": "F6", "days": [d[0] for d in f6],
                         "limitation": "overnight exits on these days: trade-report / RCA made before the fix lack entry time and buy charges; rerun them"})
    if chained:
        findings.append({"id": "F5", "limitation": f"continued from {src}, which itself continued an account: evidence inherited before the fix "
                                                   "may be incomplete (rebuilt automatically at the next migration)"})
    from nseql.screening.execution import EXECUTION_VERSION
    ex = json.loads((account / "execution.json").read_text()) if (account / "execution.json").exists() else None
    old_versions = [h for h in (ex or {}).get("history", []) if h["version"] != EXECUTION_VERSION]
    if ex is None or old_versions:
        findings.append({"id": "X", "versions": [h["version"] for h in old_versions] or ["1.x (before execution versioning)"],
                         "limitation": f"results produced before execution {EXECUTION_VERSION}: recompute them (kill-switch fills after a halt, "
                                       "exposure and cash not rechecked at the fill price, retrospective catch-up decisions traded). "
                                       "The ledger itself is kept unchanged."})
    if x3:
        findings.append({"id": "X3", "days": [d[0] for d in x3], "limitation": "cash went below zero after charges on these days"})
    if x4:
        findings.append({"id": "X4", "days": [d[0] for d in x4],
                         "limitation": "orders were decided more than 55 s after their candle closed (retrospective) on these days"})
    out = {"account": account.name, "ledger_modified": False, "findings": findings,
           "clean": not findings, "not_detectable": {"F1": "backtester only", "F3": "dashboard display only"}}
    (account / "LIMITATIONS.json").write_text(json.dumps(out, indent=2))
    return out

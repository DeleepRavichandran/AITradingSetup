#!/usr/bin/env python3
"""HUMAN-ONLY: sign an approval for one exact strategy version to pass the live gate.

Run by hand in a terminal that has HUMAN_APPROVAL_SECRET set. Never run from automation, an
agent, or the API service. An approval alone does not enable trading: LIVE_TRADING_ENABLED,
the compliance checklist and the kill switch are checked independently at order time.

    HUMAN_APPROVAL_SECRET=... python scripts/approve_strategy.py --sha <sha256> --approver "Your Name" \
        --params '{"fast": 20}' --max-capital 200000 --days 30
"""
import argparse
import json
import os
import sys
from pathlib import Path

from nseql.execution.gate import sign_approval


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sha", required=True, help="sha256 of the strategy source (shown in the Strategy Code page)")
    ap.add_argument("--approver", required=True)
    ap.add_argument("--params", default="{}")
    ap.add_argument("--max-capital", type=float, required=True)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--dir", default=os.environ.get("NSEQL_EXPERIMENTS_DIR", "experiments") + "/approvals")
    a = ap.parse_args()
    if not sys.stdin.isatty():
        print("refusing to run non-interactively: approvals must be made by a person at a terminal", file=sys.stderr)
        return 2
    print(f"You are approving strategy {a.sha[:16]}… for LIVE trading with up to Rs {a.max_capital:,.0f} for {a.days} days.")
    if input("Type APPROVE to confirm: ").strip() != "APPROVE":
        print("aborted")
        return 1
    rec = sign_approval(os.environ.get("HUMAN_APPROVAL_SECRET", ""), a.sha, json.loads(a.params), a.approver, a.days, a.max_capital)
    Path(a.dir).mkdir(parents=True, exist_ok=True)
    Path(a.dir, f"{a.sha}.json").write_text(json.dumps(rec, indent=1))
    print(f"approval written to {a.dir}/{a.sha}.json (expires {rec['expires']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

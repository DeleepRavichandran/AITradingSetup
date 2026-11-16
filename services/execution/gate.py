"""The live-trading gate.

A real order can leave this process only if ALL of the following hold at the moment of the call:

  1. LIVE_TRADING_ENABLED=true was set in the environment by the operator before the process
     started (settings are frozen at start-up; no code path writes to them);
  2. the kill switch is not engaged;
  3. every item of the compliance checklist (a file maintained by the operator) is confirmed;
  4. a human approval exists for the EXACT strategy source hash and parameters, is unexpired, and
     carries a valid HMAC signature made with HUMAN_APPROVAL_SECRET.

Approvals are created by `scripts/approve_strategy.py`, which a person runs by hand and which
reads the secret from the terminal environment. The secret is never passed to the research
workers or the sandbox, so no agent can mint or alter an approval. The default state of every
condition is "closed".
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from nseql.config import Settings, get_settings
from nseql.risk.limits import KillSwitch

COMPLIANCE_ITEMS = {
    "broker_api_terms_accepted": "Broker API terms accepted; API subscription active for the account that will trade",
    "algo_registered": "Strategy registered / approved as an algo with the broker and exchange where required by current SEBI and NSE rules",
    "static_ip_registered": "Orders will originate from a static IP registered with the broker",
    "order_rate_within_threshold": "Configured order rate is within the broker's and exchange's per-second thresholds",
    "risk_limits_reviewed": "Capital, per-order, per-position and daily-loss limits reviewed and signed off",
    "reconciliation_tested": "Position and order reconciliation against the broker tested in paper mode",
    "recovery_runbook_tested": "Disconnect / restart / kill-switch runbook rehearsed",
    "tax_and_audit_trail": "Order and trade audit trail retention arranged as required",
}


class LiveTradingDisabled(PermissionError):
    pass


def _canonical(strategy_sha256: str, params: dict, approver: str, expires: str, max_capital: float) -> bytes:
    return json.dumps({"sha": strategy_sha256, "params": params, "approver": approver, "expires": expires,
                       "max_capital": max_capital}, sort_keys=True, separators=(",", ":")).encode()


def sign_approval(secret: str, strategy_sha256: str, params: dict, approver: str, valid_days: int, max_capital: float) -> dict:
    """Used ONLY by the human-run approval script."""
    if not secret or len(secret) < 16:
        raise ValueError("HUMAN_APPROVAL_SECRET must be set (at least 16 characters)")
    if not approver.strip() or approver.lower().startswith(("agent", "ai", "system", "claude")):
        raise ValueError("approver must be a named human")
    expires = (datetime.now() + timedelta(days=valid_days)).isoformat(timespec="seconds")
    sig = hmac.new(secret.encode(), _canonical(strategy_sha256, params, approver, expires, max_capital), hashlib.sha256).hexdigest()
    return {"strategy_sha256": strategy_sha256, "params": params, "approver": approver, "expires": expires,
            "max_capital": max_capital, "signature": sig, "created": datetime.now().isoformat(timespec="seconds")}


@dataclass
class GateDecision:
    allowed: bool
    reasons: list[str]

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reasons": self.reasons}


class LiveGate:
    def __init__(self, approvals_dir: Path, compliance_file: Path, kill_switch: KillSwitch,
                 settings: Settings | None = None, env: dict | None = None):
        self.approvals_dir, self.compliance_file, self.kill = Path(approvals_dir), Path(compliance_file), kill_switch
        self.settings = settings or get_settings()
        self._env = os.environ if env is None else env

    def compliance(self) -> dict[str, bool]:
        done = {}
        if self.compliance_file.exists():
            try:
                done = json.loads(self.compliance_file.read_text())
            except ValueError:
                done = {}
        return {k: done.get(k) is True for k in COMPLIANCE_ITEMS}

    def approval(self, strategy_sha256: str) -> dict | None:
        f = self.approvals_dir / f"{strategy_sha256}.json"
        try:
            return json.loads(f.read_text()) if f.exists() else None
        except ValueError:
            return None

    def check(self, strategy_sha256: str | None, params: dict | None = None, capital: float = 0.0) -> GateDecision:
        why = []
        if not self.settings.live_trading_enabled:
            why.append("LIVE_TRADING_ENABLED is false")
        if self.kill.engaged:
            why.append(f"kill switch engaged: {self.kill.reason}")
        missing = [k for k, ok in self.compliance().items() if not ok]
        if missing:
            why.append(f"compliance checklist incomplete ({len(missing)} of {len(COMPLIANCE_ITEMS)} items open)")
        secret = self._env.get("HUMAN_APPROVAL_SECRET", "")
        ap = self.approval(strategy_sha256) if strategy_sha256 else None
        if not strategy_sha256:
            why.append("order is not attributed to an approved strategy version")
        elif ap is None:
            why.append("no human approval on file for this strategy version")
        elif not secret:
            why.append("approval cannot be verified: HUMAN_APPROVAL_SECRET not available to the execution service")
        else:
            try:
                good = hmac.new(secret.encode(), _canonical(ap["strategy_sha256"], ap["params"], ap["approver"], ap["expires"],
                                                            ap["max_capital"]), hashlib.sha256).hexdigest()
                if ap["strategy_sha256"] != strategy_sha256 or not hmac.compare_digest(good, ap.get("signature", "")):
                    why.append("approval signature is invalid")
                elif datetime.fromisoformat(ap["expires"]) < datetime.now():
                    why.append("approval has expired")
                elif params is not None and ap["params"] != params:
                    why.append("approved parameters differ from the parameters being traded")
                elif capital > ap["max_capital"]:
                    why.append("capital exceeds the approved maximum")
            except (KeyError, TypeError, ValueError):
                why.append("approval record is malformed")
        return GateDecision(not why, why)

    def require(self, strategy_sha256: str | None, params: dict | None = None, capital: float = 0.0) -> None:
        d = self.check(strategy_sha256, params, capital)
        if not d.allowed:
            raise LiveTradingDisabled("live order blocked: " + "; ".join(d.reasons))

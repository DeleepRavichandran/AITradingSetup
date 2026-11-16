"""Pre-trade risk limits shared by the backtester and the paper/live order path."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RiskLimits:
    max_order_notional: float = 5e7            # rupees
    max_position_notional: float = 1e8         # rupees per symbol
    max_gross_leverage: float = 5.0            # gross notional / equity
    max_daily_loss_pct: float = 1.0            # 1.0 = disabled (100% of start-of-day equity)
    max_orders_per_day: int = 10_000
    allow_equity_short_overnight: bool = False  # cash-market shorts must be intraday (MIS) by default
    banned_symbols: set = field(default_factory=set)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["banned_symbols"] = sorted(self.banned_symbols)
        return d


class KillSwitch:
    """Process-wide trading halt. Once engaged, only a human reset (with a reason) clears it."""

    def __init__(self):
        self.engaged = False
        self.reason = ""
        self.history: list[dict] = []

    def engage(self, reason: str, ts=None) -> None:
        if not self.engaged:
            self.engaged = True
            self.reason = reason
            self.history.append({"event": "ENGAGED", "reason": reason, "ts": str(ts)})

    def reset(self, operator: str, reason: str, ts=None) -> None:
        if not operator or operator.lower().startswith(("agent", "ai", "system")):
            raise PermissionError("kill switch can only be reset by a named human operator")
        self.engaged = False
        self.reason = ""
        self.history.append({"event": "RESET", "operator": operator, "reason": reason, "ts": str(ts)})

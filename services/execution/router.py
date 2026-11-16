"""Order router: every order — paper or live — passes the same controls in the same order.

    idempotency -> kill switch -> session hours -> rate limit -> order/position/daily-loss limits
    -> destination (paper by default; a live broker only through its gate) -> audit log
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from nseql.execution.brokers.base import BrokerAdapter, OrderRequest
from nseql.execution.gate import LiveTradingDisabled
from nseql.instruments.calendar import TradingCalendar
from nseql.risk.limits import KillSwitch


@dataclass
class RouterLimits:
    max_orders_per_second: float = 5.0
    max_order_value: float = 500_000.0
    max_daily_loss: float = 25_000.0
    max_open_positions: int = 20
    enforce_session_hours: bool = True
    consecutive_reject_breaker: int = 5      # circuit breaker: trip the kill switch after N rejects in a row


class OrderRouter:
    def __init__(self, paper_submit: Callable[[OrderRequest], dict], kill: KillSwitch, *, limits: RouterLimits | None = None,
                 calendar: TradingCalendar | None = None, live: BrokerAdapter | None = None,
                 audit: Callable[..., None] | None = None, clock: Callable[[], datetime] = datetime.now,
                 monotonic: Callable[[], float] = time.monotonic):
        self.paper_submit, self.kill, self.live = paper_submit, kill, live
        self.limits = limits or RouterLimits()
        self.calendar = calendar or TradingCalendar()
        self.audit = audit or (lambda *a, **k: None)
        self.clock, self.monotonic = clock, monotonic
        self._seen: dict[str, dict] = {}
        self._stamps: deque = deque()
        self._rejects = 0
        self.daily_pnl = 0.0
        self.open_positions = 0

    def _reject(self, req: OrderRequest, reason: str) -> dict:
        self._rejects += 1
        out = {"status": "REJECTED", "reason": reason, "client_order_id": req.client_order_id}
        self.audit("order_router", "ORDER_REJECTED", **out, symbol=req.symbol)
        if self._rejects >= self.limits.consecutive_reject_breaker and not self.kill.engaged:
            self.kill.engage(f"circuit breaker: {self._rejects} consecutive rejected orders", self.clock())
            self.audit("order_router", "KILL_SWITCH_ENGAGED", reason=self.kill.reason)
        self._seen[req.client_order_id] = out
        return out

    def submit(self, req: OrderRequest, *, ref_price: float, live: bool = False, capital: float = 0.0) -> dict:
        if req.client_order_id in self._seen:                     # duplicate-order protection
            return {**self._seen[req.client_order_id], "duplicate": True}
        if self.kill.engaged:
            return self._reject(req, f"KILL_SWITCH: {self.kill.reason}")
        if self.limits.enforce_session_hours and not self.calendar.is_open(self.clock()):
            return self._reject(req, "MARKET_CLOSED")
        now = self.monotonic()
        while self._stamps and now - self._stamps[0] > 1.0:
            self._stamps.popleft()
        if len(self._stamps) >= self.limits.max_orders_per_second:
            return self._reject(req, "RATE_LIMIT")
        if req.qty <= 0 or req.qty * ref_price > self.limits.max_order_value:
            return self._reject(req, "MAX_ORDER_VALUE")
        if self.daily_pnl <= -self.limits.max_daily_loss:
            return self._reject(req, "DAILY_LOSS_LIMIT")
        if req.side > 0 and self.open_positions >= self.limits.max_open_positions:
            return self._reject(req, "MAX_OPEN_POSITIONS")
        self._stamps.append(now)
        if live:
            if self.live is None:
                return self._reject(req, "NO_LIVE_BROKER_CONFIGURED")
            try:
                res = {"status": "SENT_LIVE", "broker": self.live.name, "response": self.live.place_order(req, capital)}
            except LiveTradingDisabled as e:
                return self._reject(req, str(e))
            except NotImplementedError as e:
                return self._reject(req, str(e))
        else:
            res = {"status": "ACCEPTED_PAPER", **self.paper_submit(req)}
        self._rejects = 0
        res["client_order_id"] = req.client_order_id
        self._seen[req.client_order_id] = res
        self.audit("order_router", "ORDER_ROUTED", destination="live" if live else "paper", symbol=req.symbol,
                   side=req.side, qty=req.qty, client_order_id=req.client_order_id)
        return res

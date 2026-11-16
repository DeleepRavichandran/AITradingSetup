"""Broker adapter interface. Market data connectivity is deliberately separate from order routing."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from nseql.execution.gate import LiveGate


@dataclass
class OrderRequest:
    client_order_id: str            # idempotency key
    symbol: str
    side: int
    qty: int
    order_type: str = "MARKET"
    limit_price: float | None = None
    stop_price: float | None = None
    product: str = "CNC"
    exchange: str = "NSE"
    strategy_sha256: str | None = None
    strategy_params: dict | None = None
    tag: str = ""


@dataclass
class Capabilities:
    order_types: set = field(default_factory=set)
    products: set = field(default_factory=set)
    segments: set = field(default_factory=set)
    orders_per_second: float = 0.0
    requires_static_ip: bool = True
    token_lifetime: str = ""
    notes: str = ""


class BrokerAdapter(ABC):
    name = "base"
    status = "NOT IMPLEMENTED"
    capabilities = Capabilities()

    def __init__(self, gate: LiveGate):
        self._gate = gate

    def place_order(self, req: OrderRequest, capital: float = 0.0) -> dict:
        """The ONLY entry point for a real order. The gate is checked here, inside the adapter, so
        no caller can skip it."""
        self._gate.require(req.strategy_sha256, req.strategy_params, capital)
        return self._send_order(req)

    @abstractmethod
    def _send_order(self, req: OrderRequest) -> dict: ...

    def cancel_order(self, broker_order_id: str) -> dict:
        raise NotImplementedError(f"{self.name}: cancel_order is NOT IMPLEMENTED")

    def positions(self) -> list[dict]:
        raise NotImplementedError(f"{self.name}: positions is NOT IMPLEMENTED")

    def instruments(self) -> list[dict]:
        raise NotImplementedError(f"{self.name}: instrument synchronisation is NOT IMPLEMENTED")

    def describe(self) -> dict:
        c = self.capabilities
        return {"name": self.name, "status": self.status, "order_types": sorted(c.order_types), "products": sorted(c.products),
                "segments": sorted(c.segments), "orders_per_second": c.orders_per_second,
                "requires_static_ip": c.requires_static_ip, "token_lifetime": c.token_lifetime, "notes": c.notes}

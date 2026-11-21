"""Brokers whose adapters are NOT IMPLEMENTED. They exist so the interface and the dashboard can
show them honestly; every operation raises."""
from nseql.execution.brokers.base import BrokerAdapter, Capabilities, OrderRequest


class _Stub(BrokerAdapter):
    status = "NOT IMPLEMENTED"

    def _send_order(self, req: OrderRequest) -> dict:
        raise NotImplementedError(f"{self.name}: order routing is NOT IMPLEMENTED")


class Upstox(_Stub):
    name = "upstox"
    capabilities = Capabilities(notes="adapter not written; interface only")


class AngelOneSmartAPI(_Stub):
    name = "angelone_smartapi"
    capabilities = Capabilities(notes="adapter not written; interface only")

"""Zerodha Kite Connect adapter.

STATUS: UNVERIFIED. Request construction follows the public Kite Connect v3 REST conventions
and is unit-tested against a mock transport. It has never been run against the real API from
this project, and it cannot send an order unless the live gate passes.

Notes for operators:
  * access tokens are issued by an interactive login (request_token -> session) and expire daily;
    there is no unattended refresh — a human must log in each trading day;
  * order placement requires a registered static IP under current exchange/broker rules.
"""
from __future__ import annotations

import hashlib
import os

import httpx

from nseql.execution.brokers.base import BrokerAdapter, Capabilities, OrderRequest
from nseql.execution.gate import LiveGate

BASE_URL = "https://api.kite.trade"
ORDER_TYPE = {"MARKET": "MARKET", "LIMIT": "LIMIT", "STOP": "SL-M", "STOP_LIMIT": "SL"}


class ZerodhaKite(BrokerAdapter):
    name = "zerodha_kite"
    status = "UNVERIFIED (mock-tested; needs KITE_API_KEY / KITE_ACCESS_TOKEN and a live-gate pass)"
    capabilities = Capabilities({"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}, {"CNC", "MIS", "NRML"}, {"NSE", "NFO", "CDS", "MCX"},
                                orders_per_second=10, token_lifetime="one trading day (manual login)",
                                notes="per-second order limits and static-IP rules are set by the broker and change; verify before use")

    def __init__(self, gate: LiveGate, api_key: str | None = None, access_token: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        super().__init__(gate)
        self.api_key = api_key or os.environ.get("KITE_API_KEY", "")
        self.access_token = access_token or os.environ.get("KITE_ACCESS_TOKEN", "")
        self._transport = transport

    @property
    def connected(self) -> bool:
        return bool(self.api_key and self.access_token)

    def _client(self) -> httpx.Client:
        if not self.connected:
            raise PermissionError("Kite credentials are not configured")
        return httpx.Client(base_url=BASE_URL, transport=self._transport, timeout=10.0, headers={
            "X-Kite-Version": "3", "Authorization": f"token {self.api_key}:{self.access_token}"})

    @staticmethod
    def session_checksum(api_key: str, request_token: str, api_secret: str) -> str:
        return hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()

    def order_payload(self, req: OrderRequest) -> dict:
        d = {"tradingsymbol": req.symbol, "exchange": req.exchange, "transaction_type": "BUY" if req.side > 0 else "SELL",
             "order_type": ORDER_TYPE[req.order_type], "quantity": req.qty, "product": req.product, "validity": "DAY",
             "tag": (req.tag or req.client_order_id)[:20]}
        if req.limit_price is not None:
            d["price"] = req.limit_price
        if req.stop_price is not None:
            d["trigger_price"] = req.stop_price
        return d

    def _send_order(self, req: OrderRequest) -> dict:
        with self._client() as c:
            r = c.post("/orders/regular", data=self.order_payload(req))
            r.raise_for_status()
            return r.json()

    def positions(self) -> list[dict]:
        with self._client() as c:
            r = c.get("/portfolio/positions")
            r.raise_for_status()
            return r.json()["data"]["net"]

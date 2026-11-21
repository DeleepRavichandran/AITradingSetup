"""WhatsApp notifications via the WhatsApp Business Cloud API (Meta Graph API).

Environment (see .env.example):
    WHATSAPP_ACCESS_TOKEN      system-user access token of your WhatsApp Business app
    WHATSAPP_PHONE_NUMBER_ID   id of the sending phone number
    WHATSAPP_TO                recipient number(s) in international format, comma separated
    WHATSAPP_TEMPLATE          optional: name of an approved message template with ONE body variable
    WHATSAPP_API_VERSION       Graph API version, e.g. v21.0 (set to the version your app uses)

How WhatsApp delivery works (a platform rule, not ours): a free-form text message is delivered
only inside the 24-hour window after the recipient last messaged your business number. Outside
it, only pre-approved TEMPLATE messages are delivered. For unattended alerts, create a template
such as "Quant Lab alert: {{1}}", get it approved, and set WHATSAPP_TEMPLATE.

STATUS: UNVERIFIED — tested against a mock transport only; no credentials were available.
Notifications are best-effort: a failure is logged and never interrupts trading or research.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime

import httpx

GRAPH = "https://graph.facebook.com"


@dataclass
class WhatsAppNotifier:
    token: str = ""
    phone_number_id: str = ""
    recipients: list[str] = field(default_factory=list)
    template: str = ""
    template_lang: str = "en"
    api_version: str = "v21.0"
    transport: httpx.BaseTransport | None = None
    log: list[dict] = field(default_factory=list)

    @staticmethod
    def from_env(env: dict | None = None) -> "WhatsAppNotifier":
        e = os.environ if env is None else env
        return WhatsAppNotifier(e.get("WHATSAPP_ACCESS_TOKEN", ""), e.get("WHATSAPP_PHONE_NUMBER_ID", ""),
                                [x.strip().lstrip("+") for x in e.get("WHATSAPP_TO", "").split(",") if x.strip()],
                                e.get("WHATSAPP_TEMPLATE", ""), e.get("WHATSAPP_TEMPLATE_LANG", "en"),
                                e.get("WHATSAPP_API_VERSION", "v21.0"))

    @property
    def configured(self) -> bool:
        return bool(self.token and self.phone_number_id and self.recipients)

    def status(self) -> dict:
        return {"configured": self.configured, "recipients": len(self.recipients),
                "mode": "template" if self.template else "text (24-hour window only)", "api_version": self.api_version,
                "status": "UNVERIFIED" if self.configured else "NOT CONFIGURED", "recent": self.log[-20:]}

    def payload(self, to: str, text: str) -> dict:
        text = text[:1000]
        if self.template:
            return {"messaging_product": "whatsapp", "to": to, "type": "template",
                    "template": {"name": self.template, "language": {"code": self.template_lang},
                                 "components": [{"type": "body", "parameters": [{"type": "text", "text": text}]}]}}
        return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"preview_url": False, "body": text}}

    def send(self, event: str, text: str) -> bool:
        """Returns True only if every recipient was accepted by the API."""
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, "text": text[:200], "ok": False, "detail": ""}
        self.log.append(entry)
        if not self.configured:
            entry["detail"] = "not configured"
            return False
        ok = True
        try:
            with httpx.Client(transport=self.transport, timeout=10.0, headers={"Authorization": f"Bearer {self.token}"}) as c:
                for to in self.recipients:
                    r = c.post(f"{GRAPH}/{self.api_version}/{self.phone_number_id}/messages", json=self.payload(to, f"[{event}] {text}"))
                    if r.status_code >= 300:
                        ok = False
                        entry["detail"] = f"HTTP {r.status_code}: {r.text[:200]}"
        except httpx.HTTPError as e:
            ok = False
            entry["detail"] = f"{type(e).__name__}"
        entry["ok"] = ok
        return ok

    def __call__(self, event: str, text: str) -> None:
        self.send(event, text)

"""WhatsApp Cloud API client.

Hard rule 3: this client can only message OWNER_WHATSAPP_NUMBER. There is no `to` parameter,
and every payload is checked again before sending. No template messages, ever.
Callers must check the 24-hour window (window.py) before sending.

DRY_RUN=1 prints every outgoing message instead of sending it.
"""
from __future__ import annotations

import json
import re
import uuid

import httpx

from scout.config import config, dry_run, require_env

GRAPH = "https://graph.facebook.com"


class NotOwnerError(Exception):
    pass


class WhatsAppError(Exception):
    def __init__(self, code, message: str):
        super().__init__(f"WhatsApp error {code}: {message}")
        self.code = code


def digits(number: str | None) -> str:
    return re.sub(r"\D", "", number or "")


def owner_number() -> str:
    return digits(require_env("OWNER_WHATSAPP_NUMBER"))


def _api_url(path: str) -> str:
    return f"{GRAPH}/{config()['whatsapp']['graph_api_version']}/{path}"


def _post(payload: dict, http: httpx.Client | None = None) -> str:
    """Send one message payload. Returns the WhatsApp message id."""
    if digits(payload.get("to")) != owner_number():
        raise NotOwnerError("Refusing to message a number that is not the owner's.")
    if payload.get("type") == "template":
        raise ValueError("Template messages cost money and are never sent.")
    if dry_run():
        print("[DRY_RUN] WhatsApp message:\n" + json.dumps(payload, ensure_ascii=False, indent=2))
        return f"dry-{uuid.uuid4().hex[:12]}"
    url = _api_url(f"{require_env('WHATSAPP_PHONE_NUMBER_ID')}/messages")
    headers = {"Authorization": f"Bearer {require_env('WHATSAPP_TOKEN')}"}
    with (http or httpx.Client()) as c:
        r = c.post(url, headers=headers, json=payload, timeout=20)
    if r.status_code >= 400:
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {"message": r.text[:200]}
        raise WhatsAppError(err.get("code", r.status_code), err.get("message", ""))
    return r.json()["messages"][0]["id"]


def send_text(body: str, http: httpx.Client | None = None) -> str:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": owner_number(),
        "type": "text",
        "text": {"preview_url": False, "body": body[:4096]},
    }
    return _post(payload, http)


def check_phone(http: httpx.Client | None = None) -> dict:
    """Free read-only check that the token works. Sends nothing."""
    url = _api_url(require_env("WHATSAPP_PHONE_NUMBER_ID"))
    with (http or httpx.Client()) as c:
        r = c.get(url, params={"fields": "display_phone_number,verified_name,quality_rating"},
                  headers={"Authorization": f"Bearer {require_env('WHATSAPP_TOKEN')}"}, timeout=20)
    if r.status_code >= 400:
        raise WhatsAppError(r.status_code, r.json().get("error", {}).get("message", ""))
    return r.json()

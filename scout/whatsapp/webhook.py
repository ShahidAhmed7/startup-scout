"""WhatsApp webhook (FastAPI). Must answer within a few seconds:

GET  /webhook  -> Meta's verification handshake (hub.verify_token must match WHATSAPP_VERIFY_TOKEN).
POST /webhook  -> check X-Hub-Signature-256 with WHATSAPP_APP_SECRET, store the event (idempotent by WhatsApp
                  message id), update the 24-hour window, then hand the work to an async worker (Modal spawn).
Messages from any number other than OWNER_WHATSAPP_NUMBER are ignored and never answered (hard rule 3).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

from scout.config import require_env
from scout.whatsapp.client import digits, owner_number


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def owner_messages(payload: dict) -> list[dict]:
    """All inbound messages in a webhook payload that come from the owner."""
    owner = owner_number()
    out = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            for m in value.get("messages", []) or []:
                if digits(m.get("from")) == owner and m.get("id"):
                    out.append(m)
    return out


def failed_statuses(payload: dict) -> list[dict]:
    out = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            for st in (change.get("value", {}) or {}).get("statuses", []) or []:
                if st.get("status") == "failed":
                    out.append(st)
    return out


def store_inbound(conn, msg: dict) -> bool:
    """Store once; move the window. Returns True if this message id is new."""
    from scout.db import repo

    new = repo.log_message(conn, direction="in", wa_message_id=msg["id"], type_=msg.get("type", "unknown"),
                           body=json.dumps(msg))
    if new and not msg["id"].startswith("selftest."):  # self-tests never open the 24-hour window
        ts = datetime.fromtimestamp(int(msg.get("timestamp") or datetime.now().timestamp()), tz=timezone.utc)
        conn.execute("""update owner_state set last_owner_message_at = greatest(coalesce(last_owner_message_at, %s), %s)
                        where id = 1""", (ts, ts))
    return new


def create_app(spawn: Callable[[str], None], connect: Callable) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/webhook")
    def verify(request: Request):
        q = request.query_params
        if q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""),
                                                                     require_env("WHATSAPP_VERIFY_TOKEN")):
            return PlainTextResponse(q.get("hub.challenge", ""))
        return Response(status_code=403)

    @app.post("/webhook")
    async def receive(request: Request):
        body = await request.body()
        if not verify_signature(require_env("WHATSAPP_APP_SECRET"), body, request.headers.get("x-hub-signature-256")):
            return Response(status_code=401)
        try:
            payload = json.loads(body)
        except ValueError:
            return Response(status_code=400)
        msgs = owner_messages(payload)
        fails = failed_statuses(payload)
        if msgs or fails:
            with connect() as conn:
                for m in msgs:
                    if store_inbound(conn, m):
                        spawn(m["id"])
                for st in fails:
                    err = (st.get("errors") or [{}])[0]
                    conn.execute("insert into messages (direction, wa_message_id, type, body) values ('in', %s, %s, %s)"
                                 " on conflict do nothing",
                                 (f"status:{st.get('id')}", "delivery_failed",
                                  f"{err.get('code')}: {err.get('title') or err.get('message')}"))
        return {"ok": True}

    from scout.archive import add_routes

    add_routes(app, connect)
    return app

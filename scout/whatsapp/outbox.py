"""Deliver messages to the owner, respecting the 24-hour window (AGENTS.md section 10).

- Window open  -> send now.
- Window closed -> queue (owner_state.pending_queue_json); digests are marked 'pending' instead.
- The next time the owner writes anything, flush_pending() sends the pending digest(s) first, then the queue.
Never uses templates.
"""
from __future__ import annotations

import json

from scout.db import repo
from scout.whatsapp import client, window
from scout.whatsapp.formats import split_text, text_payload


def owner_state(conn) -> dict:
    return conn.execute("select * from owner_state where id = 1").fetchone()


def window_open(conn) -> bool:
    return window.is_open(owner_state(conn)["last_owner_message_at"])


def _send(conn, payload: dict, kind: str) -> str:
    payload = dict(payload, to=client.owner_number())
    wamid = client.send_payload(payload)
    body = payload.get("text", {}).get("body") or json.dumps(payload.get("interactive", {}), ensure_ascii=False)
    repo.log_message(conn, direction="out", wa_message_id=wamid, type_=kind, body=body[:4000])
    return wamid


def send_now(conn, payloads: list[dict], kind: str) -> None:
    """Send while the window is open (callers replying to the owner can use this directly)."""
    for p in payloads:
        _send(conn, p, kind)


def deliver(conn, payloads: list[dict], kind: str) -> bool:
    """Send if the window is open, else queue. Returns True if sent now."""
    if window_open(conn):
        send_now(conn, payloads, kind)
        return True
    queue = owner_state(conn)["pending_queue_json"] or []
    queue.append({"kind": kind, "payloads": payloads})
    conn.execute("update owner_state set pending_queue_json = %s where id = 1", (json.dumps(queue),))
    return False


def text(body: str) -> list[dict]:
    """A text that may be longer than 4096 characters, split cleanly into several messages."""
    return [text_payload("", part) for part in split_text(body)]


def send_digest(conn, digest_row: dict) -> None:
    from scout import digest as digest_mod
    from scout.config import dry_run

    send_now(conn, [digest_mod.payload("", digest_row["body_json"])], "digest")
    if not dry_run():  # a dry run sent nothing, so the digest is still waiting
        conn.execute("update digests set status = 'sent', sent_at = now() where id = %s", (digest_row["id"],))


def flush_pending(conn) -> int:
    """Called when the owner writes. Sends pending digests first, then queued messages. Returns count."""
    n = 0
    for d in conn.execute("select * from digests where status = 'pending' order by date").fetchall():
        send_digest(conn, d)
        n += 1
    queue = owner_state(conn)["pending_queue_json"] or []
    if queue:
        conn.execute("update owner_state set pending_queue_json = '[]' where id = 1")
        for entry in queue:
            send_now(conn, entry["payloads"], entry["kind"])
            n += 1
    return n

"""Phase 0 smoke tests that use real services.

  python -m scout.smoke gemini                    # 3 tiny calls: Flash-Lite + Flash on main key, Flash-Lite on backup key
  python -m scout.smoke whatsapp --owner-said-hi  # one free-form test message to the owner

The WhatsApp test needs the 24-hour window to be open: the owner must have sent "hi"
to the bot number within the last 23h 50m. Until the webhook exists (phase 2) we can't see
that ourselves, so the owner confirms it and we pass --owner-said-hi.
"""
from __future__ import annotations

import argparse

from scout.db import repo

TEST_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "capital_of_bangladesh": {"type": "string"},
    },
    "required": ["ok", "capital_of_bangladesh"],
    "additionalProperties": False,
}


def smoke_gemini() -> None:
    from scout.llm.gemini import generate

    with repo.connect() as conn:
        def check(data):
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise ValueError("expected ok=true")

        r1 = generate("flash_lite", "Reply with ok=true and the capital of Bangladesh.", kind="test",
                      schema=TEST_SCHEMA, validate=check, conn=conn)
        print(f"Flash-Lite ({r1.model_id}, {r1.key_label} key): {r1.data}  [{r1.tokens_in} in / {r1.tokens_out} out tokens]")

        r2 = generate("flash", "In one short sentence: why do startups in Dhaka often use bKash for payments?",
                      kind="test", conn=conn)  # uses config.yaml llm settings: high thinking, 65,536 max output
        print(f"Flash ({r2.model_id}, {r2.key_label} key): {r2.text.strip()[:200]}")

        r3 = generate("flash_lite", "Reply with ok=true and the capital of Bangladesh.", kind="test",
                      schema=TEST_SCHEMA, validate=check, conn=conn, only_key="backup")
        print(f"Flash-Lite ({r3.model_id}, {r3.key_label} key): {r3.data}")
    print("Gemini smoke test passed.")


def smoke_whatsapp(owner_said_hi: bool) -> None:
    from scout.whatsapp.client import send_text

    if not owner_said_hi:
        raise SystemExit("First send 'hi' to the bot number from your phone, then re-run with --owner-said-hi.")
    body = ("Startup Scout is connected.\n\n"
            "This is a one-time setup test. From Phase 2 you'll get your 08:00 digest here.")
    wamid = send_text(body)
    with repo.connect() as conn:
        repo.log_message(conn, direction="out", wa_message_id=wamid, type_="test", body=body)
    print(f"Sent. WhatsApp message id {wamid[:24]}... Check your phone.")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("what", choices=["gemini", "whatsapp"])
    p.add_argument("--owner-said-hi", action="store_true")
    a = p.parse_args()
    if a.what == "gemini":
        smoke_gemini()
    else:
        smoke_whatsapp(a.owner_said_hi)


if __name__ == "__main__":
    main()

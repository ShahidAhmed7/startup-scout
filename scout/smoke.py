"""Phase 0 smoke tests that use real services.

  python -m scout.smoke gemini                    # tiny calls: card model, Flash-Lite (both keys), Gemma
  python -m scout.smoke whatsapp --owner-said-hi  # one free-form test message to the owner

The WhatsApp test needs the 24-hour window to be open: the owner must have sent "hi"
to the bot number within the last 23h 50m. Until the webhook exists (phase 2) we can't see
that ourselves, so the owner confirms it and we pass --owner-said-hi.
"""

from __future__ import annotations

import argparse

from scout.db import repo


def smoke_gemini() -> None:
    from scout.llm import run as ai
    from scout.llm.schemas import Ask
    import os

    os.environ["SCOUT_DEV"] = "1"
    with repo.connect() as conn:
        results = ai.run_many(
            "ask", ["Reply on_topic=true and answer=connected."] * 2, Ask, conn=conn
        )
        for r in results:
            if isinstance(r, Exception):
                raise r
            print(r.entry, r.model, "ok")


def smoke_whatsapp(owner_said_hi: bool) -> None:
    from scout.whatsapp.client import send_text

    if not owner_said_hi:
        raise SystemExit(
            "First send 'hi' to the bot number from your phone, then re-run with --owner-said-hi."
        )
    body = (
        "Startup Scout is connected.\n\n"
        "This is a one-time setup test. From Phase 2 you'll get your 08:00 digest here."
    )
    wamid = send_text(body)
    with repo.connect() as conn:
        repo.log_message(
            conn, direction="out", wa_message_id=wamid, type_="test", body=body
        )
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

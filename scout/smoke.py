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

TEST_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "capital_of_bangladesh": {"type": "string"},
    },
    "required": ["ok", "capital_of_bangladesh"],
    "additionalProperties": False,
}
JSON_PROMPT = "Reply with ok=true and the capital of Bangladesh."


def _check(data):
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise ValueError("expected ok=true")


def smoke_gemini() -> None:
    from scout.llm.gemini import GeminiError, GeminiUnavailable, generate

    # (job, key) pairs. Uses config.yaml llm settings: high thinking, big output limit.
    tests = [("flash_lite", "main"), ("flash_lite", "backup"), ("hydrogen", "main"), ("hydrogen", "backup"), ("gemma", "main")]
    failed = []
    with repo.connect() as conn:
        for alias, key in tests:
            try:
                r = generate(alias, JSON_PROMPT, kind="test", schema=TEST_SCHEMA, validate=_check,
                             conn=conn, only_key=key)
                print(f"✓ {alias:<10} {r.model_id:<22} {r.key_label:<6} key: {r.data}  "
                      f"[{r.tokens_in} in / {r.tokens_out} out tokens]")
            except (GeminiUnavailable, GeminiError) as e:
                failed.append(alias)
                print(f"✗ {alias:<10} {key:<6} key: {str(e)[:160]}")
    if failed:
        raise SystemExit(f"Not passed yet: {', '.join(failed)}. If the error says 'high demand', "
                         "Google is busy; try again later.")
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

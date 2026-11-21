"""WhatsApp message builders. Every limit is enforced here, cutting cleanly at a word boundary.

Limits (Meta docs, checked 2026-11-19): reply buttons max 3, button title 20 chars; list max 10 rows,
row title 24, row description 72, section title 24, list button 20; interactive body 1024; text 4096.
"""

from __future__ import annotations

LIMITS = {
    "button_title": 20,
    "list_button": 20,
    "row_title": 24,
    "row_desc": 72,
    "section_title": 24,
    "body": 1024,
    "text": 4096,
    "footer": 60,
    "header": 60,
    "max_buttons": 3,
    "max_rows": 10,
    "row_id": 200,
}


def clip(text: str, n: int) -> str:
    """Cut to at most n characters at a word boundary, adding '…' when cut."""
    text = (text or "").strip()
    if len(text) <= n:
        return text
    cut = text[: n - 1]
    space = cut.rfind(" ")
    if space >= n // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:·-–") + "…"


def split_text(text: str, limit: int = LIMITS["text"]) -> list[str]:
    """Split a long text into WhatsApp-sized messages at paragraph, then line, then word boundaries."""
    text = text.strip()
    parts: list[str] = []
    while len(text) > limit:
        window = text[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"))
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def text_payload(to: str, body: str) -> dict:
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": clip(body, LIMITS["text"])},
    }


def buttons_payload(
    to: str,
    body: str,
    buttons: list[tuple[str, str]],
    footer: str | None = None,
    header: str | None = None,
) -> dict:
    if not 1 <= len(buttons) <= LIMITS["max_buttons"]:
        raise ValueError("WhatsApp allows 1-3 reply buttons")
    inter = {
        "type": "button",
        "body": {"text": clip(body, LIMITS["body"])},
        "action": {
            "buttons": [
                {
                    "type": "reply",
                    "reply": {
                        "id": bid[: LIMITS["row_id"]],
                        "title": clip(title, LIMITS["button_title"]),
                    },
                }
                for bid, title in buttons
            ]
        },
    }
    if header:
        inter["header"] = {"type": "text", "text": clip(header, LIMITS["header"])}
    if footer:
        inter["footer"] = {"text": clip(footer, LIMITS["footer"])}
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": inter,
    }


def list_payload(
    to: str,
    body: str,
    button: str,
    sections: list[dict],
    footer: str | None = None,
    header: str | None = None,
) -> dict:
    """sections: [{"title": str, "rows": [{"id", "title", "description"}]}]; max 10 rows in total."""
    out_sections, total = [], 0
    for s in sections:
        rows = []
        for r in s["rows"]:
            if total >= LIMITS["max_rows"]:
                break
            row = {
                "id": r["id"][: LIMITS["row_id"]],
                "title": clip(r["title"], LIMITS["row_title"]),
            }
            if r.get("description"):
                row["description"] = clip(r["description"], LIMITS["row_desc"])
            rows.append(row)
            total += 1
        if rows:
            out_sections.append(
                {"title": clip(s["title"], LIMITS["section_title"]), "rows": rows}
            )
    if not out_sections:
        raise ValueError("a list message needs at least one row")
    inter = {
        "type": "list",
        "body": {"text": clip(body, LIMITS["body"])},
        "action": {
            "button": clip(button, LIMITS["list_button"]),
            "sections": out_sections,
        },
    }
    if header:
        inter["header"] = {"type": "text", "text": clip(header, LIMITS["header"])}
    if footer:
        inter["footer"] = {"text": clip(footer, LIMITS["footer"])}
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": inter,
    }


def row_title(name: str, label: str) -> str:
    """'{Name} · BD 8' within 24 chars: shorten the name, never the score."""
    suffix = f" · {label}"
    room = LIMITS["row_title"] - len(suffix)
    if len(name) > room:
        # "Archer (formerly hackajob)" -> "Archer": a trailing note is the first thing to go.
        import re

        name = re.sub(r"\s*\([^)]*\)\s*$", "", name) or name
    short = name if len(name) <= room else name[: max(1, room - 1)].rstrip() + "…"
    return short + suffix


def numbered_lines(text: str) -> str:
    """'1. a 2. b 3. c' -> one item per line (models sometimes squash numbered lists onto one line)."""
    import re

    if not text or "\n" in text.strip():
        return text
    parts = re.split(r"\s(?=\d{1,2}\.\s)", text.strip())
    return "\n".join(p.strip() for p in parts) if len(parts) > 2 else text

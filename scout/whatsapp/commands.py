"""Owner commands and button taps (AGENTS.md section 10). Everything else gets a one-line reply.

Not a general chatbot (hard rule 8): free questions get "I only answer questions about your cards and saved ideas."
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from scout.db import repo
from scout.settings import effective, set_value
from scout.whatsapp import outbox
from scout.whatsapp.formats import buttons_payload, clip, text_payload

DHAKA = ZoneInfo("Asia/Dhaka")

HELP = """*Commands*
hi or digest: today's picks
saved: your notebook
settings: your settings
time 7:30: change the digest time
mix 2+3: Bangladesh + global picks
skip add crypto / skip remove space
skills add mobile apps / skills remove mobile apps
ask <question>: about your last card or a saved idea
note <idea>: <text>: a private note (stays on our own model)
idea <name>: an idea's assumptions and status
drop <idea>: <reason>: drop an idea
kit <idea>: validation kit
watch <sector> <stage> <country> or watch <company>
watches / unwatch <number>
archive: a sign-in link to your private web archive"""

NOT_CHAT = "I only answer questions about your cards and saved ideas."

UNKNOWN = "I don't know that one. Send help for the list of commands."


# ---------------------------------------------------------------- helpers

def reply(conn, body: str, kind: str = "reply") -> None:
    outbox.send_now(conn, outbox.text(body), kind)


def buttons(conn, body: str, items: list[tuple[str, str]], kind: str = "buttons") -> None:
    outbox.send_now(conn, [buttons_payload("", body, items)], kind)


def parse(msg: dict) -> tuple[str, str]:
    t = msg.get("type")
    if t == "text":
        return "text", (msg.get("text") or {}).get("body", "").strip()
    if t == "interactive":
        it = msg.get("interactive") or {}
        if it.get("type") == "button_reply":
            return "button", it["button_reply"]["id"]
        if it.get("type") == "list_reply":
            return "button", it["list_reply"]["id"]
    if t == "button":
        return "text", (msg.get("button") or {}).get("text", "")
    return "other", t or "unknown"


def _card(conn, card_id: int) -> dict | None:
    return conn.execute("select * from cards where id = %s", (card_id,)).fetchone()


def find_idea(conn, name: str) -> dict | None:
    """A saved idea by (part of) its startup name, or by its number in the `saved` list."""
    name = name.strip().lower()
    rows = conn.execute(
        """select i.*, s.name from ideas i join startups s on s.id = i.startup_id order by i.created_at""").fetchall()
    if name.isdigit() and 1 <= int(name) <= len(rows):
        return rows[int(name) - 1]
    exact = [r for r in rows if r["name"].lower() == name]
    if exact:
        return exact[0]
    part = [r for r in rows if name in r["name"].lower()]
    return part[0] if len(part) == 1 else None


# ---------------------------------------------------------------- digest + cards

def send_digest(conn, already_sent_ids: set[int]) -> None:
    today = datetime.now(DHAKA).date()
    d = conn.execute("select * from digests where date = %s", (today,)).fetchone() or \
        conn.execute("select * from digests order by date desc limit 1").fetchone()
    if d is None:
        hhmm = effective(conn)["digest_time"]
        reply(conn, f"No digest yet. Your first one comes at {hhmm} (Dhaka time).")
        return
    if d["id"] in already_sent_ids:
        return
    outbox.send_digest(conn, d)


def open_card(conn, card_id: int) -> None:
    c = _card(conn, card_id)
    if c is None:
        reply(conn, "I can't find that card any more.")
        return
    conn.execute("update owner_state set last_opened_card_id = %s where id = 1", (card_id,))
    reply(conn, c["body"], "card")
    buttons(conn, "What next?", [(f"save:{card_id}", "Save"), (f"skip:{card_id}", "Skip"), (f"more:{card_id}", "More")])


def save_card(conn, card_id: int) -> None:
    c = _card(conn, card_id)
    if c is None:
        reply(conn, "I can't find that card any more.")
        return
    conn.execute("insert into signals (card_id, kind) values (%s, 'save')", (card_id,))
    idea = conn.execute("select id from ideas where startup_id = %s", (c["startup_id"],)).fetchone()
    if idea:
        reply(conn, f"{c['title']} is already in your notebook. Send saved to see it.")
        return
    conn.execute("insert into ideas (startup_id, status) values (%s, 'exploring')", (c["startup_id"],))
    reply(conn, f"Saved {c['title']} to your notebook (Exploring). Send saved to see it.")


def skip_card(conn, card_id: int) -> None:
    conn.execute("insert into signals (card_id, kind) values (%s, 'skip')", (card_id,))
    reply(conn, "Skipped.")


def more_card(conn, card_id: int) -> None:
    from scout.deepdive import deep_dive

    c = _card(conn, card_id)
    if c is None:
        reply(conn, "I can't find that card any more.")
        return
    conn.execute("insert into signals (card_id, kind) values (%s, 'more')", (card_id,))
    conn.execute("update owner_state set last_opened_card_id = %s where id = 1", (card_id,))
    reply(conn, "Digging deeper. This takes a few minutes.")
    try:
        text, _ = deep_dive(conn, card_id)
    except Exception:
        reply(conn, "Sorry, the deep dive failed (the AI models are busy). Tap More again in a while.")
        return
    reply(conn, text, "deep_dive")
    buttons(conn, "What next?", [(f"kit:{card_id}", "Validation kit"), (f"ask:{card_id}", "Ask"),
                                 (f"save:{card_id}", "Save")])


# ---------------------------------------------------------------- notebook + settings

def saved_list(conn) -> None:
    rows = conn.execute(
        """select i.id, i.status, i.created_at, s.name,
             (select count(*) from notes n where n.idea_id = i.id) as notes
           from ideas i join startups s on s.id = i.startup_id order by i.created_at""").fetchall()
    if not rows:
        reply(conn, "Your notebook is empty. Tap Save on a card to add an idea.")
        return
    lines = ["*Your notebook*"]
    for n, r in enumerate(rows, 1):
        extra = f", {r['notes']} notes" if r["notes"] else ""
        lines.append(f"{n}. {r['name']}: {r['status'].capitalize()}{extra} (saved {r["created_at"].astimezone(DHAKA):%d %b})")
    lines.append("\nSend kit <name> for a validation kit, or note <name>: <text> to add a private note.")
    reply(conn, "\n".join(lines))


def settings_text(conn) -> None:
    cfg = effective(conn)
    mix = cfg["digest_mix"]
    m = cfg["models"]
    reply(conn, "\n".join([
        "*Settings*",
        f"Digest time: {cfg['digest_time']} (Dhaka)",
        f"Mix: {mix['bangladesh']} Bangladesh + {mix['global']} global",
        f"Minimum score: {cfg['min_score']}",
        f"Skip list: {', '.join(cfg['skip_list']) or 'none'}",
        f"Your skills: {', '.join(cfg['owner']['skills'])}",
        f"Models: {m['hydrogen']['id']} first, then {m['flash_lite']['id']}, {m['gemma']['id']}, Qwen as last resort",
        "",
        "Change with: time 7:30 · mix 2+3 · skip add crypto · skills add mobile apps",
    ]))


def set_time(conn, h: int, mnt: int, ampm: str | None) -> None:
    if ampm:
        if not 1 <= h <= 12:
            reply(conn, "Use a time like 7:30 or 19:30.")
            return
        h = h % 12 + (12 if ampm.lower() == "pm" else 0)
    if not (0 <= h <= 23 and 0 <= mnt <= 59):
        reply(conn, "Use a time like 7:30 or 19:30.")
        return
    value = f"{h:02d}:{mnt:02d}"
    set_value(conn, "digest_time", value)
    reply(conn, f"Done. Your digest now comes at {value} (Dhaka time).")


def set_mix(conn, bd: int, gl: int) -> None:
    if bd + gl < 1 or bd + gl > 10:
        reply(conn, "The mix must add up to between 1 and 10, like mix 3+2.")
        return
    set_value(conn, "digest_mix", {"bangladesh": bd, "global": gl})
    reply(conn, f"Done. You'll get {bd} Bangladesh + {gl} global picks.")


def edit_list(conn, key: str, action: str, value: str) -> None:
    cfg = effective(conn)
    current = list(cfg["skip_list"] if key == "skip_list" else cfg["owner"]["skills"])
    value = value.strip().rstrip(".")
    label = "Skip list" if key == "skip_list" else "Skills"
    if action == "add":
        if value.lower() in (v.lower() for v in current):
            reply(conn, f"'{value}' is already there.")
            return
        current.append(value)
    else:
        match = [v for v in current if v.lower() == value.lower()] or [v for v in current if value.lower() in v.lower()]
        if not match:
            reply(conn, f"'{value}' isn't in the list. {label}: {', '.join(current)}")
            return
        current.remove(match[0])
    set_value(conn, key, current)
    reply(conn, f"Done. {label}: {', '.join(current) or 'none'}")


# ---------------------------------------------------------------- dispatcher

def explore_startup(conn, startup_id: int) -> None:
    """A "More to explore" row: open its card, writing it first if needed (Hydrogen, 'explore' job)."""
    from scout import cards as cards_mod
    from scout.run import today_dhaka

    c = conn.execute("select id from cards where startup_id = %s and kind = 'card' order by id desc limit 1",
                     (startup_id,)).fetchone()
    if c is None:
        sc = conn.execute("select scores_json from scores where startup_id = %s order by created_at desc limit 1",
                          (startup_id,)).fetchone()
        if sc is None:
            reply(conn, "I can't find that startup any more.")
            return
        j = sc["scores_json"]
        market = "bangladesh" if j.get("bangladesh", 0) >= j.get("global", 0) else "global"
        reply(conn, "Writing this card. This takes a few minutes.")
        try:
            res = cards_mod.write_cards(conn, [({"startup_id": startup_id}, market)], today_dhaka(), set(),
                                        job="explore")
        except Exception:
            res = {"card_ids": []}
        if not res["card_ids"]:
            reply(conn, "Sorry, I couldn't write that card right now (the AI models are busy). Try again later.")
            return
        open_card(conn, res["card_ids"][0])
        return
    open_card(conn, c["id"])


BUTTONS = {"card": open_card, "save": save_card, "skip": skip_card, "more": more_card, "startup": explore_startup}


def handle(conn, msg: dict) -> None:
    sent_digests = {d["id"] for d in conn.execute("select id from digests where status = 'pending'").fetchall()}
    outbox.flush_pending(conn)
    kind, value = parse(msg)

    if kind == "button":
        prefix, _, arg = value.partition(":")
        if prefix in BUTTONS and arg.isdigit():
            BUTTONS[prefix](conn, int(arg))
            return
        from scout import notebook, watches

        if notebook.handle_button(conn, prefix, arg) or watches.handle_button(conn, prefix, arg):
            return
        reply(conn, UNKNOWN)
        return
    if kind != "text":
        reply(conn, "I can only read text and buttons. " + UNKNOWN.split(". ", 1)[1])
        return

    text = value
    low = text.lower().strip()
    if re.fullmatch(r"(hi|hello|hey|salam|assalamualaikum|digest|picks)[!. ]*", low):
        send_digest(conn, sent_digests)
        return
    if low == "help":
        reply(conn, HELP)
        return
    if low in ("saved", "notebook"):
        from scout import notebook

        if not notebook.saved_menu(conn):
            saved_list(conn)  # empty notebook message
        return
    if low == "settings":
        settings_text(conn)
        return
    if low in ("archive", "web", "website"):
        from scout.archive import login_link

        from scout.config import env

        base = env("PUBLIC_BASE_URL")
        if not base:
            reply(conn, "The archive address isn't set up yet.")
            return
        reply(conn, "Your private archive (link works for 15 minutes, then you stay signed in for 30 days on this "
                    "phone):\n" + login_link(base))
        return
    m = re.fullmatch(r"time\s+(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?", low)
    if m:
        set_time(conn, int(m.group(1)), int(m.group(2) or 0), m.group(3))
        return
    m = re.fullmatch(r"mix\s+(\d{1,2})\s*\+\s*(\d{1,2})", low)
    if m:
        set_mix(conn, int(m.group(1)), int(m.group(2)))
        return
    m = re.fullmatch(r"skip\s+(add|remove)\s+(.+)", text.strip(), re.I)
    if m:
        edit_list(conn, "skip_list", m.group(1).lower(), m.group(2))
        return
    m = re.fullmatch(r"skills?\s+(add|remove)\s+(.+)", text.strip(), re.I)
    if m:
        edit_list(conn, "owner_skills", m.group(1).lower(), m.group(2))
        return

    from scout import notebook, watches

    if notebook.handle_text(conn, text) or watches.handle_text(conn, text):
        return
    reply(conn, NOT_CHAT if "?" in text else UNKNOWN)


def handle_stored(conn, wamid: str) -> None:
    """Entry point for the async worker: process one stored inbound message, once."""
    import json

    row = conn.execute("select * from messages where wa_message_id = %s and direction = 'in'", (wamid,)).fetchone()
    if row is None:
        return
    claimed = conn.execute(
        "update messages set type = type || ':done' where id = %s and type not like '%%:done' returning id",
        (row["id"],)).fetchone()
    if not claimed:
        return  # already handled (idempotency by WhatsApp message id)
    if wamid.startswith("selftest."):
        # Self-test from Claude: run the real code path but only print replies (nothing reaches the phone).
        import os
        old = os.environ.get("DRY_RUN")
        os.environ["DRY_RUN"] = "1"
        try:
            handle(conn, json.loads(row["body"]))
        finally:
            if old is None:
                os.environ.pop("DRY_RUN", None)
            else:
                os.environ["DRY_RUN"] = old
        return
    handle(conn, json.loads(row["body"]))

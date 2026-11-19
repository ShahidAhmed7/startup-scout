"""Notebook (phase 4): idea statuses, private notes + assumption tracking (Qwen only), validation kit, ask.

Privacy (hard rule 5): notes, assumptions and anything built from them go ONLY to Qwen on Modal
(router private=True). Public card text may go to Gemini.
"""
from __future__ import annotations

import json
import re

from scout.llm import prompts, router
from scout.whatsapp import outbox
from scout.whatsapp.formats import buttons_payload, clip, list_payload

STATUSES = {"exploring": "Exploring", "testing": "Testing", "promising": "Promising", "dropped": "Dropped"}
QWEN_WAIT = "Thinking on our private model. This can take 5-15 minutes."


def sentence_case(text: str) -> str:
    """Qwen sometimes writes all lower-case: capitalise the start of each sentence (and bKash/Bangla names)."""
    import re as _re

    t = _re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), (text or "").strip())
    for w, fixed in (("bkash", "bKash"), ("nagad", "Nagad"), ("bangla", "Bangla"), ("dhaka", "Dhaka"),
                     ("bangladesh", "Bangladesh"), ("f-commerce", "F-commerce")):
        t = _re.sub(rf"\b{w}\b", fixed, t, flags=_re.I) if w != "f-commerce" else t
    return t


def _reply(conn, body: str, kind: str = "reply") -> None:
    outbox.send_now(conn, outbox.text(body), kind)


def _buttons(conn, body: str, items: list[tuple[str, str]]) -> None:
    outbox.send_now(conn, [buttons_payload("", body, items)], "buttons")


def _ideas(conn) -> list[dict]:
    return conn.execute(
        """select i.*, s.name from ideas i join startups s on s.id = i.startup_id order by i.created_at""").fetchall()


def find_idea(conn, name: str) -> dict | None:
    name = name.strip().lower()
    rows = _ideas(conn)
    if name.isdigit() and 1 <= int(name) <= len(rows):
        return rows[int(name) - 1]
    exact = [r for r in rows if r["name"].lower() == name]
    if exact:
        return exact[0]
    part = [r for r in rows if name in r["name"].lower() or r["name"].lower() in name]
    return part[0] if len(part) == 1 else None


def _latest_card(conn, startup_id: int) -> dict | None:
    return conn.execute("select * from cards where startup_id = %s and kind = 'card' order by id desc limit 1",
                        (startup_id,)).fetchone()


def _notes(conn, idea_id: int) -> list[str]:
    return [r["text"] for r in conn.execute("select text from notes where idea_id = %s order by created_at",
                                            (idea_id,)).fetchall()]


def _not_found(conn, name: str) -> None:
    names = ", ".join(r["name"] for r in _ideas(conn)) or "none yet"
    _reply(conn, f"I can't find a saved idea called '{name}'. Saved ideas: {names}.")


# ---------------------------------------------------------------- idea view + statuses

def show_idea(conn, idea: dict) -> None:
    assumptions = conn.execute("select * from assumptions where idea_id = %s order by id", (idea["id"],)).fetchall()
    notes = _notes(conn, idea["id"])
    lines = [f"*{idea['name']}*: {STATUSES.get(idea['status'], idea['status'])}"]
    if idea["drop_reason"]:
        lines.append(f"Drop reason: {idea['drop_reason']}")
    lines.append(f"Notes: {len(notes)}")
    if assumptions:
        mark = {"holding": "Holding", "at_risk": "At risk", "untested": "Untested"}
        lines += ["", "*Assumptions*"] + [
            f"- {a['text']}: {mark.get(a['status'], a['status'])} (+{a['evidence_for']}/-{a['evidence_against']})"
            for a in assumptions]
    lines.append(f"\nAdd evidence with: note {idea['name']}: <what you learned>")
    _reply(conn, "\n".join(lines))
    _buttons(conn, "Update status?", [(f"status:{idea['id']}:testing", "Keep testing"),
                                      (f"status:{idea['id']}:promising", "Promising"),
                                      (f"status:{idea['id']}:dropped", "Drop")])


def set_status(conn, idea_id: int, status: str, reason: str | None = None) -> None:
    conn.execute("update ideas set status = %s, drop_reason = coalesce(%s, drop_reason), updated_at = now() where id = %s",
                 (status, reason, idea_id))
    name = conn.execute("select s.name from ideas i join startups s on s.id = i.startup_id where i.id = %s",
                        (idea_id,)).fetchone()["name"]
    if status == "dropped" and not reason:
        _reply(conn, f"{name} marked as Dropped. To record why, send: drop {name}: <reason>")
    else:
        _reply(conn, f"{name} marked as {STATUSES[status]}.")


def saved_menu(conn) -> bool:
    rows = _ideas(conn)
    if not rows:
        return False
    items = [{"id": f"idea:{r['id']}", "title": clip(r["name"], 24),
              "description": f"{STATUSES.get(r['status'], r['status'])} · saved {r['created_at']:%d %b}"} for r in rows[:10]]
    outbox.send_now(conn, [list_payload("", f"Your notebook has {len(rows)} idea{'s' if len(rows) != 1 else ''}. Open one to see its assumptions "
                                            "and change its status.", "Open idea",
                                        [{"title": "Notebook", "rows": items}])], "saved")
    return True


# ---------------------------------------------------------------- notes + assumptions (Qwen only)

def add_note(conn, idea: dict, text: str) -> None:
    conn.execute("insert into notes (idea_id, text) values (%s, %s)", (idea["id"], text.strip()))
    _reply(conn, f"Private note saved to {idea['name']}. Updating its assumptions. {QWEN_WAIT}")
    card = _latest_card(conn, idea["startup_id"])
    current = conn.execute("select text, status, evidence_for, evidence_against from assumptions where idea_id = %s "
                           "order by id", (idea["id"],)).fetchall()
    try:
        out = router.complete("note", prompts.assumptions_prompt(card["body"] if card else idea["name"],
                                                                 _notes(conn, idea["id"]), current),
                              schema=prompts.ASSUMPTIONS_SCHEMA, system=prompts.SYSTEM, private=True, conn=conn)
    except Exception:
        _reply(conn, "The note is saved, but the private model is unavailable right now, so assumptions were not "
                     "updated. They will update with your next note.")
        return
    with conn.transaction():
        conn.execute("delete from assumptions where idea_id = %s", (idea["id"],))
        for a in out.data.get("assumptions", [])[:10]:
            conn.execute("""insert into assumptions (idea_id, text, status, evidence_for, evidence_against)
                            values (%s, %s, %s, %s, %s)""",
                         (idea["id"], clip(sentence_case(a["text"]).rstrip("."), 200), a["status"] if a["status"] in
                          ("holding", "at_risk", "untested") else "untested",
                          max(0, int(a["evidence_for"])), max(0, int(a["evidence_against"]))))
    if out.data.get("summary"):
        _reply(conn, sentence_case(out.data["summary"]))
    show_idea(conn, find_idea(conn, idea["name"]) or idea)


# ---------------------------------------------------------------- validation kit

def _kit_text(name: str, k: dict) -> str:
    q = "\n".join(f"{n}. {i['bangla']}\n   ({i['english']})" for n, i in enumerate(k.get("interview", []), 1))
    from scout.whatsapp.formats import numbered_lines

    return (f"*Validation kit: {name}*\n\n*Interview script*\n{q}\n\n*Landing page*\n{k['headline']}\n{k['subhead']}\n"
            f"Button: {k['call_to_action']}\n\n*Prices to test*\n{k['prices']}\n\n*7-day plan*\n"
            f"{numbered_lines(k['plan_7_days'])}")


def make_kit(conn, card_id: int, shorter: bool = False, all_bangla: bool = False) -> None:
    card = conn.execute("select * from cards where id = %s", (card_id,)).fetchone()
    if card is None:
        _reply(conn, "I can't find that card any more.")
        return
    idea = conn.execute("select * from ideas where startup_id = %s", (card["startup_id"],)).fetchone()
    notes = _notes(conn, idea["id"]) if idea else []
    private = bool(notes)  # built from private notes -> Qwen only
    _reply(conn, QWEN_WAIT if private else "Building your validation kit. This takes a few minutes.")
    market = "Bangladesh" if card["market"] == "bangladesh" else "global (sold abroad from Bangladesh)"
    try:
        out = router.complete("kit", prompts.kit_prompt(card["body"][:3500], "\n".join(notes), market, shorter,
                                                        all_bangla),
                              schema=prompts.KIT_SCHEMA, system=prompts.SYSTEM, private=private, conn=conn)
    except Exception:
        _reply(conn, "Sorry, the kit failed (the AI models are busy). Try again in a while.")
        return
    text = clip(_kit_text(card["title"], out.data), 4096)
    conn.execute("""insert into cards (startup_id, kind, body, source_urls, model_id, run_date, market, title, data_json)
                    values (%s, 'kit', %s, '{}', %s, current_date, %s, %s, %s)""",
                 (card["startup_id"], text, out.model_id, card["market"], card["title"],
                  json.dumps(out.data | {"card_id": card_id})))
    _reply(conn, text, "kit")
    _buttons(conn, "Ready to test it?", [(f"kitstart:{card_id}", "Start test"), (f"kitshort:{card_id}", "Shorter"),
                                         (f"kitbangla:{card_id}", "All in Bangla")])


def start_test(conn, card_id: int) -> None:
    card = conn.execute("select * from cards where id = %s", (card_id,)).fetchone()
    idea = conn.execute("select * from ideas where startup_id = %s", (card["startup_id"],)).fetchone()
    if idea is None:
        conn.execute("insert into ideas (startup_id, status) values (%s, 'testing')", (card["startup_id"],))
    else:
        conn.execute("update ideas set status = 'testing', updated_at = now() where id = %s", (idea["id"],))
    _reply(conn, f"{card['title']} is now Testing. As you learn things, send: note {card['title']}: <what you learned>")


# ---------------------------------------------------------------- ask

def ask(conn, question: str) -> None:
    from scout.whatsapp.commands import NOT_CHAT

    idea = None
    for r in _ideas(conn):
        if r["name"].lower() in question.lower():
            idea = r
            break
    card = None
    if idea:
        card = _latest_card(conn, idea["startup_id"])
    else:
        cid = conn.execute("select last_opened_card_id from owner_state where id = 1").fetchone()["last_opened_card_id"]
        card = conn.execute("select * from cards where id = %s", (cid,)).fetchone() if cid else None
        if card:
            idea = conn.execute("select i.*, s.name from ideas i join startups s on s.id = i.startup_id "
                                "where i.startup_id = %s", (card["startup_id"],)).fetchone()
    if card is None:
        _reply(conn, "Open a card first (or name a saved idea in your question), then ask.")
        return
    deep = conn.execute("select body from cards where startup_id = %s and kind = 'deep_dive' order by id desc limit 1",
                        (card["startup_id"],)).fetchone()
    notes = _notes(conn, idea["id"]) if idea else []
    context = card["body"][:3500] + ("\n\nDEEP DIVE:\n" + deep["body"][:2500] if deep else "")
    if notes:
        context += "\n\nOWNER'S PRIVATE NOTES:\n" + "\n".join(f"- {n}" for n in notes)
    private = bool(notes)
    if private:
        _reply(conn, QWEN_WAIT)
    try:
        out = router.complete("ask", prompts.ask_prompt(question, context), schema=prompts.ASK_SCHEMA,
                              system=prompts.SYSTEM, private=private, conn=conn)
    except Exception:
        _reply(conn, "Sorry, I couldn't answer right now (the AI models are busy). Try again in a while.")
        return
    if not out.data.get("on_topic") or not out.data.get("answer"):
        _reply(conn, NOT_CHAT)
        return
    _reply(conn, f"*{card['title']}*\n{out.data['answer']}", "ask")


# ---------------------------------------------------------------- routing (called by commands.py)

def handle_text(conn, text: str) -> bool:
    t = text.strip()
    m = re.fullmatch(r"ask\s+(.+)", t, re.I | re.S)
    if m:
        ask(conn, m.group(1))
        return True
    m = re.fullmatch(r"note\s+(.+?)\s*:\s*(.+)", t, re.I | re.S)
    if m:
        idea = find_idea(conn, m.group(1))
        if idea is None:
            _not_found(conn, m.group(1))
        else:
            add_note(conn, idea, m.group(2))
        return True
    if re.fullmatch(r"note\b.*", t, re.I):
        _reply(conn, "Use: note <idea>: <text>. For example: note ClinicLoop: 3 of 5 clinics said they'd pay ৳1,000.")
        return True
    m = re.fullmatch(r"kit\s+(.+)", t, re.I)
    if m:
        idea = find_idea(conn, m.group(1))
        card = _latest_card(conn, idea["startup_id"]) if idea else None
        if card is None:
            _not_found(conn, m.group(1))
        else:
            make_kit(conn, card["id"])
        return True
    m = re.fullmatch(r"drop\s+(.+?)\s*:\s*(.+)", t, re.I | re.S)
    if m:
        idea = find_idea(conn, m.group(1))
        if idea is None:
            _not_found(conn, m.group(1))
        else:
            set_status(conn, idea["id"], "dropped", m.group(2).strip())
        return True
    m = re.fullmatch(r"idea\s+(.+)", t, re.I)
    if m:
        idea = find_idea(conn, m.group(1))
        if idea is None:
            _not_found(conn, m.group(1))
        else:
            show_idea(conn, idea)
        return True
    return False


def handle_button(conn, prefix: str, arg: str) -> bool:
    if prefix == "idea" and arg.isdigit():
        idea = conn.execute("select i.*, s.name from ideas i join startups s on s.id = i.startup_id where i.id = %s",
                            (int(arg),)).fetchone()
        if idea:
            show_idea(conn, idea)
        return True
    if prefix == "status":
        idea_id, _, status = arg.partition(":")
        if idea_id.isdigit() and status in STATUSES:
            set_status(conn, int(idea_id), status)
            return True
    if prefix in ("kit", "kitshort", "kitbangla") and arg.isdigit():
        make_kit(conn, int(arg), shorter=prefix == "kitshort", all_bangla=prefix == "kitbangla")
        return True
    if prefix == "kitstart" and arg.isdigit():
        start_test(conn, int(arg))
        return True
    if prefix == "ask" and arg.isdigit():
        conn.execute("update owner_state set last_opened_card_id = %s where id = 1", (int(arg),))
        _reply(conn, "Send your question starting with ask")
        return True
    return False

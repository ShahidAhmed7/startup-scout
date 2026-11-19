"""Watches (phase 5): alerts only on change.

  watch fintech seed Nigeria     -> new rounds matching sector + stage + country (use "any" for any part)
  watch Render  / watch render.com -> a company raises again
  watches / unwatch 2

Also: a saved idea's score changes, and (weekly) a new name appears in the Bangladesh search for a saved idea.
"""
from __future__ import annotations

import json
import re

from scout.normalize import domain_of, norm_name, region_of, short_country
from scout.whatsapp import outbox
from scout.whatsapp.formats import buttons_payload, clip

STAGE_WORDS = ["pre-seed", "preseed", "seed", "series a", "series b", "series c", "series d", "bridge", "debt",
               "grant", "any"]


def parse(text: str) -> dict:
    t = text.strip()
    low = t.lower()
    for st in sorted(STAGE_WORDS, key=len, reverse=True):
        m = re.search(rf"(^|\s){re.escape(st)}(\s|$)", low)
        if m:
            sector = t[:m.start()].strip() or "any"
            country = t[m.end():].strip() or "any"
            return {"kind": "segment", "sector": sector, "stage": st.replace("preseed", "pre-seed"), "country": country}
    if "." in t and " " not in t:
        return {"kind": "company", "domain": domain_of(t) or t.lower(), "name": ""}
    return {"kind": "company", "name": t, "domain": ""}


def describe(q: dict) -> str:
    if q["kind"] == "segment":
        return f"{q['sector']} · {q['stage']} · {q['country']}"
    return q.get("domain") or q.get("name")


def _reply(conn, body: str) -> None:
    outbox.send_now(conn, outbox.text(body), "reply")


def _list(conn) -> list[dict]:
    return conn.execute("select * from watches where active order by id").fetchall()


def handle_text(conn, text: str) -> bool:
    t = text.strip()
    low = t.lower()
    if low == "watches":
        rows = _list(conn)
        if not rows:
            _reply(conn, "No watches yet. Example: watch fintech seed Nigeria, or watch render.com")
        else:
            _reply(conn, "*Your watches*\n" + "\n".join(f"{n}. {describe(json.loads(r['query']))}"
                                                        for n, r in enumerate(rows, 1)) + "\n\nRemove one with: unwatch <number>")
        return True
    m = re.fullmatch(r"unwatch\s+(\d+)", low)
    if m:
        rows = _list(conn)
        n = int(m.group(1))
        if not 1 <= n <= len(rows):
            _reply(conn, "No watch with that number. Send watches to see the list.")
        else:
            conn.execute("update watches set active = false where id = %s", (rows[n - 1]["id"],))
            _reply(conn, f"Stopped watching {describe(json.loads(rows[n - 1]['query']))}.")
        return True
    m = re.fullmatch(r"watch\s+(.+)", t, re.I)
    if m:
        q = parse(m.group(1))
        conn.execute("insert into watches (query, kind) values (%s, %s)", (json.dumps(q), q["kind"]))
        _reply(conn, f"Watching {describe(q)}. I'll only message you when something new matches.")
        return True
    return False


def handle_button(conn, prefix: str, arg: str) -> bool:
    if prefix == "unwatch" and arg.isdigit():
        r = conn.execute("update watches set active = false where id = %s returning query", (int(arg),)).fetchone()
        _reply(conn, f"Stopped watching {describe(json.loads(r['query']))}." if r else "That watch is already gone.")
        return True
    return False


def _matches(q: dict, row: dict) -> bool:
    if q["kind"] == "company":
        if q.get("domain"):
            return (row["domain"] or "") == q["domain"]
        return norm_name(row["name"]) == norm_name(q["name"])
    text = f"{row['category'] or ''} {row['description'] or ''}".lower()
    if q["sector"].lower() != "any" and q["sector"].lower() not in text:
        return False
    if q["stage"] != "any" and q["stage"] not in (row["stage"] or "").lower():
        return False
    c = q["country"].lower()
    if c != "any":
        country = (short_country(row["country"]) or "").lower()
        if c not in (country, region_of(row["country"]).lower()) and short_country(q["country"]).lower() != country:
            return False
    return True


def check(conn, since) -> int:
    """Alerts for rounds recorded since `since`, plus saved-idea score changes. Returns alerts sent/queued."""
    sent = 0
    watches = [(w, json.loads(w["query"])) for w in _list(conn)]
    if watches:
        rounds = conn.execute(
            """select r.id, r.stage, r.amount_text, s.id as startup_id, s.name, s.domain, s.country, s.category,
                 s.description, (select id from cards c where c.startup_id = s.id and c.kind = 'card'
                                 order by id desc limit 1) as card_id
               from rounds r join startups s on s.id = r.startup_id where r.created_at >= %s""", (since,)).fetchall()
        for w, q in watches:
            hits = [r for r in rounds if _matches(q, r)]
            if not hits:
                continue
            lines = [f"*Watch: {describe(q)}*"] + [
                f"- {h['name']} ({h['country'] or 'country unknown'}): {h['stage'] or 'round'} {h['amount_text'] or ''}".rstrip()
                for h in hits[:5]]
            if len(hits) > 5:
                lines.append(f"…and {len(hits) - 5} more.")
            btns = [(f"unwatch:{w['id']}", "Stop watching")]
            first_card = next((h["card_id"] for h in hits if h["card_id"]), None)
            if first_card:
                btns.insert(0, (f"card:{first_card}", "Open card"))
            outbox.deliver(conn, [buttons_payload("", "\n".join(lines), btns)], "watch_alert")
            sent += 1
    # saved ideas whose scores changed
    for r in conn.execute(
            """select i.id, i.last_scores, s.name, sc.scores_json j,
                 (select id from cards c where c.startup_id = s.id and c.kind = 'card' order by id desc limit 1) card_id
               from ideas i join startups s on s.id = i.startup_id
               join lateral (select scores_json from scores where startup_id = s.id order by created_at desc limit 1) sc on true
               where i.status <> 'dropped'""").fetchall():
        now = {"bangladesh": r["j"]["bangladesh"], "global": r["j"]["global"]}
        if r["last_scores"] and r["last_scores"] != now:
            msg = (f"*Saved idea update: {r['name']}*\nScores changed: Bangladesh {r['last_scores']['bangladesh']} → "
                   f"{now['bangladesh']}, Global {r['last_scores']['global']} → {now['global']}.")
            outbox.deliver(conn, [buttons_payload("", msg, [(f"card:{r['card_id']}", "Open card")])]
                           if r["card_id"] else outbox.text(msg), "idea_alert")
            sent += 1
        if r["last_scores"] != now:
            conn.execute("update ideas set last_scores = %s where id = %s", (json.dumps(now), r["id"]))
    return sent


def competitor_recheck(conn) -> int:
    """Weekly: re-run the Bangladesh search for saved ideas and alert on new names."""
    from scout.search import web_search

    sent = 0
    for r in conn.execute(
            """select i.id, i.competitor_urls, s.name, sc.scores_json j from ideas i join startups s on s.id = i.startup_id
               join lateral (select scores_json from scores where startup_id = s.id order by created_at desc limit 1) sc on true
               where i.status <> 'dropped'""").fetchall():
        phrase = r["j"].get("search_phrase") or r["name"]
        results = web_search(conn, f"{phrase} Bangladesh")
        known = set(r["competitor_urls"] or [])
        new = [x for x in results if x["url"] not in known]
        if known and new:
            body = (f"*New in the Bangladesh search for {r['name']}*\n" +
                    "\n".join(f"- {clip(x['title'], 80)}: {x['url']}" for x in new[:4]))
            outbox.deliver(conn, outbox.text(body), "competitor_alert")
            sent += 1
        conn.execute("update ideas set competitor_urls = %s where id = %s",
                     (json.dumps(sorted(known | {x["url"] for x in results})), r["id"]))
    return sent

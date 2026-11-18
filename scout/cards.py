"""Steps 7-8: research the shortlist and write cards (Hydrogen, both keys in parallel).

Hard rule 6 is enforced in code, not just in the prompt: competitor names survive only if their URL is
one of the search results we fetched, and only URLs we actually fetched are listed as sources.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from scout.llm import prompts, router
from scout.search import format_results, homepage_text, web_search
from scout.whatsapp.formats import LIMITS, clip, numbered_lines

DHAKA = ZoneInfo("Asia/Dhaka")


def facts_for(conn, sid: int) -> dict:
    s = conn.execute("select * from startups where id = %s", (sid,)).fetchone()
    r = conn.execute("select * from rounds where startup_id = %s order by created_at desc limit 1", (sid,)).fetchone()
    sc = conn.execute("select * from scores where startup_id = %s order by created_at desc limit 1", (sid,)).fetchone()
    items = conn.execute(
        """select i.title, i.summary, i.url, i.published_at, src.name as source from startup_items si
           join items i on i.id = si.item_id join sources src on src.id = i.source_id
           where si.startup_id = %s order by i.published_at desc nulls last limit 5""", (sid,)).fetchall()
    return {"startup": s, "round": r, "score": sc, "items": items}


def research(conn, f: dict) -> dict:
    s, sc = f["startup"], f["score"]
    phrase = (sc["scores_json"].get("search_phrase") or s["category"] or s["name"]) if sc else s["name"]
    bd = web_search(conn, f"{phrase} Bangladesh") + web_search(conn, f"{phrase} Dhaka app")
    seen, bd_unique = set(), []
    for r in bd:
        if r["url"] not in seen:
            seen.add(r["url"])
            bd_unique.append(r)
    fail = web_search(conn, f'"{s["name"]}" shut down')
    home_url = s["website"] or (f"https://{s['domain']}" if s["domain"] else None)
    return {"phrase": phrase, "search_bd": bd_unique[:8], "search_fail": fail[:5],
            "homepage_url": home_url, "homepage": homepage_text(conn, home_url)}


def when_label(d: date | datetime | None, today: date) -> str:
    if d is None:
        return "date unknown"
    if isinstance(d, datetime):
        d = d.astimezone(DHAKA).date()
    days = (today - d).days
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago" if days < 14 else d.strftime("%d %b")


def _facts_text(f: dict) -> str:
    s, r = f["startup"], f["round"]
    lines = [f"name: {s['name']}", f"country: {s['country'] or 'unknown'}",
             f"what they do (from news): {s['description'] or 'unknown'}"]
    if r:
        lines += [f"round: {r['stage'] or 'unknown stage'} {r['amount_text'] or ''}".strip(),
                  f"investors: {', '.join(r['investors']) or 'unknown'}"]
    if s["yc_batch"]:
        lines.append(f"Y Combinator batch: {s['yc_batch']}")
    return "\n".join(lines)


def card_prompt_for(f: dict, res: dict, market: str) -> tuple[str, set[str], set[str]]:
    sc = f["score"]
    scores = {k: sc["scores_json"][k] for k in prompts.SCORE_KEYS} if sc else {}
    news = "\n".join(f"- {i['source']}: {i['title']} | {i['url']} | {(i['summary'] or '')[:800]}" for i in f["items"])
    p = prompts.card_prompt({"market": "Bangladesh" if market == "bangladesh" else "global (sell abroad from Bangladesh)",
                             "facts": _facts_text(f), "scores": json.dumps(scores), "news": news or "(none)",
                             "homepage_url": res["homepage_url"], "homepage": res["homepage"][:5000],
                             "search": format_results(res["search_bd"])})
    allowed_sources = {i["url"] for i in f["items"]} | set(f["round"]["source_urls"] if f["round"] else [])
    if res["homepage"] and res["homepage_url"]:
        allowed_sources.add(res["homepage_url"])
    search_urls = {r["url"] for r in res["search_bd"]}
    return p, allowed_sources | search_urls, search_urls


def clean_card(data: dict, allowed: set[str], search_urls: set[str], had_search: bool) -> dict:
    data = dict(data)
    data["competitors"] = [c for c in data.get("competitors", []) if c.get("url") in search_urls and c.get("name")]
    data["source_urls"] = [u for u in data.get("source_urls", []) if u in allowed]
    if not had_search:
        data["already_in_bangladesh"] = "unknown (the web search returned nothing)"
        data["competitors"] = []
    return data


def _sources(f: dict, data: dict) -> list[str]:
    """Real article links first; a Google News redirect only if there is no other news link."""
    news = [i["url"] for i in f["items"]]
    direct = [u for u in news if "news.google.com" not in u]
    gnews = [u for u in news if "news.google.com" in u]
    out: list[str] = []
    for u in [*direct, *(gnews[:1] if not direct else []), *data.get("source_urls", []),
              *[c["url"] for c in data.get("competitors", [])]]:
        if u and u not in out and not ("news.google.com" in u and direct):
            out.append(u)
    return out[:8]


def card_text(f: dict, data: dict, model_id: str, today: date, new_round: bool = False) -> str:
    s, r, sc = f["startup"], f["round"], f["score"]
    sj, rj = sc["scores_json"], sc["reasons_json"]
    round_txt = " ".join(x for x in [(r["stage"] if r else ""), (r["amount_text"] if r else "")] if x) or "round unknown"
    announced = when_label((r["announced_at"] if r and r["announced_at"] else
                            (f["items"][-1]["published_at"] if f["items"] else None)), today)
    check = data.get("score_check") or {}
    bd_line = f"Bangladesh {sj['bangladesh']}/10 · Global {sj['global']}/10"
    if check.get("note") and (check.get("bangladesh") != sj["bangladesh"] or check.get("global") != sj["global"]):
        bd_line += f"\nAfter research: Bangladesh {check.get('bangladesh')}/10 · Global {check.get('global')}/10 ({check['note'].rstrip('.')})"
    head = [f"*{s['name']}*" + (" (new round)" if new_round else ""),
            f"{s['country'] or 'Country unknown'} · {round_txt} · announced {announced}",
            bd_line,
            f"Pain {sj['pain']} · Why now {sj['why_now']} · Feasibility {sj['feasibility']} · "
            f"Fits you {sj['fits_you']} · Money to start: {(sc['money_to_start'] or 'unknown').capitalize()}"]
    comp = data.get("already_in_bangladesh") or "unknown"
    if data.get("competitors"):
        comp += "\n" + "\n".join(f"- {c['name']}: {c['url']}" for c in data["competitors"][:4])
    reasons = "\n".join(f"{label}: {rj.get(key, '').rstrip('.')}" for label, key in [
        ("Bangladesh", "bangladesh"), ("Global", "global"), ("Pain", "pain"), ("Why now", "why_now"),
        ("Feasibility", "feasibility"), ("Fits you", "fits_you"), ("Money", "money_to_start")] if rj.get(key))
    if sc["confidence"] == "low":
        reasons += "\nConfidence: low (thin information)."
    src_block = "*Sources*\n" + ("\n".join(f"{n}. {u}" for n, u in enumerate(_sources(f, data), 1)) or "unknown")
    footer = f"\n\n_Model: {model_id}_"
    sections = [("What they do", data.get("what_they_do")), ("Why it got funded", data.get("why_funded")),
                ("Bangladesh version", data.get("bangladesh_version")), ("Global angle", data.get("global_angle")),
                ("AI angle", data.get("ai_angle")), ("Already in Bangladesh", comp),
                ("Test it this week", numbered_lines(data.get("test_this_week") or "")), ("Why these scores", reasons)]

    def render(cap: int | None) -> str:
        body = "\n\n".join(f"*{t}*\n{clip(txt or 'unknown', cap) if cap else (txt or 'unknown')}" for t, txt in sections)
        return "\n".join(head) + "\n\n" + body + "\n\n" + src_block + footer

    text = render(None)
    cap = 900
    while len(text) > LIMITS["text"] and cap > 120:
        text = render(cap)
        cap -= 60
    return clip(text, LIMITS["text"])


def write_cards(conn, picks: list[tuple[dict, str]], run_date: date, new_round_ids: set[int]) -> dict:
    """picks: [(shortlist row, market)]. Returns {card_ids, failed, models}."""
    prepared = []
    for row, market in picks:
        f = facts_for(conn, row["startup_id"])
        res = research(conn, f)
        p, allowed, search_urls = card_prompt_for(f, res, market)
        prepared.append((row, market, f, res, p, allowed, search_urls))
    outs = router.complete_many("card", [x[4] for x in prepared], schema=prompts.CARD_SCHEMA,
                                system=prompts.SYSTEM, conn=conn)
    card_ids, failed, models = [], [], []
    for (row, market, f, res, _, allowed, search_urls), out in zip(prepared, outs):
        if isinstance(out, Exception):
            failed.append((row["startup_id"], str(out)[:200]))
            continue
        data = clean_card(out.data, allowed, search_urls, had_search=bool(res["search_bd"]))
        data["research"] = {"phrase": res["phrase"], "search_bd": res["search_bd"], "search_fail": res["search_fail"],
                            "homepage_url": res["homepage_url"]}
        text = card_text(f, data, out.model_id, run_date, new_round=row["startup_id"] in new_round_ids)
        cid = conn.execute(
            """insert into cards (startup_id, kind, body, source_urls, model_id, run_date, market, title, data_json)
               values (%s, 'card', %s, %s, %s, %s, %s, %s, %s) returning id""",
            (row["startup_id"], text, _sources(f, data), out.model_id, run_date, market,
             f["startup"]["name"], json.dumps(data))).fetchone()["id"]
        card_ids.append(cid)
        models.append(out.model_id)
    return {"card_ids": card_ids, "failed": failed, "models": models}

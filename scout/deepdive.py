"""The "More" button: a deep dive on one card (Hydrogen). Competitors and failures only from search results."""
from __future__ import annotations

import json

from scout.llm import prompts, router
from scout.search import format_results, web_search
from scout.whatsapp.formats import LIMITS, clip, numbered_lines


def _dedupe(results: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            out.append(r)
    return out


def deep_dive(conn, card_id: int) -> tuple[str, int]:
    """Returns (text, deep_dive_card_id). Reuses a deep dive made in the last 7 days."""
    card = conn.execute("select * from cards where id = %s", (card_id,)).fetchone()
    if card is None:
        raise ValueError("card not found")
    old = conn.execute(
        """select id, body from cards where startup_id = %s and kind = 'deep_dive'
           and created_at > now() - interval '7 days' order by id desc limit 1""", (card["startup_id"],)).fetchone()
    if old:
        return old["body"], old["id"]
    s = conn.execute("select * from startups where id = %s", (card["startup_id"],)).fetchone()
    research = (card["data_json"] or {}).get("research", {})
    phrase = research.get("phrase") or s["category"] or s["name"]
    search_bd = _dedupe(research.get("search_bd", []) + web_search(conn, f"{phrase} company Bangladesh"))[:10]
    search_fail = _dedupe(web_search(conn, f"{phrase} startup shut down") + research.get("search_fail", []))[:8]
    out = router.complete("deep_dive", prompts.deep_dive_prompt({
        "card": card["body"][:3500], "search_bd": format_results(search_bd), "search_fail": format_results(search_fail)}),
        schema=prompts.DEEP_SCHEMA, system=prompts.SYSTEM, conn=conn)
    d = out.data
    allowed = {r["url"] for r in search_bd + search_fail}
    comps = [c for c in d.get("competitors", []) if c.get("url") in allowed and c.get("name")]
    if not search_bd:
        d["competitors_bd"] = "unknown (the web search returned nothing)"
    sources = []
    for u in [*d.get("source_urls", []), *[c["url"] for c in comps]]:
        if u in allowed and u not in sources:
            sources.append(u)
    comp_txt = d["competitors_bd"] + ("\n" + "\n".join(f"- {c['name']}: {c['url']}" for c in comps[:5]) if comps else "")
    sections = [("Competitors in Bangladesh", comp_txt), ("Failure check", d["failure_check"]),
                ("How it could make money", d["how_it_makes_money"]), ("Risks", numbered_lines(d["risks"])),
                ("First 7 days", numbered_lines(d["first_7_days"]))]
    head = f"*{s['name']}: deep dive*"
    src = "*Sources*\n" + ("\n".join(f"{i}. {u}" for i, u in enumerate(sources[:8], 1)) or "unknown")
    text = head + "\n\n" + "\n\n".join(f"*{t}*\n{v or 'unknown'}" for t, v in sections) + "\n\n" + src + \
        f"\n\n_Model: {out.model_id}_"
    text = clip(text, LIMITS["text"])
    did = conn.execute(
        """insert into cards (startup_id, kind, body, source_urls, model_id, run_date, market, title, data_json)
           values (%s, 'deep_dive', %s, %s, %s, current_date, %s, %s, %s) returning id""",
        (s["id"], text, sources, out.model_id, card["market"], s["name"],
         json.dumps(d | {"competitors": comps, "parent_card_id": card_id}))).fetchone()["id"]
    return text, did

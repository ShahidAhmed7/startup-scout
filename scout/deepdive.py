"""Full analysis, sourced research and a 30-day validation plan behind More."""

import json
from scout.llm import prompts, run as ai
from scout.search import format_results, web_search
from scout.cards import full_analysis, fact_gate


def _dedupe(results):
    seen = set()
    out = []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            out.append(r)
    return out


def deep_dive(conn, card_id):
    card = conn.execute("select * from cards where id=%s", (card_id,)).fetchone()
    if not card:
        raise ValueError("Card not found")
    old = conn.execute(
        """select id,body from cards where kind='deep_dive' and data_json->>'parent_card_id'=%s
        and created_at>now()-interval '7 days' order by id desc limit 1""",
        (str(card_id),),
    ).fetchone()
    if old:
        return old["body"], old["id"]
    s = conn.execute(
        "select * from startups where id=%s", (card["startup_id"],)
    ).fetchone()
    res = (card["data_json"] or {}).get("research", {})
    phrase = res.get("phrase") or s["category"] or s["name"]
    bd = _dedupe(
        res.get("search_bd", []) + web_search(conn, f"{phrase} company Bangladesh")
    )[:10]
    fail = _dedupe(
        res.get("search_fail", []) + web_search(conn, f"{phrase} startup shut down")
    )[:8]
    abroad = web_search(conn, f"{phrase} companies international")
    out = ai.run(
        "deep_dive",
        prompts.deep_dive_prompt(
            {
                "card": full_analysis(card),
                "search_bd": format_results(bd),
                "search_fail": format_results(fail),
                "search_abroad": format_results(abroad),
            }
        ),
        prompts.DEEP_SCHEMA,
        system=prompts.SYSTEM,
        conn=conn,
    )
    d = out.data
    allowed = {r["url"] for r in bd + fail + abroad}
    d["competitors"] = [c for c in d["competitors"] if c["url"] in allowed]
    d["source_urls"] = [u for u in d["source_urls"] if u in allowed]
    material = full_analysis(card) + "\n" + format_results(bd + fail + abroad)
    removed = []
    for field in ("landscape", "failure_check"):
        gated, flags = fact_gate(
            {"what_they_do": d[field], "evidence": "unknown"}, material, s["name"], None
        )
        d[field] = gated["what_they_do"]
        removed.extend(field + ": " + flag for flag in flags)
    d["fact_gate_removed"] = removed
    d["research"] = {"search_bd": bd, "search_fail": fail, "search_abroad": abroad}
    text = full_analysis(card, d)
    if (card["data_json"] or {}).get("schema_version") != 2:
        text += "\n\n*Deep dive*\n" + "\n\n".join(
            "\n".join(d[k]) if isinstance(d[k], list) else d[k]
            for k in (
                "landscape",
                "failure_check",
                "economics",
                "regulatory_route",
                "first_customers",
                "validation_30_days",
                "risks",
            )
        )
    did = conn.execute(
        """insert into cards(startup_id,kind,body,source_urls,model_id,run_date,market,title,data_json)
        values(%s,'deep_dive',%s,%s,%s,current_date,%s,%s,%s) returning id""",
        (
            s["id"],
            text,
            d["source_urls"],
            out.model_id,
            card["market"],
            s["name"],
            json.dumps(d | {"parent_card_id": card_id}),
        ),
    ).fetchone()["id"]
    return text, did

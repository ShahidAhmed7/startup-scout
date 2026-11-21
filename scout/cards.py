"""Steps 7-8: research the shortlist and write cards (Hydrogen, both keys in parallel).

Hard rule 6 is enforced in code, not just in the prompt: competitor names survive only if their URL is
one of the search results we fetched, and only URLs we actually fetched are listed as sources.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from scout.llm import prompts, run as ai
from scout.search import format_results, homepage_text, web_search
from scout.whatsapp.formats import LIMITS, clip, numbered_lines

DHAKA = ZoneInfo("Asia/Dhaka")


def facts_for(conn, sid: int) -> dict:
    s = conn.execute("select * from startups where id = %s", (sid,)).fetchone()
    r = conn.execute(
        "select * from rounds where startup_id = %s order by created_at desc limit 1",
        (sid,),
    ).fetchone()
    sc = conn.execute(
        "select * from scores where startup_id = %s order by created_at desc limit 1",
        (sid,),
    ).fetchone()
    items = conn.execute(
        """select i.title, i.summary, i.url, i.published_at, src.name as source from startup_items si
           join items i on i.id = si.item_id join sources src on src.id = i.source_id
           where si.startup_id = %s order by i.published_at desc nulls last limit 5""",
        (sid,),
    ).fetchall()
    return {"startup": s, "round": r, "score": sc, "items": items}


def research(conn, f: dict) -> dict:
    s, sc = f["startup"], f["score"]
    phrase = (
        (sc["scores_json"].get("search_phrase") or s["category"] or s["name"])
        if sc
        else s["name"]
    )
    bd = web_search(conn, f"{phrase} Bangladesh") + web_search(
        conn, f"{phrase} Dhaka app"
    )
    seen, bd_unique = set(), []
    for r in bd:
        if r["url"] not in seen:
            seen.add(r["url"])
            bd_unique.append(r)
    fail = web_search(conn, f'"{s["name"]}" shut down')
    home_url = s["website"] or (f"https://{s['domain']}" if s["domain"] else None)
    return {
        "phrase": phrase,
        "search_bd": bd_unique[:8],
        "search_fail": fail[:5],
        "homepage_url": home_url,
        "homepage": homepage_text(conn, home_url),
    }


def when_label(d: date | datetime | None, today: date) -> str:
    if d is None:
        return "date unknown"
    if isinstance(d, datetime):
        d = d.astimezone(DHAKA).date()
    days = (today - d).days
    return (
        "today"
        if days <= 0
        else "yesterday"
        if days == 1
        else f"{days} days ago"
        if days < 14
        else d.strftime("%d %b")
    )


def _facts_text(f: dict) -> str:
    s, r = f["startup"], f["round"]
    lines = [
        f"name: {s['name']}",
        f"country: {s['country'] or 'unknown'}",
        f"what they do (from news): {s['description'] or 'unknown'}",
    ]
    if r:
        lines += [
            f"round: {r['stage'] or 'unknown stage'} {r['amount_text'] or ''}".strip(),
            f"investors: {', '.join(r['investors']) or 'unknown'}",
        ]
    if s["yc_batch"]:
        lines.append(f"Y Combinator batch: {s['yc_batch']}")
    return "\n".join(lines)


def material_text(f: dict, res: dict) -> str:
    """Exactly what the card writer sees (used by the writer's prompt and the fact gate)."""
    news = "\n".join(
        f"- {i['source']}: {i['title']} | {i['url']} | {(i['summary'] or '')[:800]}"
        for i in f["items"]
    )
    return (
        f"STARTUP FACTS:\n{_facts_text(f)}\n\nNEWS:\n{news or '(none)'}\n\n"
        f"HOMEPAGE ({res.get('homepage_url') or 'not available'}):\n{(res.get('homepage') or 'unknown')[:5000]}\n\n"
        f"WEB SEARCH RESULTS:\n{format_results(res.get('search_bd', []))}"
    )


_COMMON = {
    "the",
    "a",
    "an",
    "it",
    "its",
    "they",
    "their",
    "this",
    "these",
    "in",
    "on",
    "for",
    "and",
    "with",
    "by",
    "bangladesh",
    "bangladeshi",
    "dhaka",
    "bkash",
    "nagad",
    "facebook",
    "whatsapp",
    "google",
    "ai",
    "seed",
    "series",
    "pre",
    "arr",
    "mrr",
    "ceo",
    "cto",
    "usd",
    "us",
    "unknown",
    "no",
    "local",
    "small",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9.]+", " ", t.lower().replace(",", ""))


def unsupported_anchors(sentence: str, material: str, own_name: str) -> list[str]:
    """Numbers and proper names in a factual sentence that do not appear in the writer's material."""
    mat = _norm(material) + " " + _norm(own_name)
    bad = []
    for num in re.findall(r"\d[\d,]*(?:\.\d+)?", sentence):
        n = num.replace(",", "").rstrip(".")
        if len(n) >= 2 and n not in mat:
            bad.append(num)
    words = re.findall(
        r"(?<![.!?]\s)(?<!^)\b([A-Z][A-Za-z0-9&'-]+(?:\s+[A-Z][A-Za-z0-9&'-]+)*)",
        sentence,
    )
    for w in words:
        parts = [p for p in _norm(w).split() if p and p not in _COMMON]
        if parts and not all(p in mat.split() or p in mat for p in parts):
            bad.append(w)
    return bad


# Sentence ends: punctuation, then a capitalised word, but not after common abbreviations
# ("Tk. 1,500", "e.g. Paddle"), so prices and examples stay with their sentence.
_SENT_END = re.compile(
    r"(?<!\bTk\.)(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\bvs\.)(?<!\bInc\.)(?<!\bLtd\.)(?<!\bCo\.)"
    r"(?<!\bapprox\.)(?<!\bSt\.)(?<!\bDr\.)(?<!\bMr\.)(?<!\bMs\.)(?<=[.!?])\s+(?=[A-Z\"'(*_\[])"
)


def sentences(text: str) -> list[str]:
    return [p for p in _SENT_END.split((text or "").strip()) if p]


def fact_gate(
    card: dict, material: str, own_name: str, fallback_what: str | None
) -> tuple[dict, list[str]]:
    """Exact check (no AI): in the factual sections, drop sentences whose numbers/names aren't in the material."""
    fixed, removed = dict(card), []
    for field in ("what_they_do", "evidence" if "evidence" in card else "why_funded"):
        keep = []
        for sent in sentences(card.get(field) or ""):
            bad = unsupported_anchors(sent, material, own_name) if sent else []
            if bad:
                removed.append(f"{field}: {sent} (not in sources: {', '.join(bad)})")
            elif sent:
                keep.append(sent)
        text = " ".join(keep)
        if not text:
            text = (
                (fallback_what or "unknown") if field == "what_they_do" else "unknown"
            )
        fixed[field] = text
    return fixed, removed


def card_prompt_for(f: dict, res: dict, market: str) -> tuple[str, set[str], set[str]]:
    sc = f["score"]
    scores = sc["scores_json"] if sc else {}
    news = "\n".join(
        f"- {i['source']}: {i['title']} | {i['url']} | {(i['summary'] or '')[:800]}"
        for i in f["items"]
    )
    p = prompts.card_prompt(
        {
            "market": "Bangladesh"
            if market == "bangladesh"
            else "global (sell abroad from Bangladesh)",
            "facts": _facts_text(f),
            "scores": json.dumps(scores),
            "news": news or "(none)",
            "homepage_url": res["homepage_url"],
            "homepage": res["homepage"][:5000],
            "search": format_results(res["search_bd"]),
        }
    )
    allowed_sources = {i["url"] for i in f["items"]} | set(
        f["round"]["source_urls"] if f["round"] else []
    )
    if res["homepage"] and res["homepage_url"]:
        allowed_sources.add(res["homepage_url"])
    search_urls = {r["url"] for r in res["search_bd"]}
    return p, allowed_sources | search_urls, search_urls


def clean_card(
    data: dict, allowed: set[str], search_urls: set[str], had_search: bool
) -> dict:
    data = dict(data)
    data["competitors"] = [
        c
        for c in data.get("competitors", [])
        if c.get("url") in search_urls and c.get("name")
    ]
    data["source_urls"] = [u for u in data.get("source_urls", []) if u in allowed]
    if not had_search:
        data["already_here"] = "No one found in a quick search."
        data["competitors"] = []
    return data


def _sources(f: dict, data: dict) -> list[str]:
    """Real article links first; a Google News redirect only if there is no other news link."""
    news = [i["url"] for i in f["items"]]
    direct = [u for u in news if "news.google.com" not in u]
    gnews = [u for u in news if "news.google.com" in u]
    out: list[str] = []
    for u in [
        *direct,
        *(gnews[:1] if not direct else []),
        *data.get("source_urls", []),
        *[c["url"] for c in data.get("competitors", [])],
    ]:
        if u and u not in out and not ("news.google.com" in u and direct):
            out.append(u)
    return out[:8]


def _no_version(j, market):
    concept = j.get("concept_bd" if market == "bangladesh" else "concept_global")
    return concept is not None and concept.strip().lower() in ("", "none")


def score_line(j):
    from scout.score import half_up, shown_verdict as verdict

    parts = []
    for market, label in (("bangladesh", "Bangladesh"), ("global", "Global")):
        if _no_version(j, market):
            parts.append(f"{label}: no version")
        else:
            parts.append(f"{label} {half_up(j[market])} · {verdict(j[market])}")
    return "   |   ".join(parts)


DIM_ORDER = {
    "bangladesh": ["demand", "readiness", "competition_gap", "regulatory_path", "economics"],
    "global": ["demand_abroad", "remote_delivery", "cost_advantage", "trust_burden", "competition_gap"],
}


def card_text(f, data, model_id, today, new_round=False):
    s, r, sc = f["startup"], f["round"], f["score"]
    j = data.get("scores_snapshot") or sc["scores_json"]
    round_txt = (
        " ".join(
            x for x in [(r["stage"] if r else ""), (r["amount_text"] if r else "")] if x
        )
        or "round unknown"
    )
    announced = when_label(r.get("announced_at") if r else None, today)
    effort = j["what_it_takes"]
    effort_text = f"{effort['build_type'].replace('_', ' ')} · {effort['capital']} · team {effort['team']} · {effort['licence'] if effort['licence'].lower() != 'none' else 'no licence'} · revenue in {effort['time_to_revenue']}"
    market = data.get("market", "bangladesh")
    play = data["bangladesh_play" if market == "bangladesh" else "global_play"]
    play = " ".join(sentences(play)[:2])
    idea = " ".join(data["one_liner"].split()[:15])
    from scout.normalize import place_label

    place = place_label(s["country"], s.get("region"))
    head = f"{clip(place, 40)} · {clip(round_txt, 70)} · announced {announced}\n{score_line(j)}\n\n*The idea*\n{idea}"
    label = "In Bangladesh" if market == "bangladesh" else "Abroad from Bangladesh"
    risk = data["main_risk"]

    def render():
        return f"{head}\n\n*{label}*\n{play}\n\n*What it takes*\n{effort_text}\n\n*Watch out*\n{risk}"

    # Reduce the play first, then risk. Protect the score and the opportunity.
    while len(render()) > 1024 and len(play) > 70:
        play = clip(play, max(70, len(play) - (len(render()) - 1024)))
    while len(render()) > 1024 and len(risk) > 40:
        risk = clip(risk, max(40, len(risk) - (len(render()) - 1024)))
    if len(render()) > 1024:
        effort_text = clip(
            effort_text, max(80, len(effort_text) - (len(render()) - 1024))
        )
    text = render()
    if len(text) > 1024:
        raise ValueError("Card metadata too long for interactive body")
    return text


def full_analysis(card, deep=None, include_model=False):
    d = card.get("data_json") or {}
    if d.get("schema_version") != 2:
        return card["body"]
    j = d["scores_snapshot"]
    dims = j["dimensions"]
    u = d.get("research_update") or {}
    lines = [f"*{card['title']}: full analysis*", score_line(j)]
    for title, key in [
        ("What they do", "what_they_do"),
        ("Evidence", "evidence"),
        ("In Bangladesh", "bangladesh_play"),
        ("Abroad from Bangladesh", "global_play"),
        ("Already in Bangladesh", "already_here"),
    ]:
        lines += ["", f"*{title}*", d.get(key) or "unknown"]
        if key == "already_here":
            lines += [f"- {c['name']}: {c['url']}" for c in d.get("competitors", [])]
    lines += ["", "*Why these scores*"]
    from scout.score import half_up

    labels = {
        "demand": "demand",
        "readiness": "readiness",
        "competition_gap": "competition",
        "regulatory_path": "regulation",
        "economics": "economics",
        "demand_abroad": "demand",
        "remote_delivery": "remote",
        "cost_advantage": "cost edge",
        "trust_burden": "trust",
    }
    for mk in ("bangladesh", "global"):
        if _no_version(j, mk):
            lines += [f"{mk.capitalize()}: no sensible version for this market."]
            continue
        order = [k for k in DIM_ORDER[mk] if k in dims[mk]] + [
            k for k in dims[mk] if k not in DIM_ORDER[mk]
        ]
        lines += [
            f"{mk.capitalize()} {half_up(j[mk])}: "
            + " · ".join(f"{labels.get(k, k)} {dims[mk][k]['score']}" for k in order)
        ]
        lines += [f"- {labels.get(k, k)}: {dims[mk][k]['reason']}" for k in order]
    lines += [f"Timing {j['timing']['score']}: {j['timing']['reason']}"]
    if u.get("changed"):
        lines += [
            f"After research: competition {u['competition_before']}→{u['competition_after']}, regulation {u['regulation_before']}→{u['regulation_after']} ({u['note']})"
        ]
    if d.get("ai_angle"):
        lines += ["AI angle: " + d["ai_angle"]]
    if deep:
        lines += ["", "*Deep dive*"]
        for key, title in (
            ("landscape", "Landscape"),
            ("failure_check", "Who tried and failed"),
            ("economics", "Economics (estimates)"),
            ("regulatory_route", "Licence route"),
            ("first_customers", "First 100 customers"),
            ("validation_30_days", "30-day test plan"),
            ("risks", "Risks"),
        ):
            v = deep.get(key) or "unknown"
            body = (
                "\n".join("- " + x for x in v)
                if isinstance(v, list)
                else numbered_lines(v)
            )
            lines += [f"_{title}_", body]
    urls = list(
        dict.fromkeys(
            [*card.get("source_urls", []), *(deep or {}).get("source_urls", [])]
        )
    )
    lines += ["", "*Sources*", *[f"{i}. {url}" for i, url in enumerate(urls, 1)]]
    if include_model:
        lines += ["", "Model: " + str(card.get("model_id", "unknown"))]
    return "\n".join(lines)


def apply_update(f, data, market):
    from scout.score import calculate

    old = f["score"]["scores_json"]
    raw = dict(old, **old["dimensions"])
    raw.pop("dimensions", None)
    update = data["update"]
    before = raw[market]
    raw[market] = {k: dict(v) for k, v in before.items()}
    raw[market]["competition_gap"] = {
        "score": update["competition_gap"],
        "reason": (
            update["note"]
            if update["competition_gap"] != before["competition_gap"]["score"]
            else ""
        )
        or before["competition_gap"]["reason"],
    }
    if market == "bangladesh":
        raw[market]["regulatory_path"] = {
            "score": update["regulatory_path"],
            "reason": (
                update["note"]
                if update["regulatory_path"] != before["regulatory_path"]["score"]
                else ""
            )
            or before["regulatory_path"]["reason"],
        }
    d, ranks = calculate(raw, f["startup"]["classification_json"] or {})
    change = {
        "competition_before": before["competition_gap"]["score"],
        "competition_after": update["competition_gap"],
        "regulation_before": before.get("regulatory_path", {}).get("score"),
        "regulation_after": update["regulatory_path"]
        if market == "bangladesh"
        else None,
        "note": update["note"],
    }
    change["changed"] = (
        change["competition_before"] != change["competition_after"]
        or change["regulation_before"] != change["regulation_after"]
    )
    return d, ranks, change


def write_cards(
    conn,
    picks: list[tuple[dict, str]],
    run_date: date,
    new_round_ids: set[int],
    job: str = "card",
) -> dict:
    """picks: [(shortlist row, market)]. Returns {card_ids, failed, models}."""

    def prepare(pair):
        row, market = pair
        f = facts_for(conn, row["startup_id"])
        if not f["score"] or f["score"]["scores_json"].get("schema_version") != 2:
            # Older explore rows remain usable; append a new score instead of replacing history.
            from scout.classify import classify
            from scout.score import score

            eligible, _ = classify(conn, [row["startup_id"]])
            if (
                not eligible
                or eligible[0].get("skip")
                or not eligible[0].get("product_known")
            ):
                return None
            score(conn, [row["startup_id"]], run_date)
            f = facts_for(conn, row["startup_id"])
            if not f["score"] or f["score"]["scores_json"].get("schema_version") != 2:
                return None
        res = research(conn, f)
        p, allowed, search_urls = card_prompt_for(f, res, market)
        return (row, market, f, res, p, allowed, search_urls)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as workers:
        prepared = [p for p in workers.map(prepare, picks) if p is not None]
    outs = ai.run_many(
        job,
        [x[4] for x in prepared],
        output_type=prompts.CARD_SCHEMA,
        system=prompts.SYSTEM,
        conn=conn,
    )
    card_ids, failed, models = [], [], []
    ok = [(x, out) for x, out in zip(prepared, outs) if not isinstance(out, Exception)]
    for x, out in zip(prepared, outs):
        if isinstance(out, Exception):
            failed.append((x[0]["startup_id"], str(out)[:200]))
    cleaned = [
        clean_card(out.data, x[5], x[6], had_search=bool(x[3]["search_bd"]))
        for x, out in ok
    ]
    gated = [
        fact_gate(
            d,
            material_text(x[2], x[3]),
            x[2]["startup"]["name"],
            x[2]["startup"]["description"],
        )
        for d, (x, _) in zip(cleaned, ok)
    ]
    for (x, out), (data, removed) in zip(ok, gated):
        row, market, f, res = x[0], x[1], x[2], x[3]
        data["schema_version"] = 2
        data["model_id"] = out.model_id
        data["market"] = market
        data["new_round"] = row["startup_id"] in new_round_ids
        data["fill"] = row.get("fill", False)
        data["fill_reason"] = row.get("fill_reason", "")
        data["diversity_relaxed"] = row.get("diversity_relaxed", False)
        data["score_id"] = f["score"]["id"]
        if not res["search_bd"]:
            data["update"]["competition_gap"] = f["score"]["scores_json"]["dimensions"][
                market
            ]["competition_gap"]["score"]
        updated, ranks, change = apply_update(f, data, market)
        data["scores_before_research"] = f["score"]["scores_json"]
        data["scores_snapshot"] = updated
        data["research_update"] = change
        data["fact_gate_fields"] = ["what_they_do", "evidence"]
        data["writer_material"] = material_text(f, res)
        data["fact_gate_removed"] = removed
        data["research"] = {
            "phrase": res["phrase"],
            "search_bd": res["search_bd"],
            "search_fail": res["search_fail"],
            "homepage_url": res["homepage_url"],
            "homepage": res["homepage"],
        }
        text = card_text(
            f,
            data,
            out.model_id,
            datetime.now(DHAKA).date(),
            new_round=row["startup_id"] in new_round_ids,
        )
        cid = conn.execute(
            """insert into cards (startup_id, kind, body, source_urls, model_id, run_date, market, title, data_json)
               values (%s, 'card', %s, %s, %s, %s, %s, %s, %s) returning id""",
            (
                row["startup_id"],
                text,
                _sources(f, data),
                out.model_id,
                run_date,
                market,
                f["startup"]["name"],
                json.dumps(data),
            ),
        ).fetchone()["id"]
        card_ids.append(cid)
        models.append(out.model_id)
    return {"card_ids": card_ids, "failed": failed, "models": models}

"""Step 2: pull company facts out of funding/launch news (Flash-Lite, batches of 10, both keys in parallel).

A cheap local keyword pre-filter runs first, so only items that look like funding or launch news reach
the model. YC companies are already structured and skip the model entirely.
"""
from __future__ import annotations

import re

from scout.config import sources
from scout.llm import prompts, run as ai

BATCH = 10
FUNDING_WORDS = re.compile(
    r"(\braise[sd]?\b|\braising\b|\bfunding\b|\bfunded\b|\bseed\b|pre-seed|\bseries [a-f]\b|\bround\b|"
    r"\binvestments?\b|\binvests?\b|\bback(ed|s|ing)\b|\bsecures?\b|\bcloses?\b|\bbags\b|\bnets\b|\blands\b|"
    r"\$\s?\d|€\s?\d|£\s?\d|₹|\bcrore\b|\blakh\b|\bventure\b|\bangel\b|launch hn|\blaunch(es|ed)?\b|"
    r"融资|天使轮|[A-E]\+?轮|领投|投资|تمويل|جولة|استثمار|pendanaan|investasi)", re.I)


def prefilter(title: str, summary: str) -> bool:
    return bool(FUNDING_WORDS.search(f"{title} {summary}"))


# Aggregators (Google News) sometimes re-date old articles. A plan aimed at a past year gives them away:
# "Lime raises $523 mln, eyes going public in 2022".
_PLAN_YEAR = re.compile(
    r"\b(?:eyes|eyeing|plans?|planning|targets?|targeting|aims?|aiming|expects?|expecting|to go public|ipo)\b"
    r"[^.|]{0,40}?\b(20\d\d)\b", re.I)


def stale(title: str, summary: str, year: int | None = None) -> bool:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    year = year or datetime.now(ZoneInfo("Asia/Dhaka")).year
    return any(int(m.group(1)) < year for m in _PLAN_YEAR.finditer(f"{title} {summary}"))


def pending_items(conn, hours: int = 48) -> list[dict]:
    return conn.execute(
        """select i.id, i.source_id, i.url, i.title, i.summary, i.kind, i.raw_json, i.published_at
           from items i where i.extracted_at is null and i.fetched_at > now() - make_interval(hours => %s)
           order by i.id""", (hours,)).fetchall()




def extract(conn, limit: int | None = None) -> dict:
    """Returns {"facts": [(item_row, company_dict, kind)], "stats": {...}}."""
    src = {s["id"]: s for s in sources()}
    items = pending_items(conn)
    facts: list[tuple[dict, dict, str]] = []
    to_llm, stats = [], {"pending": len(items), "yc": 0, "context": 0, "prefiltered": 0, "sent_to_llm": 0,
                         "funding": 0, "launch": 0, "other": 0, "failed_batches": 0}
    for it in items:
        s = src.get(it["source_id"], {})
        if it["kind"] == "yc":
            raw = it["raw_json"] or {}
            regions = [r for r in (raw.get("regions") or []) if "/" not in r]
            facts.append((it, {"name": raw.get("name"), "website": raw.get("website") or "",
                               "country": regions[0] if regions else "", "stage": f"YC {raw.get('batch')}",
                               "amount_text": "", "amount_usd": 0, "investors": ["Y Combinator"],
                               "what_they_do": raw.get("one_liner") or "", "yc_batch": raw.get("batch")}, "yc"))
            stats["yc"] += 1
            conn.execute("update items set extracted_at = now() where id = %s", (it["id"],))
        elif it["kind"] == "trustmrr":
            raw = it["raw_json"] or {}
            facts.append((it, {"name": raw.get("name"), "website": raw.get("website") or "",
                               "country": raw.get("country") or "", "stage": "Bootstrapped (TrustMRR)",
                               "amount_text": f"${raw.get('rev30', 0):,.0f} verified revenue last 30 days",
                               "amount_usd": 0, "investors": [], "what_they_do": raw.get("description") or ""},
                          "trustmrr"))
            stats["trustmrr"] = stats.get("trustmrr", 0) + 1
            conn.execute("update items set extracted_at = now() where id = %s", (it["id"],))
        elif s.get("role") == "context":
            stats["context"] += 1
            conn.execute("update items set kind = 'context', extracted_at = now() where id = %s", (it["id"],))
        elif stale(it["title"] or "", it["summary"] or ""):
            stats["stale"] = stats.get("stale", 0) + 1
            conn.execute("update items set kind = 'stale', extracted_at = now() where id = %s", (it["id"],))
        elif not prefilter(it["title"] or "", it["summary"] or ""):
            stats["prefiltered"] += 1
            conn.execute("update items set kind = 'prefiltered', extracted_at = now() where id = %s", (it["id"],))
        else:
            to_llm.append(it)
    if limit is not None:
        to_llm = to_llm[:limit]
    stats["sent_to_llm"] = len(to_llm)

    batches = [to_llm[i:i + BATCH] for i in range(0, len(to_llm), BATCH)]
    prompt_list = [prompts.extract_prompt([{"id": it["id"], "source": src.get(it["source_id"], {}).get("name"),
                                            "title": it["title"], "summary": (it["summary"] or "")[:1200]}
                                           for it in b]) for b in batches]
    outs = ai.run_many("extract", prompt_list, output_type=prompts.EXTRACT_SCHEMA,
                                system=prompts.SYSTEM, conn=conn)
    for batch, out in zip(batches, outs):
        if isinstance(out, Exception):
            stats["failed_batches"] += 1
            continue  # items stay pending; next run retries them
        by_id = {it["id"]: it for it in batch}
        for art in out.data.get("articles", []):
            it = by_id.get(art.get("item_id"))
            if not it:
                continue
            kind = art.get("kind", "other")
            stats[kind] = stats.get(kind, 0) + 1
            conn.execute("update items set kind = %s, extracted_at = now() where id = %s", (kind, it["id"]))
            if kind in ("funding", "launch"):
                for c in art.get("companies", []):
                    if (c.get("name") or "").strip():
                        facts.append((it, c, kind))
        # items the model skipped: mark them so they are not retried forever
        for it in batch:
            conn.execute("update items set extracted_at = coalesce(extracted_at, now()) where id = %s", (it["id"],))
    return {"facts": facts, "stats": stats}

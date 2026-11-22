"""Quality audit: code-only checks after every pipeline.

Flash-Lite was tried as a score reviewer and fact checker on 2026-11-19 and removed at the owner's request
("if it's not reliable, don't use it"): it found nothing the code checks didn't, raised false alarms, and missed
the one real invented fact. Everything below is deterministic code; read flagged items by hand.

  python -m scout.audit [YYYY-MM-DD]     # default: today (Dhaka)

Three layers, report saved as a `runs` row (kind 'audit') and printed:
1. Code checks: opportunity dimensions, normal/fill thresholds, diversity, pool balance and known products.
2. Card fact check: exact code check that every number and proper name in a card's factual sections appears in
   the material the writer had (the same check the cards pass before sending), plus competitor links opened to
   confirm they load and mention the company. (A Flash-Lite fact checker was tried on 2026-11-19 and was too noisy:
   it flagged true statements and missed the one real error.)
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import date

import httpx

from scout.config import source_defaults
from scout.db import repo
from scout.normalize import norm_name, core_name
from scout.settings import effective

GENERIC = {
    "ai",
    "labs",
    "lab",
    "ventures",
    "technologies",
    "technology",
    "tech",
    "hq",
    "io",
    "app",
    "group",
    "holdings",
    "inc",
    "ltd",
    "sas",
    "the",
}

# ---------------------------------------------------------------- 1. code checks


def code_checks(conn, day):
    from collections import Counter
    from scout.score import todays_rows, shortlist
    from scout.markets import MARKETS, SHORT
    from scout.pool import SIMILAR

    cfg = effective(conn)
    issues = []
    pick_issues = []
    raw = conn.execute(
        "select sc.*,s.name from scores sc join startups s on s.id=sc.startup_id where run_date=%s",
        (day,),
    ).fetchall()
    rows = [r for r in raw if r["scores_json"].get("schema_version") == 2]
    for r in rows:
        j = r["scores_json"]
        if not j.get("product_known"):
            if (
                max(j.get(m) or 0 for m in MARKETS)
                > cfg["scoring"]["unknown_product_max"]
            ):
                issues.append(r["name"] + ": unknown product score exceeds cap")
            if any(
                d["score"] > cfg["scoring"]["unknown_product_max"]
                for dims in j["dimensions"].values()
                for d in dims.values()
            ):
                issues.append(r["name"] + ": unknown product dimension exceeds cap")
    digest = conn.execute("select * from digests where date=%s", (day,)).fetchone()
    picks = []
    shown = []
    if digest:
        cards = conn.execute(
            "select * from cards where id=any(%s)", (digest["card_ids"],)
        ).fetchall()
        for c in cards:
            d = c["data_json"] or {}
            if d.get("schema_version") != 2:
                continue
            j = d["scores_snapshot"]
            mk = c["market"]
            fill = d.get("fill", False)
            picks.append((c, d, j))
            shown.append(j)
            floor = cfg["fill_min_score"] if fill else cfg["min_score"]
            if j[mk] < floor:
                pick_issues.append(c["title"] + ": below pick threshold")
            if not j.get("product_known"):
                pick_issues.append(c["title"] + ": unknown product picked")
            if len(c["body"]) > 1024:
                pick_issues.append(c["title"] + ": body exceeds 1024")
            if d.get("fact_gate_fields") != ["what_they_do", "evidence"]:
                pick_issues.append(c["title"] + ": fact gate fields missing")
            allowed = {r["url"] for r in d.get("research", {}).get("search_bd", [])}
            if any(comp["url"] not in allowed for comp in d.get("competitors", [])):
                pick_issues.append(c["title"] + ": competitor URL absent from search")
        explore_ids = [
            int(r["id"].split(":")[1])
            for sec in digest["body_json"].get("sections", [])
            for r in sec["rows"]
            if r["id"].startswith("startup:")
        ]
        for sid in explore_ids:
            match = next((r for r in rows if r["startup_id"] == sid), None)
            if match:
                shown.append(match["scores_json"])
        if (
            sum(bool(j.get("ai_native")) for j in shown)
            > cfg["diversity"]["max_ai_native"]
        ):
            pick_issues.append("AI-first cap exceeded")
        # Only the explicitly marked minimum-pick fills may relax these constraints.
        strict = [j for c, d, j in picks if not d.get("diversity_relaxed")]
        sectors = Counter(j.get("sector") for j in strict)
        per_track = Counter((d.get("market"), j.get("sector")) for c, d, j in picks if not d.get("diversity_relaxed"))
        if cfg["diversity"]["one_per_sector"] and (
            any(n > 1 for n in per_track.values())
            or any(n > cfg["diversity"].get("max_same_sector", 2) for n in sectors.values())
        ):
            pick_issues.append("sector repeated without fill exception")
        models = Counter(j.get("business_model") for j in strict)
        if any(
            n > cfg["diversity"]["max_same_business_model"] for n in models.values()
        ):
            pick_issues.append("business-model cap exceeded")
        eligible = todays_rows(conn, day)
        # Research snapshots replace initial scores for the audit selection.
        for c, d, j in picks:
            for r in eligible:
                if r["startup_id"] == c["startup_id"]:
                    r.update({SHORT[m]: j[m] for m in MARKETS if j.get(m) is not None})
        expected = shortlist(eligible, cfg["digest_mix"], cfg["min_score"], cfg=cfg)
        expected_picks = [r for m in MARKETS for r in expected[m]]
        if len(picks) < min(cfg["min_picks"], len(expected_picks)):
            pick_issues.append(
                "fewer than minimum eligible picks (check card failures and swap limit)"
            )
        regions = {r["startup_id"]: r["region"] for r in eligible}
        non_us = lambda reg: reg not in ("usa", "unknown", "")
        if any(non_us(r["region"]) for r in expected_picks) and not any(
            non_us(regions.get(c["startup_id"], "unknown")) for c, d, j in picks
        ):
            pick_issues.append("eligible outside-USA pick omitted")
    pool_issues = []
    regions = Counter((r["scores_json"].get("region") or "") for r in rows)
    # Read the recorded pool report, which also explains unavoidable source shortages.
    run = conn.execute(
        "select stats_json from runs where kind='pipeline' and stats_json->>'run_date'=%s order by id desc limit 1",
        (str(day),),
    ).fetchone()
    pool = (run or {}).get("stats_json", {}).get("pool", {})
    n = pool.get("count", 0)
    if n:
        for reg, cap in cfg["pool"]["max_share"].items():
            if pool.get("regions", {}).get(reg, 0) > n * cap + 1 and not pool.get(
                "shortages"
            ):
                pool_issues.append(reg + " exceeds pool cap")
        if pool.get("ai_native", 0) > n * cfg["pool"]["max_ai_native_share"] + 1e-8:
            pool_issues.append("pool AI share exceeds cap")
    groups = defaultdict(list)
    for r in rows:
        groups[core_name(r["name"])].append(r["name"])
    return {
        "scored": len(rows),
        "rule_breaks": issues,
        "duplicates": [names for names in groups.values() if len(set(names)) > 1],
        "unknown_but_scored_5plus": [
            r["name"]
            for r in rows
            if not r["scores_json"].get("product_known")
            and max(r["scores_json"].get(m) or 0 for m in MARKETS) >= 5
        ],
        "pick_issues": pick_issues,
        "pool_issues": pool_issues,
        "pool_shortages": pool.get("shortages", []),
    }


# ---------------------------------------------------------------- 2. exact fact check of cards


def _fetch_text(url: str) -> tuple[int, str]:
    try:
        import trafilatura

        with httpx.Client(
            headers={"User-Agent": source_defaults()["user_agent"]},
            timeout=15,
            follow_redirects=True,
        ) as c:
            r = c.get(url)
        text = (
            trafilatura.extract(r.text[:2_000_000])
            if "html" in r.headers.get("content-type", "")
            else ""
        )
        return r.status_code, (text or "")[:6000]
    except Exception as e:
        return 0, type(e).__name__


def fact_check_cards(conn, day) -> list[dict]:
    from scout.cards import facts_for

    cards = conn.execute(
        "select * from cards where kind = 'card' and run_date = %s order by id", (day,)
    ).fetchall()
    prompts_, meta = [], []
    for c in cards:
        f = facts_for(conn, c["startup_id"])
        data = c["data_json"] or {}
        research = data.get("research", {})
        from scout.cards import material_text
        from scout.search import homepage_text

        res = dict(
            research,
            homepage=homepage_text(conn, research.get("homepage_url"))
            if research.get("homepage_url")
            else "",
        )
        material = [
            data.get("writer_material") or material_text(f, res),
            f"(news dates: {[str(i['published_at'])[:10] for i in f['items']]}; the "
            f"card's 'announced X days ago' is computed from these; today is {day})",
        ]
        prompts_.append((c, f, "\n".join(material)))
        # competitor links: do they load and mention the company?
        links = []
        for comp in data.get("competitors", [])[:5]:
            code, text = _fetch_text(comp["url"])
            words = [w for w in re.findall(r"[a-z0-9]{4,}", comp["name"].lower())]
            hay = (text or "").lower() + " " + comp["url"].lower().replace("-", "")
            mentioned = any(w in hay for w in words) if words else False
            links.append(
                {
                    "name": comp["name"],
                    "url": comp["url"],
                    "status": code,
                    "mentions_name": mentioned,
                }
            )
        meta.append({"card_id": c["id"], "name": c["title"], "links": links})
    results = []
    for (c, f, mat), m in zip(prompts_, meta):
        from scout.cards import fact_gate

        _, removed = fact_gate(c["data_json"] or {}, mat, f["startup"]["name"], None)
        results.append(
            m
            | {
                "verdict": "clean" if not removed else "serious_issues",
                "unsupported_claims": [
                    {"claim": r, "why": "not in the writer's material"} for r in removed
                ],
            }
        )
    return results


# ---------------------------------------------------------------- report


def audit(conn, day) -> dict:
    rid = repo.start_run(conn, "audit")
    report = {
        "date": str(day),
        "code": code_checks(conn, day),
        "facts": fact_check_cards(conn, day),
    }
    report["summary"] = {
        "rule_breaks": len(report["code"]["rule_breaks"]),
        "pool_issues": len(report["code"].get("pool_issues", [])),
        "duplicates": len(report["code"]["duplicates"]),
        "pick_issues": len(report["code"]["pick_issues"]),
        "unknown_products_scored_5plus": len(
            report["code"]["unknown_but_scored_5plus"]
        ),
        "cards_serious": sum(
            1 for f in report["facts"] if f.get("verdict") == "serious_issues"
        ),
        "dead_competitor_links": sum(
            1 for f in report["facts"] for l in f["links"] if l["status"] != 200
        ),
    }
    repo.finish_run(conn, rid, "ok", report)
    return report


def print_report(r: dict) -> None:
    c = r["code"]
    print(f"AUDIT {r['date']}: {c['scored']} scores\n")
    print("1. CODE CHECKS")
    print("   hard-rule breaks:", c["rule_breaks"] or "none")
    print("   likely duplicates:", c["duplicates"] or "none")
    print("   unknown product but scored 5+:", c["unknown_but_scored_5plus"] or "none")
    print("   pick issues:", c["pick_issues"] or "none")
    print("\n2. CARD FACT CHECK")
    for f in r["facts"]:
        print(f"   {f['name']}: {f.get('verdict')}")
        for u in f.get("unsupported_claims", []):
            print(f"      unsupported: {u['claim']} ({u['why']})")
        for l in f["links"]:
            ok = "ok" if l["status"] == 200 and l["mentions_name"] else "CHECK"
            print(
                f"      competitor {l['name']}: HTTP {l['status']}, mentions name: {l['mentions_name']} [{ok}]"
            )
    print("\nSUMMARY:", r["summary"])


if __name__ == "__main__":
    from scout.run import today_dhaka

    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else today_dhaka()
    with repo.connect() as conn:
        print_report(audit(conn, day))

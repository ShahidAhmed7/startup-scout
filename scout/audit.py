"""Quality audit (owner, 2026-11-19: "Flash-Lite will be our algorithm and rules checker").

  python -m scout.audit [YYYY-MM-DD]     # default: today (Dhaka)

Three layers, report saved as a `runs` row (kind 'audit') and printed:
1. Code checks (exact): hard scoring rules (market score <= feasibility + 2; money high -> <= 6), picks obey
   min score / feasibility / AI cap / distinct sectors, unknown products not picked, likely duplicate startups.
2. Flash-Lite rule check: every score vs the rubric -> ok / questionable / wrong, with the rule and a suggested score.
3. Card fact check: exact code check that every number and proper name in a card's factual sections appears in
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
from scout.llm import prompts, router
from scout.normalize import norm_name
from scout.settings import effective

GENERIC = {"ai", "labs", "lab", "ventures", "technologies", "technology", "tech", "hq", "io", "app", "group",
           "holdings", "inc", "ltd", "sas", "the"}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "startup_id": {"type": "integer"},
            "verdict": {"type": "string", "enum": ["ok", "questionable", "wrong"]},
            "rule": {"type": "string"},
            "suggested_bangladesh": {"type": "integer"},
            "suggested_global": {"type": "integer"},
            "why": {"type": "string"},
        },
        "required": ["startup_id", "verdict", "rule", "suggested_bangladesh", "suggested_global", "why"]}}},
    "required": ["verdicts"],
}

FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "unsupported_claims": {"type": "array", "items": {
            "type": "object", "properties": {"claim": {"type": "string"}, "why": {"type": "string"}},
            "required": ["claim", "why"]}},
        "verdict": {"type": "string", "enum": ["clean", "minor_issues", "serious_issues"]},
    },
    "required": ["unsupported_claims", "verdict"],
}


def core_name(name: str) -> str:
    return " ".join(w for w in norm_name(name).split() if w not in GENERIC) or norm_name(name)


# ---------------------------------------------------------------- 1. code checks

def code_checks(conn, day) -> dict:
    cfg = effective(conn)
    rows = conn.execute(
        """select distinct on (sc.startup_id) sc.startup_id, s.name, s.description, sc.skip, sc.scores_json j,
             sc.money_to_start m
           from scores sc join startups s on s.id = sc.startup_id where sc.run_date = %s
           order by sc.startup_id, sc.created_at desc""", (day,)).fetchall()
    issues = []
    for r in rows:
        j = r["j"]
        for mk in ("bangladesh", "global"):
            if j[mk] > j["feasibility"] + 2:
                issues.append(f"{r['name']}: {mk} {j[mk]} > feasibility {j['feasibility']} + 2")
            if r["m"] == "high" and j[mk] > 6:
                issues.append(f"{r['name']}: {mk} {j[mk]} > 6 with money_to_start high")
    groups = defaultdict(list)
    for r in rows:
        groups[core_name(r["name"])].append(r)
    dupes = [[g["name"] for g in v] for k, v in groups.items() if len(v) > 1 and k]
    unknown_ok = [r["name"] for r in rows if not r["skip"] and not (r["description"] or "").strip()
                  and max(r["j"]["bangladesh"], r["j"]["global"]) >= 5]
    digest = conn.execute("select * from digests where date = %s", (day,)).fetchone()
    pick_issues = []
    if digest and digest["card_ids"]:
        picks = conn.execute(
            """select c.id, c.market, s.name, s.description, sc.scores_json j from cards c
               join startups s on s.id = c.startup_id
               join lateral (select scores_json from scores where startup_id = s.id order by created_at desc limit 1) sc on true
               where c.id = any(%s)""", (digest["card_ids"],)).fetchall()
        sectors = [p["j"].get("sector") for p in picks if p["j"].get("sector")]
        if len(sectors) != len(set(sectors)):
            pick_issues.append(f"two picks share a sector: {sectors}")
        ai = sum(1 for p in picks if p["j"].get("ai_native"))
        if ai > cfg.get("diversity", {}).get("max_ai_native", 99):
            pick_issues.append(f"{ai} AI-first picks")
        for p in picks:
            j, mk = p["j"], ("bangladesh" if p["market"] == "bangladesh" else "global")
            if j[mk] < cfg["min_score"] or j["feasibility"] < cfg.get("min_feasibility", 0):
                pick_issues.append(f"{p['name']} picked with {mk} {j[mk]}, feasibility {j['feasibility']}")
            if not (p["description"] or "").strip():
                pick_issues.append(f"{p['name']} picked though its product is unknown")
    return {"scored": len(rows), "rule_breaks": issues, "duplicates": dupes, "unknown_but_scored_5plus": unknown_ok,
            "pick_issues": pick_issues}


# ---------------------------------------------------------------- 2. Flash-Lite rule check

def judge_prompt(batch: list[dict], cfg: dict) -> str:
    rubric = prompts.RUBRIC.format(skills=", ".join(cfg["owner"]["skills"]), skip_list="; ".join(cfg["skip_list"]))
    lines = ["You are auditing another analyst's scores. Check each one against the rubric below. Be strict but fair:",
             "verdict 'ok' if the scores and reasons follow the rubric, 'questionable' if one score looks 2+ points off,",
             "'wrong' if a hard rule is broken, a skip-list category was not skipped (or a non-skip-list one was), the",
             "reason contradicts the score, or the score relies on facts not in the material. Name the rule, suggest the",
             "scores you think are right, and say why in one line.", "", "RUBRIC:", rubric, "", "SCORES TO AUDIT:"]
    for b in batch:
        lines.append(json.dumps(b, ensure_ascii=False))
    return "\n".join(lines)


def judge_scores(conn, day) -> list[dict]:
    cfg = effective(conn)
    rows = conn.execute(
        """select distinct on (sc.startup_id) sc.startup_id, s.name, s.country, s.description, sc.skip,
             sc.scores_json j, sc.reasons_json r, sc.money_to_start m, sc.category
           from scores sc join startups s on s.id = sc.startup_id where sc.run_date = %s
           order by sc.startup_id, sc.created_at desc""", (day,)).fetchall()
    from scout.score import startup_material

    material = {m["id"]: m for m in startup_material(conn, [r["startup_id"] for r in rows])}
    items = [{"startup_id": r["startup_id"], "material_the_scorer_had": material.get(r["startup_id"]),
              "category": r["category"], "skipped": r["skip"],
              "scores": {k: r["j"][k] for k in prompts.SCORE_KEYS}, "money_to_start": r["m"], "reasons": r["r"]}
             for r in rows]
    batches = [items[i:i + 10] for i in range(0, len(items), 10)]
    outs = router.complete_many("extract", [judge_prompt(b, cfg) for b in batches], schema=JUDGE_SCHEMA,
                                system=prompts.SYSTEM, conn=conn)  # 'extract' job = Flash-Lite
    names = {r["startup_id"]: r for r in rows}
    flagged = []
    for out in outs:
        if isinstance(out, Exception):
            continue
        for v in out.data.get("verdicts", []):
            r = names.get(v["startup_id"])
            if r and v["verdict"] != "ok":
                flagged.append(v | {"name": r["name"], "bd": r["j"]["bangladesh"], "gl": r["j"]["global"]})
    return flagged


# ---------------------------------------------------------------- 3. Flash-Lite fact check of cards

def _fetch_text(url: str) -> tuple[int, str]:
    try:
        import trafilatura

        with httpx.Client(headers={"User-Agent": source_defaults()["user_agent"]}, timeout=15,
                          follow_redirects=True) as c:
            r = c.get(url)
        text = trafilatura.extract(r.text[:2_000_000]) if "html" in r.headers.get("content-type", "") else ""
        return r.status_code, (text or "")[:6000]
    except Exception as e:
        return 0, type(e).__name__


def fact_check_cards(conn, day) -> list[dict]:
    from scout.cards import facts_for

    cards = conn.execute("select * from cards where kind = 'card' and run_date = %s order by id", (day,)).fetchall()
    prompts_, meta = [], []
    for c in cards:
        f = facts_for(conn, c["startup_id"])
        data = c["data_json"] or {}
        research = data.get("research", {})
        from scout.cards import material_text
        from scout.search import homepage_text

        res = dict(research, homepage=homepage_text(conn, research.get("homepage_url")) if research.get("homepage_url") else "")
        material = [material_text(f, res), f"(news dates: {[str(i['published_at'])[:10] for i in f['items']]}; the "
                    f"card's 'announced X days ago' is computed from these; today is {day})"]
        prompts_.append((c, f, "\n".join(material)))
        _unused = (
            "Fact-check this startup card. List every FACTUAL claim (funding, traction, team, what the company does, "
            "named competitors and what they do, market facts) that is NOT supported by the material. Ideas, "
            "suggestions, plans and opinions (Bangladesh version, test steps, AI angle) are NOT facts; ignore them.\n\n"
            f"CARD:\n{c['body'][:3500]}\n\nMATERIAL THE WRITER HAD:\n" + "\n".join(material))
        # competitor links: do they load and mention the company?
        links = []
        for comp in data.get("competitors", [])[:5]:
            code, text = _fetch_text(comp["url"])
            words = [w for w in re.findall(r"[a-z0-9]{4,}", comp["name"].lower())]
            hay = (text or "").lower() + " " + comp["url"].lower().replace("-", "")
            mentioned = any(w in hay for w in words) if words else False
            links.append({"name": comp["name"], "url": comp["url"], "status": code, "mentions_name": mentioned})
        meta.append({"card_id": c["id"], "name": c["title"], "links": links})
    results = []
    for (c, f, mat), m in zip(prompts_, meta):
        from scout.cards import fact_gate

        _, removed = fact_gate(c["data_json"] or {}, mat, f["startup"]["name"], None)
        results.append(m | {"verdict": "clean" if not removed else "serious_issues",
                            "unsupported_claims": [{"claim": r, "why": "not in the writer's material"} for r in removed]})
    return results


# ---------------------------------------------------------------- report

def audit(conn, day) -> dict:
    rid = repo.start_run(conn, "audit")
    report = {"date": str(day), "code": code_checks(conn, day), "judge": judge_scores(conn, day),
              "facts": fact_check_cards(conn, day)}
    report["summary"] = {
        "rule_breaks": len(report["code"]["rule_breaks"]), "duplicates": len(report["code"]["duplicates"]),
        "pick_issues": len(report["code"]["pick_issues"]),
        "judge_wrong": sum(1 for v in report["judge"] if v["verdict"] == "wrong"),
        "judge_questionable": sum(1 for v in report["judge"] if v["verdict"] == "questionable"),
        "cards_serious": sum(1 for f in report["facts"] if f.get("verdict") == "serious_issues"),
        "dead_competitor_links": sum(1 for f in report["facts"] for l in f["links"] if l["status"] != 200),
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
    print(f"\n2. FLASH-LITE RULE CHECK: {len(r['judge'])} flagged")
    for v in sorted(r["judge"], key=lambda v: v["verdict"] != "wrong"):
        print(f"   [{v['verdict']}] {v['name']}: BD {v['bd']}->{v['suggested_bangladesh']}, "
              f"GL {v['gl']}->{v['suggested_global']} | {v['rule']} | {v['why']}")
    print("\n3. CARD FACT CHECK")
    for f in r["facts"]:
        print(f"   {f['name']}: {f.get('verdict')}")
        for u in f.get("unsupported_claims", []):
            print(f"      unsupported: {u['claim']} ({u['why']})")
        for l in f["links"]:
            ok = "ok" if l["status"] == 200 and l["mentions_name"] else "CHECK"
            print(f"      competitor {l['name']}: HTTP {l['status']}, mentions name: {l['mentions_name']} [{ok}]")
    print("\nSUMMARY:", r["summary"])


if __name__ == "__main__":
    from scout.run import today_dhaka

    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else today_dhaka()
    with repo.connect() as conn:
        print_report(audit(conn, day))

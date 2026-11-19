"""Steps 4-6: skip filter + scoring (Hydrogen, batches of 10, both keys in parallel) and the shortlist."""
from __future__ import annotations

import json
from collections import Counter

from scout.llm import prompts, router
from scout.settings import effective

BATCH = 10


def startup_material(conn, ids: list[int]) -> list[dict]:
    out = []
    for sid in ids:
        s = conn.execute("select * from startups where id = %s", (sid,)).fetchone()
        r = conn.execute("select * from rounds where startup_id = %s order by created_at desc limit 1", (sid,)).fetchone()
        items = conn.execute(
            """select i.title, i.summary, i.url, src.name as source from startup_items si
               join items i on i.id = si.item_id join sources src on src.id = i.source_id
               where si.startup_id = %s order by i.published_at desc nulls last limit 3""", (sid,)).fetchall()
        out.append({
            "id": sid, "name": s["name"], "country": s["country"], "what_they_do": s["description"],
            "yc_batch": s["yc_batch"],
            "stage": r["stage"] if r else None,
            "amount": (r["amount_text"] or (f"${int(r['amount_usd']):,}" if r["amount_usd"] else None)) if r else None,
            "investors": ", ".join(r["investors"][:5]) if r and r["investors"] else None,
            "sources": [f"{i['source']}: {i['title']} — {(i['summary'] or '')[:500]}" for i in items],
        })
    return out


def _validator(ids: set[int]):
    def check(data):
        rows = data.get("startups", [])
        got = {r["startup_id"] for r in rows}
        if not ids <= got:
            raise ValueError(f"missing startups {sorted(ids - got)[:5]}")
        for r in rows:
            for k, v in r["scores"].items():
                if not isinstance(v, int) or not 1 <= v <= 10:
                    raise ValueError(f"score {k}={v} out of range")
    return check


def rank_value(market_score: int, sub: dict, ai_strong: bool, cfg: dict) -> float:
    """Market score, nudged by the weighted sub-scores (±1 at most) and the AI-angle boost."""
    w = cfg["weights"]
    total_w = sum(w.values())
    mean = sum(sub[k] * w[k] for k in w) / total_w
    nudge = max(-1.0, min(1.0, (mean - 5.5) * 0.2))
    boost = cfg["ai_angle"]["boost"] if ai_strong else 0.0
    return round(market_score + nudge + boost, 2)


def score(conn, ids: list[int], run_date) -> dict:
    cfg = effective(conn)
    material = startup_material(conn, ids)
    batches = [material[i:i + BATCH] for i in range(0, len(material), BATCH)]
    plist = [prompts.score_prompt(b, cfg["owner"]["skills"], cfg["skip_list"]) for b in batches]
    validators = [_validator({s["id"] for s in b}) for b in batches]
    outs = []
    # one validator per batch -> call complete_many per batch group, still parallel across keys
    results = router.complete_many("score", plist, schema=prompts.SCORE_SCHEMA, system=prompts.SYSTEM, conn=conn,
                                   validate=None)
    for b, v, out in zip(batches, validators, results):
        if not isinstance(out, Exception):
            try:
                v(out.data)
            except ValueError:
                pass  # keep the valid rows; missing ones are simply not scored today
        outs.append(out)

    stats = {"scored": 0, "skipped": 0, "failed_batches": 0, "skip_reasons": Counter(), "models": Counter()}
    valid_ids = set(ids)
    for out in outs:
        if isinstance(out, Exception):
            stats["failed_batches"] += 1
            continue
        stats["models"][out.model_id] += 1
        for r in out.data.get("startups", []):
            sid = r.get("startup_id")
            if sid not in valid_ids:
                continue
            sc = {k: max(1, min(10, int(r["scores"].get(k, 1)))) for k in prompts.SCORE_KEYS}
            sub = {k: sc[k] for k in cfg["weights"]}
            conn.execute(
                """insert into scores (startup_id, model_id, scores_json, reasons_json, money_to_start, ai_angle,
                     confidence, run_date, category, skip, rank_bd, rank_global)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (sid, out.model_id, json.dumps(sc | {"search_phrase": r.get("search_phrase", ""),
                                                     "ai_angle_strong": bool(r.get("ai_angle_strong")),
                                                     "sector": r.get("sector") or "other",
                                                     "ai_native": bool(r.get("ai_native"))}),
                 json.dumps(r["reasons"]), r["money_to_start"], r["ai_angle"], r["confidence"], run_date,
                 (r["category"] or "").strip().lower()[:60], bool(r["skip"]),
                 rank_value(sc["bangladesh"], sub, r.get("ai_angle_strong", False), cfg),
                 rank_value(sc["global"], sub, r.get("ai_angle_strong", False), cfg)))
            # the startup's category = its broad sector (used for variety, the archive and funding snapshots)
            conn.execute("update startups set category = %s where id = %s",
                         ((r.get("sector") or r["category"] or "")[:60], sid))
            stats["scored"] += 1
            if r["skip"]:
                stats["skipped"] += 1
                reason = (r.get("skip_reason") or r["category"] or "skip list").strip().lower()
                stats["skip_reasons"][reason] += 1
                conn.execute("insert into skipped (startup_id, category, reason) values (%s, %s, %s)",
                             (sid, r["category"], reason))
            valid_ids.discard(sid)
    stats["skip_reasons"] = dict(stats["skip_reasons"])
    stats["models"] = dict(stats["models"])
    return stats


def buzz(conn, sid: int) -> float:
    row = conn.execute(
        """select count(distinct i.source_id) as n, coalesce(max((i.raw_json->>'points')::int), 0) as pts
           from startup_items si join items i on i.id = si.item_id where si.startup_id = %s""", (sid,)).fetchone()
    return row["n"] + row["pts"] / 100.0


def shortlist(rows: list[dict], mix: dict, min_score: int, reserve: int = 3, min_feasibility: int = 0,
              max_ai_native: int | None = None, explore_rows: int = 0, explore_min_score: int = 5) -> dict:
    """rows: [{startup_id, sector, ai_native, bd, gl, rank_bd, rank_global, buzz, feas}] (not skipped).

    Picks: top `mix['bangladesh']` by Bangladesh score and `mix['global']` by global score, each >= min_score and
    feasibility >= min_feasibility; every pick from a DIFFERENT broad sector; at most `max_ai_native` AI-first
    products; tie-break on buzz. Explore: up to `explore_rows` more startups (score >= explore_min_score) from
    sectors not used yet, non-AI first, to show the breadth of what got funded. Reserve: spare picks."""
    feasible = [r for r in rows if r.get("feas", 10) >= min_feasibility]
    used_ids: set[int] = set()
    used_sectors: set[str] = set()
    ai_used = [0]

    def sector(r) -> str:
        return (r.get("sector") or r.get("category") or "").lower()

    def ok(r) -> bool:
        if r["startup_id"] in used_ids:
            return False
        if sector(r) and sector(r) != "other" and sector(r) in used_sectors:
            return False
        return not (max_ai_native is not None and r.get("ai_native") and ai_used[0] >= max_ai_native)

    def take(r) -> None:
        used_ids.add(r["startup_id"])
        if sector(r):
            used_sectors.add(sector(r))
        if r.get("ai_native"):
            ai_used[0] += 1

    def pick(key_rank: str, key_score: str, n: int) -> list[dict]:
        out = []
        for r in sorted(feasible, key=lambda r: (r[key_rank], r["buzz"]), reverse=True):
            if len(out) >= n:
                break
            if r[key_score] >= min_score and ok(r):
                out.append(r)
                take(r)
        return out

    bd = pick("rank_bd", "bd", mix.get("bangladesh", 3))
    gl = pick("rank_global", "gl", mix.get("global", 2))
    best = lambda r: (max(r["rank_bd"], r["rank_global"]), r["buzz"])
    extra = [r for r in sorted(feasible, key=best, reverse=True)
             if r["startup_id"] not in used_ids and max(r["bd"], r["gl"]) >= min_score][:reserve]
    explore: list[dict] = []
    for r in sorted(rows, key=lambda r: (not r.get("ai_native"), *best(r)), reverse=True):
        if len(explore) >= explore_rows:
            break
        if max(r["bd"], r["gl"]) < explore_min_score or r["startup_id"] in used_ids or r in extra:
            continue
        if sector(r) and sector(r) != "other" and sector(r) in used_sectors:
            continue
        explore.append(r)
        used_ids.add(r["startup_id"])
        if sector(r):
            used_sectors.add(sector(r))
    return {"bangladesh": bd, "global": gl, "reserve": extra, "explore": explore}


def todays_rows(conn, run_date) -> list[dict]:
    rows = conn.execute(
        """select distinct on (sc.startup_id) sc.startup_id, sc.category, sc.rank_bd, sc.rank_global,
             (sc.scores_json->>'bangladesh')::int as bd, (sc.scores_json->>'global')::int as gl,
             (sc.scores_json->>'feasibility')::int as feas,
             coalesce(sc.scores_json->>'sector', sc.category) as sector,
             coalesce((sc.scores_json->>'ai_native')::boolean, false) as ai_native
           from scores sc where sc.run_date = %s and not sc.skip
           order by sc.startup_id, sc.created_at desc""", (run_date,)).fetchall()
    for r in rows:
        r["buzz"] = buzz(conn, r["startup_id"])
        r["rank_bd"] = float(r["rank_bd"])
        r["rank_global"] = float(r["rank_global"])
    return rows

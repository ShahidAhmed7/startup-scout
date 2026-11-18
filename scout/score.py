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
                                                     "ai_angle_strong": bool(r.get("ai_angle_strong"))}),
                 json.dumps(r["reasons"]), r["money_to_start"], r["ai_angle"], r["confidence"], run_date,
                 (r["category"] or "").strip().lower()[:60], bool(r["skip"]),
                 rank_value(sc["bangladesh"], sub, r.get("ai_angle_strong", False), cfg),
                 rank_value(sc["global"], sub, r.get("ai_angle_strong", False), cfg)))
            conn.execute("update startups set category = %s where id = %s", ((r["category"] or "")[:60], sid))
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


def shortlist(rows: list[dict], mix: dict, min_score: int, reserve: int = 3, min_feasibility: int = 0) -> dict:
    """rows: [{startup_id, category, bd, gl, rank_bd, rank_global, buzz}] (not skipped).
    Top `mix['bangladesh']` by Bangladesh score and `mix['global']` by global score, each >= min_score,
    no two picks from the same category, tie-break on buzz. Plus up to `reserve` extras."""
    rows = [r for r in rows if r.get("feas", 10) >= min_feasibility]
    used_ids: set[int] = set()
    used_cats: set[str] = set()

    def pick(key_rank: str, key_score: str, n: int) -> list[dict]:
        out = []
        for r in sorted(rows, key=lambda r: (r[key_rank], r["buzz"]), reverse=True):
            if len(out) >= n:
                break
            if r[key_score] < min_score or r["startup_id"] in used_ids:
                continue
            cat = (r["category"] or "").lower()
            if cat and cat in used_cats:
                continue
            out.append(r)
            used_ids.add(r["startup_id"])
            if cat:
                used_cats.add(cat)
        return out

    bd = pick("rank_bd", "bd", mix.get("bangladesh", 3))
    gl = pick("rank_global", "gl", mix.get("global", 2))
    extra = [r for r in sorted(rows, key=lambda r: (max(r["rank_bd"], r["rank_global"]), r["buzz"]), reverse=True)
             if r["startup_id"] not in used_ids and max(r["bd"], r["gl"]) >= min_score][:reserve]
    return {"bangladesh": bd, "global": gl, "reserve": extra}


def todays_rows(conn, run_date) -> list[dict]:
    rows = conn.execute(
        """select distinct on (sc.startup_id) sc.startup_id, sc.category, sc.rank_bd, sc.rank_global,
             (sc.scores_json->>'bangladesh')::int as bd, (sc.scores_json->>'global')::int as gl,
             (sc.scores_json->>'feasibility')::int as feas
           from scores sc where sc.run_date = %s and not sc.skip
           order by sc.startup_id, sc.created_at desc""", (run_date,)).fetchall()
    for r in rows:
        r["buzz"] = buzz(conn, r["startup_id"])
        r["rank_bd"] = float(r["rank_bd"])
        r["rank_global"] = float(r["rank_global"])
    return rows

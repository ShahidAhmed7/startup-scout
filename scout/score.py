"""Dimension scores, code-computed market scores, and constrained selection."""

from __future__ import annotations
import copy
import json
from decimal import Decimal, ROUND_HALF_UP
from collections import Counter
from scout.llm import prompts, run as ai
from scout.settings import effective


def startup_material(conn, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    startups = conn.execute(
        "select * from startups where id=any(%s)", (ids,)
    ).fetchall()
    rounds = conn.execute(
        "select distinct on(startup_id) * from rounds where startup_id=any(%s) order by startup_id,created_at desc,id desc",
        (ids,),
    ).fetchall()
    rounds = {r["startup_id"]: r for r in rounds}
    items = conn.execute(
        """select si.startup_id,i.title,i.summary,i.url,src.name as source from startup_items si
        join items i on i.id=si.item_id join sources src on src.id=i.source_id where si.startup_id=any(%s)
        order by si.startup_id,i.published_at desc nulls last""",
        (ids,),
    ).fetchall()
    grouped = {sid: [] for sid in ids}
    for item in items:
        if len(grouped[item["startup_id"]]) < 3:
            grouped[item["startup_id"]].append(item)
    by_id = {s["id"]: s for s in startups}
    out = []
    for sid in ids:
        s = by_id[sid]
        r = rounds.get(sid)
        out.append(
            {
                "id": sid,
                "name": s["name"],
                "country": s["country"],
                "what_they_do": s["description"],
                "yc_batch": s["yc_batch"],
                "classification": s.get("classification_json"),
                "extraction": s.get("extraction_json"),
                "stage": r["stage"] if r else None,
                "amount": (
                    r["amount_text"]
                    or (f"${int(r['amount_usd']):,}" if r["amount_usd"] else None)
                )
                if r
                else None,
                "investors": ", ".join(r["investors"][:5]) if r else None,
                "sources": [
                    {
                        "title": i["source"] + ": " + i["title"],
                        "text": (i["summary"] or "")[:500],
                        "url": i["url"],
                    }
                    for i in grouped[sid]
                ],
            }
        )
    return out


def buzz(conn, sid: int) -> float:
    row = conn.execute(
        """select count(distinct i.source_id) as n, coalesce(max((i.raw_json->>'points')::int), 0) as pts
           from startup_items si join items i on i.id = si.item_id where si.startup_id = %s""",
        (sid,),
    ).fetchone()
    return row["n"] + row["pts"] / 100.0


def half_up(value):
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def verdict(value, cfg=None):
    thresholds = (cfg or effective()).get("verdicts", {})
    for key, default in [("strong", 8), ("promising", 6.5), ("watch", 5)]:
        if value >= thresholds.get(key, default):
            return key.capitalize()
    return "Below threshold"


def calculate(data, classification, cfg=None):
    cfg = cfg or effective()
    d = copy.deepcopy(data)
    known = classification.get("product_known", False)
    cap = cfg["scoring"]["unknown_product_max"]
    if not known:
        for group in ("bangladesh", "global"):
            for dim in d[group].values():
                dim["score"] = min(cap, dim["score"])
        d["timing"]["score"] = min(cap, d["timing"]["score"])
        d["confidence"] = "low"
    markets = {}
    for market, concept in [("bangladesh", "concept_bd"), ("global", "concept_global")]:
        weights = cfg["scoring"][market]
        val = sum(
            (d["timing"] if key == "timing" else d[market][key])["score"] * weight
            for key, weight in weights.items()
        ) / sum(weights.values())
        markets[market] = (
            1.0
            if d[concept].strip().lower() in ("none", "")
            else float(
                Decimal(str(val)).quantize(Decimal(".1"), rounding=ROUND_HALF_UP)
            )
        )
    d["dimensions"] = {k: d.pop(k) for k in ("bangladesh", "global")}
    d.update(markets)
    d.update(classification)
    d["schema_version"] = 2
    return d, rank_scores(d, cfg)


def rank_scores(d, cfg):
    bonus = cfg["scoring"]["evidence_bonus"]
    evidence = sum(
        bonus[key]
        for key, field in [
            ("traction_known", "traction"),
            ("investor_known", "investor_signal"),
        ]
        if d.get(field, "").strip().lower() not in ("", "unknown", "none")
    )
    penalty = (
        cfg["scoring"]["low_confidence_penalty"] if d["confidence"] == "low" else 0
    )
    return {
        market: round(val + evidence - penalty, 2)
        for market, val in ((m, d[m]) for m in ("bangladesh", "global"))
    }


def score(conn, ids, run_date):
    from scout.classify import exact_ids
    from concurrent.futures import ThreadPoolExecutor

    cfg = effective(conn)
    material = startup_material(conn, ids)
    batches = [material[i : i + 8] for i in range(0, len(material), 8)]

    def one(batch):
        try:
            return ai.run(
                "score",
                prompts.score_prompt(batch),
                prompts.SCORE_SCHEMA,
                system=prompts.SYSTEM,
                conn=conn,
                validate=exact_ids([s["id"] for s in batch]),
            )
        except Exception as e:
            return e

    with ThreadPoolExecutor(max_workers=6) as ex:
        outs = list(ex.map(one, batches))
    stats = {
        "scored": 0,
        "failed_batches": 0,
        "skipped": 0,
        "skip_reasons": {},
        "models": Counter(),
    }
    for batch, out in zip(batches, outs):
        if isinstance(out, Exception):
            stats["failed_batches"] += 1
            continue
        stats["models"][out.model_id] += 1
        by_id = {s["id"]: s for s in batch}
        for result in out.output.startups:
            c = by_id[result.startup_id]["classification"] or {}
            if c.get("skip"):
                continue
            raw = result.model_dump(by_alias=True)
            import re

            material_text = json.dumps(by_id[result.startup_id], default=str)
            if not re.search(r"\bMRR\b|monthly recurring revenue", material_text, re.I):

                def normalize_metric(text):
                    return re.sub(
                        r"\bMRR\b|monthly recurring revenue",
                        "revenue in the last 30 days",
                        text,
                        flags=re.I,
                    )

                raw["traction"] = normalize_metric(raw["traction"])
                for market in ("bangladesh", "global"):
                    for dim in raw[market].values():
                        dim["reason"] = normalize_metric(dim["reason"])
            raw["bangladesh"]["competition_gap"] = {
                "score": 5,
                "reason": "not checked yet",
            }
            d, ranks = calculate(raw, c, cfg)
            conn.execute(
                "insert into scores(startup_id,model_id,scores_json,reasons_json,ai_angle,confidence,run_date,category,skip,rank_bd,rank_global,concepts_json)\n                values(%s,%s,%s,%s,%s,%s,%s,%s,false,%s,%s,%s)",
                (
                    result.startup_id,
                    out.model_id,
                    json.dumps(d),
                    json.dumps(d["dimensions"]),
                    d["ai_angle"],
                    d["confidence"],
                    run_date,
                    c.get("sector", "other"),
                    ranks["bangladesh"],
                    ranks["global"],
                    json.dumps(
                        {"bangladesh": d["concept_bd"], "global": d["concept_global"]}
                    ),
                ),
            )
            stats["scored"] += 1
    stats["models"] = dict(stats["models"])
    return stats


def ambition_ok(row, ambition):
    allowed = {
        "any": {"bootstrap", "seed", "venture"},
        "lean": {"bootstrap"},
        "seed": {"bootstrap", "seed"},
        "venture": {"seed", "venture"},
    }
    return ambition == "any" or row.get("capital") in allowed.get(ambition, set())


def shortlist(rows, mix, min_score=6.5, reserve=3, *, cfg=None, **options):
    cfg = copy.deepcopy(cfg or effective())
    div = cfg["diversity"]
    div.update(options)
    minimum = cfg.get("min_picks", 3)
    floor = cfg.get("fill_min_score", 5)
    candidates = [
        dict(r)
        for r in rows
        if r.get("known", False)
        and not r.get("skip")
        and not r.get("novelty_blocked")
        and ambition_ok(r, cfg.get("ambition", "any"))
    ]
    confidence = {"high": 2, "medium": 1, "low": 0}

    def ranking(r, mk):
        return (
            float(r["rank_bd" if mk == "bangladesh" else "rank_global"]),
            confidence.get(r.get("conf"), 0),
            r.get("buzz", 0),
            -r["startup_id"],
        )

    def best(r):
        return max(ranking(r, "bangladesh"), ranking(r, "global"))

    picks = []

    def ok(r, current, relax=False):
        if any(p["startup_id"] == r["startup_id"] for p in current):
            return False
        if r.get("ai_native") and sum(
            bool(p.get("ai_native")) for p in current
        ) >= div.get("max_ai_native", 3):
            return False
        if not relax:
            if div.get("one_per_sector", True) and any(
                p.get("sector") == r.get("sector") for p in current
            ):
                return False
            if sum(
                p.get("business_model") == r.get("business_model") for p in current
            ) >= div.get("max_same_business_model", 2):
                return False
        return True

    def add(r, mk, fill=False, relaxed=False, reason=""):
        picks.append(
            dict(r, market=mk, fill=fill, diversity_relaxed=relaxed, fill_reason=reason)
        )

    for mk, key in [("bangladesh", "bd"), ("global", "gl")]:
        for r in sorted(candidates, key=lambda r: ranking(r, mk), reverse=True):
            if sum(p["market"] == mk for p in picks) >= mix.get(mk, 0):
                break
            if r[key] >= min_score and ok(r, picks):
                add(r, mk)
    normal_count = len(picks)

    def non_us(r):
        return r.get("region", "unknown") not in ("usa", "unknown", "")

    if div.get("min_non_us_picks", 1) and not any(non_us(r) for r in picks):
        targets = sorted(picks, key=lambda r: ranking(r, r["market"])) or [None]
        found = False
        for threshold in (min_score, floor):
            for target in targets:
                remaining = [p for p in picks if p is not target]
                for mk in (
                    [
                        target["market"],
                        "global" if target["market"] == "bangladesh" else "bangladesh",
                    ]
                    if target
                    else ["bangladesh", "global"]
                ):
                    key = "bd" if mk == "bangladesh" else "gl"
                    eligible = [
                        r
                        for r in candidates
                        if non_us(r) and r[key] >= threshold and ok(r, remaining)
                    ]
                    if eligible:
                        r = max(eligible, key=lambda r: ranking(r, mk))
                        picks[:] = remaining
                        add(r, mk, fill=r[key] < min_score, reason="outside USA")
                        found = True
                        break
                if found:
                    break
            if found:
                break
    for relaxed in (False, True):
        for r in sorted(candidates, key=best, reverse=True):
            if len(picks) >= minimum:
                break
            mk = "bangladesh" if r["bd"] >= r["gl"] else "global"
            if max(r["bd"], r["gl"]) >= floor and ok(r, picks, relaxed):
                add(r, mk, True, relaxed, "minimum picks")
    selected = {r["startup_id"] for r in picks}
    spare = [
        r
        for r in sorted(candidates, key=best, reverse=True)
        if r["startup_id"] not in selected and max(r["bd"], r["gl"]) >= floor
    ]
    explore = []
    for r in sorted(
        spare, key=lambda r: (not r.get("ai_native"), best(r)), reverse=True
    ):
        if len(explore) >= div.get("explore_rows", 5):
            break
        if max(r["bd"], r["gl"]) >= div.get("explore_min_score", 5) and ok(
            r, picks + explore
        ):
            explore.append(r)
    return {
        **{
            mk: sorted(
                [r for r in picks if r["market"] == mk],
                key=lambda r: ranking(r, mk),
                reverse=True,
            )
            for mk in ("bangladesh", "global")
        },
        "reserve": spare[:reserve],
        "explore": explore,
        "normal_count": normal_count,
    }


def todays_rows(conn, run_date):
    raw = conn.execute(
        "select distinct on(sc.startup_id) sc.*,s.region,s.classification_json from scores sc join startups s on s.id=sc.startup_id\n        where sc.run_date=%s and not sc.skip order by sc.startup_id,sc.created_at desc,sc.id desc",
        (run_date,),
    ).fetchall()
    rows = []
    for sc in raw:
        j = sc["scores_json"]
        cl = sc["classification_json"] or {}
        if j.get("schema_version") != 2:
            continue
        rows.append(
            {
                "startup_id": sc["startup_id"],
                "score_id": sc["id"],
                "bd": float(j["bangladesh"]),
                "gl": float(j["global"]),
                "rank_bd": float(sc["rank_bd"]),
                "rank_global": float(sc["rank_global"]),
                "sector": j.get("sector", cl.get("sector")),
                "business_model": j.get("business_model", cl.get("business_model")),
                "ai_native": j.get("ai_native", False),
                "known": j.get("product_known", False),
                "conf": sc["confidence"],
                "region": sc["region"] or "unknown",
                "capital": j["what_it_takes"]["capital"],
                "taste": float(sc.get("taste") or 0),
                "novelty_blocked": bool(j.get("novelty_blocked", False)),
                "build_type": j["what_it_takes"]["build_type"],
                "buzz": buzz(conn, sc["startup_id"]),
            }
        )
    return rows


def exploration(rows, picks, cfg):
    div = cfg["diversity"]
    used = {r["startup_id"] for r in picks}
    sectors = {r.get("sector") for r in picks}
    models = Counter(r.get("business_model") for r in picks)
    ai = sum(bool(r.get("ai_native")) for r in picks)
    out = []
    for r in sorted(
        rows,
        key=lambda r: (not r.get("ai_native"), max(r["rank_bd"], r["rank_global"])),
        reverse=True,
    ):
        if len(out) >= min(div["explore_rows"], 10 - len(picks)):
            break
        if (
            r["startup_id"] in used
            or not r.get("known")
            or r.get("skip")
            or r.get("novelty_blocked")
            or not ambition_ok(r, cfg["ambition"])
        ):
            continue
        if max(r["bd"], r["gl"]) < div["explore_min_score"]:
            continue
        if div["one_per_sector"] and r.get("sector") in sectors:
            continue
        if models[r.get("business_model")] >= div["max_same_business_model"]:
            continue
        if r.get("ai_native") and ai >= div["max_ai_native"]:
            continue
        out.append(r)
        used.add(r["startup_id"])
        sectors.add(r.get("sector"))
        models[r.get("business_model")] += 1
        ai += bool(r.get("ai_native"))
    return out

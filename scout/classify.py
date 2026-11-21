"""Cheap classification of all candidates, before skip filtering and balancing."""

import json
from collections import Counter
from scout.llm import prompts, run as ai
from scout.score import startup_material, buzz
from scout.settings import effective
from scout.pool import region


def exact_ids(ids):
    def validate(output):
        got = [r.startup_id for r in output.startups]
        if len(got) != len(ids) or set(got) != set(ids):
            raise ValueError("Missing, duplicate or unexpected startup IDs")

    return validate


def classify(conn, ids):
    material = startup_material(conn, ids)
    cfg = effective(conn)
    batches = [material[i : i + 20] for i in range(0, len(material), 20)]
    # A validator per batch is part of the model retry/fallback, never ignored.
    from concurrent.futures import ThreadPoolExecutor

    def one(batch):
        try:
            return ai.run(
                "classify",
                prompts.classify_prompt(batch, cfg["skip_list"]),
                prompts.CLASSIFY_SCHEMA,
                system=prompts.SYSTEM,
                conn=conn,
                validate=exact_ids([s["id"] for s in batch]),
            )
        except Exception as e:
            return e

    with ThreadPoolExecutor(max_workers=6) as ex:
        outputs = list(ex.map(one, batches))
    metadata = {
        r["id"]: r
        for r in conn.execute(
            """select s.id,s.country,s.last_seen_at,
        (select src.region from startup_items si join items i on i.id=si.item_id join sources src on src.id=i.source_id
         where si.startup_id=s.id order by i.fetched_at,i.id limit 1) src_region from startups s where s.id=any(%s)""",
            (ids,),
        ).fetchall()
    }
    rows = []
    stats = {
        "classified": 0,
        "skipped": 0,
        "failed_batches": 0,
        "skip_reasons": Counter(),
    }
    for batch, out in zip(batches, outputs):
        if isinstance(out, Exception):
            stats["failed_batches"] += 1
            continue
        for obj in out.output.startups:
            d = obj.model_dump()
            sid = d["startup_id"]
            s = metadata[sid]
            reg = region(s["country"], s["src_region"])
            conn.execute(
                "update startups set classification_json=%s,region=%s,category=%s where id=%s",
                (json.dumps(d), reg, d["sector"], sid),
            )
            stats["classified"] += 1
            if d["skip"]:
                stats["skipped"] += 1
                stats["skip_reasons"][d["skip_reason"] or "skip list"] += 1
                conn.execute(
                    "insert into skipped(startup_id,category,reason) values(%s,%s,%s)",
                    (sid, d["sector"], d["skip_reason"]),
                )
            else:
                rows.append(
                    d
                    | {
                        "region": reg,
                        "buzz": buzz(conn, sid),
                        "recency": s["last_seen_at"],
                    }
                )
    stats["skip_reasons"] = dict(stats["skip_reasons"])
    return rows, stats

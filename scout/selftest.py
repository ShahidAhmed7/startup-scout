"""Real-service tests, rollback-only; usage stays durable. Default uses Flash-Lite."""

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date
from scout.db import repo

TEST_DATE = date(2000, 1, 1)


def variety_report(rows, picks):
    out = {}
    for name, data in [("scored", rows), ("picks", picks)]:
        total = len(data)
        out[name] = {
            "count": total,
            "shares": {
                k: {
                    str(v): round(n / total, 3)
                    for v, n in Counter(r.get(k) for r in data).items()
                }
                if total
                else {}
                for k in (
                    "region",
                    "sector",
                    "business_model",
                    "build_type",
                    "capital",
                    "ai_native",
                )
            },
        }
        sectors = defaultdict(list)
        for r in data:
            sectors[r["sector"]].append(r)
        out[name]["sector_average"] = {
            k: {
                "bd": round(sum(r["bd"] for r in v) / len(v), 2),
                "global": round(sum(r["gl"] for r in v) / len(v), 2),
                "service": round(sum(r.get("sv") or 0 for r in v) / len(v), 2),
            }
            for k, v in sectors.items()
        }
    return out


def variety(conn, dev=True, full=False):
    from psycopg import Rollback
    from scout.run import run_pipeline, print_dry
    from scout.score import todays_rows, shortlist
    from scout.settings import effective
    from scout.classify import classify
    from scout.pool import balance
    from scout.score import score
    from scout.config import ROOT

    os.environ["DRY_RUN"] = "1"
    if dev:
        os.environ["SCOUT_DEV"] = "1"
    with conn.transaction():
        ids = [
            r["id"]
            for r in conn.execute(
                "select id from startups order by last_seen_at desc limit 400"
            ).fetchall()
        ]
        if full:
            stats = run_pipeline(
                conn, skip_collect=True, run_date=TEST_DATE, candidate_ids=ids
            )
            print(json.dumps(stats, indent=2, default=str))
            print_dry(conn, TEST_DATE)
        else:
            rows, cs = classify(conn, ids)
            pool, ps = balance(rows, effective(conn)["pool"])
            st = score(conn, [r["startup_id"] for r in pool], TEST_DATE)
            print(json.dumps({"classify": cs, "pool": ps, "score": st}, indent=2))
        rows = todays_rows(conn, TEST_DATE)
        cfg = effective(conn)
        sl = shortlist(rows, cfg["digest_mix"], cfg["min_score"], cfg=cfg)
        report = variety_report(rows, [r for m in ("bangladesh", "global", "service") for r in sl[m]])
        print(json.dumps(report, indent=2))
        evidence = {
            name: conn.execute(sql, (TEST_DATE,)).fetchall()
            for name, sql in {
                "scores": "select s.name,s.country,s.description,sc.* from scores sc join startups s on s.id=sc.startup_id where sc.run_date=%s",
                "cards": "select * from cards where run_date=%s",
                "digest": "select * from digests where date=%s",
            }.items()
        }
        evidence["startups"] = conn.execute(
            "select id,name,country,region,description,classification_json from startups where id=any(%s)",
            (ids,),
        ).fetchall()
        evidence["source_material"] = conn.execute(
            """select si.startup_id,i.title,i.summary,i.url from startup_items si join items i on i.id=si.item_id
            where si.startup_id=any(%s) order by si.startup_id,i.id""",
            (ids,),
        ).fetchall()
        path = ROOT / "data" / "redesign"
        path.mkdir(parents=True, exist_ok=True)
        (path / "live-results.json").write_text(
            json.dumps({"variety": report, "evidence": evidence}, indent=2, default=str)
        )
        raise Rollback()


if __name__ == "__main__":
    if sys.argv[1:2] == ["qwen-digest"]:
        os.environ["FORCE_GEMINI_FAIL"] = "1"
        with repo.connect() as conn:
            variety(conn, dev=False, full=True)
    elif sys.argv[1:2] == ["variety"]:
        with repo.connect() as conn:
            variety(conn, dev="--hydrogen" not in sys.argv, full="--full" in sys.argv)
    else:
        raise SystemExit(
            "Use: python -m scout.selftest variety [--full] [--hydrogen] | qwen-digest"
        )

"""Daily pipeline and rollback-only, real-service dry run."""

from __future__ import annotations
import argparse
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from scout import cards as cards_mod, digest as digest_mod
from scout.collect import collect
from scout.db import repo
from scout.dedupe import candidates, merge
from scout.extract import extract
from scout.classify import classify
from scout.pool import balance
from scout.score import score, shortlist, todays_rows
from scout.settings import effective

from scout.markets import MARKETS, SHORT, CONCEPT

DHAKA = ZoneInfo("Asia/Dhaka")


def today_dhaka():
    return datetime.now(DHAKA).date()


def _check_and_swap(conn, cr, picks, rows, run_date, new_round, cfg, max_swaps):
    """Re-run the same selection rules after each research update, including replacements."""
    pool = {r["startup_id"]: dict(r) for r in rows}
    written = {}
    failed = set()
    swaps = []

    def ingest(result):
        for sid, _ in result["failed"]:
            failed.add(sid)
        for cid in result["card_ids"]:
            c = conn.execute("select * from cards where id=%s", (cid,)).fetchone()
            d = c["data_json"]
            j = d["scores_snapshot"]
            sid = c["startup_id"]
            old = pool[sid]
            from scout.score import rank_scores

            ranks = rank_scores(j, cfg)
            from scout.markets import MARKETS, SHORT, RANK

            for mk in MARKETS:
                if j.get(mk) is not None:
                    old[SHORT[mk]] = j[mk]
                    old[RANK[mk]] = ranks[mk] + old.get("taste", 0)
            written[sid] = c

    ingest(cr)
    for turn in range(max_swaps + 1):
        sl = shortlist(
            [r for sid, r in pool.items() if sid not in failed],
            cfg["digest_mix"],
            cfg["min_score"],
            cfg=cfg,
        )
        wanted = [r for mk in MARKETS for r in sl[mk]]
        missing = [r for r in wanted if r["startup_id"] not in written]
        if not missing or turn == max_swaps:
            break
        r = missing[0]
        extra = cards_mod.write_cards(conn, [(r, r["market"])], run_date, new_round)
        ingest(extra)
        swaps.append(
            {
                "startup_id": r["startup_id"],
                "card_ids": extra["card_ids"],
                "failed": extra["failed"],
            }
        )
    # Only researched, successful cards may be in the final digest.
    final_sl = shortlist(
        [pool[sid] for sid in written if sid not in failed],
        cfg["digest_mix"],
        cfg["min_score"],
        cfg=cfg,
    )
    final = []
    kept = set()
    for mk in MARKETS:
        for row in final_sl[mk]:
            c = written[row["startup_id"]]
            d = c["data_json"]
            d.update(
                market=mk,
                fill=row["fill"],
                fill_reason=row["fill_reason"],
                diversity_relaxed=row["diversity_relaxed"],
            )
            if c["market"] != mk:
                d["one_liner"] = d["scores_snapshot"][CONCEPT[mk]]
            f = cards_mod.facts_for(conn, c["startup_id"])
            body = cards_mod.card_text(f, d, c["model_id"], today_dhaka())
            conn.execute(
                "update cards set market=%s,body=%s,data_json=%s where id=%s",
                (mk, body, json.dumps(d), c["id"]),
            )
            kept.add(c["id"])
            final.append(
                {
                    "card_id": c["id"],
                    "market": mk,
                    "fill": row["fill"],
                    "score": row[SHORT[mk]],
                }
            )
    for c in written.values():
        if c["id"] not in kept:
            conn.execute(
                "update cards set data_json=data_json || '{\"dropped_after_research\":true}'::jsonb where id=%s",
                (c["id"],),
            )
    # Explore selection must share the FINAL picks' diversity counters. Update candidate scores from research.
    # Build explore against the actual final picks, not a stale pre-research shortlist.
    from scout.score import exploration

    explore = exploration(
        list(pool.values()),
        [
            dict(pool[written_sid], market=written[written_sid]["market"])
            for written_sid in written
            if written[written_sid]["id"] in kept
        ],
        cfg,
    )
    return final, swaps, cr["failed"], explore


def health_notes(conn) -> list[str]:
    """Sources failing 3 days in a row or empty for 7 days, mentioned once (AGENTS.md section 6)."""
    from scout import health

    return health.digest_notes(conn)


def run_pipeline(
    conn, *, skip_collect=False, limit=None, run_date=None, candidate_ids=None
):
    run_date = run_date or today_dhaka()
    rid = repo.start_run(conn, "pipeline")
    started = conn.execute(
        "select started_at from runs where id=%s", (rid,)
    ).fetchone()["started_at"]
    cfg = effective(conn)
    t = time.time()
    stats = {"run_date": str(run_date)}
    from scout.llm import run as ai

    trips = ai.google_trips()
    try:
        if not skip_collect:
            stats["collect"] = collect(conn)
        ex = extract(conn)
        stats["extract"] = ex["stats"]
        from scout.extract import BATCH

        llm_batches = -(-ex["stats"]["sent_to_llm"] // BATCH)
        if llm_batches and ex["stats"]["failed_batches"] * 2 > llm_batches:
            # Unread news stays pending; the retry (or tomorrow's run) reads it. Scoring a fraction is worse.
            raise RuntimeError("Most extraction batches failed")
        m = merge(conn, ex["facts"])
        stats["merge"] = {k: len(v) if isinstance(v, set) else v for k, v in m.items()}
        if candidate_ids is None:
            # A retry after a failure finds the day's items already extracted: include every startup touched
            # since the first attempt (within the last 12 hours), so the retry scores the same day's news.
            earlier = conn.execute(
                """select s.id from startups s where s.last_seen_at >= (
                     select min(started_at) from runs where kind='pipeline' and started_at > now() - interval '12 hours')""",
            ).fetchall()
            touched = set(m["touched"]) | {r["id"] for r in earlier}
        ids = (
            candidate_ids
            if candidate_ids is not None
            else candidates(conn, touched, m["new_round"])
        )
        if not ids and os.environ.get("DRY_RUN") == "1":
            ids = [
                r["id"]
                for r in conn.execute(
                    "select id from startups order by last_seen_at desc limit 300"
                ).fetchall()
            ]
        stats["candidates"] = len(ids)
        print(f"Classifying {len(ids)} candidates", flush=True)
        classified, stats["classify"] = classify(conn, ids)
        batches = -(-len(ids) // 20)
        if batches and stats["classify"]["failed_batches"] * 2 > batches:
            # A digest built from a fraction of the day's startups is worse than the tick's retry.
            raise RuntimeError("Most classification batches failed")
        pool_cfg = dict(cfg["pool"])
        if limit is not None:
            pool_cfg["max_to_score"] = limit
        selected, stats["pool"] = balance(classified, pool_cfg)
        print("Balanced pool: " + json.dumps(stats["pool"]), flush=True)
        stats["score"] = score(conn, [r["startup_id"] for r in selected], run_date)
        print("Scoring: " + json.dumps(stats["score"]), flush=True)
        if selected and not stats["score"]["scored"]:
            raise RuntimeError("All scoring batches failed")
        # Keep behavioural taste nudges, never alter displayed opportunity scores.
        from scout import taste

        stats["taste"] = taste.apply(conn, run_date)
        from scout.novelty import apply

        rows = apply(conn, todays_rows(conn, run_date), cfg)
        for row in rows:
            conn.execute(
                "update scores set scores_json=scores_json || %s::jsonb where id=%s",
                (
                    json.dumps({"novelty_blocked": row.get("novelty_blocked", False)}),
                    row["score_id"],
                ),
            )
        sl = shortlist(rows, cfg["digest_mix"], cfg["min_score"], cfg=cfg)
        picks = [(r, mk) for mk in MARKETS for r in sl[mk]]
        print(f"Writing {len(picks)} researched cards", flush=True)
        cr = cards_mod.write_cards(conn, picks, run_date, m["new_round"])
        final, swaps, failed, explore = _check_and_swap(
            conn,
            cr,
            picks,
            rows,
            run_date,
            m["new_round"],
            cfg,
            cfg["quality"]["max_research_swaps"],
        )
        if picks and not final:
            raise RuntimeError("No researched cards passed")
        stats.update(cards=len(final), card_failures=failed, research_swaps=swaps)
        st = stats["classify"]
        d = digest_mod.build(
            conn,
            run_date,
            [(c["card_id"], c["market"]) for c in final],
            {
                "found": len(ids),
                "scored": stats["score"]["scored"],
                "skipped": st["skipped"],
                "skip_reasons": st["skip_reasons"],
                "watch_fills": sum(
                    c["fill"] and c["score"] < cfg["min_score"] for c in final
                ),
            },
            notes=health_notes(conn),
            funding_since=started,
            explore=[r["startup_id"] for r in explore],
        )
        stats["digest_id"] = d["id"]
        if os.environ.get("DRY_RUN") != "1":
            from scout import watches

            stats["alerts"] = watches.check(conn, started)
        stats["items"] = stats.get("collect", {}).get(
            "new_items", ex["stats"]["pending"]
        )
        stats["seconds"] = round(time.time() - t)
        repo.finish_run(conn, rid, "ok", stats)
        from scout import audit

        stats["audit"] = audit.audit(conn, run_date)["summary"]
        repo.finish_run(conn, rid, "ok", stats)
        return stats
    except Exception as exc:
        # The scheduler keeps retrying through the morning only when Google itself could not be reached.
        error = (
            "GoogleUnreachable"
            if ai.google_trips() > trips or ai.google_unreachable()
            else type(exc).__name__
        )
        stats["google_unreachable"] = error == "GoogleUnreachable"
        repo.finish_run(conn, rid, "error", stats, error=error)
        raise


def print_dry(conn, run_date) -> None:
    d = conn.execute("select * from digests where date = %s", (run_date,)).fetchone()
    if not d:
        print("No digest for", run_date)
        return
    data = d["body_json"]
    print("=" * 70 + "\nDIGEST (WhatsApp list message)\n" + "=" * 70)
    print(data["body"])
    for s in data["sections"]:
        print(f"\n[{s['title']}]")
        for r in s["rows"]:
            print(f"  • {r['title']}  —  {r.get('description', '')}")
    print(f"\n(button: {data['button']})")
    for cid in d["card_ids"]:
        c = conn.execute("select body from cards where id = %s", (cid,)).fetchone()
        print("\n" + "=" * 70 + f"\nCARD {cid}\n" + "=" * 70)
        print(c["body"])
        print("\n[ Save ] [ Skip ] [ More ]")
    if d["card_ids"]:
        from scout.deepdive import deep_dive

        text, _ = deep_dive(conn, d["card_ids"][0])
        print("\nMORE (split into WhatsApp text messages)\n" + text)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--dry", action="store_true", help="print the digest and cards (never sends)"
    )
    p.add_argument(
        "--dev",
        action="store_true",
        help="use Flash-Lite instead of Hydrogen (saves quota)",
    )
    p.add_argument("--skip-collect", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="max startups to score")
    a = p.parse_args()
    if a.dev:
        os.environ["SCOUT_DEV"] = "1"
    os.environ.setdefault("DRY_RUN", "1" if a.dry else os.environ.get("DRY_RUN", "0"))
    with repo.connect() as conn:
        if a.dry:
            from psycopg import Rollback

            with conn.transaction():
                # Use an isolated historical date; the entire pipeline transaction is rolled back.
                from datetime import date

                test_date = date(2000, 1, 1)
                stats = run_pipeline(
                    conn, skip_collect=a.skip_collect, limit=a.limit, run_date=test_date
                )
                print(json.dumps(stats, indent=1, default=str))
                print_dry(conn, test_date)
                raise Rollback()
        else:
            stats = run_pipeline(conn, skip_collect=a.skip_collect, limit=a.limit)
            print(json.dumps(stats, indent=1, default=str))


if __name__ == "__main__":
    main()

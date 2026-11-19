"""The daily pipeline (CLAUDE.md section 8).

  python -m scout.run --dry            # full run, prints today's digest and cards (nothing sent)
  python -m scout.run --dry --dev      # same, but scoring/cards use Flash-Lite to save Hydrogen quota
  python -m scout.run --dry --skip-collect --limit 30
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from scout import cards as cards_mod
from scout import digest as digest_mod
from scout.collect import collect
from scout.db import repo
from scout.dedupe import candidates, merge
from scout.extract import extract
from scout.score import score, shortlist, todays_rows
from scout.settings import effective

DHAKA = ZoneInfo("Asia/Dhaka")
MAX_TO_SCORE = 120  # keeps Hydrogen inside its daily allowance (12 batches of 10)


def today_dhaka():
    return datetime.now(DHAKA).date()


def _priority_order(conn, ids: list[int]) -> list[int]:
    """High-priority sources (YC/US) first, then buzz (number of sources)."""
    rows = conn.execute(
        """select si.startup_id, count(distinct i.source_id) as n,
             bool_or(src.region = 'usa' or i.kind = 'yc') as us
           from startup_items si join items i on i.id = si.item_id join sources src on src.id = i.source_id
           where si.startup_id = any(%s) group by si.startup_id""", (ids,)).fetchall()
    rank = {r["startup_id"]: (r["us"], r["n"]) for r in rows}
    return sorted(ids, key=lambda i: rank.get(i, (False, 0)), reverse=True)


def health_notes(conn) -> list[str]:
    """Sources failing 3 days in a row or empty for 7 days, mentioned once (CLAUDE.md section 6)."""
    from scout import health
    return health.digest_notes(conn)


def run_pipeline(conn, *, skip_collect: bool = False, limit: int | None = None, run_date=None) -> dict:
    run_date = run_date or today_dhaka()
    run_id = repo.start_run(conn, "pipeline")
    run_started = conn.execute("select started_at from runs where id = %s", (run_id,)).fetchone()["started_at"]
    stats: dict = {"run_date": str(run_date)}
    t0 = time.time()
    try:
        if not skip_collect:
            c = collect(conn)
            stats["collect"] = {k: v for k, v in c.items()}
        ex = extract(conn)
        stats["extract"] = ex["stats"]
        m = merge(conn, ex["facts"])
        stats["merge"] = {"touched": len(m["touched"]), "created": m["created"], "new_round": len(m["new_round"])}
        cands = _priority_order(conn, candidates(conn, m["touched"], m["new_round"]))
        cap = limit or MAX_TO_SCORE
        stats["candidates"] = len(cands)
        sc = score(conn, cands[:cap], run_date)
        stats["score"] = sc
        try:  # taste learning: never more than ±1, only after 3+ Save/Skip signals
            from scout import taste
            stats["taste"] = taste.apply(conn, run_date)
        except Exception as e:
            stats["taste"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
        cfg = effective(conn)
        rows = todays_rows(conn, run_date)
        div = cfg.get("diversity", {})
        sl = shortlist(rows, cfg["digest_mix"], cfg["min_score"], min_feasibility=cfg.get("min_feasibility", 0),
                       max_ai_native=div.get("max_ai_native_picks"), explore_rows=div.get("explore_rows", 0),
                       explore_min_score=div.get("explore_min_score", 5))
        picks = [(r, "bangladesh") for r in sl["bangladesh"]] + [(r, "global") for r in sl["global"]]
        cr = cards_mod.write_cards(conn, picks, run_date, m["new_round"])
        # if a card failed, use a reserve pick for the same market
        if cr["failed"] and sl["reserve"]:
            market_of = {r["startup_id"]: mk for r, mk in picks}
            reserve = [(r, market_of.get(sid, "bangladesh"))
                       for r, (sid, _) in zip(sl["reserve"], cr["failed"])]
            extra = cards_mod.write_cards(conn, reserve, run_date, m["new_round"])
            cr["card_ids"] += extra["card_ids"]
            cr["models"] += extra["models"]
        stats["cards"] = len(cr["card_ids"])
        stats["card_models"] = cr["models"]
        stats["card_failures"] = cr["failed"]
        card_market = {r["id"]: r["market"] for r in conn.execute(
            "select id, market from cards where id = any(%s) order by id", (cr["card_ids"],)).fetchall()}
        ordered = sorted(cr["card_ids"], key=lambda c: (card_market[c] != "bangladesh", c))
        d = digest_mod.build(conn, run_date, [(cid, card_market[cid]) for cid in ordered],
                             {"found": len(cands), "scored": sc["scored"], "skipped": sc["skipped"],
                              "skip_reasons": sc["skip_reasons"]},
                             notes=health_notes(conn), funding_since=run_started,
                             explore=[r["startup_id"] for r in sl["explore"]])
        stats["digest_id"] = d["id"]
        try:  # watches and saved-idea alerts, only on change (queued if the window is closed)
            from scout import watches
            stats["alerts"] = watches.check(conn, run_started)
        except Exception as e:
            stats["alerts"] = f"error: {type(e).__name__}: {str(e)[:120]}"
        stats["items"] = stats.get("collect", {}).get("new_items", ex["stats"]["pending"])
        stats["seconds"] = round(time.time() - t0)
        repo.finish_run(conn, run_id, "ok", stats)
        return stats
    except Exception as e:
        stats["seconds"] = round(time.time() - t0)
        repo.finish_run(conn, run_id, "error", stats, error=f"{type(e).__name__}: {str(e)[:300]}")
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


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry", action="store_true", help="print the digest and cards (never sends)")
    p.add_argument("--dev", action="store_true", help="use Flash-Lite instead of Hydrogen (saves quota)")
    p.add_argument("--skip-collect", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="max startups to score")
    a = p.parse_args()
    if a.dev:
        os.environ["SCOUT_DEV"] = "1"
    os.environ.setdefault("DRY_RUN", "1" if a.dry else os.environ.get("DRY_RUN", "0"))
    with repo.connect() as conn:
        stats = run_pipeline(conn, skip_collect=a.skip_collect, limit=a.limit)
        print(json.dumps(stats, indent=1, default=str))
        if a.dry:
            print_dry(conn, today_dhaka())


if __name__ == "__main__":
    main()

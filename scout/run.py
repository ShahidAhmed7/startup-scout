"""The daily pipeline (AGENTS.md section 8).

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


def _after(conn, card_id: int, market: str) -> int | None:
    d = conn.execute("select data_json from cards where id = %s", (card_id,)).fetchone()["data_json"] or {}
    v = (d.get("score_check") or {}).get(market)
    return int(v) if isinstance(v, (int, float)) else None


def _check_and_swap(conn, cr, picks, reserve, run_date, new_round, cfg, max_swaps):
    """After research: drop picks whose after-research score for their market fell below min_score (or whose card
    failed), and fill each gap from the reserve for the same market, at most `max_swaps` extra cards."""
    from scout.score import research_check

    market_of = {r["startup_id"]: mk for r, mk in picks}
    rows = conn.execute("select id, market from cards where id = any(%s) order by id", (cr["card_ids"],)).fetchall()
    cards = [{"card_id": r["id"], "market": r["market"], "after": _after(conn, r["id"], r["market"])} for r in rows]
    keep, drop = research_check(cards, cfg["min_score"])
    gaps = [c["market"] for c in drop] + [market_of.get(sid, "bangladesh") for sid, _ in cr["failed"]]
    swaps = []
    pool = list(reserve)
    for market in gaps:
        if len(swaps) >= max_swaps:
            break
        key = "bd" if market == "bangladesh" else "gl"
        cand = next((r for r in pool if r[key] >= cfg["min_score"]), None)
        if cand is None:
            continue
        pool.remove(cand)
        extra = cards_mod.write_cards(conn, [(cand, market)], run_date, new_round)
        for cid in extra["card_ids"]:
            c = {"card_id": cid, "market": market, "after": _after(conn, cid, market)}
            ok, _ = research_check([c], cfg["min_score"])
            swaps.append({"in": cid, "kept": bool(ok)})
            keep += ok
    for c in drop:  # keep the card in the archive, marked as dropped after research
        conn.execute("update cards set data_json = data_json || '{\"dropped_after_research\": true}'::jsonb "
                     "where id = %s", (c["card_id"],))
    keep, _ = research_check(keep, cfg["min_score"])  # re-sort by after-research score
    return keep, swaps, cr["failed"]


def health_notes(conn) -> list[str]:
    """Sources failing 3 days in a row or empty for 7 days, mentioned once (AGENTS.md section 6)."""
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
        q = cfg.get("quality", {})
        sl = shortlist(rows, cfg["digest_mix"], cfg["min_score"], min_feasibility=cfg.get("min_feasibility", 0),
                       max_ai_native=div.get("max_ai_native"), explore_rows=div.get("explore_rows", 0),
                       explore_min_score=div.get("explore_min_score", 5),
                       low_confidence_penalty=q.get("low_confidence_penalty", 0.0))
        picks = [(r, "bangladesh") for r in sl["bangladesh"]] + [(r, "global") for r in sl["global"]]
        cr = cards_mod.write_cards(conn, picks, run_date, m["new_round"])
        final, swaps, failed = _check_and_swap(conn, cr, picks, sl["reserve"], run_date, m["new_round"], cfg,
                                               q.get("max_research_swaps", 3))
        stats["cards"] = len(final)
        stats["card_failures"] = failed
        stats["research_swaps"] = swaps
        ordered = [c["card_id"] for c in final]
        card_market = {c["card_id"]: c["market"] for c in final}
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
        try:  # quality audit by Flash-Lite + code checks (owner's rule checker); never blocks the digest
            from scout import audit
            stats["audit"] = audit.audit(conn, run_date)["summary"]
        except Exception as e:
            stats["audit"] = f"error: {type(e).__name__}"
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

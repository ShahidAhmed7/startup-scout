"""End-to-end self-tests that use real services (run by Claude; the owner doesn't need these).

  python -m scout.selftest qwen-digest   # phase 3 "done when": force a Gemini failure, build a digest via Qwen.
                                         # Uses a past TEST date, prints the result, then deletes the test rows.
"""
from __future__ import annotations

import os
import sys
from datetime import date

TEST_DATE = date(2026, 11, 1)


def qwen_digest(conn, n: int = 4) -> bool:
    from scout import cards as cards_mod
    from scout import digest as digest_mod
    from scout.run import print_dry
    from scout.score import score, shortlist, todays_rows

    os.environ["FORCE_GEMINI_FAIL"] = "1"
    try:
        ids = [r["startup_id"] for r in conn.execute(
            """select startup_id from scores where not skip and run_date <> %s
               order by greatest(rank_bd, rank_global) desc limit %s""", (TEST_DATE, n)).fetchall()]
        sc = score(conn, ids, TEST_DATE)
        print("scoring via:", sc["models"], "| scored", sc["scored"])
        rows = todays_rows(conn, TEST_DATE)
        sl = shortlist(rows, {"bangladesh": 1, "global": 1}, min_score=1)
        picks = [(r, "bangladesh") for r in sl["bangladesh"]] + [(r, "global") for r in sl["global"]]
        cr = cards_mod.write_cards(conn, picks, TEST_DATE, set())
        print("cards via:", cr["models"], "| failures:", cr["failed"])
        market = {r["id"]: r["market"] for r in conn.execute("select id, market from cards where id = any(%s)",
                                                             (cr["card_ids"],)).fetchall()}
        digest_mod.build(conn, TEST_DATE, [(c, market[c]) for c in cr["card_ids"]],
                         {"found": len(ids), "scored": sc["scored"], "skipped": sc["skipped"],
                          "skip_reasons": sc["skip_reasons"]})
        print_dry(conn, TEST_DATE)
        models = set(sc["models"]) | set(cr["models"])
        ok = bool(cr["card_ids"]) and all(m.startswith("Qwen/") for m in models)
        print("\nPASS: digest built entirely by Qwen" if ok else f"\nFAIL: models used {models}")
        return ok
    finally:
        os.environ.pop("FORCE_GEMINI_FAIL", None)
        conn.execute("delete from digests where date = %s", (TEST_DATE,))
        conn.execute("delete from cards where run_date = %s", (TEST_DATE,))
        conn.execute("delete from skipped where created_at > now() - interval '1 hour' and startup_id in "
                     "(select startup_id from scores where run_date = %s)", (TEST_DATE,))
        conn.execute("delete from scores where run_date = %s", (TEST_DATE,))


def variety(conn, dev: bool = True) -> None:
    """Re-score today's startups on TEST_DATE with the current rules and show picks + explore rows (no cards)."""
    from collections import Counter

    from scout.score import score, shortlist, todays_rows
    from scout.settings import effective

    if dev:
        os.environ["SCOUT_DEV"] = "1"
    try:
        ids = [r["startup_id"] for r in conn.execute(
            "select distinct startup_id from scores where run_date <> %s", (TEST_DATE,)).fetchall()]
        st = score(conn, ids, TEST_DATE)
        print("scored:", st["scored"], "skipped:", st["skipped"], "via", st["models"])
        rows = todays_rows(conn, TEST_DATE)
        cfg = effective(conn)
        div = cfg["diversity"]
        sl = shortlist(rows, cfg["digest_mix"], cfg["min_score"], min_feasibility=cfg["min_feasibility"],
                       max_ai_native=div["max_ai_native"], explore_rows=div["explore_rows"],
                       explore_min_score=div["explore_min_score"])
        names = {r["id"]: r["name"] for r in conn.execute("select id, name from startups where id = any(%s)",
                                                         ([r["startup_id"] for r in rows],)).fetchall()}
        print("AI-native share of scored (not skipped):", sum(r["ai_native"] for r in rows), "/", len(rows))
        print("sectors:", Counter(r["sector"] for r in rows).most_common(12))
        for label in ("bangladesh", "global", "explore"):
            print(f"\n[{label}]")
            for r in sl[label]:
                print(f"  {names[r['startup_id']][:24]:24} {r['sector'][:30]:30} AI={'yes' if r['ai_native'] else 'no '} "
                      f"BD {r['bd']} GL {r['gl']} F {r['feas']}")
    finally:
        os.environ.pop("SCOUT_DEV", None)
        conn.execute("delete from skipped where created_at > now() - interval '1 hour' and startup_id in "
                     "(select startup_id from scores where run_date = %s)", (TEST_DATE,))
        conn.execute("delete from scores where run_date = %s", (TEST_DATE,))


if __name__ == "__main__":
    if sys.argv[1:] in (["variety"], ["variety", "--hydrogen"]):
        from scout.db import repo

        with repo.connect() as c:
            variety(c, dev="--hydrogen" not in sys.argv)
        raise SystemExit(0)
    from scout.db import repo

    if sys.argv[1:] == ["qwen-digest"]:
        with repo.connect() as c:
            raise SystemExit(0 if qwen_digest(c) else 1)
    print(__doc__)

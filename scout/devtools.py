"""Developer helpers.

  python -m scout.devtools reset   # wipe pipeline data (items, startups, scores, cards, digests...) for a clean
                                   # re-run. Keeps usage (quota tracking), search cache, messages and settings.
"""
import sys

from scout.db import repo

PIPELINE_TABLES = ["signals", "notes", "assumptions", "ideas", "cards", "digests", "skipped", "scores", "rounds",
                   "startup_items", "startups", "items", "yc_seen", "lane_seen"]


def reset(conn) -> None:
    conn.execute("truncate " + ", ".join(PIPELINE_TABLES) + " restart identity cascade")
    conn.execute("update sources set etag = null, last_modified = null, fail_count = 0")
    conn.execute("update owner_state set last_opened_card_id = null")


if __name__ == "__main__":
    if sys.argv[1:] == ["reset"]:
        with repo.connect() as c:
            reset(c)
        print("Pipeline data cleared (usage, search cache, messages and settings kept).")
    else:
        print(__doc__)

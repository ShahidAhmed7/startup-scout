"""Source health (CLAUDE.md section 6): a source that fails 3 runs in a row, or returns nothing for 7 days,
is mentioned once in the next digest. When it recovers, it can be mentioned again later."""
from __future__ import annotations


def digest_notes(conn) -> list[str]:
    conn.execute("""update sources set notified_at = null
                    where notified_at is not null and fail_count = 0
                      and last_nonempty_at > now() - interval '7 days'""")
    rows = conn.execute(
        """select id, name, fail_count, last_nonempty_at from sources
           where status = 'active' and notified_at is null and (
             fail_count >= 3 or
             (coalesce(last_nonempty_at, first_seen_at) < now() - interval '7 days'))
           order by name""").fetchall()
    notes = []
    for r in rows:
        if r["fail_count"] >= 3:
            notes.append(f"{r['name']} feed has failed {r['fail_count']} days in a row.")
        else:
            notes.append(f"{r['name']} has had no new items for 7 days.")
        conn.execute("update sources set notified_at = now() where id = %s", (r["id"],))
    if len(notes) > 3:
        notes = notes[:3] + [f"…and {len(notes) - 3} more sources (see python -m scout.status)."]
    return notes


def down_sources(conn) -> list[dict]:
    return conn.execute(
        """select name, fail_count, last_ok_at, last_nonempty_at from sources
           where status = 'active' and (fail_count >= 3 or
             coalesce(last_nonempty_at, first_seen_at) < now() - interval '7 days') order by name""").fetchall()

"""Skip close matches to cards actually sent in the preceding novelty window."""

import json
from scout import taste


def apply(conn, rows, cfg):
    div = cfg["diversity"]
    days = div.get("novelty_days", 30)
    if not rows:
        return rows
    try:
        old = conn.execute(
            """select distinct s.id,s.embedding::text emb from startups s join cards c on c.startup_id=s.id
            join digests d on c.id=any(d.card_ids) where d.status='sent' and d.sent_at>now()-make_interval(days=>%s)""",
            (days,),
        ).fetchall()
        if not old:
            return rows
        taste.ensure_embeddings(
            conn,
            [r["startup_id"] for r in rows] + [r["id"] for r in old if not r["emb"]],
        )
        vectors = {
            r["id"]: json.loads(r["emb"])
            for r in conn.execute(
                "select id,embedding::text emb from startups where embedding is not null"
            ).fetchall()
        }
        history = [vectors[r["id"]] for r in old if r["id"] in vectors]
        return [
            dict(
                r,
                novelty_blocked=bool(
                    r["startup_id"] in vectors
                    and any(
                        taste._cos(vectors[r["startup_id"]], v)
                        >= div.get("novelty_similarity", 0.85)
                        for v in history
                    )
                ),
            )
            for r in rows
        ]
    except Exception:
        return rows  # embedding availability is explicitly optional

"""Step 3: merge duplicates. Same company (website domain, else normalised name) = one startup with
many sources. A startup already sent on a card is only sent again if it raised a new round."""
from __future__ import annotations

from datetime import datetime

from scout.normalize import clean_text, domain_of, norm_name, short_country


def upsert_startup(conn, c: dict) -> tuple[int, bool]:
    name = clean_text(c["name"], 120)
    c = dict(c, country=short_country(c.get("country")) or "")
    nn = norm_name(name)
    dom = domain_of(c.get("website"))
    row = None
    if dom:
        row = conn.execute("select * from startups where domain = %s", (dom,)).fetchone()
    if not row and nn:
        row = conn.execute("select * from startups where norm_name = %s order by id limit 1", (nn,)).fetchone()
    if row:
        conn.execute(
            """update startups set domain = coalesce(domain, %s), website = coalesce(website, %s),
                 country = coalesce(nullif(country, ''), %s), description = coalesce(nullif(description, ''), %s),
                 yc_batch = coalesce(yc_batch, %s), last_seen_at = now() where id = %s""",
            (dom if not _domain_taken(conn, dom, row["id"]) else None, c.get("website") or None,
             c.get("country") or None, c.get("what_they_do") or None, c.get("yc_batch"), row["id"]))
        return row["id"], False
    new = conn.execute(
        """insert into startups (name, norm_name, domain, website, country, description, yc_batch)
           values (%s, %s, %s, %s, %s, %s, %s) returning id""",
        (name, nn, dom, c.get("website") or None, c.get("country") or None, c.get("what_they_do") or None,
         c.get("yc_batch"))).fetchone()
    return new["id"], True


def _domain_taken(conn, dom, startup_id) -> bool:
    if not dom:
        return True
    r = conn.execute("select id from startups where domain = %s and id <> %s", (dom, startup_id)).fetchone()
    return r is not None


def add_round(conn, startup_id: int, c: dict, source_url: str, announced: datetime | None) -> tuple[int, bool]:
    """One round per raise: a mention within 45 days with the same stage, or with no stage on either side,
    or with the same dollar amount, is the same round (we fill in whatever was missing)."""
    stage = (c.get("stage") or "").strip()
    amount = c.get("amount_usd") or None
    amount = amount if amount and amount > 0 else None
    row = conn.execute(
        """select * from rounds where startup_id = %s and created_at > now() - interval '45 days'
             and (lower(coalesce(stage, '')) = lower(%s) or coalesce(stage, '') = '' or %s = ''
                  or (amount_usd is not null and amount_usd = %s))
           order by id desc limit 1""", (startup_id, stage, stage, amount)).fetchone()
    if row:
        conn.execute(
            """update rounds set stage = coalesce(nullif(stage, ''), nullif(%s, '')),
                 amount_usd = coalesce(amount_usd, %s), amount_text = coalesce(amount_text, %s),
                 investors = case when cardinality(investors) = 0 then %s else investors end,
                 source_urls = case when %s = any(source_urls) then source_urls else array_append(source_urls, %s) end
               where id = %s""",
            (stage, amount, c.get("amount_text") or None, [i for i in (c.get("investors") or []) if i][:10],
             source_url, source_url, row["id"]))
        return row["id"], False
    new = conn.execute(
        """insert into rounds (startup_id, stage, amount_usd, amount_text, investors, announced_at, source_urls)
           values (%s, %s, %s, %s, %s, %s, %s) returning id""",
        (startup_id, stage, amount, c.get("amount_text") or None,
         [i for i in (c.get("investors") or []) if i][:10], announced.date() if announced else None,
         [source_url])).fetchone()
    return new["id"], True


def merge(conn, facts: list[tuple[dict, dict, str]]) -> dict:
    """Returns startup ids touched, plus those with a brand-new round."""
    touched: set[int] = set()
    new_round: set[int] = set()
    created_now: set[int] = set()
    for item, c, kind in facts:
        sid, is_new = upsert_startup(conn, c)
        if is_new:
            created_now.add(sid)
        touched.add(sid)
        conn.execute("insert into startup_items (startup_id, item_id) values (%s, %s) on conflict do nothing",
                     (sid, item["id"]))
        _, fresh = add_round(conn, sid, c, item["url"], item.get("published_at"))
        if fresh and sid not in created_now:
            new_round.add(sid)  # a startup we knew before this run raised again
    return {"touched": touched, "new_round": new_round, "created": len(created_now)}


def candidates(conn, touched: set[int], new_round: set[int]) -> list[int]:
    """Startups worth scoring today: never sent on a card and not scored in the last 7 days,
    or sent before but with a new round now."""
    if not touched:
        return []
    rows = conn.execute(
        """select s.id,
             exists (select 1 from cards c where c.startup_id = s.id and c.kind = 'card') as carded,
             exists (select 1 from scores sc where sc.startup_id = s.id
                     and sc.created_at > now() - interval '7 days') as scored
           from startups s where s.id = any(%s)""", (list(touched),)).fetchall()
    out = []
    for r in rows:
        if r["id"] in new_round and r["carded"]:
            out.append(r["id"])
        elif not r["carded"] and not r["scored"]:
            out.append(r["id"])
    return out

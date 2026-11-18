"""Step 9: the prepared 08:00 digest (one interactive list message) — CLAUDE.md section 10."""
from __future__ import annotations

import json
from datetime import date

from collections import Counter

from scout.normalize import region_of
from scout.whatsapp.formats import clip, list_payload, row_title, text_payload


def _row(conn, cid: int, market: str) -> dict:
    c = conn.execute(
        """select c.id, c.title, s.country, s.description, sc.scores_json, r.stage, r.amount_text
           from cards c join startups s on s.id = c.startup_id
           left join lateral (select * from scores where startup_id = s.id order by created_at desc limit 1) sc on true
           left join lateral (select * from rounds where startup_id = s.id order by created_at desc limit 1) r on true
           where c.id = %s""", (cid,)).fetchone()
    sj = c["scores_json"] or {}
    label = f"BD {sj.get('bangladesh', '?')}" if market == "bangladesh" else f"Global {sj.get('global', '?')}"
    round_txt = " ".join(x for x in [c["stage"], c["amount_text"]] if x) or "round unknown"
    what = (sj.get("search_phrase") or c["description"] or "unknown").rstrip(".")
    what = what[:1].upper() + what[1:]
    return {"id": f"card:{cid}", "title": row_title(c["title"], label),
            "description": clip(f"{clip(what, 38)} · {c['country'] or '?'} · {round_txt}", 72)}


def _money(usd) -> str:
    usd = float(usd)
    return f"${usd / 1e9:.1f}B" if usd >= 1e9 else f"${usd / 1e6:.0f}M" if usd >= 1e6 else f"${usd / 1e3:.0f}K"


def funding_snapshot(conn, since) -> str:
    """One line on what got funded worldwide since `since` (CLAUDE.md: the owner wants to see the world)."""
    rows = conn.execute(
        """select r.stage, r.amount_usd, s.name, s.country, s.category,
             (select src.region from startup_items si join items i on i.id = si.item_id
              join sources src on src.id = i.source_id where si.startup_id = s.id limit 1) as src_region
           from rounds r join startups s on s.id = r.startup_id where r.created_at >= %s""", (since,)).fetchall()
    yc = [r for r in rows if (r["stage"] or "").startswith("YC ")]
    rounds = [r for r in rows if not (r["stage"] or "").startswith("YC ")]
    if not rounds and not yc:
        return ""
    # country when the article states it, else the news source's region (rough, but good for a snapshot)
    src_map = {"usa": "USA", "india": "India", "pakistan": "Pakistan", "mideast": "Middle East", "africa": "Africa",
               "sea": "SE Asia", "china": "China", "bangladesh": "Bangladesh", "europe": "Europe"}
    regions = Counter(region_of(r["country"]) if r["country"] else src_map.get(r["src_region"] or "", "Unknown")
                      for r in rounds)
    regions.pop("Unknown", None)
    reg = ", ".join(f"{k} {v}" for k, v in regions.most_common(6))
    line = f"Funded worldwide: {len(rounds)} rounds" + (f" ({reg})" if reg else "")
    big = max((r for r in rounds if r["amount_usd"]), key=lambda r: r["amount_usd"], default=None)
    if big:
        line += f". Biggest: {big['name']} {_money(big['amount_usd'])}" + (f" ({big['country']})" if big["country"] else "")
    cats = Counter((r["category"] or "").lower() for r in rounds if r["category"])
    if cats:
        line += ". Busy: " + ", ".join(f"{k} {v}" for k, v in cats.most_common(3))
    if yc:
        line += f". Plus {len(yc)} new YC companies"
    return line + "."


def body_text(scored: int, skipped: int, skip_reasons: dict, k: int, notes: list[str], world: str = "") -> str:
    top = [r for r, _ in sorted(skip_reasons.items(), key=lambda x: -x[1])[:3]]
    skipped_txt = f"skipped {skipped} ({', '.join(top)})" if skipped and top else f"skipped {skipped}"
    if k:
        body = (f"Good morning. I read {scored} new startups overnight and {skipped_txt}. "
                f"Here are your {k} picks.")
    else:
        body = (f"Good morning. I read {scored} new startups overnight and {skipped_txt}. "
                f"None scored 6 or more today, so there are no picks.")
    if world:
        body += "\n\n" + world
    if notes:
        body += "\n\n" + "\n".join(notes)
    return clip(body, 1024)


def build(conn, run_date: date, picks: list[tuple[int, str]], stats: dict, notes: list[str] | None = None,
          funding_since=None) -> dict:
    """picks: [(card_id, market)]. Stores/updates the digest for run_date as 'prepared' (unless already sent)."""
    sections = [{"title": "For Bangladesh", "rows": [_row(conn, c, m) for c, m in picks if m == "bangladesh"]},
                {"title": "Global from Bangladesh", "rows": [_row(conn, c, m) for c, m in picks if m == "global"]}]
    sections = [s for s in sections if s["rows"]]
    world = funding_snapshot(conn, funding_since) if funding_since else ""
    body = body_text(stats.get("found", stats.get("scored", 0)), stats.get("skipped", 0),
                     stats.get("skip_reasons", {}), len(picks), notes or [], world)
    data = {"body": body, "button": "See picks", "sections": sections}
    existing = conn.execute("select id, status from digests where date = %s", (run_date,)).fetchone()
    if existing and existing["status"] == "sent":
        return {"id": existing["id"], "status": "sent", "data": data}
    row = conn.execute(
        """insert into digests (date, card_ids, body_json, status) values (%s, %s, %s, 'prepared')
           on conflict (date) do update set card_ids = excluded.card_ids, body_json = excluded.body_json,
             status = 'prepared' returning id""",
        (run_date, [c for c, _ in picks], json.dumps(data))).fetchone()
    return {"id": row["id"], "status": "prepared", "data": data}


def payload(to: str, data: dict) -> dict:
    if data["sections"]:
        return list_payload(to, data["body"], data["button"], data["sections"])
    return text_payload(to, data["body"])

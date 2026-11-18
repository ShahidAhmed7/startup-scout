"""Friday report (phase 3) and monthly report (phase 5). Delivered through the outbox, so they wait for the
24-hour window if it is closed."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from scout.db import repo
from scout.digest import _money
from scout.normalize import region_of
from scout.whatsapp import outbox
from scout.whatsapp.formats import clip, list_payload, row_title


def _week_funding(conn, since) -> str:
    rows = conn.execute(
        """select r.stage, r.amount_usd, s.name, s.country, s.category,
             (select src.region from startup_items si join items i on i.id = si.item_id
              join sources src on src.id = i.source_id where si.startup_id = s.id limit 1) as src_region
           from rounds r join startups s on s.id = r.startup_id where r.created_at >= %s""", (since,)).fetchall()
    rounds = [r for r in rows if not (r["stage"] or "").startswith("YC ")]
    if not rounds:
        return "No funding rounds recorded this week."
    src_map = {"usa": "USA", "india": "India", "pakistan": "Pakistan", "mideast": "Middle East", "africa": "Africa",
               "sea": "SE Asia", "china": "China", "bangladesh": "Bangladesh", "europe": "Europe"}
    regions = Counter(region_of(r["country"]) if r["country"] else src_map.get(r["src_region"] or "", "Unknown")
                      for r in rounds)
    regions.pop("Unknown", None)
    cats = Counter((r["category"] or "").lower() for r in rounds if r["category"])
    top = sorted((r for r in rounds if r["amount_usd"]), key=lambda r: -r["amount_usd"])[:3]
    lines = [f"{len(rounds)} rounds. By region: " + ", ".join(f"{k} {v}" for k, v in regions.most_common(8)) + "."]
    if cats:
        lines.append("Busiest sectors: " + ", ".join(f"{k} {v}" for k, v in cats.most_common(5)) + ".")
    if top:
        lines.append("Largest: " + "; ".join(f"{r['name']} {_money(r['amount_usd'])}" for r in top) + ".")
    return " ".join(lines)


def friday(conn) -> dict:
    """New YC companies this week (count, skipped by reason, 3 worth a look), one deep dive, your week."""
    rid = repo.start_run(conn, "friday")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    try:
        yc = conn.execute("select id from startups where yc_batch is not null and first_seen_at >= %s",
                          (since,)).fetchall()
        skipped = conn.execute(
            """select k.reason, count(*) as n from skipped k join startups s on s.id = k.startup_id
               where s.yc_batch is not null and k.created_at >= %s group by k.reason order by n desc""",
            (since,)).fetchall()
        picks = conn.execute(
            """select distinct on (s.id) s.id, s.name, s.yc_batch, s.description, sc.scores_json j, sc.reasons_json rj,
                 greatest(sc.rank_bd, sc.rank_global) as best
               from startups s join scores sc on sc.startup_id = s.id
               where s.yc_batch is not null and s.first_seen_at >= %s and not sc.skip
                 and (sc.scores_json->>'feasibility')::int >= 5
               order by s.id, sc.created_at desc""", (since,)).fetchall()
        picks = sorted(picks, key=lambda r: -float(r["best"]))[:3]
        signals = {r["kind"]: r["n"] for r in conn.execute(
            "select kind, count(*) as n from signals where created_at >= %s group by kind", (since,)).fetchall()}
        digests = conn.execute("select count(*) as n from digests where status = 'sent' and sent_at >= %s",
                               (since,)).fetchone()["n"]

        lines = ["*Friday report*", ""]
        sk = ", ".join(f"{r['reason']} {r['n']}" for r in skipped[:4])
        lines.append(f"New YC companies this week: {len(yc)}." + (f" Skipped {sum(r['n'] for r in skipped)} ({sk})." if skipped else ""))
        if picks:
            lines += ["", "*3 worth a look*"]
            for n, p in enumerate(picks, 1):
                j, rj = p["j"], p["rj"]
                lines.append(f"{n}. {p['name']} ({p['yc_batch']}): {clip(p['description'] or 'unknown', 90)}")
                lines.append(f"   BD {j['bangladesh']} · Global {j['global']} · Feasibility {j['feasibility']}. "
                             f"{clip(rj.get('global') if j['global'] >= j['bangladesh'] else rj.get('bangladesh'), 140)}")
        lines += ["", "*Funded worldwide this week*", _week_funding(conn, since)]
        lines += ["", "*Your week*",
                  f"{digests} digests, {signals.get('save', 0)} saved, {signals.get('skip', 0)} skipped, "
                  f"{signals.get('more', 0)} deep dives."]
        payloads = outbox.text("\n".join(lines))

        deep_text = None
        if picks:
            deep_text = _deep_dive_for(conn, picks[0]["id"])
        if deep_text:
            payloads += outbox.text("*Friday deep dive*\n\n" + deep_text)
        # quick access to the picks' cards
        rows = []
        for p in picks:
            c = conn.execute("select id from cards where startup_id = %s and kind = 'card' order by id desc limit 1",
                             (p["id"],)).fetchone()
            if c:
                rows.append({"id": f"card:{c['id']}", "title": row_title(p["name"], f"G {p['j']['global']}"),
                             "description": clip(p["description"] or "", 72)})
        if rows:
            payloads.append(list_payload("", "Open a card:", "See cards", [{"title": "YC this week", "rows": rows}]))
        sent = outbox.deliver(conn, payloads, "friday_report")
        stats = {"yc": len(yc), "picks": [p["name"] for p in picks], "sent_now": sent}
        repo.finish_run(conn, rid, "ok", stats)
        return stats
    except Exception as e:
        repo.finish_run(conn, rid, "error", error=f"{type(e).__name__}: {str(e)[:300]}")
        raise


def _deep_dive_for(conn, startup_id: int) -> str | None:
    """Deep dive on a startup; writes its card first if it has none (Hydrogen, 'friday' job)."""
    from scout import cards as cards_mod
    from scout.deepdive import deep_dive
    from scout.run import today_dhaka

    c = conn.execute("select id from cards where startup_id = %s and kind = 'card' order by id desc limit 1",
                     (startup_id,)).fetchone()
    try:
        if not c:
            row = {"startup_id": startup_id}
            res = cards_mod.write_cards(conn, [(row, "global")], today_dhaka(), set(), job="friday")
            if not res["card_ids"]:
                return None
            card_id = res["card_ids"][0]
        else:
            card_id = c["id"]
        text, _ = deep_dive(conn, card_id)
        return text
    except Exception:
        return None


def monthly(conn) -> dict:
    rid = repo.start_run(conn, "monthly")
    repo.finish_run(conn, rid, "ok", {"note": "built in phase 5"})
    return {}

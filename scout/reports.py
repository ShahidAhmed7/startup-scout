"""Friday report (phase 3) and monthly report (phase 5). Delivered through the outbox, so they wait for the
24-hour window if it is closed."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from scout.db import repo
from scout.digest import _money
from scout.score import half_up
from scout.normalize import region_of
from scout.whatsapp import outbox
from scout.whatsapp.formats import clip, list_payload, row_title


def _week_funding(conn, since) -> str:
    rows = conn.execute(
        """select r.stage, r.amount_usd, s.name, s.country, s.category,
             (select src.region from startup_items si join items i on i.id = si.item_id
              join sources src on src.id = i.source_id where si.startup_id = s.id limit 1) as src_region
           from rounds r join startups s on s.id = r.startup_id where r.created_at >= %s""",
        (since,),
    ).fetchall()
    rounds = [r for r in rows if not (r["stage"] or "").startswith("YC ")]
    if not rounds:
        return "No funding rounds recorded this week."
    src_map = {
        "usa": "USA",
        "india": "India",
        "pakistan": "Pakistan",
        "mideast": "Middle East",
        "africa": "Africa",
        "sea": "SE Asia",
        "china": "China",
        "bangladesh": "Bangladesh",
        "europe": "Europe",
    }
    regions = Counter(
        region_of(r["country"])
        if r["country"]
        else src_map.get(r["src_region"] or "", "Unknown")
        for r in rounds
    )
    regions.pop("Unknown", None)
    cats = Counter(
        (r["category"] or "").lower()
        for r in rounds
        if r["category"] and r["category"].lower() not in ("unknown", "other")
    )
    top = sorted(
        (r for r in rounds if r["amount_usd"]), key=lambda r: -r["amount_usd"]
    )[:3]
    lines = [
        f"{len(rounds)} rounds. By region: "
        + ", ".join(f"{k} {v}" for k, v in regions.most_common(8))
        + "."
    ]
    if cats:
        lines.append(
            "Busiest sectors: "
            + ", ".join(f"{k} {v}" for k, v in cats.most_common(5))
            + "."
        )
    if top:
        lines.append(
            "Largest: "
            + "; ".join(f"{r['name']} {_money(r['amount_usd'])}" for r in top)
            + "."
        )
    return " ".join(lines)


def friday(conn) -> dict:
    """New YC companies this week (count, skipped by reason, 3 worth a look), one deep dive, your week."""
    rid = repo.start_run(conn, "friday")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    try:
        yc = conn.execute(
            "select id from startups where yc_batch is not null and first_seen_at >= %s",
            (since,),
        ).fetchall()
        skipped = conn.execute(
            """select k.reason, count(*) as n from skipped k join startups s on s.id = k.startup_id
               where s.yc_batch is not null and k.created_at >= %s group by k.reason order by n desc""",
            (since,),
        ).fetchall()
        picks = conn.execute(
            """select distinct on (s.id) s.id, s.name, s.yc_batch, s.description, sc.scores_json j, sc.reasons_json rj,
                 greatest(sc.rank_bd, sc.rank_global) as best
               from startups s join scores sc on sc.startup_id = s.id
               where s.yc_batch is not null and s.first_seen_at >= %s and not sc.skip
                 and greatest((sc.scores_json->>'bangladesh')::numeric,(sc.scores_json->>'global')::numeric)>=5
               order by s.id, sc.created_at desc""",
            (since,),
        ).fetchall()
        picks = sorted(picks, key=lambda r: -float(r["best"]))[:3]
        signals = {
            r["kind"]: r["n"]
            for r in conn.execute(
                "select kind, count(*) as n from signals where created_at >= %s group by kind",
                (since,),
            ).fetchall()
        }
        digests = conn.execute(
            "select count(*) as n from digests where status = 'sent' and sent_at >= %s",
            (since,),
        ).fetchone()["n"]

        lines = ["*Friday report*", ""]
        sk = ", ".join(f"{r['reason']} {r['n']}" for r in skipped[:4])
        lines.append(
            f"New YC companies this week: {len(yc)}."
            + (f" Skipped {sum(r['n'] for r in skipped)} ({sk})." if skipped else "")
        )
        if picks:
            lines += ["", "*3 worth a look*"]
            for n, p in enumerate(picks, 1):
                j, rj = p["j"], p["rj"]
                lines.append(
                    f"{n}. {p['name']} ({p['yc_batch']}): {clip(p['description'] or 'unknown', 90)}"
                )
                lines.append(
                    f"   BD {half_up(j['bangladesh'])} · Global {half_up(j['global'])} · {j.get('what_it_takes', {}).get('capital', 'unknown')} capital. "
                    f"{clip(j.get('concept_global' if j['global'] >= j['bangladesh'] else 'concept_bd') or str(rj.get('global') or ''), 140)}"
                )
        lines += ["", "*Funded worldwide this week*", _week_funding(conn, since)]
        lines += [
            "",
            "*Your week*",
            f"{digests} digests, {signals.get('save', 0)} saved, {signals.get('skip', 0)} skipped, "
            f"{signals.get('more', 0)} deep dives.",
        ]
        payloads = outbox.text("\n".join(lines))

        deep_text = None
        if picks:
            deep_text = _deep_dive_for(conn, picks[0]["id"])
        if deep_text:
            payloads += outbox.text("*Friday deep dive*\n\n" + deep_text)
        # quick access to the picks' cards
        rows = []
        for p in picks:
            c = conn.execute(
                "select id from cards where startup_id = %s and kind = 'card' order by id desc limit 1",
                (p["id"],),
            ).fetchone()
            if c:
                rows.append(
                    {
                        "id": f"card:{c['id']}",
                        "title": row_title(p["name"], f"G {p['j']['global']}"),
                        "description": clip(p["description"] or "", 72),
                    }
                )
        if rows:
            payloads.append(
                list_payload(
                    "",
                    "Open a card:",
                    "See cards",
                    [{"title": "YC this week", "rows": rows}],
                )
            )
        sent = outbox.deliver(conn, payloads, "friday_report")
        try:  # weekly re-check of saved ideas: alert on new names in the Bangladesh search
            from scout import watches

            new_comp = watches.competitor_recheck(conn)
        except Exception:
            new_comp = 0
        stats = {
            "yc": len(yc),
            "picks": [p["name"] for p in picks],
            "sent_now": sent,
            "competitor_alerts": new_comp,
        }
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

    c = conn.execute(
        "select id from cards where startup_id = %s and kind = 'card' order by id desc limit 1",
        (startup_id,),
    ).fetchone()
    try:
        if not c:
            row = {"startup_id": startup_id}
            res = cards_mod.write_cards(
                conn, [(row, "global")], today_dhaka(), set(), job="friday"
            )
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
    """Markets like ours, what the bot learned, 3 close-call skips, saved ideas re-checked, model usage."""
    from scout import budget
    from scout.config import config
    from scout.normalize import short_country

    rid = repo.start_run(conn, "monthly")
    since = datetime.now(timezone.utc) - timedelta(days=30)
    cfg = config()
    try:
        month = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%B %Y")
        lines = [f"*Monthly report: {month}*", ""]

        # 1. markets like ours
        ours = {m.lower() for m in cfg["markets_like_ours"]}
        rows = conn.execute(
            """select r.amount_usd, r.stage, s.name, s.country, s.category from rounds r
               join startups s on s.id = r.startup_id where r.created_at >= %s
               and coalesce(r.stage, '') not like 'YC %%'""",
            (since,),
        ).fetchall()
        local = [
            r
            for r in rows
            if (short_country(r["country"]) or "").lower() in ours
            or region_of(r["country"]).lower() in ours
        ]
        lines.append("*What got funded in markets like ours*")
        if local:
            by_c = Counter(short_country(r["country"]) for r in local)
            cats = Counter(
                (r["category"] or "").lower()
                for r in local
                if r["category"] and r["category"].lower() not in ("unknown", "other")
            )
            big = sorted(
                (r for r in local if r["amount_usd"]), key=lambda r: -r["amount_usd"]
            )[:3]
            lines.append(
                f"{len(local)} rounds: "
                + ", ".join(f"{k} {v}" for k, v in by_c.most_common(6))
                + "."
            )
            if cats:
                lines.append(
                    "Top themes: "
                    + ", ".join(f"{k} {v}" for k, v in cats.most_common(5))
                    + "."
                )
            if big:
                lines.append(
                    "Largest: "
                    + "; ".join(
                        f"{r['name']} {_money(r['amount_usd'])} ({r['country']})"
                        for r in big
                    )
                    + "."
                )
        else:
            lines.append("No rounds with a known country in these markets this month.")
        lines.append(f"Worldwide: {_week_funding(conn, since)}")

        balance = conn.execute(
            """select coalesce(s.region,'unknown') region,count(*) n,
            count(*) filter(where (sc.scores_json->>'ai_native')::boolean) ai from scores sc join startups s on s.id=sc.startup_id
            where sc.created_at>=%s and sc.scores_json->>'schema_version'='2' group by s.region""",
            (since,),
        ).fetchall()
        total = sum(r["n"] for r in balance)
        if total:
            lines.append(
                "Pool balance: "
                + ", ".join(f"{r['region']} {r['n'] / total:.0%}" for r in balance)
                + f"; AI-first {sum(r['ai'] for r in balance) / total:.0%}."
            )
        # 2. what the bot learned
        sig = conn.execute(
            """select g.kind, lower(coalesce(s.category, '')) as cat from signals g join cards c on c.id = g.card_id
               join startups s on s.id = c.startup_id where g.created_at >= %s and g.kind in ('save', 'skip')""",
            (since,),
        ).fetchall()
        saved = Counter(r["cat"] for r in sig if r["kind"] == "save" and r["cat"])
        skipped = Counter(r["cat"] for r in sig if r["kind"] == "skip" and r["cat"])
        taste_rows = conn.execute(
            "select taste from scores where created_at >= %s and taste is not null",
            (since,),
        ).fetchall()
        lines += ["", "*What I learned about you*"]
        if not sig:
            lines.append("No Save or Skip taps yet, so ranking is not personalised.")
        else:
            lines.append(
                f"{sum(1 for r in sig if r['kind'] == 'save')} saves, "
                f"{sum(1 for r in sig if r['kind'] == 'skip')} skips."
            )
            if saved:
                lines.append(
                    "You save: " + ", ".join(k for k, _ in saved.most_common(3)) + "."
                )
            if skipped:
                lines.append(
                    "You skip: " + ", ".join(k for k, _ in skipped.most_common(3)) + "."
                )
            if taste_rows:
                ups = sum(1 for r in taste_rows if r["taste"] > 0.2)
                downs = sum(1 for r in taste_rows if r["taste"] < -0.2)
                lines.append(
                    f"Taste nudges (max ±1 point): {ups} startups moved up, {downs} moved down."
                )
            else:
                lines.append("Taste nudges start after 3 Save/Skip taps.")

        # 3. close calls: skip-list removals that scored best
        close = conn.execute(
            """select s.name, k.reason, greatest((sc.scores_json->>'bangladesh')::numeric,
                 (sc.scores_json->>'global')::numeric) as best
               from skipped k join startups s on s.id = k.startup_id
               join lateral (select scores_json from scores where startup_id = s.id order by created_at desc limit 1) sc on true
               where k.created_at >= %s order by best desc limit 3""",
            (since,),
        ).fetchall()
        if close:
            lines += ["", "*3 close calls your skip list removed*"] + [
                f"- {r['name']} ({r['reason']}): best score {r['best']}" for r in close
            ]
            lines.append(
                "If one of these looks interesting, send skip remove <category>."
            )

        # 4. saved ideas re-checked
        ideas = conn.execute(
            """select s.name, i.status, sc.scores_json j,
                 (select count(*) from notes n where n.idea_id = i.id) as notes,
                 (select count(*) from assumptions a where a.idea_id = i.id and a.status = 'at_risk') as at_risk
               from ideas i join startups s on s.id = i.startup_id
               left join lateral (select scores_json from scores where startup_id = s.id order by created_at desc limit 1) sc on true
               order by i.created_at"""
        ).fetchall()
        if ideas:
            lines += ["", "*Your saved ideas*"] + [
                f"- {r['name']}: {r['status'].capitalize()}, BD {r['j']['bangladesh'] if r['j'] else '?'} · "
                f"Global {r['j']['global'] if r['j'] else '?'}, {r['notes']} notes"
                + (f", {r['at_risk']} assumptions at risk" if r["at_risk"] else "")
                for r in ideas
            ]

        # 5. free limits and backup model
        use = conn.execute(
            """select model_id, count(*) filter (where status = 'ok') as ok,
                 count(*) filter (where status in ('quota', 'cap')) as limited,
                 coalesce(sum(gpu_seconds), 0) as gpu
               from usage where created_at >= date_trunc('month', now()) group by model_id order by ok desc"""
        ).fetchall()
        lines += ["", "*AI usage this month*"]
        for r in use:
            if r["model_id"] and r["model_id"].startswith("Qwen"):
                lines.append(f"- Qwen backup: {float(r['gpu']) / 3600:.2f} GPU hours")
            elif r["ok"] or r["limited"]:
                lines.append(
                    f"- {r['model_id']}: {r['ok']} call{'s' if r['ok'] != 1 else ''}"
                    + (f", {r['limited']} hit a limit" if r["limited"] else "")
                )
        lines.append(
            f"Modal spend estimate: ${budget.month_spend(conn):.2f} of the ${cfg['budget']['modal_stop_usd']} limit."
        )

        sent = outbox.deliver(conn, outbox.text("\n".join(lines)), "monthly_report")
        stats = {
            "local_rounds": len(local),
            "close_calls": len(close),
            "ideas": len(ideas),
            "sent_now": sent,
        }
        repo.finish_run(conn, rid, "ok", stats)
        return stats
    except Exception as e:
        repo.finish_run(conn, rid, "error", error=f"{type(e).__name__}: {str(e)[:300]}")
        raise

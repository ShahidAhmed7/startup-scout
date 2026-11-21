"""Private web archive (phase 5), served by the same Modal web endpoint.

  /archive            search + filters (market, country, sector, minimum score, idea status), table of every card
  /archive/card/<id>  one card with its deep dives and validation kits
  /archive/notebook   the notebook board (Exploring / Testing / Promising / Dropped), with private notes

Sign-in (owner only):
  - WhatsApp: send `archive` to the bot -> a sign-in link valid 15 minutes -> a 30-day cookie on that phone.
  - Browser: /archive/login with the password (ARCHIVE_PASSWORD in the Modal secret / .env).
  - HTTP Basic auth still works for scripts. Without ARCHIVE_PASSWORD the archive is off.
"""

from __future__ import annotations

import html
import re
import secrets

import base64
import hashlib
import hmac
import time

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from scout.config import env

security = HTTPBasic(realm="Startup Scout", auto_error=False)
COOKIE = "scout_session"
LINK_TTL = 15 * 60
SESSION_TTL = 30 * 24 * 3600


def _sign(purpose: str, exp: int) -> str:
    key = (env("ARCHIVE_PASSWORD") or "").encode()
    return hmac.new(key, f"{purpose}:{exp}".encode(), hashlib.sha256).hexdigest()[:40]


def make_token(purpose: str, ttl: int) -> str:
    exp = int(time.time()) + ttl
    return f"{exp}.{_sign(purpose, exp)}"


def check_token(purpose: str, token: str | None) -> bool:
    if not token or "." not in token or not env("ARCHIVE_PASSWORD"):
        return False
    exp_s, sig = token.split(".", 1)
    return (
        exp_s.isdigit()
        and int(exp_s) > time.time()
        and hmac.compare_digest(sig, _sign(purpose, int(exp_s)))
    )


def login_link(base_url: str) -> str:
    """A one-time-ish sign-in link for the owner (sent only over WhatsApp, valid 15 minutes)."""
    return f"{base_url.rstrip('/')}/archive/login?t={make_token('link', LINK_TTL)}"


class NotSignedIn(Exception):
    pass


CSS = """
:root{--bg:#fafaf8;--fg:#1c1c1a;--muted:#6b6b66;--line:#e2e2dc;--card:#fff;--accent:#0b6e4f}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecece6;--muted:#9a9a92;--line:#2c2c29;--card:#1c1c1a;--accent:#4fc39a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}a{color:var(--accent)}h1{font-size:20px;margin:4px 0 12px}
nav a{margin-right:14px}form{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
input,select{padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);font:inherit}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px}table{border-collapse:collapse;width:100%;min-width:720px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600}
td.n{text-align:right;font-variant-numeric:tabular-nums}pre{white-space:pre-wrap;word-wrap:break-word;background:var(--card);
border:1px solid var(--line);border-radius:8px;padding:14px;font:14px/1.55 system-ui,sans-serif}
.board{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}.col{background:var(--card);
border:1px solid var(--line);border-radius:8px;padding:10px}.col h2{font-size:15px;margin:0 0 8px}.idea{border-top:1px solid var(--line);
padding:8px 0}.muted{color:var(--muted);font-size:13px}
"""


def require_password(
    request: Request, creds: HTTPBasicCredentials | None = Depends(security)
) -> None:
    pw = env("ARCHIVE_PASSWORD")
    if not pw:
        raise HTTPException(status_code=401)
    if check_token("session", request.cookies.get(COOKIE)):
        return
    if creds and secrets.compare_digest(creds.password.encode(), pw.encode()):
        return
    raise NotSignedIn()


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>
<nav><a href="/archive">Cards</a><a href="/archive/notebook">Notebook</a></nav>{body}</main></body></html>""")


def _linkify(text: str) -> str:
    t = html.escape(text)
    t = re.sub(r"\*([^*\n]+)\*", r"<strong>\1</strong>", t)
    return re.sub(
        r"(https?://[^\s<]+)",
        r'<a href="\1" rel="noreferrer" target="_blank">\1</a>',
        t,
    )


def _signed_in_response(target: str = "/archive") -> RedirectResponse:
    r = RedirectResponse(target, status_code=303)
    r.set_cookie(
        COOKIE,
        make_token("session", SESSION_TTL),
        max_age=SESSION_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/archive",
    )
    return r


LOGIN_FORM = """<h1>Sign in</h1><p class="muted">On your phone: send <strong>archive</strong> to the bot on WhatsApp
and tap the link. On a computer: enter the archive password (ARCHIVE_PASSWORD in your .env file).</p>
<form method="post" action="/archive/login"><input type="password" name="password" placeholder="Password"
autocomplete="current-password" required><input type="submit" value="Sign in"></form>{error}"""


def add_routes(app: FastAPI, connect) -> None:
    @app.exception_handler(NotSignedIn)
    def _to_login(request: Request, exc: NotSignedIn):
        return RedirectResponse("/archive/login", status_code=303)

    @app.get("/archive/login", response_class=HTMLResponse)
    def login_page(t: str | None = None):
        if t is not None:
            if check_token("link", t):
                return _signed_in_response()
            return _page(
                "Sign in",
                LOGIN_FORM.format(
                    error="<p>That link has expired. Send archive to the bot "
                    "for a new one.</p>"
                ),
            )
        return _page("Sign in", LOGIN_FORM.format(error=""))

    @app.post("/archive/login", response_class=HTMLResponse)
    def login_submit(password: str = Form("")):
        pw = env("ARCHIVE_PASSWORD")
        if pw and secrets.compare_digest(password.encode(), pw.encode()):
            return _signed_in_response()
        return _page("Sign in", LOGIN_FORM.format(error="<p>Wrong password.</p>"))

    @app.get(
        "/archive",
        response_class=HTMLResponse,
        dependencies=[Depends(require_password)],
    )
    def cards(
        q: str = "",
        market: str = "",
        country: str = "",
        sector: str = "",
        min_raw: str = Query("", alias="min"),
        status: str = "",
        ambition: str = "",
        business_model: str = "",
    ):
        # the browser sends every field, empty ones as "" (e.g. min=), so parse leniently
        try:
            minimum = float(min_raw)
            min_score = minimum if 0 <= minimum <= 10 else 0
        except ValueError:
            min_score = 0
        q, market, country, sector, status = (
            x.strip() for x in (q, market, country, sector, status)
        )
        where, args = ["c.kind = 'card'"], []
        if q:
            where.append(
                "(s.name ilike %s or s.description ilike %s or c.body ilike %s)"
            )
            args += [f"%{q}%"] * 3
        if market in ("bangladesh", "global"):
            where.append("c.market = %s")
            args.append(market)
        if country:
            where.append("s.country ilike %s")
            args.append(f"%{country}%")
        if sector:
            where.append("s.category ilike %s")
            args.append(f"%{sector}%")
        if min_score:
            where.append(
                "greatest((coalesce(c.data_json->'scores_snapshot',sc.scores_json)->>'bangladesh')::numeric, (coalesce(c.data_json->'scores_snapshot',sc.scores_json)->>'global')::numeric) >= %s"
            )
            args.append(min_score)
        if status:
            where.append("i.status = %s")
            args.append(status)
        if ambition in ("lean", "seed", "venture"):
            allowed = {
                "lean": ["bootstrap"],
                "seed": ["bootstrap", "seed"],
                "venture": ["seed", "venture"],
            }[ambition]
            where.append(
                "coalesce(c.data_json->'scores_snapshot',sc.scores_json)->'what_it_takes'->>'capital' = any(%s)"
            )
            args.append(allowed)
        if business_model:
            where.append(
                "coalesce(c.data_json->'scores_snapshot',sc.scores_json)->>'business_model'=%s"
            )
            args.append(business_model)
        with connect() as conn:
            rows = conn.execute(
                f"""select c.id, c.run_date, c.market, c.model_id, s.name, s.country, s.category, i.status,
                      (coalesce(c.data_json->'scores_snapshot',sc.scores_json)->>'bangladesh')::numeric as bd, (coalesce(c.data_json->'scores_snapshot',sc.scores_json)->>'global')::numeric as gl,
                      coalesce(c.data_json->'scores_snapshot',sc.scores_json)->'what_it_takes'->>'build_type' as build_type,
                      coalesce(c.data_json->'scores_snapshot',sc.scores_json)->'what_it_takes'->>'capital' as capital
                    from cards c join startups s on s.id = c.startup_id
                    left join ideas i on i.startup_id = s.id
                    left join lateral (select scores_json from scores where startup_id = s.id
                                       order by created_at desc limit 1) sc on true
                    where {" and ".join(where)} order by c.id desc limit 500""",
                args,
            ).fetchall()
        e = html.escape
        opts = lambda vals, cur: "".join(
            f'<option value="{v}"{" selected" if v == cur else ""}>{v or "any"}</option>'
            for v in vals
        )
        form = f"""<form method="get"><input name="q" placeholder="Search" value="{e(q)}">
<select name="market">{opts(["", "bangladesh", "global"], market)}</select>
<input name="country" placeholder="Country" value="{e(country)}" size="10">
<input name="sector" placeholder="Sector" value="{e(sector)}" size="10">
<input name="min" type="number" min="0" max="10" step="0.1" placeholder="Min score" value="{min_score or ""}" style="width:90px">
<select name="status">{opts(["", "exploring", "testing", "promising", "dropped"], status)}</select>
<select name="ambition">{opts(["", "lean", "seed", "venture"], ambition)}</select>
<select name="business_model">{opts(["", "software_subscription", "marketplace", "financial_product", "commerce", "tech_enabled_service", "hardware_light", "deep_tech", "infrastructure_api", "media_content", "other"], business_model)}</select>
<input type="submit" value="Filter"></form>"""
        from scout.score import shown_verdict as verdict, half_up

        trs = "".join(
            f"""<tr><td>{r["run_date"]}</td><td><a href="/archive/card/{r["id"]}">{e(r["name"])}</a></td>
<td>{e(r["market"] or "")}</td><td>{e(r["country"] or "")}</td><td>{e(r["category"] or "")}</td>
<td class="n">{half_up(r["bd"]) if r["bd"] is not None else ""}</td><td class="n">{half_up(r["gl"]) if r["gl"] is not None else ""}</td>
<td>{e(r.get("build_type") or "")}</td><td>{e(r.get("capital") or "")}</td><td>{verdict(float(r["bd"] if r["market"] == "bangladesh" else r["gl"])) if r["bd"] is not None and r["gl"] is not None else ""}</td><td>{e(r["status"] or "")}</td><td class="muted">{e(r["model_id"] or "")}</td></tr>"""
            for r in rows
        )
        table = (
            f'<div class="wrap"><table><tr><th>Date</th><th>Startup</th><th>Market</th><th>Country</th>'
            f"<th>Sector</th><th>BD</th><th>Global</th><th>Build type</th><th>Capital</th><th>Verdict</th><th>Idea</th><th>Model</th></tr>{trs}</table></div>"
            if rows
            else "<p>No cards match.</p>"
        )
        return _page(
            "Startup Scout archive", f"<h1>Every card ({len(rows)})</h1>{form}{table}"
        )

    @app.get(
        "/archive/card/{card_id}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_password)],
    )
    def card(card_id: int):
        with connect() as conn:
            c = conn.execute("select * from cards where id = %s", (card_id,)).fetchone()
            if c is None:
                raise HTTPException(status_code=404)
            extra = conn.execute(
                "select kind, body, created_at from cards where startup_id = %s and kind <> 'card' "
                "order by id",
                (c["startup_id"],),
            ).fetchall()
        from scout.cards import full_analysis

        body = full_analysis(c, include_model=True)
        parts = [
            f"<h1>{html.escape(c['title'] or 'Card')}</h1><pre>{_linkify(body)}</pre>"
        ]
        for x in extra:
            parts.append(
                f"<h2>{x['kind'].replace('_', ' ').capitalize()} · {x['created_at']:%d %b %Y}</h2>"
                f"<pre>{_linkify(x['body'])}</pre>"
            )
        return _page(c["title"] or "Card", "".join(parts))

    @app.get(
        "/archive/notebook",
        response_class=HTMLResponse,
        dependencies=[Depends(require_password)],
    )
    def notebook():
        with connect() as conn:
            ideas = conn.execute(
                """select i.*, s.name, (select id from cards c where c.startup_id = s.id and c.kind = 'card'
                                         order by id desc limit 1) as card_id
                   from ideas i join startups s on s.id = i.startup_id order by i.updated_at desc"""
            ).fetchall()
            notes = {
                r["idea_id"]: r["n"]
                for r in conn.execute(
                    "select idea_id, count(*) as n from notes group by idea_id"
                ).fetchall()
            }
            assumptions = conn.execute(
                "select * from assumptions order by id"
            ).fetchall()
        cols = []
        for st in ("exploring", "testing", "promising", "dropped"):
            items = []
            for i in (x for x in ideas if x["status"] == st):
                asm = "".join(
                    f"<li>{html.escape(a['text'])} <span class='muted'>({a['status'].replace('_', ' ')}, "
                    f"+{a['evidence_for']}/-{a['evidence_against']})</span></li>"
                    for a in assumptions
                    if a["idea_id"] == i["id"]
                )
                link = (
                    f'<a href="/archive/card/{i["card_id"]}">{html.escape(i["name"])}</a>'
                    if i["card_id"]
                    else html.escape(i["name"])
                )
                drop = (
                    f"<div class='muted'>Dropped: {html.escape(i['drop_reason'])}</div>"
                    if i["drop_reason"]
                    else ""
                )
                items.append(
                    f"<div class='idea'>{link}<div class='muted'>{notes.get(i['id'], 0)} notes · updated "
                    f"{i['updated_at']:%d %b}</div>{drop}{'<ul>' + asm + '</ul>' if asm else ''}</div>"
                )
            cols.append(
                f"<div class='col'><h2>{st.capitalize()} ({len(items)})</h2>{''.join(items) or '<p class=muted>Empty</p>'}</div>"
            )
        return _page(
            "Notebook", f"<h1>Notebook</h1><div class='board'>{''.join(cols)}</div>"
        )

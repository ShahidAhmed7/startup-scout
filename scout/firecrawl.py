"""Firecrawl (owner's decision, 2026-11-19): read sites that block our plain reader.

Overrides AGENTS.md rule 9 ("never bypass blocks") at the owner's request; rule 1 still holds: the free plan gives
1,000 credits/month (1 credit per page) and our caps (config.yaml `firecrawl`) stay below it. Without
FIRECRAWL_API_KEY nothing here runs and those sources are simply skipped.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from scout.config import config, env

API = "https://api.firecrawl.dev/v2/scrape"


class FirecrawlUnavailable(Exception):
    pass


def available() -> bool:
    return bool(env("FIRECRAWL_API_KEY"))


def credits_used(conn, since: datetime) -> int:
    row = conn.execute("""select count(*) as n from usage where provider = 'firecrawl' and status = 'ok'
                          and created_at >= %s""", (since,)).fetchone()
    return row["n"]


def _caps_ok(conn) -> bool:
    caps = config()["firecrawl"]
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return credits_used(conn, month_start) < caps["monthly_credit_cap"] and \
        credits_used(conn, day_start) < caps["daily_credit_cap"]


def scrape(conn, url: str, fmt: str = "rawHtml", kind: str = "feed", http: httpx.Client | None = None) -> str:
    """Page content via Firecrawl: "rawHtml" (feeds: the original XML) or "markdown" (homepages).
    Raises FirecrawlUnavailable if there is no key, the cap is reached, or the fetch fails."""
    key = env("FIRECRAWL_API_KEY")
    if not key:
        raise FirecrawlUnavailable("no FIRECRAWL_API_KEY")
    if conn is not None and not _caps_ok(conn):
        raise FirecrawlUnavailable("Firecrawl credit cap reached")
    from scout.db import repo

    status = "ok"
    try:
        with (http or httpx.Client()) as c:
            r = c.post(API, headers={"Authorization": f"Bearer {key}"},
                       json={"url": url, "formats": [fmt], "onlyMainContent": fmt == "markdown", "maxAge": 0},
                       timeout=90)
        if r.status_code >= 400:
            status = "server" if r.status_code >= 500 else "bad_request"
            raise FirecrawlUnavailable(f"HTTP {r.status_code}")
        body = r.json()
        data = body.get("data") or {}
        code = (data.get("metadata") or {}).get("statusCode", 200)
        content = data.get(fmt) or ""
        if code >= 400 or not content:
            status = "empty"
            raise FirecrawlUnavailable(f"site answered {code}")
        return content
    except httpx.HTTPError as e:
        status = "network"
        raise FirecrawlUnavailable(type(e).__name__) from None
    finally:
        if conn is not None:
            # a request that reached Firecrawl uses a credit even if the site refused
            repo.record_usage(conn, provider="firecrawl", model_id=None, key_label=None, kind=kind,
                              status="ok" if status in ("ok", "empty") else status)

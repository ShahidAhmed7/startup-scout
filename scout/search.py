"""Research helpers: polite web search (ddgs, no key) and homepage text (trafilatura).

ddgs is unofficial: requests are rate-limited, results cached for 7 days in `search_cache`, and any
failure simply means "no results" (cards then say "unknown"). We never bypass paywalls or logins.
"""
from __future__ import annotations

import json
import threading
import time

import httpx

from scout.config import source_defaults

SEARCH_GAP_SECONDS = 2.5
BACKENDS = ("auto", "bing", "brave", "duckduckgo")
_lock = threading.Lock()
_last = [0.0]


def _cached(conn, key: str, days: int = 7):
    row = conn.execute("select results_json from search_cache where query = %s and fetched_at > now() - make_interval(days => %s)",
                       (key, days)).fetchone()
    return row["results_json"] if row else None


def _store(conn, key: str, value) -> None:
    conn.execute("""insert into search_cache (query, results_json) values (%s, %s)
                    on conflict (query) do update set results_json = excluded.results_json, fetched_at = now()""",
                 (key, json.dumps(value)))


def web_search(conn, query: str, max_results: int = 6) -> list[dict]:
    """[{title, url, snippet}], [] on any failure."""
    key = f"ddg:{query}"
    hit = _cached(conn, key)
    if hit is not None:
        return hit
    from ddgs import DDGS

    results: list[dict] = []
    for backend in BACKENDS:
        with _lock:
            wait = _last[0] + SEARCH_GAP_SECONDS - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.monotonic()
        try:
            raw = DDGS(timeout=15).text(query, max_results=max_results, backend=backend)
            results = [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": (r.get("body") or "")[:300]}
                       for r in raw if r.get("href")]
        except Exception:
            results = []
        if results:
            break
    _store(conn, key, results)
    return results


def homepage_text(conn, url: str | None, limit: int = 6000) -> str:
    """Main text of a public homepage, '' if unavailable."""
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    key = f"home:{url}"
    hit = _cached(conn, key, days=14)
    if hit is not None:
        return hit
    text = ""
    try:
        import trafilatura

        with httpx.Client(headers={"User-Agent": source_defaults()["user_agent"]}, timeout=15,
                          follow_redirects=True) as c:
            r = c.get(url)
        if r.status_code == 200 and "html" in r.headers.get("content-type", ""):
            text = (trafilatura.extract(r.text[:2_000_000], include_comments=False) or "")[:limit]
    except Exception:
        text = ""
    _store(conn, key, text)
    return text


def format_results(results: list[dict]) -> str:
    if not results:
        return "(no results)"
    return "\n".join(f"- {r['title']} | {r['url']} | {r['snippet']}" for r in results)

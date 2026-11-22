"""Step 1: collect raw items from every active source in sources.yaml into the `items` table.

Polite by design: one clear User-Agent, conditional GET (ETag / Last-Modified), at least
`min_seconds_between_requests_same_host` between requests to the same host, no paywall or
login bypass. Source health (last_ok_at, fail_count, last_nonempty_at) is updated per source.
"""
from __future__ import annotations

import calendar
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

import feedparser
import httpx

from scout.config import source_defaults, sources
from scout.normalize import clean_text, clean_url


@dataclass
class RawItem:
    source_id: str
    url: str
    title: str
    summary: str
    published_at: datetime | None
    lang: str = "en"
    kind: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class FetchResult:
    items: list[RawItem]
    etag: str | None = None
    last_modified: str | None = None


# ---------- politeness ----------

class HostLimiter:
    def __init__(self, gap: float):
        self.gap = gap
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        with self._lock:
            slot = max(time.monotonic(), self._next.get(host, 0.0))
            self._next[host] = slot + self.gap
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def make_client() -> httpx.Client:
    d = source_defaults()
    return httpx.Client(headers={"User-Agent": d["user_agent"]}, timeout=d["timeout_seconds"],
                        follow_redirects=True)


def _entry_time(e) -> datetime | None:
    t = e.get("published_parsed") or e.get("updated_parsed")
    return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc) if t else None


def _recent(dt: datetime | None, since: datetime) -> bool:
    return dt is None or dt >= since


# ---------- fetchers ----------

def _parse_feed(content, src, since) -> list[RawItem]:
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"unreadable feed: {type(feed.bozo_exception).__name__}")
    items = []
    for e in feed.entries:
        dt = _entry_time(e)
        link = e.get("link")
        if not link or not _recent(dt, since):
            continue
        items.append(RawItem(src["id"], clean_url(link), clean_text(e.get("title"), 400),
                             clean_text(e.get("summary") or e.get("description"), 2000), dt,
                             lang=src.get("lang", "en")))
    return items


def fetch_via_firecrawl(conn, src, since) -> FetchResult:
    from scout import firecrawl

    return FetchResult(_parse_feed(firecrawl.scrape(conn, src["url"], "rawHtml", kind="feed"), src, since))


def fetch_rss(client, limiter, src, since, state, conn=None) -> FetchResult:
    """Plain polite fetch; if the site blocks us (403/429/503) and Firecrawl is set up, retry through Firecrawl."""
    from scout import firecrawl

    if src.get("via") == "firecrawl":
        return fetch_via_firecrawl(conn, src, since)
    try:
        return _fetch_rss_plain(client, limiter, src, since, state)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403, 429, 503) and firecrawl.available():
            return fetch_via_firecrawl(conn, src, since)
        raise


def _fetch_rss_plain(client, limiter, src, since, state) -> FetchResult:
    headers = {}
    if state.get("etag"):
        headers["If-None-Match"] = state["etag"]
    if state.get("last_modified"):
        headers["If-Modified-Since"] = state["last_modified"]
    limiter.wait(src["url"])
    r = client.get(src["url"], headers=headers)
    if r.status_code == 304:
        return FetchResult([], state.get("etag"), state.get("last_modified"))
    r.raise_for_status()
    return FetchResult(_parse_feed(r.content, src, since), r.headers.get("etag"), r.headers.get("last-modified"))


def fetch_gnews(client, limiter, src, since, state) -> FetchResult:
    q = src["url"]
    url = "https://news.google.com/rss/search?" + urlencode({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    limiter.wait(url)
    r = client.get(url)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries:
        dt = _entry_time(e)
        if not e.get("link") or not _recent(dt, since):
            continue
        publisher = (e.get("source") or {}).get("title") if isinstance(e.get("source"), dict) else None
        title = clean_text(e.get("title"), 400)
        items.append(RawItem(src["id"], e["link"], title, title, dt, raw={"publisher": publisher}))
    return FetchResult(items)


def fetch_hn(client, limiter, src, since, state) -> FetchResult:
    url = "https://hn.algolia.com/api/v1/search_by_date"
    query = "Launch HN" if src["id"] == "hn_launch" else None
    params = {"tags": "story" if query else "show_hn", "hitsPerPage": 100,
              "numericFilters": f"created_at_i>{int(since.timestamp())}"}
    if query:
        params["query"] = f'"{query}"'
    limiter.wait(url)
    r = client.get(url, params=params)
    r.raise_for_status()
    items = []
    for h in r.json().get("hits", []):
        title = h.get("title") or ""
        if query and not title.lower().startswith("launch hn"):
            continue
        hn_url = f"https://news.ycombinator.com/item?id={h['objectID']}"
        dt = datetime.fromtimestamp(h["created_at_i"], tz=timezone.utc)
        items.append(RawItem(src["id"], hn_url, clean_text(title, 400),
                             clean_text(h.get("story_text"), 2000) or clean_text(title, 400), dt, kind="launch",
                             raw={"points": h.get("points") or 0, "link": h.get("url")}))
    return FetchResult(items)


_SEASON = {"winter": 1, "spring": 2, "summer": 3, "fall": 4}


def _batch_key(slug: str) -> tuple[int, int]:
    parts = slug.split("-")
    if len(parts) == 2 and parts[1].isdigit() and parts[0] in _SEASON:
        return int(parts[1]), _SEASON[parts[0]]
    return (0, 0)


def fetch_yc(client, limiter, src, since, state, conn=None) -> FetchResult:
    """New YC companies: diff the newest batches against yc_seen. On the very first run we
    only treat companies added to the directory in the last 14 days as new."""
    meta = client.get(src["url"]).json()
    batches = sorted((k for k in meta["batches"] if _batch_key(k) != (0, 0)), key=_batch_key, reverse=True)[:4]
    seen = {r["slug"] for r in conn.execute("select slug from yc_seen").fetchall()}
    first_run = not seen
    fresh_cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    items = []
    for b in batches:
        api = meta["batches"][b]["api"]
        limiter.wait(api)
        for c in client.get(api).json():
            if c["slug"] in seen:
                continue
            launched = datetime.fromtimestamp(c["launched_at"], tz=timezone.utc) if c.get("launched_at") else None
            conn.execute("insert into yc_seen (slug, name, batch) values (%s, %s, %s) on conflict do nothing",
                         (c["slug"], c["name"], c.get("batch")))
            if first_run and (launched is None or launched < fresh_cutoff):
                continue
            items.append(RawItem(
                src["id"], c["url"], clean_text(f"{c['name']} (YC {c.get('batch')}): {c.get('one_liner') or ''}", 400),
                clean_text(c.get("long_description") or c.get("one_liner"), 2000), launched, kind="yc",
                raw={k: c.get(k) for k in ("name", "slug", "website", "one_liner", "batch", "industries",
                                            "regions", "team_size", "stage", "status", "all_locations")}))
    return FetchResult(items)


TRUSTMRR_MAX_PER_DAY = 40  # raised from 20 with the service track (2026-11-22)
TRUSTMRR_MIN_30D_USD = 300  # keep only startups with real traction


def _md_field(text: str, label: str) -> str:
    import re
    m = re.search(rf"^- {re.escape(label)}: (.+)$", text, re.M)
    return m.group(1).strip() if m else ""


def _usd(s: str) -> float:
    import re
    m = re.search(r"\$([\d,]+(?:\.\d+)?)\s*([kKmM]?)", s or "")
    if not m:
        return 0.0
    v = float(m.group(1).replace(",", ""))
    return v * {"k": 1e3, "m": 1e6}.get(m.group(2).lower(), 1)


def fetch_trustmrr(client, limiter, src, since, state, conn=None) -> FetchResult:
    """New startups on TrustMRR's 'recently added' page, read from their public .md profiles."""
    import re

    limiter.wait(src["url"])
    page = client.get(src["url"])
    page.raise_for_status()
    slugs = list(dict.fromkeys(re.findall(r"/startup/([a-z0-9][a-z0-9\-]*)", page.text)))
    seen = {r["key"] for r in conn.execute("select key from lane_seen where lane = 'trustmrr'").fetchall()}
    items = []
    for slug in [s for s in slugs if s not in seen][:TRUSTMRR_MAX_PER_DAY]:
        conn.execute("insert into lane_seen (lane, key) values ('trustmrr', %s) on conflict do nothing", (slug,))
        url = f"https://trustmrr.com/startup/{slug}.md"
        limiter.wait(url)
        r = client.get(url)
        if r.status_code != 200:
            continue
        md = r.text
        rev30 = _usd(_md_field(md, "Last 30 days revenue snapshot"))
        if rev30 < TRUSTMRR_MIN_30D_USD:
            continue
        name = _md_field(md, "Name") or slug
        website = re.sub(r"^\[([^\]]+)\].*$", r"\1", _md_field(md, "Website"))
        desc = _md_field(md, "Description")
        items.append(RawItem(
            src["id"], f"https://trustmrr.com/startup/{slug}", clean_text(f"{name}: {desc}", 400),
            clean_text(f"{desc} Verified revenue last 30 days: ${rev30:,.0f}.", 2000), datetime.now(timezone.utc),
            kind="trustmrr", raw={"name": name, "website": website, "description": desc, "rev30": rev30,
                                  "country": _md_field(md, "Country"), "founded": _md_field(md, "Founded date")}))
    return FetchResult(items)


FETCHERS = {"rss": fetch_rss, "gnews": fetch_gnews, "trustmrr": fetch_trustmrr}


def _fetcher(src):
    if src["kind"] == "api" and src["id"].startswith("hn_"):
        return fetch_hn
    if src["kind"] == "api" and src["id"] == "yc_oss":
        return fetch_yc
    return FETCHERS.get(src["kind"])


# ---------- main ----------

def sync_sources_table(conn) -> None:
    for s in sources():
        conn.execute(
            """insert into sources (id, name, url, kind, region, status) values (%s, %s, %s, %s, %s, %s)
               on conflict (id) do update set name = excluded.name, url = excluded.url, kind = excluded.kind,
                 region = excluded.region, status = excluded.status, updated_at = now()""",
            (s["id"], s["name"], s["url"], s["kind"], s.get("region"), s["status"]))


def store_items(conn, items: list[RawItem]) -> int:
    new = 0
    for it in items:
        row = conn.execute(
            """insert into items (source_id, url, title, summary, lang, published_at, kind, raw_json)
               values (%s, %s, %s, %s, %s, %s, %s, %s) on conflict (url) do nothing returning id""",
            (it.source_id, it.url, it.title, it.summary, it.lang, it.published_at, it.kind,
             json.dumps(it.raw) if it.raw else None)).fetchone()
        new += row is not None
    return new


def collect(conn, lookback_hours: float | None = None, only: list[str] | None = None, workers: int = 8) -> dict:
    sync_sources_table(conn)
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours or source_defaults()["lookback_hours"])
    limiter = HostLimiter(source_defaults()["min_seconds_between_requests_same_host"])
    from scout import firecrawl

    active = [s for s in sources("active") if _fetcher(s) and (not only or s["id"] in only)
              and (s.get("via") != "firecrawl" or firecrawl.available())]  # no key: skip quietly
    state = {r["id"]: r for r in conn.execute("select id, etag, last_modified from sources").fetchall()}
    lock = threading.Lock()
    stats = {"sources_ok": 0, "sources_failed": [], "fetched": 0, "new_items": 0}

    with make_client() as client:
        def one(src):
            try:
                fn = _fetcher(src)
                kwargs = {"conn": conn} if fn in (fetch_yc, fetch_trustmrr, fetch_rss) else {}
                res = fn(client, limiter, src, since, state.get(src["id"], {}), **kwargs)
                with lock:
                    n_new = store_items(conn, res.items)
                    conn.execute(
                        """update sources set last_ok_at = now(), fail_count = 0, etag = %s, last_modified = %s,
                             last_nonempty_at = case when %s > 0 then now() else last_nonempty_at end,
                             updated_at = now() where id = %s""",
                        (res.etag, res.last_modified, len(res.items), src["id"]))
                    stats["sources_ok"] += 1
                    stats["fetched"] += len(res.items)
                    stats["new_items"] += n_new
            except Exception as e:  # one bad source never stops the run
                with lock:
                    conn.execute("update sources set fail_count = fail_count + 1, updated_at = now() where id = %s",
                                 (src["id"],))
                    stats["sources_failed"].append(f"{src['id']}: {type(e).__name__}: {str(e)[:80]}")

        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, active))
    return stats


if __name__ == "__main__":
    from scout.db import repo

    with repo.connect() as c:
        t = time.time()
        s = collect(c)
        print(f"Collected in {time.time() - t:.0f}s: {s['sources_ok']} sources ok, {s['fetched']} recent items, "
              f"{s['new_items']} new")
        for f in s["sources_failed"]:
            print("  failed:", f)

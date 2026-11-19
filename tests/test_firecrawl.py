from datetime import datetime, timedelta, timezone

import httpx
import pytest

from scout import collect, firecrawl

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Acme raises $3M seed</title><link>https://ex.com/acme</link><pubDate>{d}</pubDate>
<description>Acme, a Lagos-based logistics startup, raised $3M.</description></item></channel></rss>"""


def rss_now():
    return RSS.format(d=datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000"))


def fc_client(raw, code=200):
    def handler(request):
        assert request.headers["authorization"] == "Bearer fc-key"
        return httpx.Response(200, json={"success": True, "data": {"rawHtml": raw, "metadata": {"statusCode": code}}})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_no_key_means_unavailable(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    assert not firecrawl.available()
    with pytest.raises(firecrawl.FirecrawlUnavailable):
        firecrawl.scrape(None, "https://ex.com/feed")


def test_scrape_returns_raw_feed(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-key")
    assert "Acme" in firecrawl.scrape(None, "https://ex.com/feed", http=fc_client(rss_now()))


def test_site_refusing_even_firecrawl_is_an_error(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-key")
    with pytest.raises(firecrawl.FirecrawlUnavailable):
        firecrawl.scrape(None, "https://ex.com/feed", http=fc_client("<html>no</html>", code=403))


def test_monthly_cap_stops_spending(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-key")
    monkeypatch.setattr(firecrawl, "credits_used", lambda conn, since: 900)
    with pytest.raises(firecrawl.FirecrawlUnavailable, match="cap"):
        firecrawl.scrape(object(), "https://ex.com/feed", http=fc_client(rss_now()))


def test_blocked_feed_falls_back_to_firecrawl(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-key")
    monkeypatch.setattr(firecrawl, "scrape", lambda conn, url, fmt="rawHtml", kind="feed", http=None: rss_now())
    blocked = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    src = {"id": "tech_eu", "url": "https://tech.eu/feed/", "kind": "rss"}
    since = datetime.now(timezone.utc) - timedelta(hours=36)
    res = collect.fetch_rss(blocked, collect.HostLimiter(0), src, since, {}, conn=None)
    assert [i.title for i in res.items] == ["Acme raises $3M seed"]


def test_blocked_feed_without_key_still_fails_loudly(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    blocked = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    src = {"id": "tech_eu", "url": "https://tech.eu/feed/", "kind": "rss"}
    with pytest.raises(httpx.HTTPStatusError):
        collect.fetch_rss(blocked, collect.HostLimiter(0), src, datetime.now(timezone.utc), {}, conn=None)

"""Small text helpers: clean HTML, normalise company names and domains."""
from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_SUFFIXES = {"inc", "ltd", "limited", "llc", "pvt", "private", "corp", "corporation", "co", "gmbh",
             "plc", "pte", "sa", "sas", "sarl", "srl", "spa", "bv", "nv", "ag", "oy", "ab", "aps", "as", "pty",
             "sl", "kk", "pbc", "the"}


def clean_text(s: str | None, limit: int | None = None) -> str:
    if not s:
        return ""
    s = html.unescape(_TAG.sub(" ", s))
    s = _WS.sub(" ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


def norm_name(name: str) -> str:
    words = re.sub(r"[^\w\s]", " ", (name or "").lower()).split()
    words = [w for w in words if w not in _SUFFIXES]
    return " ".join(words)


def domain_of(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or host in {"news.google.com", "linkedin.com", "twitter.com", "x.com", "facebook.com",
                                   "ycombinator.com", "news.ycombinator.com", "crunchbase.com"}:
        return None
    return host


# ---------- countries and regions ----------

_COUNTRY_SHORT = {
    "united states of america": "USA", "united states": "USA", "us": "USA", "u.s.": "USA", "u.s": "USA",
    "america": "USA", "united kingdom": "UK", "great britain": "UK", "england": "UK",
    "united arab emirates": "UAE", "people's republic of china": "China", "prc": "China",
    "republic of korea": "South Korea", "korea": "South Korea", "kingdom of saudi arabia": "Saudi Arabia",
    "ksa": "Saudi Arabia", "the netherlands": "Netherlands", "remote": "", "none": "", "unknown": "",
    "global": "",
}

_REGIONS = {
    "USA": "USA", "Canada": "USA",
    "India": "India", "Pakistan": "Pakistan", "Bangladesh": "Bangladesh", "China": "China", "Hong Kong": "China",
    "Taiwan": "China",
    "UAE": "Middle East", "Saudi Arabia": "Middle East", "Qatar": "Middle East", "Bahrain": "Middle East",
    "Kuwait": "Middle East", "Oman": "Middle East", "Jordan": "Middle East", "Lebanon": "Middle East",
    "Israel": "Middle East", "Turkey": "Middle East", "Iraq": "Middle East",
    "Nigeria": "Africa", "Kenya": "Africa", "Egypt": "Africa", "South Africa": "Africa", "Ghana": "Africa",
    "Rwanda": "Africa", "Morocco": "Africa", "Tunisia": "Africa", "Ethiopia": "Africa", "Uganda": "Africa",
    "Tanzania": "Africa", "Senegal": "Africa", "Côte d'Ivoire": "Africa", "Ivory Coast": "Africa",
    "Zambia": "Africa", "Cameroon": "Africa", "Algeria": "Africa",
    "Indonesia": "SE Asia", "Vietnam": "SE Asia", "Philippines": "SE Asia", "Malaysia": "SE Asia",
    "Singapore": "SE Asia", "Thailand": "SE Asia", "Cambodia": "SE Asia", "Myanmar": "SE Asia",
    "Japan": "East Asia", "South Korea": "East Asia",
    "Brazil": "LatAm", "Mexico": "LatAm", "Argentina": "LatAm", "Colombia": "LatAm", "Chile": "LatAm",
    "Peru": "LatAm",
}
_EUROPE = {"UK", "Germany", "France", "Netherlands", "Sweden", "Spain", "Italy", "Ireland", "Bulgaria", "Poland",
           "Estonia", "Finland", "Denmark", "Norway", "Switzerland", "Austria", "Belgium", "Portugal", "Lithuania",
           "Latvia", "Czech Republic", "Romania", "Greece", "Ukraine", "Luxembourg", "Hungary", "Croatia"}


def short_country(c: str | None) -> str | None:
    if not c:
        return None
    c = c.strip().strip(".")
    v = _COUNTRY_SHORT.get(c.lower(), c)
    return v or None


def region_of(country: str | None) -> str:
    c = short_country(country)
    if not c:
        return "Unknown"
    if c in _EUROPE:
        return "Europe"
    return _REGIONS.get(c, "Other")


def clean_url(url: str) -> str:
    """Drop utm_* tracking parameters (keeps Google News links intact)."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    if "news.google.com" in url or "?" not in url:
        return url
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunsplit(parts._replace(query=urlencode(q)))

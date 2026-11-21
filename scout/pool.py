"""Geographic balancing happens before scoring, independent of source priority."""

import math
from collections import Counter, defaultdict
from scout.normalize import region_of, short_country

SIMILAR = {"bangladesh", "india", "pakistan", "sea", "africa", "mideast"}
REGION_NAMES = {
    "USA": "usa",
    "Europe": "europe",
    "China": "china",
    "India": "india",
    "Pakistan": "pakistan",
    "Bangladesh": "bangladesh",
    "SE Asia": "sea",
    "Africa": "africa",
    "Middle East": "mideast",
    "Unknown": "unknown",
    "Other": "other",
}


def region(country, source=None):
    if short_country(country):
        # Canada must not satisfy the US-only bucket.
        if short_country(country).lower() == "canada":
            return "other"
        return REGION_NAMES.get(region_of(country), "other")
    return (
        source.lower()
        if source and source.lower() in {*REGION_NAMES.values(), "latam", "east asia"}
        else "unknown"
    )


def balance(rows, cfg):
    target = min(cfg.get("max_to_score", 120), len(rows))
    if not target:
        return [], {"regions": {}, "ai_native": 0, "count": 0, "shortages": []}
    groups = defaultdict(list)
    for r in sorted(
        rows,
        key=lambda r: (r.get("buzz", 0), str(r.get("recency", "")), -r["startup_id"]),
        reverse=True,
    ):
        groups[r.get("region", "unknown")].append(r)
    selected = []
    counts = Counter()
    ai = 0
    shortages = []
    ai_limit = math.floor(target * cfg.get("max_ai_native_share", 0.4))
    min_sim = min(
        sum(len(v) for k, v in groups.items() if k in SIMILAR),
        math.ceil(target * cfg.get("min_share", {}).get("similar_markets", 0.4)),
    )
    caps = {k: math.floor(target * v) for k, v in cfg.get("max_share", {}).items()}
    order = sorted(groups)

    def take(keys, relax=False):
        nonlocal ai
        for key in keys:
            if not relax and key in caps and counts[key] >= caps[key]:
                continue
            r = next(
                (r for r in groups[key] if not r.get("ai_native") or ai < ai_limit),
                None,
            )
            if r:
                groups[key].remove(r)
                selected.append(r)
                counts[key] += 1
                ai += bool(r.get("ai_native"))
                return True
        return False

    while len(selected) < min_sim:
        similar = sorted(
            (k for k in order if k in SIMILAR), key=lambda k: (counts[k], k)
        )
        if not take(similar):
            break
    while len(selected) < target:
        keys = sorted(order, key=lambda k: (counts[k], order.index(k)))
        if not take(keys):
            if not take(keys, relax=True):
                break
            if "regional supply exhausted; region caps relaxed" not in shortages:
                shortages.append("regional supply exhausted; region caps relaxed")
    # An AI-heavy input must not inflate the allowed share merely by leaving slots empty.
    share = cfg.get("max_ai_native_share", 0.4)
    while selected and ai / len(selected) > share:
        idx = next(
            (
                i
                for i in range(len(selected) - 1, -1, -1)
                if selected[i].get("ai_native")
            ),
            None,
        )
        if idx is None:
            break
        selected.pop(idx)
        ai -= 1
    counts = Counter(r.get("region", "unknown") for r in selected)
    if sum(counts[k] for k in SIMILAR) < math.ceil(
        len(selected) * cfg.get("min_share", {}).get("similar_markets", 0.4)
    ):
        shortages.append("insufficient similar-market candidates")
    return selected, {
        "count": len(selected),
        "regions": dict(counts),
        "ai_native": ai,
        "shortages": shortages,
    }

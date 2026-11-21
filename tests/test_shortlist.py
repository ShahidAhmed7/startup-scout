from copy import deepcopy
from scout.config import config
from scout.score import shortlist, half_up, verdict, calculate, exploration
from scout.pool import balance


def row(i, bd=7, gl=6, **kw):
    return (
        dict(
            startup_id=i,
            bd=bd,
            gl=gl,
            rank_bd=bd,
            rank_global=gl,
            sector=f"sector {i}",
            business_model=f"model {i}",
            ai_native=False,
            known=True,
            region="usa",
            conf="high",
            capital="bootstrap",
            buzz=1,
        )
        | kw
    )


def choose(rows, **kw):
    cfg = deepcopy(config())
    cfg.update(kw)
    return shortlist(rows, cfg["digest_mix"], cfg["min_score"], cfg=cfg)


def picks(sl):
    return sl["bangladesh"] + sl["global"]


def test_slow_day_fills_to_three_with_real_watch_verdict():
    sl = choose([row(1), row(2, 7, 6, region="india"), row(3, 5.5, 5)])
    assert len(picks(sl)) == 3
    assert len([r for r in picks(sl) if r["fill"]]) == 1
    assert verdict(5.5) == "Watch"


def test_nothing_below_five_and_unknown_never_shown():
    sl = choose(
        [row(1, 9, 9, known=False), row(2, 4.9, 4.9), row(3, 5, 5, region="india")]
    )
    assert [r["startup_id"] for r in picks(sl)] == [3]


def test_outside_us_replacement_and_unknown_does_not_count():
    sl = choose(
        [
            *[row(i, 9 - i * 0.1, 8, region="usa") for i in range(1, 7)],
            row(8, 6, 5.5, region="india"),
        ]
    )
    assert any(r["region"] == "india" and r["fill"] for r in picks(sl))


def test_ambition_filters_but_any_allows_venture():
    rows = [
        row(1, 9, 9, capital="venture"),
        row(2, 8, 8, capital="seed"),
        row(3, 7, 7, region="africa"),
    ]
    assert len(picks(choose(rows, ambition="any"))) == 3
    assert len(picks(choose(rows, ambition="lean"))) == 1
    assert len(picks(choose(rows, ambition="seed"))) == 2
    assert len(picks(choose(rows, ambition="venture"))) == 2


def test_fill_relaxes_sector_and_model_only_when_needed():
    rows = [
        row(
            i,
            7,
            6,
            sector="fintech",
            business_model="financial_product",
            region="india",
        )
        for i in range(1, 5)
    ]
    sl = choose(rows)
    assert len(picks(sl)) == 3 and sum(r["diversity_relaxed"] for r in picks(sl)) == 2


def test_ai_maximum_covers_explore_not_quota():
    rows = [row(i, 8, 8, ai_native=i < 6, region="india") for i in range(1, 15)]
    sl = choose(rows)
    assert sum(r["ai_native"] for r in picks(sl) + sl["explore"]) <= 3
    assert not any(
        r["ai_native"]
        for r in picks(choose([row(i, 8, 8, region="africa") for i in range(10)]))
    )


def test_novelty_and_skip_apply_to_fill_and_explore():
    rows = [
        row(1, 9, 9, novelty_blocked=True),
        row(2, 9, 9, skip=True),
        row(3, 5, 5, region="india"),
    ]
    assert [r["startup_id"] for r in picks(choose(rows))] == [3]


def test_half_up_uses_unrounded_verdict_threshold():
    assert half_up(6.5) == 7 and half_up(8.5) == 9
    assert verdict(6.4) == "Watch" and verdict(6.5) == "Promising"


def test_balanced_pool_with_sufficient_supply():
    rows = [
        row(i, region=reg, ai_native=i % 4 == 0)
        for i, reg in enumerate(
            ["usa"] * 120
            + ["europe"] * 50
            + ["china"] * 40
            + ["india"] * 50
            + ["africa"] * 50
            + ["sea"] * 50
        )
    ]
    selected, stats = balance(rows, config()["pool"])
    n = len(selected)
    assert (
        n == 120
        and stats["regions"]["usa"] <= 42
        and stats["regions"]["europe"] <= 18
        and stats["regions"]["china"] <= 12
    )
    assert sum(stats["regions"].get(r, 0) for r in ("india", "africa", "sea")) >= 48
    assert stats["ai_native"] / n <= 0.4


def test_pool_handles_missing_regions_and_limits_ai_in_actual_size():
    selected, stats = balance(
        [row(i, ai_native=i < 19) for i in range(20)], config()["pool"]
    )
    assert sum(r["ai_native"] for r in selected) / len(selected) <= 0.4
    assert stats["shortages"]


def test_region_fallback_requires_a_known_regional_source():
    from scout.pool import region

    assert region("Canada", "usa") == "other"
    assert region(None, "global") == "unknown"
    assert region(None, "india") == "india"
    assert region("Bangladesh", "usa") == "bangladesh"


def test_similar_market_pool_reservation_rotates_regions():
    rows = [
        row(i, region=reg)
        for i, reg in enumerate(
            ["india"] * 100 + ["africa"] * 100 + ["sea"] * 100 + ["usa"] * 100
        )
    ]
    selected, stats = balance(rows, config()["pool"])
    similar = [r["region"] for r in selected[:48]]
    assert (
        similar.count("africa") == similar.count("india") == similar.count("sea") == 16
    )


def test_outside_us_normal_replacement_respects_other_diversity():
    rows = [row(i, 9 - i * 0.1, 8) for i in range(1, 7)] + [
        row(8, 8, 8, region="europe")
    ]
    sl = choose(rows)
    assert len(picks(sl)) == 5
    assert any(r["region"] == "europe" and not r["fill"] for r in picks(sl))
    assert len({r["startup_id"] for r in picks(sl)}) == 5


def test_ties_prefer_confidence_then_buzz():
    rows = [
        row(1, 8, 7, conf="low"),
        row(2, 8, 7, conf="high", buzz=2),
        row(3, 8, 7, conf="high", buzz=5),
    ]
    cfg = deepcopy(config())
    cfg["min_picks"] = 1
    cfg["diversity"]["min_non_us_picks"] = 0
    sl = shortlist(rows, {"bangladesh": 1, "global": 0}, 6.5, cfg=cfg)
    assert sl["bangladesh"][0]["startup_id"] == 3

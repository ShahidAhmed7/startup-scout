from scout.score import rank_value, shortlist

CFG = {"weights": {"pain": 1.0, "why_now": 0.8, "feasibility": 1.0, "fits_you": 0.7}, "ai_angle": {"boost": 0.5}}


def row(i, cat, bd, gl, buzz=1, feas=7):
    return {"startup_id": i, "category": cat, "bd": bd, "gl": gl, "rank_bd": bd, "rank_global": gl, "buzz": buzz,
            "feas": feas}


def test_top3_bd_and_top2_global_with_min_score():
    rows = [row(1, "fintech", 9, 3), row(2, "health", 8, 4), row(3, "edtech", 7, 2), row(4, "logistics", 6, 5),
            row(5, "dev tools", 2, 9), row(6, "marketing", 3, 8), row(7, "legal", 2, 5)]
    sl = shortlist(rows, {"bangladesh": 3, "global": 2}, 6)
    assert [r["startup_id"] for r in sl["bangladesh"]] == [1, 2, 3]
    assert [r["startup_id"] for r in sl["global"]] == [5, 6]
    assert [r["startup_id"] for r in sl["reserve"]] == [4]


def test_no_two_picks_from_same_category():
    rows = [row(1, "fintech", 9, 3), row(2, "fintech", 8, 3), row(3, "health", 7, 3), row(4, "Fintech", 2, 9)]
    sl = shortlist(rows, {"bangladesh": 3, "global": 2}, 6)
    ids = [r["startup_id"] for r in sl["bangladesh"] + sl["global"]]
    assert ids == [1, 3]


def test_same_startup_never_in_both_lists():
    rows = [row(1, "fintech", 9, 9), row(2, "health", 3, 7)]
    sl = shortlist(rows, {"bangladesh": 1, "global": 1}, 6)
    assert [r["startup_id"] for r in sl["bangladesh"]] == [1] and [r["startup_id"] for r in sl["global"]] == [2]


def test_feasibility_gate_drops_ideas_a_small_team_cannot_build():
    rows = [row(1, "agriculture", 8, 3, feas=3), row(2, "fintech", 7, 3, feas=7)]
    sl = shortlist(rows, {"bangladesh": 2, "global": 0}, 6, min_feasibility=5)
    assert [r["startup_id"] for r in sl["bangladesh"]] == [2]


def test_buzz_breaks_ties():
    rows = [row(1, "fintech", 8, 1, buzz=1), row(2, "health", 8, 1, buzz=3)]
    sl = shortlist(rows, {"bangladesh": 1, "global": 0}, 6)
    assert sl["bangladesh"][0]["startup_id"] == 2


def test_rank_value_nudge_is_bounded_and_ai_boost_applies():
    hi = {"pain": 10, "why_now": 10, "feasibility": 10, "fits_you": 10}
    lo = {"pain": 1, "why_now": 1, "feasibility": 1, "fits_you": 1}
    assert rank_value(7, hi, False, CFG) <= 8.0
    assert rank_value(7, lo, False, CFG) >= 6.0
    assert rank_value(7, hi, True, CFG) == rank_value(7, hi, False, CFG) + 0.5


def ai_row(i, sector, bd, gl, ai=False, feas=7, buzz=1):
    return {"startup_id": i, "category": sector, "sector": sector, "ai_native": ai, "bd": bd, "gl": gl,
            "rank_bd": bd, "rank_global": gl, "buzz": buzz, "feas": feas}


def test_at_most_two_ai_native_picks_and_sectors_differ():
    rows = [ai_row(1, "marketing and sales", 9, 8, ai=True), ai_row(2, "real estate and construction", 9, 7, ai=True),
            ai_row(3, "developer tools and AI infrastructure", 8, 9, ai=True), ai_row(4, "health", 8, 9, ai=True),
            ai_row(5, "fintech", 7, 5), ai_row(6, "logistics and mobility", 6, 7), ai_row(7, "education", 6, 6)]
    sl = shortlist(rows, {"bangladesh": 3, "global": 2}, 6, max_ai_native=2)
    picks = sl["bangladesh"] + sl["global"]
    assert sum(r["ai_native"] for r in picks) <= 2
    assert len({r["sector"] for r in picks}) == len(picks) == 5


def test_explore_adds_other_sectors_non_ai_first():
    rows = [ai_row(1, "fintech", 9, 3), ai_row(2, "health", 3, 9),
            ai_row(3, "fintech", 6, 4), ai_row(4, "agriculture and food", 5, 3),
            ai_row(5, "travel and hospitality", 5, 5, ai=True), ai_row(6, "education", 3, 5), ai_row(7, "media and creators", 2, 3)]
    sl = shortlist(rows, {"bangladesh": 1, "global": 1}, 6, reserve=0, explore_rows=3, explore_min_score=5)
    ids = [r["startup_id"] for r in sl["explore"]]
    assert 3 not in ids                      # same sector as a pick
    assert 7 not in ids                      # below the explore minimum
    assert ids[:2] == [4, 6] and ids[2] == 5 # non-AI first, then the AI one

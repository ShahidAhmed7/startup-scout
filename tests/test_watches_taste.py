from scout import taste, watches


def test_parse_segment_and_company():
    assert watches.parse("fintech seed Nigeria") == {"kind": "segment", "sector": "fintech", "stage": "seed",
                                                     "country": "Nigeria"}
    assert watches.parse("health software series a any")["stage"] == "series a"
    assert watches.parse("render.com") == {"kind": "company", "domain": "render.com", "name": ""}
    assert watches.parse("Render")["kind"] == "company"


def row(**kw):
    base = {"name": "Kora", "domain": "kora.ng", "country": "Nigeria", "category": "fintech",
            "description": "Payments API", "stage": "Seed"}
    return base | kw


def test_segment_matching():
    q = watches.parse("fintech seed Nigeria")
    assert watches._matches(q, row())
    assert not watches._matches(q, row(stage="Series B"))
    assert not watches._matches(q, row(country="Kenya"))
    assert watches._matches(watches.parse("fintech seed Africa"), row())   # region works too
    assert watches._matches(watches.parse("any seed any"), row(category="logistics"))


def test_company_matching():
    assert watches._matches(watches.parse("kora.ng"), row())
    assert watches._matches(watches.parse("Kora Inc"), row())
    assert not watches._matches(watches.parse("Paystack"), row())


def test_taste_nudge_needs_three_signals_and_is_bounded():
    a, b = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]
    assert taste.nudge(a, [a], [b]) == 0.0                       # only 2 signals: no effect yet
    assert taste.nudge(a, [a, a], [b]) == 1.0                   # like what was saved: capped at +1
    assert taste.nudge(b, [a, a], [b]) == -1.0                  # like what was skipped: capped at -1
    mid = taste.nudge([1.0, 1.0, 0.0], [a, a], [b])
    assert -1.0 <= mid <= 1.0 and abs(mid) < 0.5

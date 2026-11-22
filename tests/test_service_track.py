"""The service-first track: done-for-you work delivered through software from Bangladesh."""

from copy import deepcopy

from scout.config import config
from scout.score import calculate, shortlist
from tests.test_redesign import data, CL
from tests.test_shortlist import row


def test_service_score_is_computed_and_gated_on_software_delivery():
    raw = data(7)
    j, ranks = calculate(raw, CL)
    assert j["service"] == 7.0 and "service" in j["dimensions"] and "service" in ranks
    raw = data(7)
    raw["service"]["remote_delivery"]["score"] = 4  # on-site work: cannot be done from Bangladesh
    j, _ = calculate(raw, CL)
    assert j["service"] == 1.0 and j["concept_service"] == "none"


def test_old_scores_without_a_service_track_still_calculate():
    raw = data(7)
    raw.pop("service")
    raw.pop("concept_service")
    j, ranks = calculate(raw, CL)
    assert "service" not in ranks and j.get("service") is None and j["bangladesh"] == 7.0


def svc(i, bd, gl, sv, **kw):
    return row(i, bd, gl, sv=sv, rank_service=sv, **kw)


def test_six_picks_two_per_track():
    rows = [svc(i, 8 - i * 0.1, 3, 3) for i in range(1, 4)]
    rows += [svc(i, 3, 8 - i * 0.1, 3, region="india") for i in range(4, 7)]
    rows += [svc(i, 3, 3, 8 - i * 0.1) for i in range(7, 10)]
    sl = shortlist(rows, config()["digest_mix"], config()["min_score"], cfg=deepcopy(config()))
    assert [len(sl[m]) for m in ("bangladesh", "global", "service")] == [2, 2, 2]
    assert all(r["market"] == "service" for r in sl["service"])


def test_slow_day_fills_each_track_with_watch_ideas_first():
    rows = [svc(1, 7, 3, 3), svc(2, 3, 3, 5.5, region="india"), svc(3, 3, 3, 5.2)]
    sl = shortlist(rows, config()["digest_mix"], config()["min_score"], cfg=deepcopy(config()))
    assert {r["startup_id"] for r in sl["service"]} == {2, 3}
    # The Indian one is placed first by the outside-USA rule; the other fills the service share.
    assert all(r["fill"] for r in sl["service"])
    assert {r["fill_reason"] for r in sl["service"]} == {"outside USA", "track share"}


def test_service_card_and_digest_labels():
    from scout import cards
    from scout.markets import SECTION, ROW_LABEL

    j = data(7)
    j, _ = calculate(j, CL)
    line = cards.score_line(j)
    assert "Service 7 · Promising" in line
    assert SECTION["service"] == "Service from Bangladesh" and len(SECTION["service"]) <= 24
    assert ROW_LABEL["service"] == "Service"
    j["concept_service"] = "none"
    assert "Service: no version" in cards.score_line(j)


def test_mix_accepts_three_tracks():
    import re

    m = re.fullmatch(r"mix\s+(\d{1,2})\s*\+\s*(\d{1,2})(?:\s*\+\s*(\d{1,2}))?", "mix 2+2+2")
    assert m and m.groups() == ("2", "2", "2")


def test_service_track_is_not_starved_by_sectors_already_picked():
    # The 2026-11-22 rehearsal: Bangladesh and global picks took fintech, retail and security, and every service
    # candidate was in one of those sectors, so the service section came out empty.
    rows = [
        svc(1, 8, 2, 2, sector="fintech", business_model="financial_product"),
        svc(2, 7.5, 2, 2, sector="commerce and retail", business_model="commerce", region="india"),
        svc(3, 2, 7.5, 2, sector="security", business_model="software_subscription"),
        svc(4, 2, 7.2, 2, sector="health", business_model="infrastructure_api"),
        svc(5, 2, 2, 8.1, sector="fintech", business_model="tech_enabled_service"),
        svc(6, 2, 2, 8.0, sector="commerce and retail", business_model="tech_enabled_service"),
        svc(7, 2, 2, 6.8, sector="security", business_model="tech_enabled_service"),
    ]
    sl = shortlist(rows, config()["digest_mix"], config()["min_score"], cfg=deepcopy(config()))
    assert {r["startup_id"] for r in sl["service"]} == {5, 6}
    assert not any(r["diversity_relaxed"] for m in ("bangladesh", "global", "service") for r in sl[m])


def test_service_card_shows_the_service_effort_and_a_clean_idea_line(monkeypatch):
    from scout import cards

    j, _ = calculate(data(7), CL)
    f = {"startup": {"country": "USA", "region": "usa", "description": "Sources products."},
         "round": {"stage": "Series A", "amount_text": "$48.5M", "announced_at": None},
         "score": {"scores_json": j}}
    long_idea = " ".join(["word"] * 25)
    d = {"market": "service", "service_play": "Retailers abroad outsource sourcing. $500 a month to test.",
         "one_liner": long_idea, "main_risk": "Trust.", "what_they_do": "Everful sources products.",
         "scores_snapshot": j}
    text = cards.card_text(f, d, "m", None)
    assert "*Service from Bangladesh*" in text
    assert "tech enabled service · bootstrap · team 2-5" in text  # not the funded company's venture labels
    assert "word…" in text and long_idea not in text

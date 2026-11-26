from copy import deepcopy
from datetime import date
import pytest
from scout.config import config
from scout.score import calculate
from scout.cards import card_text, apply_update, full_analysis
from scout.llm.schemas import Scored
from scout.whatsapp.formats import buttons_payload, split_text


def data(score=7):
    dim = lambda: {"score": score, "reason": "A reason supported by the material."}
    return {
        "startup_id": 1,
        "concept_bd": "A local delivery service for retailers.",
        "concept_global": "A remote operations service.",
        "concept_service": "Done-for-you order processing for online retailers abroad.",
        "bangladesh": {
            k: dim()
            for k in (
                "demand",
                "readiness",
                "competition_gap",
                "regulatory_path",
                "economics",
            )
        },
        "global": {
            k: dim()
            for k in (
                "demand_abroad",
                "remote_delivery",
                "cost_advantage",
                "trust_burden",
                "competition_gap",
            )
        },
        "service": {
            k: dim()
            for k in (
                "buyer_breadth",
                "pain_budget",
                "cost_advantage",
                "reachability",
                "repeatable_revenue",
                "remote_delivery",
                "trust_burden",
                "competition_gap",
            )
        },
        "timing": dim(),
        "traction": "unknown",
        "investor_signal": "unknown",
        "what_it_takes": {
            "build_type": "operations_heavy",
            "capital": "venture",
            "team": "15+",
            "licence": "Licensed partner needed",
            "time_to_revenue": "over 12 months",
        },
        "service_what_it_takes": {
            "build_type": "tech_enabled_service",
            "capital": "bootstrap",
            "team": "2-5",
            "licence": "none",
            "time_to_revenue": "under 3 months",
        },
        "ai_angle": "",
        "confidence": "medium",
    }


CL = {
    "product_known": True,
    "sector": "logistics and mobility",
    "ai_native": False,
    "business_model": "tech_enabled_service",
}


def facts():
    j, _ = calculate(data(), CL)
    return {
        "startup": {
            "id": 1,
            "name": "Example",
            "country": "India",
            "classification_json": CL,
        },
        "round": {
            "stage": "Series A",
            "amount_text": "$2M",
            "announced_at": date(2026, 11, 20),
        },
        "score": {"id": 1, "scores_json": j},
        "items": [],
    }


def card():
    return {
        "one_liner": "Manage local deliveries for retailers.",
        "what_they_do": "Example handles deliveries.",
        "evidence": "unknown",
        "bangladesh_play": "Offer delivery to retailers. Test a price of ৳100 to test.",
        "global_play": "Provide remote operations to retailers abroad. Invoice for the service.",
        "main_risk": "Deliveries may cost more than customers pay.",
        "already_here": "No one found in a quick search.",
        "competitors": [],
        "ai_angle": "",
        "update": {
            "competition_gap": 3,
            "regulatory_path": 5,
            "note": "A competitor was found.",
        },
        "source_urls": [],
    }


def test_large_business_has_no_capital_or_team_penalty():
    d, r = calculate(data(9), CL)
    assert d["bangladesh"] == 9 and d["global"] == 9
    assert r["bangladesh"] == 9


def test_weighted_mean_and_no_ai_bonus():
    raw = data()
    raw["bangladesh"]["demand"]["score"] = 10
    d, r = calculate(raw, CL)
    cfg = config()["scoring"]["bangladesh"]
    assert d["bangladesh"] == round(
        (7 * sum(cfg.values()) + 3 * cfg["demand"]) / sum(cfg.values()), 1
    )
    assert calculate(raw, CL | {"ai_native": True})[1] == r


def test_unknown_caps_every_dimension_and_timing():
    d, r = calculate(data(10), CL | {"product_known": False})
    assert d["bangladesh"] == d["global"] == 3 and d["confidence"] == "low"
    assert d["timing"]["score"] == 3 and all(
        v["score"] == 3 for dims in d["dimensions"].values() for v in dims.values()
    )


def test_none_concept_score_is_one():
    raw = data()
    raw["concept_global"] = "none"
    assert calculate(raw, CL)[0]["global"] == 1


def test_evidence_bonus_changes_rank_not_score():
    raw = data()
    raw["traction"] = "100 paying customers"
    raw["investor_signal"] = "Named investor"
    d, r = calculate(raw, CL)
    assert d["bangladesh"] == 7 and r["bangladesh"] == 7.5
    raw["confidence"] = "low"
    assert calculate(raw, CL)[1]["global"] == 6.5


def test_schema_enforces_dimension_ranges_and_literals():
    raw = data()
    raw["bangladesh"]["demand"]["score"] = 11
    with pytest.raises(ValueError):
        Scored.model_validate(raw)


def test_research_can_only_change_competition_and_bd_regulation():
    f = facts()
    d, r, change = apply_update(f, card(), "bangladesh")
    assert d["bangladesh"] < 7 and d["global"] == 7
    assert d["dimensions"]["bangladesh"]["demand"]["score"] == 7
    assert change["changed"] and f["score"]["scores_json"]["bangladesh"] == 7
    d, r, change = apply_update(f, card(), "global")
    assert (
        d["bangladesh"] == 7 and d["global"] < 7 and change["regulation_after"] is None
    )


def test_short_card_buttons_attached_and_limits():
    f = facts()
    d = card() | {"scores_snapshot": f["score"]["scores_json"], "market": "bangladesh"}
    d["bangladesh_play"] = "A long sentence with useful reasoning. " * 100
    d["main_risk"] = "A long risk. " * 100
    body = card_text(f, d, "model", date(2026, 11, 21))
    p = buttons_payload(
        "",
        body,
        [("save:1", "Save"), ("skip:1", "Skip"), ("more:1", "More")],
        header="Example " * 50,
        footer="More " * 50,
    )
    i = p["interactive"]
    assert (
        len(body) <= 1024
        and len(i["header"]["text"]) <= 60
        and len(i["footer"]["text"]) <= 60
    )
    assert (
        "Bangladesh 7 · Promising" in body
        and d["one_liner"] in body
        and "model" not in body
    )


def test_full_analysis_preserves_sources_and_long_content():
    f = facts()
    d = card() | {"scores_snapshot": f["score"]["scores_json"], "schema_version": 2}
    c = {
        "title": "Example",
        "body": "short",
        "data_json": d,
        "source_urls": ["https://example.com"],
        "model_id": "test-model",
    }
    text = full_analysis(c, {"economics": "Estimates. " * 1000}, include_model=True)
    parts = split_text(text)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
    assert "test-model" in text and "https://example.com" in text


def test_digest_uses_real_watch_fill_count_and_zero_message():
    from scout.digest import body_text

    body = body_text(20, 2, {"chips": 2}, 3, [], fill=1)
    assert "Slow day: 1 of these is a Watch pick" in body
    assert "No startup scored 5 or more today" in body_text(2, 0, {}, 0, [])
    assert "Only 2 startups scored 5 or more today" in body_text(2, 0, {}, 2, [])


def test_settings_ignore_legacy_skills_and_internal_keys():
    from scout.settings import effective

    class Conn:
        def execute(self, *a):
            return self

        def fetchall(self):
            return [
                {"key": "owner_skills", "value": ["AI"]},
                {"key": "budget", "value": {}},
                {"key": "ambition", "value": "seed"},
            ]

    cfg = effective(Conn())
    assert (
        "owner" not in cfg
        and cfg["ambition"] == "seed"
        and cfg["budget"]["modal_stop_usd"] == 30
    )


def test_resume_monitor_ignores_prompt_and_tool_text():
    import json
    from scripts.resume_redesign import usage_limited_events

    error = "usage_limit_reached"
    assert not usage_limited_events(
        json.dumps(
            {
                "type": "response_item",
                "payload": {"type": "function_call", "arguments": error},
            }
        )
    )
    assert not usage_limited_events(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": error},
            }
        )
    )
    assert usage_limited_events(
        json.dumps(
            {"type": "event_msg", "payload": {"type": "error", "message": error}}
        )
    )


@pytest.mark.parametrize(
    "after,expected,expect_fill", [(4, 4, True), (5.8, 4, True), (6.8, 1, False)]
)
def test_after_research_reselects_candidates_and_marks_drops(
    monkeypatch, after, expected, expect_fill
):
    import json
    from scout.run import _check_and_swap
    from scout import cards as cm
    from tests.test_shortlist import row

    rows = [
        row(1, 8, 2),
        row(2, 7.5, 2, region="india"),
        row(3, 2, 7.5),
        row(4, 6, 2, region="africa"),
    ]

    def stored(sid, bd, gl, market):
        raw = data()
        raw["startup_id"] = sid
        j, _ = calculate(raw, CL)
        # This mechanism test is about the two original tracks.
        j.update({"bangladesh": bd, "global": gl, "service": None})
        return {
            "id": sid,
            "startup_id": sid,
            "market": market,
            "title": str(sid),
            "model_id": "test",
            "body": "card",
            "data_json": card() | {"scores_snapshot": j, "schema_version": 2},
        }

    cache = {
        1: stored(1, after, 2, "bangladesh"),
        2: stored(2, 7.5, 2, "bangladesh"),
        3: stored(3, 2, 7.5, "global"),
    }

    class Conn:
        def execute(self, sql, args=None):
            self.result = None
            if sql.startswith("select * from cards"):
                self.result = cache[args[0]]
            elif sql.startswith("update cards set market"):
                mk, body, d, cid = args
                cache[cid].update(market=mk, body=body, data_json=json.loads(d))
            elif "dropped_after_research" in sql:
                cache[args[0]]["data_json"]["dropped_after_research"] = True
            return self

        def fetchone(self):
            return self.result

    generated = []

    def write(conn, picks, day, newround):
        for r, mk in picks:
            sid = r["startup_id"]
            cache[sid] = stored(sid, r["bd"], r["gl"], mk)
            generated.append(sid)
        return {"card_ids": [r["startup_id"] for r, mk in picks], "failed": []}

    monkeypatch.setattr(cm, "write_cards", write)
    monkeypatch.setattr(cm, "facts_for", lambda *a: {})
    monkeypatch.setattr(cm, "card_text", lambda *a: "rendered")
    cfg = deepcopy(config())
    cfg["diversity"]["explore_rows"] = 0
    cfg["digest_mix"] = {"bangladesh": 3, "global": 2}
    cfg["min_picks"] = 3
    final, swaps, failed, explore = _check_and_swap(
        Conn(),
        {"card_ids": [1, 2, 3], "failed": []},
        [],
        rows,
        date.today(),
        set(),
        cfg,
        3,
    )
    ids = {c["card_id"] for c in final}
    assert expected in ids and len(ids) >= 3
    assert any(c["fill"] for c in final) == expect_fill
    if expected == 4:
        assert cache[1]["data_json"]["dropped_after_research"]


def test_scoring_does_not_turn_last_month_revenue_into_mrr(monkeypatch):
    import json
    from types import SimpleNamespace
    from scout import score as sm
    from scout.llm.schemas import Scoring

    raw = data()
    raw["traction"] = "$1087 MRR"
    raw["global"]["demand_abroad"]["reason"] = "Verified $1087 MRR."
    source = {
        "id": 1,
        "name": "Example",
        "classification": CL,
        "sources": [
            {"title": "TrustMRR", "text": "$1087 verified revenue last 30 days"}
        ],
    }
    monkeypatch.setattr(sm, "startup_material", lambda conn, ids: [source])
    monkeypatch.setattr(sm, "effective", lambda conn: deepcopy(config()))
    monkeypatch.setattr(
        sm.ai,
        "run",
        lambda *a, **k: SimpleNamespace(
            model_id="test", output=Scoring(startups=[Scored.model_validate(raw)])
        ),
    )
    stored = []

    class Conn:
        def execute(self, sql, args=None):
            stored.append(args)

    sm.score(Conn(), [1], date.today())
    result = json.loads(stored[0][2])
    assert "MRR" not in result["traction"] and "last 30 days" in result["traction"]
    assert "MRR" not in result["dimensions"]["global"]["demand_abroad"]["reason"]
    assert result["dimensions"]["bangladesh"]["competition_gap"] == {
        "score": 5,
        "reason": "not checked yet",
    }

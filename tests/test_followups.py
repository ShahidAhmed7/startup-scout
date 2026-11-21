"""Follow-up fixes after the redesign review (2026-11-21)."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from scout.config import config
from scout.llm import guard


class UsageConn:
    """Records the final usage update; `warm` decides whether an earlier private request is still running."""

    def __init__(self, warm):
        self.warm = warm
        self.updates = []

    def execute(self, sql, args=()):
        self.sql = sql
        if sql.strip().startswith("update usage"):
            self.updates.append(args)
        return self

    def fetchone(self):
        return {"x": 1} if self.warm and "provider='qwen'" in self.sql else None


def _run_private(monkeypatch, warm):
    cfg = deepcopy(config())
    monkeypatch.setattr(guard, "config", lambda: cfg)
    conn = UsageConn(warm)

    @contextmanager
    def connection(_):
        yield conn

    monkeypatch.setattr(guard, "usage_connection", connection)
    monkeypatch.setattr(guard, "reserve_attempt", lambda *a: 7)
    guard._slots.clear()
    guard._next.clear()

    async def reply(messages, info):
        raise AssertionError("the private entry must stream (Modal cuts silent requests at 150 s)")

    async def stream(messages, info):
        for chunk in ("o", "k"):
            yield chunk

    model = guard.GuardedModel("private", FunctionModel(reply, stream_function=stream), "private")
    from pydantic_ai import Agent

    async def go():
        return await Agent(model).run("hello")

    result = asyncio.run(go())
    assert result.output == "ok"
    status, _, _, seconds, gpu, attempt = conn.updates[-1]
    return status, seconds, gpu, attempt, cfg


def test_cold_private_request_pays_cold_start(monkeypatch):
    status, seconds, gpu, attempt, cfg = _run_private(monkeypatch, warm=False)
    assert status == "ok" and attempt == 7
    assert gpu == seconds + 120  # cold-start time is inside `seconds` (readiness wait); plus the idle tail


def test_warm_private_request_pays_only_its_own_time(monkeypatch):
    _, seconds, gpu, _, _ = _run_private(monkeypatch, warm=True)
    assert gpu == seconds


def test_more_message_labels_deep_dive_sections():
    from scout.cards import full_analysis

    dims = {"score": 6, "reason": "r"}
    card = {
        "title": "Acme",
        "body": "",
        "source_urls": ["https://a.example"],
        "data_json": {
            "schema_version": 2,
            "scores_snapshot": {
                "bangladesh": 6.6,
                "global": 5.0,
                "timing": dims,
                "dimensions": {
                    "bangladesh": {k: dims for k in ("demand", "readiness", "competition_gap",
                                                     "regulatory_path", "economics")},
                    "global": {k: dims for k in ("demand_abroad", "remote_delivery", "cost_advantage",
                                                 "trust_burden", "competition_gap")},
                },
            },
        },
    }
    deep = {"landscape": "L", "failure_check": "F", "economics": "E", "regulatory_route": "R",
            "first_customers": "C", "validation_30_days": "1. a 2. b 3. c", "risks": ["x", "y", "z"],
            "source_urls": ["https://b.example"]}
    text = full_analysis(card, deep)
    for title in ("_Landscape_", "_Who tried and failed_", "_30-day test plan_", "_Risks_"):
        assert title in text
    assert "1. a\n2. b\n3. c" in text and "- x\n- y\n- z" in text
    assert "Bangladesh 7 · Promising" in text  # half-up rounding of 6.6 and the verdict


def test_saved_idea_alert_ignores_scale_switch_and_decimals(monkeypatch):
    from scout import watches

    sent = []
    monkeypatch.setattr(watches.outbox, "deliver", lambda conn, payloads, kind: sent.append(kind))
    rows = [
        # old 1-10 integer scores stored before the redesign, new decimal scores now: baseline reset only
        {"id": 1, "name": "A", "card_id": None, "last_scores": {"bangladesh": 7, "global": 3},
         "j": {"bangladesh": 5.2, "global": 6.1, "schema_version": 2}},
        # same displayed scores after rounding: no alert
        {"id": 2, "name": "B", "card_id": None,
         "last_scores": {"bangladesh": 7, "global": 6, "schema_version": 2},
         "j": {"bangladesh": 6.5, "global": 6.4, "schema_version": 2}},
        # a real change: alert
        {"id": 3, "name": "C", "card_id": None,
         "last_scores": {"bangladesh": 7, "global": 6, "schema_version": 2},
         "j": {"bangladesh": 5.4, "global": 6.0, "schema_version": 2}},
    ]

    class Conn:
        def execute(self, sql, args=()):
            self.sql = sql
            return self

        def fetchall(self):
            return [] if "from watches" in self.sql else rows

    assert watches.check(Conn(), None) == 1 and sent == ["idea_alert"]


def _overload_harness(monkeypatch, plan):
    """plan: entry -> list of outcomes per call ('503' or 'ok'); returns the calls made."""
    import tests.test_gemini as tg
    from pydantic_ai.exceptions import ModelUnavailableError
    from pydantic_ai.profiles import ModelProfile
    from scout.llm import registry, run as ai

    cfg = deepcopy(config())
    for m in cfg["models"].values():
        m["rpm"] = 0
    for module in (registry, guard, ai):
        monkeypatch.setattr(module, "config", lambda: cfg)

    @contextmanager
    def connection(_):
        yield tg.Conn()

    monkeypatch.setattr(guard, "usage_connection", connection)
    monkeypatch.setattr(guard, "reserve_attempt", lambda *a: 1)
    monkeypatch.setattr(ai, "BACKOFF", (0, 0))
    monkeypatch.setattr(ai, "CHAIN_PATIENCE", 0)
    monkeypatch.setattr(ai.random, "uniform", lambda a, b: 0)
    for d in (guard._slots, guard._next, guard._busy):
        d.clear()
    calls = []

    def get(entry):
        async def function(messages, info):
            calls.append(entry)
            outcome = plan[entry].pop(0) if plan[entry] else "ok"
            if outcome == "503":
                raise ModelUnavailableError(model_name="m", message="high demand")
            return ModelResponse(parts=[TextPart('{"on_topic":true,"answer":"fine"}')])

        return FunctionModel(function, profile=ModelProfile(supports_json_schema_output=True))

    monkeypatch.setattr(registry, "get_model", get)
    return calls


def test_overload_retries_same_key_before_moving_on(monkeypatch):
    from scout.llm import run as ai
    from scout.llm.schemas import Ask

    monkeypatch.setenv("SCOUT_DEV", "1")
    calls = _overload_harness(monkeypatch, {"fast_main": ["503", "503"], "fast_backup": []})
    out = ai.run("classify", "q", Ask)
    assert out.entry == "fast_main" and calls == ["fast_main"] * 3


def test_chain_waits_and_tries_again_when_every_model_is_overloaded(monkeypatch):
    from scout.llm import run as ai
    from scout.llm.schemas import Ask

    monkeypatch.setenv("SCOUT_DEV", "1")
    calls = _overload_harness(monkeypatch, {"fast_main": ["503"] * 3, "fast_backup": ["503"] * 3})
    out = ai.run("classify", "q", Ask)
    assert out.output.answer == "fine" and len(calls) == 7  # 3 + 3 overloads, then the second pass


def test_overload_is_not_counted_against_daily_caps():
    from pydantic_ai.exceptions import ModelHTTPError, ModelUnavailableError

    assert guard.attempt_status(ModelUnavailableError(model_name="m", message="x")) == "unavailable"
    assert guard.attempt_status(ModelHTTPError(503, "m")) == "unavailable"
    assert guard.attempt_status(ModelHTTPError(429, "m")) == "quota"
    assert guard.attempt_status(ModelHTTPError(500, "m")) == "server"
    assert "unavailable" in guard.NOT_COUNTED and "server" not in guard.NOT_COUNTED
    assert guard.transient(ModelHTTPError(500, "m")) and not guard.transient(ModelHTTPError(400, "m"))


def test_funding_round_is_not_traction_and_caps_demand_abroad():
    from scout.score import evidence_rules, traction_known

    for text in ("euro 4.28 million seed round", "raised Rs 485 crore in Series E", "$30 million seed raised",
                 "unknown", ""):
        assert not traction_known(text), text
    for text in ("2.2 million registered users", "$7M Seed, $20M run rate", "$2,146 verified revenue last 30 days",
                 "grew 300% year on year", "1,200 merchants"):
        assert traction_known(text), text

    def raw(traction, demand):
        return {"traction": traction, "global": {"demand_abroad": {"score": demand, "reason": "big seed round."}}}

    capped = evidence_rules(raw("$4M seed round", 9))
    assert capped["traction"] == "unknown" and capped["global"]["demand_abroad"]["score"] == 7
    assert "without traction numbers" in capped["global"]["demand_abroad"]["reason"]
    kept = evidence_rules(raw("40,000 paying users", 9))
    assert kept["traction"] == "40,000 paying users" and kept["global"]["demand_abroad"]["score"] == 9
    low = evidence_rules(raw("unknown", 5))
    assert low["global"]["demand_abroad"] == {"score": 5, "reason": "big seed round."}


def test_place_label_never_presents_a_source_region_as_the_country():
    from scout.normalize import place_label

    assert place_label("United States", "india") == "USA"
    assert place_label("", "india") == "India news"
    assert place_label(None, "usa") == "USA news"
    assert place_label(None, "unknown") == "country unknown"
    assert place_label("Global", None) == "country unknown"


def test_score_line_and_more_handle_a_market_with_no_version():
    from scout.cards import full_analysis, score_line

    dim = {"score": 1, "reason": "none"}
    j = {"bangladesh": 7.4, "global": 1.0, "concept_bd": "Value fashion stores.", "concept_global": "none",
         "timing": {"score": 7, "reason": "t"},
         "dimensions": {"bangladesh": {"economics": {"score": 7, "reason": "e"}, "demand": {"score": 9, "reason": "d"},
                                       "readiness": {"score": 9, "reason": "r"},
                                       "competition_gap": {"score": 4, "reason": "c"},
                                       "regulatory_path": {"score": 7, "reason": "g"}},
                        "global": {k: dim for k in ("trust_burden", "demand_abroad", "cost_advantage",
                                                    "competition_gap", "remote_delivery")}}}
    assert score_line(j) == "Bangladesh 7 · Promising   |   Global: no version"
    text = full_analysis({"title": "X", "body": "", "source_urls": [],
                          "data_json": {"schema_version": 2, "scores_snapshot": j}})
    assert "Bangladesh 7: demand 9 · readiness 9 · competition 4 · regulation 7 · economics 7" in text
    assert "Global: no sensible version for this market." in text and "trust 1" not in text
    j["concept_global"] = "Remote styling service."
    j["global"] = 3.2
    assert score_line(j).endswith("Global 3 · Low")


def test_shown_verdict_matches_the_shown_number():
    from scout.score import half_up, shown_verdict

    for value, shown, word in [(7.95, 8, "Strong"), (7.4, 7, "Promising"), (6.5, 7, "Promising"),
                               (6.45, 6, "Watch"), (5.0, 5, "Watch"), (4.4, 4, "Low"), (1.0, 1, "Low")]:
        assert (half_up(value), shown_verdict(value)) == (shown, word)


def test_country_codes_and_placeholders():
    from scout.normalize import region_of, short_country

    assert short_country("AU") == "Australia" and short_country("SG") == "Singapore"
    assert region_of("SG") == "SE Asia" and region_of("NG") == "Africa"
    assert short_country("Unspecified") is None and short_country("US") == "USA"


def test_hidden_trustmrr_listing_is_never_a_known_product():
    from scout.classify import hidden_listing

    assert hidden_listing("Stealth Company") and hidden_listing(" stealth startup")
    assert not hidden_listing("Redstring: Murder Mystery") and not hidden_listing("Stealthy Socks Ltd")


def test_sentences_keep_prices_and_examples_together():
    from scout.cards import sentences

    text = "The offer is financing at Tk. 1,500 per month. Sell via e.g. Paddle. Raised $2.5M."
    assert sentences(text) == [
        "The offer is financing at Tk. 1,500 per month.",
        "Sell via e.g. Paddle.",
        "Raised $2.5M.",
    ]


def test_row_title_drops_trailing_note_before_cutting_the_name():
    from scout.whatsapp.formats import row_title

    assert row_title("Archer (formerly hackajob)", "BD 7") == "Archer · BD 7"
    assert row_title("Mission Mobile", "BD 7") == "Mission Mobile · BD 7"
    assert len(row_title("A Very Long Company Name Here", "Global 8")) <= 24


def test_prefilter_catches_backs_investments_and_euro_amounts():
    from scout.extract import prefilter

    assert prefilter("Terra Industries backs Community Wolf crime platform", "")
    assert prefilter("Daniel Ek is backing a fusion startup", "")
    assert prefilter("SIFC starts attracting global investments for local startups", "")
    assert prefilter("Berlin's Acme picks up €4M", "")
    assert not prefilter("Pakistan wins Blind T20 Asia Cup", "")

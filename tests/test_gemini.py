"""Real Pydantic AI adapters with deterministic FunctionModel responses; no service calls."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.profiles import ModelProfile
from scout.llm import registry, guard, run as ai
from scout.llm.schemas import Ask
from scout.config import config


class Conn:
    def execute(self, *a, **kw):
        return self

    def fetchone(self):
        return {"n": 0, "id": 1}

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


@pytest.fixture
def harness(monkeypatch):
    cfg = deepcopy(config())
    for m in cfg["models"].values():
        m["rpm"] = 0
    for module in (registry, guard, ai):
        monkeypatch.setattr(module, "config", lambda: cfg)

    @contextmanager
    def connection(conn):
        yield Conn()

    monkeypatch.setattr(guard, "usage_connection", connection)
    monkeypatch.setattr(guard, "reserve_attempt", lambda *a: 1)
    guard._slots.clear()
    guard._next.clear()
    guard._busy.clear()
    calls = []
    fail = set()
    invalid = set()

    def get(entry):
        async def function(messages, info):
            calls.append(entry)
            await asyncio.sleep(0.01)
            if entry in fail:
                raise RuntimeError("provider unavailable")
            return ModelResponse(
                parts=[
                    TextPart(
                        "invalid"
                        if entry in invalid
                        else '{"on_topic":true,"answer":"supported"}'
                    )
                ]
            )

        async def stream(messages, info):
            # streamed entries (the private model) get the same reply in chunks
            response = await function(messages, info)
            text = response.parts[0].content
            for i in range(0, len(text), 8):
                yield text[i : i + 8]

        return FunctionModel(
            function,
            stream_function=stream,
            profile=ModelProfile(supports_json_schema_output=True),
        )

    monkeypatch.setattr(registry, "get_model", get)
    return cfg, calls, fail, invalid


def test_chain_order_and_validation_failure_fallback(harness):
    cfg, calls, fail, invalid = harness
    fail.update(["strong_main", "strong_backup"])
    invalid.add("fast_main")
    assert ai.run("card", "public", Ask).entry == "fast_backup"
    assert calls == ["strong_main"] * 2 + ["strong_backup"] * 2 + ["fast_main"] * 2 + [
        "fast_backup"
    ]


def test_parallel_spreads_keys(harness):
    cfg, calls, fail, invalid = harness
    out = ai.run_many("card", ["x"] * 4, Ask)
    assert len(out) == 4 and all(isinstance(r, ai.Result) for r in out)
    assert calls.count("strong_main") == 2 and calls.count("strong_backup") == 2


def test_private_never_leaves_private_chain(harness):
    cfg, calls, fail, invalid = harness
    assert ai.run("note", "private note", Ask, private=True).entry == "private"
    fail.add("private")
    with pytest.raises(ai.LLMUnavailable):
        ai.run("kit", "private note", Ask, private=True)
    assert all(e == "private" for e in calls)


def test_forced_google_failure(harness, monkeypatch):
    monkeypatch.setenv("FORCE_GEMINI_FAIL", "1")
    assert ai.run("card", "x", Ask).entry == "private"
    assert harness[1] == ["private"]


def test_dev_uses_config_without_strong_fallback(harness, monkeypatch):
    monkeypatch.setenv("SCOUT_DEV", "1")
    assert ai.run("score", "x", Ask).entry == "fast_main"
    assert harness[1] == ["fast_main"]


def test_extra_validation_retries_then_falls_back(harness):
    def reject(out):
        if harness[1][-1] == "strong_main":
            raise ValueError("wrong IDs")

    assert ai.run("score", "x", Ask, validate=reject).entry == "strong_backup"
    assert harness[1] == ["strong_main", "strong_main", "strong_backup"]


def test_provider_string_can_change_without_router_change(harness):
    harness[0]["models"]["strong_main"]["model"] = "openai-chat:any-compatible-model"
    r = ai.run("score", "x", Ask)
    assert (
        r.model == "openai-chat:any-compatible-model" and r.output.answer == "supported"
    )


def test_cap_and_reserve(monkeypatch):
    class Usage(Conn):
        used = 14

        def fetchone(self):
            return {"n": self.used, "id": 99}

    c = Usage()
    with pytest.raises(guard.CapReached):
        guard.reserve_attempt(c, "strong_main", "score")
    assert guard.reserve_attempt(c, "strong_main", "ask") == 99
    c.used = 18
    with pytest.raises(guard.CapReached):
        guard.reserve_attempt(c, "strong_main", "ask")


def test_native_settings():
    assert registry.settings("strong_main")["google_thinking_config"] == {
        "thinking_level": "HIGH"
    }
    assert registry.settings("strong_main")["max_tokens"] == 65536


def test_registry_constructs_an_openai_compatible_model_without_environment_key(
    monkeypatch,
):
    cfg = deepcopy(config())
    cfg["models"]["test_other"] = {
        "model": "openai-chat:example/model",
        "key_env": "OTHER_TEST_KEY",
        "base_url_env": "OTHER_TEST_URL",
    }
    monkeypatch.setenv("OTHER_TEST_KEY", "non-secret-test-key")
    monkeypatch.setenv("OTHER_TEST_URL", "https://example.invalid/v1")
    monkeypatch.setattr(registry, "config", lambda: cfg)
    registry.get_model.cache_clear()
    model = registry.get_model("test_other")
    assert model.model_name == "example/model"
    assert str(model.client.base_url) == "https://example.invalid/v1/"
    registry.get_model.cache_clear()


def test_budget_guard_prevents_private_request(monkeypatch):
    from scout import budget

    monkeypatch.setattr(budget, "month_spend", lambda conn: 30)
    with pytest.raises(guard.CapReached):
        guard.reserve_attempt(Conn(), "private", "note")


def test_caps_count_old_labels_and_reject_concurrent_reservations(monkeypatch):
    seen = []

    class Usage(Conn):
        def execute(self, sql, args=None):
            seen.append((sql, args))
            return self

        def fetchone(self):
            return {"n": 18, "id": 1}

    with pytest.raises(guard.CapReached):
        guard.reserve_attempt(Usage(), "strong_backup", "ask")
    query = next(args for sql, args in seen if "count(*)" in sql)
    assert "backup" in query[1] and "strong_backup" in query[1]
    assert any("pg_advisory_xact_lock" in sql for sql, args in seen)

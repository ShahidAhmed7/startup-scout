import json

import httpx
import pytest

from scout.llm import gemini


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(gemini, "_throttle", lambda *a: None)
    monkeypatch.setattr(gemini, "_backoff", lambda: None)


def ok_response(payload: dict | str):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    })


def mock(responses_by_key: dict):
    """responses_by_key: {'main-key': [resp, ...], 'backup-key': [...]}; records which keys were used."""
    calls = []

    def handler(request: httpx.Request):
        key = request.headers["x-goog-api-key"]
        calls.append(key)
        return responses_by_key[key].pop(0)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def test_main_key_used_first():
    http, calls = mock({"main-key": [ok_response({"ok": True})]})
    r = gemini.generate("flash_lite", "x", kind="test", schema=SCHEMA, http=http)
    assert r.key_label == "main" and r.data == {"ok": True}
    assert calls == ["main-key"]


@pytest.mark.parametrize("status", [429, 401, 403])
def test_backup_key_used_at_once_on_quota_or_auth_errors(status):
    http, calls = mock({
        "main-key": [httpx.Response(status, json={"error": {"message": "nope"}})],
        "backup-key": [ok_response({"ok": True})],
    })
    r = gemini.generate("flash_lite", "x", kind="test", schema=SCHEMA, http=http)
    assert r.key_label == "backup"
    assert calls == ["main-key", "backup-key"]


@pytest.mark.parametrize("status", [500, 503])
def test_server_errors_retry_main_once_then_backup(status):
    http, calls = mock({
        "main-key": [httpx.Response(status, json={}), httpx.Response(status, json={})],
        "backup-key": [ok_response({"ok": True})],
    })
    r = gemini.generate("flash_lite", "x", kind="test", schema=SCHEMA, http=http)
    assert r.key_label == "backup"
    assert calls == ["main-key", "main-key", "backup-key"]


def test_overload_that_clears_on_retry_stays_on_main():
    http, calls = mock({"main-key": [httpx.Response(503, json={}), ok_response({"ok": True})]})
    r = gemini.generate("flash_lite", "x", kind="test", schema=SCHEMA, http=http)
    assert r.key_label == "main" and calls == ["main-key", "main-key"]


def test_both_keys_fail_raises_unavailable():
    http, _ = mock({
        "main-key": [httpx.Response(429, json={})],
        "backup-key": [httpx.Response(503, json={}), httpx.Response(503, json={})],
    })
    with pytest.raises(gemini.GeminiUnavailable) as e:
        gemini.generate("hydrogen", "x", kind="test", http=http)
    assert [label for label, _ in e.value.errors] == ["main", "backup"]
    assert [err.kind for _, err in e.value.errors] == ["quota", "server"]


def test_bad_request_does_not_switch_keys():
    http, calls = mock({"main-key": [httpx.Response(400, json={"error": {"message": "bad schema"}})]})
    with pytest.raises(gemini.GeminiError) as e:
        gemini.generate("flash_lite", "x", kind="test", http=http)
    assert e.value.kind == "bad_request" and calls == ["main-key"]


def test_invalid_json_is_retried_once_on_same_key():
    http, calls = mock({"main-key": [ok_response("not json"), ok_response({"ok": True})]})
    r = gemini.generate("flash_lite", "x", kind="test", schema=SCHEMA, http=http)
    assert r.data == {"ok": True} and calls == ["main-key", "main-key"]


def test_failed_validation_twice_moves_to_backup():
    def must_be_true(d):
        if d.get("ok") is not True:
            raise ValueError("ok must be true")

    http, calls = mock({
        "main-key": [ok_response({"ok": False}), ok_response({"ok": False})],
        "backup-key": [ok_response({"ok": True})],
    })
    r = gemini.generate("flash_lite", "x", kind="test", schema=SCHEMA, validate=must_be_true, http=http)
    assert r.key_label == "backup" and calls == ["main-key", "main-key", "backup-key"]


def test_thought_parts_are_ignored():
    resp = httpx.Response(200, json={"candidates": [{"content": {"parts": [
        {"text": "thinking...", "thought": True}, {"text": "answer"}]}, "finishReason": "STOP"}]})
    http, _ = mock({"main-key": [resp]})
    assert gemini.generate("hydrogen", "x", kind="test", http=http).text == "answer"


def test_missing_main_key_goes_straight_to_backup(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY_MAIN")
    http, calls = mock({"backup-key": [ok_response("hi")]})
    assert gemini.generate("hydrogen", "x", kind="test", http=http).key_label == "backup"
    assert calls == ["backup-key"]


@pytest.mark.parametrize("status,body,kind", [
    (429, "", "quota"),
    (400, '{"error":{"status":"RESOURCE_EXHAUSTED"}}', "quota"),
    (400, "API key not valid. Please pass a valid API key.", "auth"),
    (403, "", "auth"),
    (500, "", "server"),
    (404, "model not found", "bad_request"),
])
def test_classify(status, body, kind):
    assert gemini.classify(status, body) == kind


def test_calls_use_owner_defaults_high_thinking_and_big_output():
    sent = {}

    def handler(request: httpx.Request):
        sent.update(json.loads(request.content))
        return ok_response({"ok": True})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    gemini.generate("hydrogen", "x", kind="test", schema=SCHEMA, http=http)
    gen = sent["generationConfig"]
    assert gen["thinkingConfig"] == {"thinkingLevel": "high"}
    assert gen["maxOutputTokens"] == 65536
    assert "temperature" not in gen  # model default, as Google advises for Gemini 3
    assert gen["responseMimeType"] == "application/json" and gen["responseJsonSchema"] == SCHEMA


def test_chain_card_falls_back_to_flash_lite_then_gemma():
    """Owner's order: Hydrogen (both keys) -> Flash-Lite (both keys) -> Gemma -> (Qwen, phase 3)."""
    seen = []

    def handler(request: httpx.Request):
        model = request.url.path.split("/models/")[1].split(":")[0]
        seen.append((model, request.headers["x-goog-api-key"]))
        if model == "gemma-4-31b-it":
            return ok_response({"ok": True})
        return httpx.Response(429, json={})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    r = gemini.generate_chain("hydrogen", "x", kind="card", schema=SCHEMA, http=http)
    assert r.model_id == "gemma-4-31b-it" and r.key_label == "main"
    assert seen == [
        ("gemini-4-hydrogen", "main-key"), ("gemini-4-hydrogen", "backup-key"),
        ("gemini-3.5-flash-lite", "main-key"), ("gemini-3.5-flash-lite", "backup-key"),
        ("gemma-4-31b-it", "main-key"),
    ]


def test_chain_stops_at_first_success():
    http, calls = mock({"main-key": [ok_response({"ok": True})]})
    r = gemini.generate_chain("hydrogen", "x", kind="card", schema=SCHEMA, http=http)
    assert r.model_id == "gemini-4-hydrogen" and calls == ["main-key"]


def test_chain_all_fail_lists_every_error_for_qwen_router():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, json={})))
    with pytest.raises(gemini.GeminiUnavailable) as e:
        gemini.generate_chain("hydrogen", "x", kind="card", http=http)
    assert [label for label, _ in e.value.errors] == [
        "hydrogen/main", "hydrogen/backup", "flash_lite/main", "flash_lite/backup", "gemma/main", "gemma/backup"]


def test_gemma_output_limit_is_respected():
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return ok_response("hi")

    gemini.generate("gemma", "x", kind="test", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert sent["generationConfig"]["maxOutputTokens"] == 32768


class FakeUsage:
    """Stands in for the database: counts Hydrogen calls already made today per key."""

    def __init__(self, monkeypatch, used: dict):
        from scout.db import repo
        self.logged = []
        monkeypatch.setattr(repo, "gemini_calls_today", lambda conn, model_id, label: used.get((model_id, label), 0))
        monkeypatch.setattr(repo, "record_usage", lambda conn, **kw: self.logged.append(kw))


def test_run_job_uses_hydrogen_for_scoring_and_flash_lite_for_extraction():
    seen = []

    def handler(request):
        seen.append(request.url.path.split("/models/")[1].split(":")[0])
        return ok_response({"ok": True})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert gemini.run_job("score", "x", schema=SCHEMA, http=http).model_id == "gemini-4-hydrogen"
    assert gemini.run_job("extract", "x", schema=SCHEMA, http=http).model_id == "gemini-3.5-flash-lite"
    assert seen == ["gemini-4-hydrogen", "gemini-3.5-flash-lite"]


def test_pipeline_leaves_reserved_hydrogen_calls_for_chat(monkeypatch):
    # 2 keys x 18 cap = 36; reserve 8 -> pipeline stops using Hydrogen at 28 calls.
    FakeUsage(monkeypatch, {("gemini-4-hydrogen", "main"): 18, ("gemini-4-hydrogen", "backup"): 10})
    seen = []

    def handler(request):
        seen.append(request.url.path.split("/models/")[1].split(":")[0])
        return ok_response({"ok": True})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    r = gemini.run_job("card", "x", schema=SCHEMA, http=http, conn=object())
    assert r.model_id == "gemini-3.5-flash-lite"          # pipeline fell back
    assert "gemini-4-hydrogen" not in seen
    r = gemini.run_job("deep_dive", "x", schema=SCHEMA, http=http, conn=object())
    assert r.model_id == "gemini-4-hydrogen" and r.key_label == "backup"   # chat may use the reserve


def test_pipeline_uses_hydrogen_below_the_reserve(monkeypatch):
    FakeUsage(monkeypatch, {("gemini-4-hydrogen", "main"): 18, ("gemini-4-hydrogen", "backup"): 9})
    http, calls = mock({"backup-key": [ok_response({"ok": True})]})
    r = gemini.run_job("score", "x", schema=SCHEMA, http=http, conn=object())
    assert r.model_id == "gemini-4-hydrogen" and r.key_label == "backup"


# ---------- both keys in parallel (owner, 2026-11-19) ----------

REAL_THROTTLE = gemini._throttle  # captured at import, before the autouse fixture patches it


def test_parallel_jobs_use_both_keys_at_the_same_time():
    import threading
    import time
    lock = threading.Lock()
    state = {"now": 0, "max": 0}
    keys = []

    def handler(request):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            keys.append(request.headers["x-goog-api-key"])
        time.sleep(0.2)
        with lock:
            state["now"] -= 1
        return ok_response({"ok": True})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    results = gemini.run_parallel([("card", f"card {i}", {"schema": SCHEMA, "http": http}) for i in range(4)])
    assert all(r.model_id == "gemini-4-hydrogen" for r in results)
    assert state["max"] == 2                       # two requests in flight at once
    assert keys.count("main-key") == 2 and keys.count("backup-key") == 2


def test_parallel_one_failed_task_keeps_the_others():
    def handler(request):
        if b"bad" in request.content:
            return httpx.Response(400, json={"error": {"message": "bad request"}})
        return ok_response({"ok": True})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    results = gemini.run_parallel([("score", p, {"schema": SCHEMA, "http": http}) for p in ("good", "bad", "good")])
    assert results[0].data == {"ok": True} and results[2].data == {"ok": True}
    assert isinstance(results[1], gemini.GeminiError) and results[1].kind == "bad_request"


def test_parallel_failover_when_one_key_is_out_of_quota():
    def handler(request):
        if request.headers["x-goog-api-key"] == "main-key":
            return httpx.Response(429, json={})
        return ok_response({"ok": True})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    results = gemini.run_parallel([("card", f"c{i}", {"schema": SCHEMA, "http": http}) for i in range(3)])
    assert [r.key_label for r in results] == ["backup", "backup", "backup"]


def test_throttle_wait_on_one_key_never_delays_the_other(monkeypatch):
    slept = []
    monkeypatch.setattr(gemini.time, "sleep", lambda s: slept.append(s))
    gemini._next_slot.clear()
    REAL_THROTTLE("main", "hydrogen")
    REAL_THROTTLE("main", "hydrogen")     # same key: must wait ~12.5s (5 requests/minute)
    REAL_THROTTLE("backup", "hydrogen")   # other key: no wait
    gemini._next_slot.clear()
    assert len(slept) == 1 and 12 <= slept[0] <= 12.5

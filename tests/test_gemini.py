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
        gemini.generate("flash", "x", kind="test", http=http)
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
    assert gemini.generate("flash", "x", kind="test", http=http).text == "answer"


def test_missing_main_key_goes_straight_to_backup(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY_MAIN")
    http, calls = mock({"backup-key": [ok_response("hi")]})
    assert gemini.generate("flash", "x", kind="test", http=http).key_label == "backup"
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
    gemini.generate("flash", "x", kind="test", schema=SCHEMA, http=http)
    gen = sent["generationConfig"]
    assert gen["thinkingConfig"] == {"thinkingLevel": "high"}
    assert gen["maxOutputTokens"] == 65536
    assert "temperature" not in gen  # model default, as Google advises for Gemini 3
    assert gen["responseMimeType"] == "application/json" and gen["responseJsonSchema"] == SCHEMA

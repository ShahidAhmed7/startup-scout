import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from scout.whatsapp import webhook
from tests.conftest import FAKE_OWNER

SECRET = "app-secret"


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return self


def make(monkeypatch, seen_ids):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    spawned = []

    def fake_store(conn, msg):
        if msg["id"] in seen_ids:
            return False
        seen_ids.add(msg["id"])
        return True

    monkeypatch.setattr(webhook, "store_inbound", fake_store)
    app = webhook.create_app(spawn=spawned.append, connect=FakeConn)
    return TestClient(app), spawned


def signed(body: dict):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"x-hub-signature-256": sig, "content-type": "application/json"}


def payload(sender, wamid):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"from": sender, "id": wamid, "timestamp": "1790000000", "type": "text", "text": {"body": "hi"}}]}}]}]}


def test_verify_handshake(monkeypatch):
    client, _ = make(monkeypatch, set())
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
    assert bad.status_code == 403


def test_bad_signature_rejected(monkeypatch):
    client, spawned = make(monkeypatch, set())
    raw, headers = signed(payload(FAKE_OWNER, "w1"))
    headers["x-hub-signature-256"] = "sha256=" + "0" * 64
    assert client.post("/webhook", content=raw, headers=headers).status_code == 401
    assert spawned == []


def test_owner_message_is_stored_and_spawned_once(monkeypatch):
    client, spawned = make(monkeypatch, set())
    raw, headers = signed(payload(FAKE_OWNER, "w1"))
    assert client.post("/webhook", content=raw, headers=headers).status_code == 200
    assert client.post("/webhook", content=raw, headers=headers).status_code == 200  # Meta retry
    assert spawned == ["w1"]


def test_other_numbers_are_ignored(monkeypatch):
    client, spawned = make(monkeypatch, set())
    raw, headers = signed(payload("8801999999999", "w2"))
    assert client.post("/webhook", content=raw, headers=headers).status_code == 200
    assert spawned == []

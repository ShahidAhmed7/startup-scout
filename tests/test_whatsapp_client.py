import httpx
import pytest

from scout.whatsapp import client
from tests.conftest import FAKE_OWNER


def test_dry_run_prints_and_does_not_send(capsys):
    wamid = client.send_text("hello")
    out = capsys.readouterr().out
    assert wamid.startswith("dry-")
    assert "[DRY_RUN]" in out and FAKE_OWNER in out and "hello" in out


def test_refuses_any_number_but_the_owner():
    payload = {"messaging_product": "whatsapp", "to": "8801999999999", "type": "text", "text": {"body": "x"}}
    with pytest.raises(client.NotOwnerError):
        client._post(payload)


def test_owner_number_formats_are_normalised():
    payload = {"messaging_product": "whatsapp", "to": "+880 1700-000000", "type": "text", "text": {"body": "x"}}
    assert client._post(payload).startswith("dry-")


def test_templates_are_never_sent():
    payload = {"messaging_product": "whatsapp", "to": FAKE_OWNER, "type": "template", "template": {}}
    with pytest.raises(ValueError):
        client._post(payload)


def test_long_text_is_cut_to_whatsapp_limit(capsys):
    client.send_text("a" * 5000)
    assert "a" * 4097 not in capsys.readouterr().out


def test_real_send_uses_owner_and_returns_id(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}]})

    wamid = client.send_text("hi", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert wamid == "wamid.ABC"
    assert seen["url"].endswith("/123/messages")
    assert seen["auth"] == "Bearer test-token"
    assert FAKE_OWNER in seen["body"]


def test_api_error_is_raised(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")

    def handler(request):
        return httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}})

    with pytest.raises(client.WhatsAppError) as e:
        client.send_text("hi", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert e.value.code == 131047

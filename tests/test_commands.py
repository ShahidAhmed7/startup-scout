import pytest

from scout.whatsapp import commands


def test_parse_text_button_and_list():
    assert commands.parse({"type": "text", "text": {"body": " hi "}}) == ("text", "hi")
    assert commands.parse({"type": "interactive", "interactive": {"type": "button_reply",
                                                                  "button_reply": {"id": "save:3"}}}) == ("button", "save:3")
    assert commands.parse({"type": "interactive", "interactive": {"type": "list_reply",
                                                                  "list_reply": {"id": "card:9"}}}) == ("button", "card:9")
    assert commands.parse({"type": "image"}) == ("other", "image")


@pytest.fixture
def capture(monkeypatch):
    out = {"replies": [], "settings": {}}
    monkeypatch.setattr(commands, "reply", lambda conn, body, kind="reply": out["replies"].append(body))
    monkeypatch.setattr(commands, "set_value", lambda conn, k, v: out["settings"].__setitem__(k, v))
    monkeypatch.setattr(commands.outbox, "flush_pending", lambda conn: 0)
    return out


class Conn:
    def execute(self, *a, **k):
        class R:
            def fetchall(self):
                return []

            def fetchone(self):
                return None
        return R()


def text(body):
    return {"type": "text", "text": {"body": body}}


@pytest.mark.parametrize("msg,value", [("time 7:30", "07:30"), ("TIME 19:05", "19:05"), ("time 7pm", "19:00"),
                                       ("time 12:15 am", "00:15")])
def test_time_command(capture, msg, value):
    commands.handle(Conn(), text(msg))
    assert capture["settings"]["digest_time"] == value


def test_bad_time_rejected(capture):
    commands.handle(Conn(), text("time 25:00"))
    assert "digest_time" not in capture["settings"]


def test_mix_command(capture):
    commands.handle(Conn(), text("mix 2+3"))
    assert capture["settings"]["digest_mix"] == {"bangladesh": 2, "global": 3}
    commands.handle(Conn(), text("mix 8+5"))
    assert capture["settings"]["digest_mix"] == {"bangladesh": 2, "global": 3}  # over 10 rejected


def test_skip_add_and_remove(capture):
    commands.handle(Conn(), text("skip add crypto"))
    assert "crypto" in capture["settings"]["skip_list"]
    commands.handle(Conn(), text("skip remove space"))
    assert "space" not in capture["settings"]["skip_list"]


def test_not_a_general_chatbot(capture):
    commands.handle(Conn(), text("What is the capital of France?"))
    assert capture["replies"][-1] == commands.NOT_CHAT
    commands.handle(Conn(), text("write me a poem"))
    assert capture["replies"][-1] == commands.UNKNOWN


def test_help(capture):
    commands.handle(Conn(), text("Help"))
    assert capture["replies"][-1].startswith("*Commands*")

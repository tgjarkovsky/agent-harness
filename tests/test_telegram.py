import io
import json
import urllib.error

import pytest

from harness.telegram import Telegram, TelegramError


class FakeUrlopen:
    def __init__(self, result=None, error=None):
        self.requests, self.result, self.error = [], result, error

    def __call__(self, req, timeout):
        self.requests.append((req.full_url, json.loads(req.data) if req.data else None, timeout))
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps({"ok": True, "result": self.result}).encode())


@pytest.fixture
def fake(monkeypatch):
    f = FakeUrlopen()
    monkeypatch.setattr("urllib.request.urlopen", f)
    return f


def test_send_with_buttons_returns_message_id(fake):
    fake.result = {"message_id": 7}
    tg = Telegram("TOKEN", 42)
    assert tg.send("ahoj", [[("✅ Schválit", "ap:x-1"), ("❌", "rj:x-1")]]) == 7
    url, payload, _ = fake.requests[0]
    assert url == "https://api.telegram.org/botTOKEN/sendMessage"
    assert payload == {"chat_id": 42, "text": "ahoj", "reply_markup": {"inline_keyboard": [
        [{"text": "✅ Schválit", "callback_data": "ap:x-1"}, {"text": "❌", "callback_data": "rj:x-1"}]]}}


def test_https_button_is_a_link(fake):
    fake.result = {"message_id": 1}
    Telegram("T", 1).send("x", [[("📅 Blok", "https://calendar.google.com/x"), ("✔", "td:k")]])
    assert fake.requests[0][1]["reply_markup"]["inline_keyboard"] == [
        [{"text": "📅 Blok", "url": "https://calendar.google.com/x"}, {"text": "✔", "callback_data": "td:k"}]]


def test_long_text_is_truncated(fake):
    fake.result = {"message_id": 1}
    Telegram("T", 1).send("x" * 5000)
    assert len(fake.requests[0][1]["text"]) == 4096


def test_updates_long_poll(fake):
    fake.result = [{"update_id": 5}]
    assert Telegram("T", 1).updates(offset=5, timeout=20) == [{"update_id": 5}]
    url, payload, timeout = fake.requests[0]
    assert url.endswith("/getUpdates")
    assert payload == {"offset": 5, "timeout": 20, "allowed_updates": ["message", "callback_query"]}
    assert timeout > 20  # socket outlives the long poll


def test_edit_and_ack(fake):
    tg = Telegram("T", 1)
    tg.edit(9, "hotovo")
    tg.ack("cb1", "ok")
    assert [r[0].rsplit("/", 1)[1] for r in fake.requests] == ["editMessageText", "answerCallbackQuery"]
    assert fake.requests[0][1] == {"chat_id": 1, "message_id": 9, "text": "hotovo"}
    assert fake.requests[1][1] == {"callback_query_id": "cb1", "text": "ok"}


def test_http_error_becomes_telegram_error_without_token(monkeypatch):
    err = urllib.error.HTTPError("https://api.telegram.org/botSECRET/getMe", 401, "Unauthorized", {},
                                 io.BytesIO(b'{"ok":false,"description":"Unauthorized"}'))
    monkeypatch.setattr("urllib.request.urlopen", FakeUrlopen(error=err))
    with pytest.raises(TelegramError, match="401 Unauthorized") as e:
        Telegram("SECRET", 1).send("x")
    assert "SECRET" not in str(e.value)


def test_network_error_becomes_telegram_error(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", FakeUrlopen(error=urllib.error.URLError("no route")))
    with pytest.raises(TelegramError, match="no route"):
        Telegram("T", 1).updates(0, 0)


def test_garbled_response_becomes_telegram_error(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: io.BytesIO(b"<html>bad gateway"))
    with pytest.raises(TelegramError, match="getUpdates"):
        Telegram("T", 1).updates(0, 0)

"""Minimal Telegram Bot API client over stdlib urllib (same approach as secondbrain/bot/telegram.py)."""
import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

MAX_TEXT = 4096

Buttons = list[list[tuple[str, str]]]  # rows of (label, callback_data or an https:// link)


def _button(text: str, data: str) -> dict:
    return {"text": text, "url": data} if data.startswith("https://") else {"text": text, "callback_data": data}


class TelegramError(Exception):
    pass


@dataclass(frozen=True)
class Telegram:
    token: str
    chat_id: int

    def _call(self, method: str, payload: dict, timeout: float = 30) -> object:
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}",
                                     data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:  # its str() would carry the URL, i.e. the token
            raise TelegramError(f"{method}: {e.code} {e.reason}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise TelegramError(f"{method}: {getattr(e, 'reason', e)}") from None
        except ValueError:  # not JSON, e.g. a proxy's HTML error page
            raise TelegramError(f"{method}: unreadable response") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description')}")
        return body["result"]

    def updates(self, offset: int, timeout: int) -> list[dict]:
        return self._call("getUpdates", {"offset": offset, "timeout": timeout,
                                         "allowed_updates": ["message", "callback_query"]}, timeout=timeout + 10)

    def send(self, text: str, buttons: Buttons | None = None) -> int:
        payload: dict = {"chat_id": self.chat_id, "text": text[:MAX_TEXT]}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[_button(t, d) for t, d in row] for row in buttons]}
        return self._call("sendMessage", payload)["message_id"]

    def edit(self, message_id: int, text: str) -> None:
        self._call("editMessageText", {"chat_id": self.chat_id, "message_id": message_id, "text": text[:MAX_TEXT]})

    def ack(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


class NoTelegram:
    """serve without a bot: messages only go to stdout (None under pythonw), control is the dashboard."""
    chat_id = None

    def __init__(self):
        self._id = 0

    def updates(self, offset: int, timeout: int) -> list[dict]:
        time.sleep(timeout)
        return []

    def send(self, text: str, buttons: Buttons | None = None) -> int:
        print(text, flush=True)
        self._id += 1
        return self._id

    def edit(self, message_id: int, text: str) -> None:
        pass

    def ack(self, callback_id: str, text: str = "") -> None:
        pass

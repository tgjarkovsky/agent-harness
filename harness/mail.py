"""Gmail over IMAP, read-only: new messages of the inbox for task suggestions.

Login with an app password (Google account → Security → 2-Step Verification → App passwords) from the
environment: HARNESS_GMAIL_USER, HARNESS_GMAIL_APP_PASSWORD. The mailbox is opened read-only and messages are
fetched with BODY.PEEK, so nothing gets marked as read."""
import email
import html
import imaplib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import policy
from email.utils import parsedate_to_datetime
from urllib.parse import quote

USER_ENV, PASSWORD_ENV = "HARNESS_GMAIL_USER", "HARNESS_GMAIL_APP_PASSWORD"
HOST = "imap.gmail.com"
BODY_CHARS = 1500
MAX_LINKS = 6
MAX_BYTES = 200_000  # a partial fetch: attachments beyond this never leave Gmail
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
SKIP_LINK_RE = re.compile(r"unsubscribe|odhlas|/track|click\.|list-manage|\.(png|jpe?g|gif)(\?|$)", re.IGNORECASE)
TAG_RE = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)
HREF_RE = re.compile(r"""href=["'](https?://[^"']+)["']""", re.IGNORECASE)


@dataclass(frozen=True)
class Mail:
    msgid: str
    sender: str
    subject: str
    date: datetime  # naive local time
    body: str
    links: tuple[str, ...]

    @property
    def url(self) -> str:
        """The message in Gmail's web UI, found by its Message-ID."""
        return "https://mail.google.com/mail/u/0/#search/" + quote(f"rfc822msgid:{self.msgid.strip('<>')}", safe="")


def _links(text: str, html_part: str) -> tuple[str, ...]:
    found = HREF_RE.findall(html_part) + URL_RE.findall(text)
    keep = [html.unescape(u).rstrip(".,;") for u in found if not SKIP_LINK_RE.search(u)]
    return tuple(dict.fromkeys(keep))[:MAX_LINKS]


def _text(msg) -> tuple[str, str]:
    """(plain text, raw html) of the message; html only when there is no plain part."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return "", ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeError):  # an unknown charset
        content = part.get_payload(decode=True).decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        return html.unescape(TAG_RE.sub(" ", content)), content
    return content, ""


def parse(raw: bytes) -> Mail | None:
    msg = email.message_from_bytes(raw, policy=policy.default)
    try:
        sent = parsedate_to_datetime(msg["Date"])
    except (TypeError, ValueError):
        return None
    sent = sent.astimezone().replace(tzinfo=None) if sent.tzinfo else sent
    text, html_part = _text(msg)
    body = " ".join(text.split())[:BODY_CHARS]
    msgid = str(msg["Message-ID"] or f"<{sent.isoformat()}-{msg['Subject']}>").strip()
    return Mail(msgid, str(msg["From"] or ""), str(msg["Subject"] or "(bez předmětu)"), sent, body, _links(text, html_part))


def fetch(user: str, password: str, since: datetime, limit: int = 40, imap=imaplib.IMAP4_SSL) -> list[Mail]:
    """Inbox messages received at or after `since`, newest last, at most `limit` (the newest ones)."""
    box = imap(HOST)
    try:
        box.login(user, password)
        box.select("INBOX", readonly=True)
        day = (since - timedelta(days=1)).strftime("%d-%b-%Y")  # SINCE is a date in the server's zone
        _, data = box.search(None, "SINCE", day)
        ids = (data[0] or b"").split()[-limit:]
        out = []
        for i in ids:
            _, parts = box.fetch(i, f"(BODY.PEEK[]<0.{MAX_BYTES}>)")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw and (m := parse(raw)) and m.date >= since:
                out.append(m)
        return sorted(out, key=lambda m: m.date)
    finally:
        try:
            box.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


class Inbox:
    def __init__(self, user: str, password: str):
        self._user, self._password = user, password

    def new(self, since: datetime) -> list[Mail]:
        return fetch(self._user, self._password, since)


def from_env() -> Inbox | None:
    user, password = os.environ.get(USER_ENV, "").strip(), os.environ.get(PASSWORD_ENV, "").replace(" ", "")
    return Inbox(user, password) if user and password else None

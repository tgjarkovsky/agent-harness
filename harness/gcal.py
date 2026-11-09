"""Google Calendar over REST: the day's events and writing time blocks.

OAuth client (Desktop app) from Google Cloud Console in ~/.harness/gcal_client.json, `harness gcal-auth` stores
the token next to it. The google libraries are imported lazily: harness runs without them, just with no calendar."""
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote

DIR = Path.home() / ".harness"
CLIENT, TOKEN = DIR / "gcal_client.json", DIR / "gcal_token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
API = "https://www.googleapis.com/calendar/v3/calendars"
MIN_SLOT = timedelta(minutes=30)
CACHE_SECONDS = 5 * 60


@dataclass(frozen=True)
class Event:
    start: datetime  # naive local time
    end: datetime
    title: str
    all_day: bool = False


def authorize() -> Path:
    from google_auth_oauthlib.flow import InstalledAppFlow
    creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES).run_local_server(port=0)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return TOKEN


def _session():
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials
    if not TOKEN.exists():
        raise RuntimeError("chybí token Google Calendar, spusť `harness gcal-auth`")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return AuthorizedSession(creds)


def _local(raw: dict) -> tuple[datetime, bool]:
    if "dateTime" in raw:
        return datetime.fromisoformat(raw["dateTime"]).astimezone().replace(tzinfo=None), False
    return datetime.combine(date.fromisoformat(raw["date"]), time()), True


def _busy(item: dict) -> bool:
    declined = any(a.get("self") and a.get("responseStatus") == "declined" for a in item.get("attendees", []))
    return item.get("status") != "cancelled" and not declined


def parse_events(items: list[dict]) -> list[Event]:
    out = []
    for it in items:
        if not _busy(it):
            continue
        (start, all_day), (end, _) = _local(it["start"]), _local(it["end"])
        out.append(Event(start, end, it.get("summary", "(bez názvu)"), all_day))
    return out


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat()


class Calendar:
    """One calendar of the owner; `session` is injectable for tests."""

    def __init__(self, calendar_id: str = "primary", session=None):
        self.url = f"{API}/{quote(calendar_id, safe='')}/events"
        self._session = session
        self._cache: dict[date, tuple[float, list[Event]]] = {}  # the dashboard asks every minute, under serve's lock

    def _s(self):
        self._session = self._session or _session()
        return self._session

    def events(self, day: date) -> list[Event]:
        hit = self._cache.get(day)
        if hit and _time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        out = self._fetch(day)
        self._cache[day] = (_time.monotonic(), out)
        return out

    def _fetch(self, day: date) -> list[Event]:
        start = datetime.combine(day, time())
        r = self._s().get(self.url, params={"timeMin": _iso(start), "timeMax": _iso(start + timedelta(days=1)),
                                            "singleEvents": "true", "orderBy": "startTime", "maxResults": 100})
        r.raise_for_status()
        return parse_events(r.json().get("items", []))

    def insert(self, start: datetime, end: datetime, title: str, description: str = "") -> str:
        self._cache.pop(start.date(), None)
        r = self._s().post(self.url, json={"summary": title, "description": description,
                                           "start": {"dateTime": _iso(start)}, "end": {"dateTime": _iso(end)},
                                           "transparency": "opaque"})
        r.raise_for_status()
        return r.json().get("id", "")


def free_slots(events: list[Event], day: date, hours: tuple[time, time], now: datetime | None = None,
               min_len: timedelta = MIN_SLOT) -> list[tuple[datetime, datetime]]:
    """Gaps of at least `min_len` in the working hours of `day` between timed events (all-day ones don't block)."""
    start, end = datetime.combine(day, hours[0]), datetime.combine(day, hours[1])
    if now and now.date() == day:
        start = max(start, now.replace(second=0, microsecond=0))
    slots, cursor = [], start
    for e in sorted((e for e in events if not e.all_day), key=lambda e: e.start):
        if e.start >= end:
            break
        if e.start - cursor >= min_len:
            slots.append((cursor, e.start))
        cursor = max(cursor, e.end)
    if end - cursor >= min_len:
        slots.append((cursor, end))
    return slots

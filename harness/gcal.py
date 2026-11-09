"""Google Calendar without OAuth: the day's events from the secret iCal address, blocks as prefilled links.

The secret address (Google Calendar → Settings → the calendar → "Secret address in iCal format") lets anyone
read the whole calendar, so it lives in the HARNESS_GCAL_ICS environment variable, never in the repo."""
import os
import time as _time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

ENV = "HARNESS_GCAL_ICS"
MIN_SLOT = timedelta(minutes=30)
CACHE_SECONDS = 5 * 60
ADD_URL = "https://calendar.google.com/calendar/render"


@dataclass(frozen=True)
class Event:
    start: datetime  # naive local time
    end: datetime
    title: str
    all_day: bool = False
    free: bool = False  # "show as available": shown, but does not block a slot


def _local(value) -> tuple[datetime, bool]:
    if not isinstance(value, datetime):  # a date: all-day
        return datetime.combine(value, time()), True
    return (value.astimezone().replace(tzinfo=None) if value.tzinfo else value), False


def parse_range(text: str | bytes, first: date, days: int = 1) -> list[Event]:
    """Events from `first` for `days` days, recurring ones expanded; cancelled ones dropped."""
    import icalendar
    import recurring_ical_events
    # ponytail: declined invitations still show; telling them apart needs the owner's e-mail in the feed's ATTENDEE lines
    out = []
    for c in recurring_ical_events.of(icalendar.Calendar.from_ical(text)).between(first, first + timedelta(days=days)):
        if str(c.get("STATUS", "")).upper() == "CANCELLED":
            continue
        start, all_day = _local(c["DTSTART"].dt)
        end = _local(c["DTEND"].dt)[0] if c.get("DTEND") else start + (c["DURATION"].dt if c.get("DURATION") else timedelta())
        out.append(Event(start, end, str(c.get("SUMMARY", "(bez názvu)")), all_day,
                         str(c.get("TRANSP", "")).upper() == "TRANSPARENT"))
    return sorted(out, key=lambda e: (e.start.date(), not e.all_day, e.start))


def parse_ics(text: str | bytes, day: date) -> list[Event]:
    return parse_range(text, day)


class Calendar:
    """The owner's calendar over its secret iCal address; `fetch` is injectable for tests."""

    def __init__(self, url: str, fetch=None):
        self._url = url
        self._fetch = fetch or (lambda u: urllib.request.urlopen(u, timeout=20).read())
        self._cache: dict[tuple[date, int], tuple[float, list[Event]]] = {}  # the dashboard asks every minute

    def upcoming(self, first: date, days: int) -> list[Event]:
        hit = self._cache.get((first, days))
        if hit and _time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        out = parse_range(self._fetch(self._url), first, days)
        self._cache[(first, days)] = (_time.monotonic(), out)
        return out

    def events(self, day: date) -> list[Event]:
        return self.upcoming(day, 1)


def from_env() -> Calendar | None:
    url = os.environ.get(ENV, "").strip()
    return Calendar(url) if url else None


def add_link(start: datetime, end: datetime, title: str, details: str = "") -> str:
    """Google Calendar's "new event" page, prefilled; local times without a zone use the calendar's own zone."""
    dates = f"{start:%Y%m%dT%H%M%S}/{end:%Y%m%dT%H%M%S}"
    return f"{ADD_URL}?" + urlencode({"action": "TEMPLATE", "text": title, "dates": dates, "details": details})


def free_slots(events: list[Event], day: date, hours: tuple[time, time], now: datetime | None = None,
               min_len: timedelta = MIN_SLOT) -> list[tuple[datetime, datetime]]:
    """Gaps of at least `min_len` in the working hours of `day` between busy timed events."""
    start, end = datetime.combine(day, hours[0]), datetime.combine(day, hours[1])
    if now and now.date() == day:
        start = max(start, now.replace(second=0, microsecond=0))
    slots, cursor = [], start
    for e in sorted((e for e in events if not e.all_day and not e.free), key=lambda e: e.start):
        if e.start >= end:
            break
        if e.start - cursor >= min_len:
            slots.append((cursor, e.start))
        cursor = max(cursor, e.end)
    if end - cursor >= min_len:
        slots.append((cursor, end))
    return slots

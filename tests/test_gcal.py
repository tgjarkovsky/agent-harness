from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from harness import agenda, gcal

DAY = date(2026, 11, 9)
HOURS = (time(9), time(18))


def at(h, m=0):
    return datetime.combine(DAY, time(h, m))


def ev(h1, h2, title="x", all_day=False):
    return gcal.Event(at(h1), at(h2), title, all_day)


def test_free_slots_between_events_skip_short_gaps_and_all_day():
    events = [ev(10, 11), ev(11, 12), gcal.Event(at(12, 10), at(13), "krátká mezera"), ev(0, 0, "svátek", True)]
    assert gcal.free_slots(events, DAY, HOURS) == [(at(9), at(10)), (at(13), at(18))]


def test_free_slots_starts_now_and_ignores_events_after_hours():
    assert gcal.free_slots([ev(19, 20)], DAY, HOURS, now=at(15, 20)) == [(at(15, 20), at(18))]
    assert gcal.free_slots([ev(8, 19)], DAY, HOURS) == []


ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:standup
DTSTART:20261005T100000
DTEND:20261005T103000
RRULE:FREQ=WEEKLY
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:allday
DTSTART;VALUE=DATE:20261109
DTEND;VALUE=DATE:20261110
END:VEVENT
BEGIN:VEVENT
UID:utc
DTSTART:20261109T130000Z
DURATION:PT1H
SUMMARY:Klient
END:VEVENT
BEGIN:VEVENT
UID:cancelled
DTSTART:20261109T140000
DTEND:20261109T150000
STATUS:CANCELLED
SUMMARY:Zrušeno
END:VEVENT
BEGIN:VEVENT
UID:free
DTSTART:20261109T160000
DTEND:20261109T170000
TRANSP:TRANSPARENT
SUMMARY:Volno
END:VEVENT
BEGIN:VEVENT
UID:other-day
DTSTART:20261110T090000
DTEND:20261110T100000
SUMMARY:Zítra
END:VEVENT
END:VCALENDAR
"""


def test_parse_ics_expands_recurring_converts_utc_and_drops_cancelled():
    utc = datetime(2026, 11, 9, 13, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert gcal.parse_ics(ICS, DAY) == [
        gcal.Event(at(0), datetime(2026, 11, 10), "(bez názvu)", True),
        gcal.Event(at(10), at(10, 30), "Standup"),
        gcal.Event(utc, utc + timedelta(hours=1), "Klient"),
        gcal.Event(at(16), at(17), "Volno", free=True),
    ]


def test_free_event_does_not_block_a_slot():
    assert gcal.free_slots([gcal.Event(at(9), at(18), "Volno", free=True)], DAY, HOURS) == [(at(9), at(18))]


def test_calendar_fetches_once_per_cache_window():
    calls = []
    cal = gcal.Calendar("https://secret", fetch=lambda u: calls.append(u) or ICS.encode())
    assert [e.title for e in cal.events(DAY)][1] == "Standup"
    cal.events(DAY)
    assert calls == ["https://secret"]


def test_add_link_prefills_google_calendar():
    link = gcal.add_link(at(10), at(11, 30), "🎯 Zavolat", "#kytky")
    q = parse_qs(urlparse(link).query)
    assert link.startswith("https://calendar.google.com/calendar/render?")
    assert q == {"action": ["TEMPLATE"], "text": ["🎯 Zavolat"], "dates": ["20261109T100000/20261109T113000"],
                 "details": ["#kytky"]}


def test_plan_blocks_greedy_fill():
    items = agenda.parse("- [ ] A\n- [ ] B\n- [ ] C\n- [ ] D\n")
    slots = [(at(9), at(10, 15)), (at(13), at(15))]
    blocks = agenda.plan_blocks(items, slots, timedelta(minutes=90))
    assert [(b.item.title, b.start, b.end) for b in blocks] == [
        ("A", at(9), at(10, 15)), ("B", at(13), at(14, 30)), ("C", at(14, 30), at(15))]

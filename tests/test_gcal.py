from datetime import date, datetime, time, timedelta

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


def test_parse_events_skips_cancelled_and_declined():
    items = [
        {"summary": "Porada", "start": {"dateTime": "2026-11-09T10:00:00+01:00"}, "end": {"dateTime": "2026-11-09T11:00:00+01:00"}},
        {"summary": "Zrušeno", "status": "cancelled", "start": {"date": "2026-11-09"}, "end": {"date": "2026-11-10"}},
        {"summary": "Odmítnuto", "attendees": [{"self": True, "responseStatus": "declined"}],
         "start": {"date": "2026-11-09"}, "end": {"date": "2026-11-10"}},
        {"start": {"date": "2026-11-09"}, "end": {"date": "2026-11-10"}},
    ]
    porada, allday = gcal.parse_events(items)
    assert porada.title == "Porada" and porada.end - porada.start == timedelta(hours=1) and not porada.all_day
    assert allday == gcal.Event(at(0), datetime(2026, 11, 10), "(bez názvu)", True)


class FakeSession:
    def __init__(self):
        self.calls = []

    def _r(self, body):
        return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: body})()

    def get(self, url, params):
        self.calls.append(("get", url, params))
        return self._r({"items": [{"summary": "A", "start": {"date": "2026-11-09"}, "end": {"date": "2026-11-10"}}]})

    def post(self, url, json):
        self.calls.append(("post", url, json))
        return self._r({"id": "e1"})


def test_calendar_rest_calls():
    s = FakeSession()
    cal = gcal.Calendar("me@x.cz", session=s)
    assert [e.title for e in cal.events(DAY)] == ["A"]
    assert cal.insert(at(10), at(11), "🎯 T", "#kytky") == "e1"
    (_, url, params), (_, _, body) = s.calls
    assert url.endswith("/calendars/me%40x.cz/events") and params["singleEvents"] == "true"
    assert body["summary"] == "🎯 T" and body["start"]["dateTime"].startswith("2026-11-09T10:00:00")


def test_plan_blocks_greedy_fill():
    items = agenda.parse("- [ ] A\n- [ ] B\n- [ ] C\n- [ ] D\n")
    slots = [(at(9), at(10, 15)), (at(13), at(15))]
    blocks = agenda.plan_blocks(items, slots, timedelta(minutes=90))
    assert [(b.item.title, b.start, b.end) for b in blocks] == [
        ("A", at(9), at(10, 15)), ("B", at(13), at(14, 30)), ("C", at(14, 30), at(15))]

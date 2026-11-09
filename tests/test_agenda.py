from datetime import date

from harness import agenda

TODAY = date(2026, 11, 9)  # Monday
TEXT = """# TODO

- [ ] Zavolat klientovi #guth-pictures 📅 2026-11-12 ⏫ [[Svatba Novákovi]]
- [ ] Zalít kytky #kytky 🔁 every week ⏳ 2026-11-09
- [x] Faktura za říjen #faktury ✅ 2026-11-08
- [ ] Koupit mléko
- [ ] Opravit kolo 📅 2026-11-01
- [ ] Důležité bez data 🔺
poznámka, ne úkol
"""


def items():
    return agenda.parse(TEXT)


def test_parse_fields():
    call, plants, invoice, *_ = items()
    assert call.title == "Zavolat klientovi [[Svatba Novákovi]]"
    assert call.tags == ("guth-pictures",) and call.due == date(2026, 11, 12) and call.priority == 1
    assert plants.recur == "every week" and plants.scheduled == TODAY and plants.due is None
    assert invoice.status == "x" and invoice.done_on == date(2026, 11, 8)
    assert len(items()) == 6


def test_project_is_a_known_tag():
    call = items()[0]
    assert call.project({"guth-pictures", "kytky"}) == "guth-pictures"
    assert call.project({"kytky"}) is None


def test_today_items_overdue_first_then_today_then_urgent_undated():
    titles = [i.title for i in agenda.today_items(items(), TODAY)]
    assert titles == ["Opravit kolo", "Důležité bez data", "Zalít kytky"]


def test_complete_keeps_the_rest_of_the_line():
    call = items()[0]
    text, done = agenda.complete(TEXT, call.key, TODAY)
    assert done == call
    assert "- [x] Zavolat klientovi #guth-pictures 📅 2026-11-12 ⏫ [[Svatba Novákovi]] ✅ 2026-11-09\n" in text
    assert text.count("\n") == TEXT.count("\n")


def test_complete_recurring_adds_next_occurrence_above():
    plants = items()[1]
    text, _ = agenda.complete(TEXT, plants.key, TODAY)
    lines = text.splitlines()
    i = lines.index("- [ ] Zalít kytky #kytky 🔁 every week ⏳ 2026-11-16")
    assert lines[i + 1] == "- [x] Zalít kytky #kytky 🔁 every week ⏳ 2026-11-09 ✅ 2026-11-09"


def test_complete_unknown_or_done_key_changes_nothing():
    assert agenda.complete(TEXT, "nope", TODAY) == (TEXT, None)
    assert agenda.complete(TEXT, items()[2].key, TODAY) == (TEXT, None)


def test_next_date_month_end_and_steps():
    assert agenda.next_date(date(2026, 1, 31), "every month") == date(2026, 2, 28)
    assert agenda.next_date(date(2026, 11, 9), "every 2 weeks") == date(2026, 11, 23)
    assert agenda.next_date(date(2026, 11, 9), "every year") == date(2027, 11, 9)
    assert agenda.next_date(date(2026, 11, 9), "every weekday") is None


def test_add_creates_file_text_and_appends():
    text = agenda.add("", "Nový úkol")
    assert text == "# TODO\n\n- [ ] Nový úkol\n"
    assert agenda.add("x", "B") == "x\n- [ ] B\n"


def test_capture_dates():
    assert agenda.capture("zalít kytky #kytky zítra", TODAY) == "zalít kytky #kytky 📅 2026-11-10"
    assert agenda.capture("porada pá", TODAY) == "porada 📅 2026-11-13"
    assert agenda.capture("porada po", TODAY) == "porada 📅 2026-11-16"  # next Monday, not today
    assert agenda.capture("hotovo 2026-12-01", TODAY) == "hotovo 📅 2026-12-01"
    assert agenda.capture("bez data", TODAY) == "bez data"


def test_file_roundtrip_keeps_lf(tmp_path):
    p = tmp_path / "TODO.md"
    agenda.write(p, agenda.add(agenda.read(p), "A"))
    assert p.read_bytes() == "# TODO\n\n- [ ] A\n".encode()

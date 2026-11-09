import json
from datetime import date, datetime

from harness import agenda, gcal, mail, serve, suggest

from .test_serve_agenda import FakeCal, MONDAY, env_with_todo

TODAY = date(2026, 11, 9)
INVOICE = mail.Mail("<inv-1@wedos.cz>", "Wedos <fakturace@wedos.cz>", "Faktura 2026/123", datetime(2026, 11, 9, 6, 30),
                    "Dobrý den, faktura je splatná 15. 11. 2026.", ("https://wedos.cz/faktura/123", "https://wedos.cz/podpora"))
MEETING = gcal.Event(datetime(2026, 11, 12, 10), datetime(2026, 11, 12, 11), "Prezentace u klienta")


def answer(*items):
    return "Tady jsou návrhy:\n```json\n" + json.dumps(list(items), ensure_ascii=False) + "\n```"


def test_parse_builds_tasks_line_with_mail_and_picked_links_only():
    text = answer(
        {"source": "m1", "title": "Zaplatit fakturu Wedos #hack [x](https://evil)", "due": "2026-11-15",
         "priority": "high", "project": "faktury", "links": [1, 7, "2"], "why": "Wedos poslal fakturu."},
        {"source": "c1", "title": "Připravit prezentaci", "due": "2026-11-12", "scheduled": "2026-11-10",
         "project": "neznámý", "links": [1], "why": "Prezentace ve čtvrtek."},
        {"source": "m9", "title": "Neexistující zdroj"},
        {"source": "c1", "title": "Minulost", "due": "2026-11-01"},
        {"source": "m1", "title": ""})
    inv, prep, past = suggest.parse(text, [INVOICE], [MEETING], TODAY, {"faktury"})
    assert inv["line"] == ("Zaplatit fakturu Wedos hack (x)(https://evil) #faktury 📅 2026-11-15 ⏫ "
                           f"[✉️ mail]({INVOICE.url}) [odkaz 1](https://wedos.cz/faktura/123)")
    assert inv["origin"] == "✉️ Wedos <fakturace@wedos.cz> · Faktura 2026/123" and inv["mail_url"] == INVOICE.url
    assert prep["line"] == "Připravit prezentaci ⏳ 2026-11-10 📅 2026-11-12"  # unknown project, no links for events
    assert prep["origin"] == "📅 12. 11. 10:00 Prezentace u klienta" and prep["mail_url"] is None
    assert past["line"] == "Minulost" and past["source"] == suggest.event_key(MEETING)  # a past date is dropped


def test_parsed_line_reads_back_as_a_task():
    [p] = suggest.parse(answer({"source": "m1", "title": "Zaplatit fakturu", "due": "2026-11-15", "links": [1]}),
                        [INVOICE], [], TODAY, set())
    item = agenda.parse_line(0, f"- [ ] {p['line']}")
    assert item.title == "Zaplatit fakturu" and item.due == date(2026, 11, 15) and item.tags == ()
    assert item.links == (("✉️ mail", INVOICE.url), ("odkaz 1", "https://wedos.cz/faktura/123"))


def test_prompt_lists_sources_with_ids_and_links():
    tasks = agenda.parse("- [ ] Starý úkol 📅 2026-11-20\n")
    text = suggest.prompt([INVOICE], [MEETING], tasks, TODAY, ["faktury"])
    assert "Today is 2026-11-09 (pondělí)" in text
    assert "[m1] 2026-11-09 06:30 · Od: Wedos <fakturace@wedos.cz> · Předmět: Faktura 2026/123" in text
    assert "  1) https://wedos.cz/faktura/123" in text and "[c1] 2026-11-12 10:00–11:00 Prezentace u klienta" in text
    assert "- Starý úkol (📅 2026-11-20)" in text and "faktury" in text


RAW = (b"From: =?utf-8?q?Jan_Nov=C3=A1k?= <jan@x.cz>\r\nSubject: =?utf-8?q?Smlouva_k_podpisu?=\r\n"
       b"Date: Mon, 09 Nov 2026 08:15:00 +0000\r\nMessage-ID: <abc@x.cz>\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
       b"<p>Pros\xc3\xadm podepsat <a href=\"https://sign.example/doc?a=1&amp;b=2\">zde</a>.</p>"
       b"<a href=\"https://news.example/unsubscribe\">odhl\xc3\xa1sit</a><style>p{color:red}</style>")


def test_mail_parse_html_links_and_gmail_url():
    m = mail.parse(RAW)
    assert m.sender == "Jan Novák <jan@x.cz>" and m.subject == "Smlouva k podpisu"
    assert m.body == "Prosím podepsat zde . odhlásit"
    assert m.links == ("https://sign.example/doc?a=1&b=2",)
    assert m.date == datetime(2026, 11, 9, 8, 15, tzinfo=__import__("datetime").timezone.utc).astimezone().replace(tzinfo=None)
    assert m.url == "https://mail.google.com/mail/u/0/#search/rfc822msgid%3Aabc%40x.cz"


class FakeImap:
    calls = []

    def __init__(self, host):
        FakeImap.calls = [("host", host)]

    def login(self, user, password):
        self.calls.append(("login", user))

    def select(self, box, readonly):
        self.calls.append(("select", box, readonly))

    def search(self, charset, *criteria):
        self.calls.append(("search", criteria))
        return "OK", [b"1 2"]

    def fetch(self, i, what):
        self.calls.append(("fetch", what))
        old = RAW.replace(b"09 Nov 2026 08:15", b"01 Nov 2026 08:15")
        return "OK", [(b"1 (BODY[]<0>", RAW if i == b"2" else old), b")"]

    def logout(self):
        self.calls.append(("logout",))


def test_fetch_is_read_only_and_filters_by_time():
    out = mail.fetch("me@gmail.com", "pw", datetime(2026, 11, 8), imap=FakeImap)
    assert [m.subject for m in out] == ["Smlouva k podpisu"]
    assert ("select", "INBOX", True) in FakeImap.calls and ("search", ("SINCE", "07-Nov-2026")) in FakeImap.calls
    assert all(c[1].startswith("(BODY.PEEK[]") for c in FakeImap.calls if c[0] == "fetch")
    assert FakeImap.calls[-1] == ("logout",)


class FakeInbox:
    def __init__(self, mails):
        self.mails, self.since = list(mails), []

    def new(self, since):
        self.since.append(since)
        return [m for m in self.mails if m.date >= since]


def suggest_env(tmp_path, model_answer):
    env, todo = env_with_todo(tmp_path)
    prompts = []
    env.ctx.mail, env.ctx.cal = FakeInbox([INVOICE]), FakeCal([MEETING])
    env.ctx.suggest_model = lambda p: prompts.append(p) or model_answer
    env.ctx.state |= {"last_digest": "2026-11-09"}  # keep the day plan out of these tests
    return env, todo, prompts


def test_suggestions_sent_with_buttons_and_accepted_into_todo(tmp_path):
    reply = answer({"source": "m1", "title": "Zaplatit fakturu", "due": "2026-11-15", "links": [1], "why": "Faktura."},
                   {"source": "c1", "title": "Připravit prezentaci", "due": "2026-11-12", "why": "Čtvrtek."})
    env, todo, prompts = suggest_env(tmp_path, reply)
    env.clock[0] = MONDAY.replace(hour=7, minute=1)
    env.tick()
    msgs = [m for m in env.bot.sent if m["text"].startswith("📬")]
    assert len(prompts) == 1 and len(msgs) == 2
    assert msgs[0]["text"] == ("📬 Návrh úkolu\nZaplatit fakturu 📅 2026-11-15\n↳ Faktura.\n"
                               "✉️ Wedos <fakturace@wedos.cz> · Faktura 2026/123")
    (ok, accept), (_, reject) = msgs[0]["buttons"][0]
    assert ok == "✅ Přidat" and msgs[0]["buttons"][1] == [("✉️ Otevřít mail", INVOICE.url)]
    env.tick(env.press(accept, msgs[0]), env.press(msgs[1]["buttons"][0][1][1], msgs[1]))
    text = todo.read_bytes().decode()
    assert "- [ ] Zaplatit fakturu 📅 2026-11-15 [✉️ mail](" in text and "prezentaci" not in text
    assert [e[1].split("→ ")[-1] for e in env.bot.edits] == ["přidáno do úkolů", "zahozeno"]
    assert serve.agenda_data(env.ctx, MONDAY.date())["proposals"] == []
    env.tick()
    assert len(prompts) == 1  # one call per slot
    env.clock[0] = MONDAY.replace(hour=14, minute=2)
    env.tick()
    assert len(prompts) == 1  # 14:00: nothing new, no model call
    assert env.ctx.mail.since[-1] == MONDAY.replace(hour=7, minute=1)


def test_failing_mail_is_reported_once_and_retried(tmp_path):
    env, _, prompts = suggest_env(tmp_path, "[]")

    class Broken:
        def new(self, since):
            raise OSError("login failed")
    env.ctx.mail = Broken()
    env.clock[0] = MONDAY.replace(hour=7, minute=1)
    env.tick()
    assert env.bot.texts()[-1] == "⚠️ Návrhy úkolů: mail: OSError: login failed"
    assert len(prompts) == 1 and env.ctx.state["mail_since"] is None  # events still went to the model
    env.clock[0] = MONDAY.replace(hour=14, minute=2)
    env.tick()
    assert sum(t.startswith("⚠️ Návrhy") for t in env.bot.texts()) == 1


def test_reminders_today_and_tomorrow_once(tmp_path):
    env, todo = env_with_todo(tmp_path, "- [ ] Dnešní 📅 2026-11-09\n- [ ] Zítřejší #demo 📅 2026-11-10\n- [x] Hotová 📅 2026-11-09\n")
    env.ctx.state |= {"last_digest": "2026-11-09"}
    env.clock[0] = MONDAY.replace(hour=15, minute=1)
    env.tick()
    env.tick()
    assert env.bot.texts() == ["⏰ Termín dnes:\n• Dnešní"]
    env.clock[0] = MONDAY.replace(hour=20, minute=0)
    env.tick()
    assert env.bot.texts()[-1] == "⏰ Termín zítra:\n• Zítřejší [demo]"
    assert env.bot.sent[-1]["buttons"][0][0][1].startswith("td:")


def test_day_plan_lists_coming_deadlines(tmp_path):
    env, _ = env_with_todo(tmp_path, "- [ ] Za dva dny 📅 2026-11-11 [✉️ mail](https://mail.google.com/x)\n- [ ] Daleko 📅 2026-11-30\n")
    text, _ = serve.day_plan(env.ctx, MONDAY.date())
    assert text.endswith("⏰ Blíží se\n• 11. 11. Za dva dny")
    [soon] = serve.agenda_data(env.ctx, MONDAY.date())["soon"]
    assert soon["links"] == [["✉️ mail", "https://mail.google.com/x"]]

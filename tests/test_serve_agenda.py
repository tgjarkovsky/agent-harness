from dataclasses import replace
from datetime import datetime

import pytest

from harness import gcal, serve
from harness.projects import Agenda

from .test_serve import Env

MONDAY = datetime(2026, 11, 9, 12, 0)


def env_with_todo(tmp_path, text=None):
    env = Env(tmp_path, "happy-path", now=MONDAY, auto=False)
    todo = tmp_path / "TODO.md"
    if text is not None:
        todo.write_bytes(text.encode())
    env.ctx.cfg = replace(env.ctx.cfg, agenda=Agenda(todo))
    return env, todo


def test_todo_adds_line_with_date_and_done_button(tmp_path):
    env, todo = env_with_todo(tmp_path)
    env.tick(env.msg("/todo zalít kytky #kytky zítra"))
    assert todo.read_bytes().decode() == "# TODO\n\n- [ ] zalít kytky #kytky 📅 2026-11-10\n"
    msg = env.bot.sent[-1]
    assert msg["text"] == "📝 zalít kytky #kytky 📅 2026-11-10"
    env.tick(env.press(msg["buttons"][0][0][1], msg))
    assert todo.read_bytes().decode().endswith("- [x] zalít kytky #kytky 📅 2026-11-10 ✅ 2026-11-09\n")
    assert env.bot.acks[-1][1] == "✔ zalít kytky"
    env.tick(env.press(msg["buttons"][0][0][1], msg))
    assert env.bot.acks[-1][1] == "úkol už není otevřený"


def test_todo_without_agenda_config_explains(tmp_path):
    env = Env(tmp_path, "happy-path", now=MONDAY, auto=False)
    env.tick(env.msg("/todo něco"))
    assert "agenda.todo" in env.bot.texts()[-1]


def test_dnes_lists_today_with_buttons(tmp_path):
    env, todo = env_with_todo(tmp_path, "- [ ] Starý #demo 📅 2026-11-01\n- [ ] Dnešní ⏳ 2026-11-09\n- [ ] Pozdější 📅 2026-11-20\n")
    env.tick(env.msg("/dnes"))
    msg = env.bot.sent[-1]
    assert msg["text"] == "📋 Dnes\n• Starý [demo] ⚠️ po termínu\n• Dnešní"
    assert [row[0][0] for row in msg["buttons"]] == ["✔ Starý", "✔ Dnešní"]
    env.tick(env.press(msg["buttons"][1][0][1], msg))
    assert "- [x] Dnešní ⏳ 2026-11-09 ✅ 2026-11-09" in todo.read_bytes().decode()
    assert env.bot.edits == []  # the list keeps its buttons


def test_dev_off_keeps_proposals_out_of_chat_and_on_does_not_flood(tmp_path):
    env, _ = env_with_todo(tmp_path)
    env.ctx.state["dev"] = False
    with env.sim.backlog_path.open("a", encoding="utf-8") as f:
        f.write("## demo2\n- [~] P3 p-1 TODO: jedna\n")
    env.tick()
    assert not [t for t in env.bot.texts() if t.startswith("💡")]
    env.tick(env.msg("/dev on"))
    assert env.bot.texts()[-1] == "🛠 Dev notifikace: zapnuté"
    with env.sim.backlog_path.open("a", encoding="utf-8") as f:
        f.write("- [~] P3 p-2 TODO: dva\n")
    env.tick()
    assert [t.split("\n")[1] for t in env.bot.texts() if t.startswith("💡")] == ["TODO: dva"]


class FakeCal:
    def __init__(self, events=(), fail=False):
        self.events_, self.fail = list(events), fail

    def events(self, day):
        if self.fail:
            raise OSError("offline")
        return [e for e in self.events_ if e.start.date() == day]

    def upcoming(self, first, days):
        if self.fail:
            raise OSError("offline")
        return [e for e in self.events_ if 0 <= (e.start.date() - first).days < days]


def morning_env(tmp_path, cal, dev=False):
    env, _ = env_with_todo(tmp_path, "- [ ] Zavolat klientovi #demo 📅 2026-11-09\n- [ ] Později 📅 2026-12-01\n")
    env.clock[0] = MONDAY.replace(hour=7, minute=5)
    env.ctx.state |= {"dev": dev, "last_digest": "2026-11-08"}
    env.ctx.cal = cal
    return env


def test_morning_plan_with_meetings_tasks_blocks_and_dev_line(tmp_path):
    cal = FakeCal([gcal.Event(datetime(2026, 11, 9, 9), datetime(2026, 11, 9, 10), "Standup")])
    env = morning_env(tmp_path, cal)
    env.tick()
    plan = env.bot.sent[-1]
    assert plan["text"].splitlines()[:9] == [
        "☀️ Plán dne 9. 11.", "", "📅 Schůzky", "09:00–10:00 Standup", "", "✅ Úkoly", "• Zavolat klientovi [demo]",
        "", "🎯 Návrh bloků (tlačítko otevře Google Kalendář, ulož)"]
    assert "10:00–11:30 Zavolat klientovi" in plan["text"] and "🛠 Dev:" in plan["text"]
    assert len(env.bot.sent) == 1  # dev off: no separate digest
    label, link = plan["buttons"][0][0]
    assert label == "📅 10:00 Zavolat klientovi"
    assert link == gcal.add_link(datetime(2026, 11, 9, 10), datetime(2026, 11, 9, 11, 30), "🎯 Zavolat klientovi", "#demo")
    env.tick()
    assert len(env.bot.sent) == 1  # once a day


def test_saved_block_is_not_proposed_again(tmp_path):
    saved = gcal.Event(datetime(2026, 11, 9, 10), datetime(2026, 11, 9, 11, 30), "🎯 Zavolat klientovi")
    env = morning_env(tmp_path, FakeCal([saved]))
    data = serve.agenda_data(env.ctx, MONDAY.date())
    assert data["blocks"] == [] and [t["title"] for t in data["tasks"]] == ["Zavolat klientovi"]


def test_dashboard_actions_add_done_dev_and_block_links(tmp_path):
    env, todo = env_with_todo(tmp_path)
    env.ctx.cal = FakeCal()
    assert serve.act(env.ctx, "todo_add", {"text": "Poslat fotky zítra", "project": "demo"}) == "📝 Poslat fotky"
    assert todo.read_bytes().decode().endswith("- [ ] Poslat fotky #demo 📅 2026-11-10\n")
    with pytest.raises(ValueError):
        serve.act(env.ctx, "todo_add", {"text": "x", "project": "neznámý"})
    serve.act(env.ctx, "todo_add", {"text": "Dnes hotovo dnes"})
    data = serve.agenda_data(env.ctx, MONDAY.date())
    assert [t["title"] for t in data["tasks"]] == ["Dnes hotovo"]
    [block] = data["blocks"]
    assert (block["start"], block["end"]) == ("2026-11-09T12:00", "2026-11-09T13:30")
    assert block["link"].startswith("https://calendar.google.com/calendar/render?action=TEMPLATE")
    with pytest.raises(KeyError):
        serve.act(env.ctx, "blocks", {"day": "2026-11-09"})  # no writing to the calendar any more
    assert serve.act(env.ctx, "todo_done", {"key": data["tasks"][0]["key"]}) == "✔ Dnes hotovo"
    assert serve.act(env.ctx, "dev", {"on": False}).endswith("vypnuté (návrhy a lekce jen v dashboardu)")
    assert serve.snapshot(env.ctx)["dev"] is False


def test_dashboard_agenda_and_project_todos_without_serve(tmp_path):
    from fastapi.testclient import TestClient
    from harness.dashboard.app import create_app
    (tmp_path / "a").mkdir()
    (tmp_path / "projects.yaml").write_text("agenda: {todo: TODO.md}\nprojects:\n  alpha: {path: a, base: main, verify: [x]}\n",
                                            encoding="utf-8")
    (tmp_path / "TODO.md").write_bytes("- [ ] Zavolat #alpha 📅 2000-01-01\n- [x] Hotové #alpha\n- [ ] Jiné\n".encode())
    client = TestClient(create_app(tmp_path, prs=lambda p: []))
    [alpha] = client.get("/overview").json()["projects"]
    assert [t["title"] for t in alpha["todos"]] == ["Zavolat"]
    agenda = client.get("/agenda").json()
    assert [t["title"] for t in agenda["tasks"]] == ["Zavolat"] and agenda["tasks"][0]["late"] is True
    assert agenda["error"].startswith("kalendář není připojený")


def test_morning_plan_survives_calendar_error_and_dev_on_sends_digest(tmp_path):
    env = morning_env(tmp_path, FakeCal(fail=True), dev=True)
    env.tick()
    plan, digest = env.bot.sent[-2:]
    assert "⚠️ kalendář: OSError: offline" in plan["text"] and "• Zavolat klientovi [demo]" in plan["text"]
    assert "🛠 Dev:" not in plan["text"] and digest["text"].startswith("📋 Digest")

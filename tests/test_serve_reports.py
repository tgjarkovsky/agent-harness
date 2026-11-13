import json

from harness import serve

from .test_bugfinder import PROPOSAL, RAW, answer
from .test_serve import Env


class FakeRelay:
    def __init__(self, reports, fail=False):
        self.reports, self.fail, self.acked, self.pulls = list(reports), fail, [], 0

    def pull(self):
        self.pulls += 1
        if self.fail:
            raise OSError("relay down")
        return [r for r in self.reports if r["id"] not in self.acked]

    def shot(self, url, dest):
        dest.write_bytes(b"jpeg")
        return dest

    def ack(self, ids):
        self.acked += ids


def env_with(tmp_path, reports, answers):
    env = Env(tmp_path, "happy-path", auto=False)
    calls = []

    def model(project, instructions, shot, known):
        calls.append({"project": project, "instructions": instructions, "shot": shot})
        return answers.pop(0)
    env.ctx.relay, env.ctx.report_model = FakeRelay(reports), model
    return env, calls


def later(env, seconds=serve.REPORT_PULL_EVERY):
    env.clock[0] = env.clock[0].fromtimestamp(env.clock[0].timestamp() + seconds)


def report_msgs(env):
    return [m for m in env.bot.sent if m["text"].startswith("🐞")]


def test_client_report_waits_for_triage_then_question_answer_and_proposal(tmp_path):
    env, calls = env_with(tmp_path, [RAW | {"shot_url": "https://blob/x.jpg"}],
                          [answer({"question": "Na mobilu, nebo na počítači?"}), answer(PROPOSAL | {"size": "large"})])
    env.tick()
    assert calls == [] and env.ctx.relay.acked == ["r1"]
    (r,) = env.ctx.state["reports"]
    assert r["status"] == "triage" and r["shot"].endswith("r1.jpg")
    triage = report_msgs(env)[-1]
    assert triage["text"].startswith("🐞 Hlášení od klienta [demo] Tlačítko Uložit nic nedělá")
    later(env)
    env.tick()
    assert calls == [] and env.ctx.relay.pulls == 2  # acked, not pulled twice, still no model call

    env.tick(env.press(triage["buttons"][0][0][1], triage))
    assert len(calls) == 1 and calls[0]["shot"].name == "r1.jpg" and "Tlačítko Uložit" in calls[0]["instructions"]
    question = report_msgs(env)[-1]
    assert "Na mobilu, nebo na počítači?" in question["text"]

    env.tick(env.msg("Na mobilu v Safari", reply_to=question["id"]))
    assert len(calls) == 2 and "A: Na mobilu v Safari" in calls[1]["instructions"]
    statuses = env.statuses()
    pid = next(k for k in statuses if k.startswith("demo-ai-"))
    assert statuses[pid] == "~"
    done = report_msgs(env)[-1]
    assert done["text"].startswith("🐞 [demo] → návrh ke schválení: Opravit Uložit")
    assert done["buttons"][0][0] == ("✅ Schválit", f"ap:{pid}")
    assert env.ctx.state["reports"] == [] and not (tmp_path / "reports" / "r1.jpg").exists()
    assert pid in env.ctx.state["proposals_sent"]  # no second 💡 message for the same proposal


def test_trusted_small_bug_goes_straight_to_queue(tmp_path):
    env, calls = env_with(tmp_path, [RAW | {"trusted": True}], [answer(PROPOSAL)])
    env.tick()
    assert len(calls) == 1
    pid = next(k for k in env.statuses() if k.startswith("demo-ai-"))
    assert env.statuses()[pid] == " " and report_msgs(env)[-1]["text"].startswith("🐞 [demo] → do fronty")


def test_rejected_report_can_be_retried_or_dropped_from_dashboard(tmp_path):
    env, calls = env_with(tmp_path, [RAW | {"trusted": True}], [answer({"reject": "Je to záměr."}), "[]"])
    env.tick()
    snap = serve.snapshot(env.ctx)["reports"]
    assert snap[0]["status"] == "rejected" and snap[0]["why"] == "Je to záměr." and snap[0]["shot"] is False
    assert "Je to záměr." in report_msgs(env)[-1]["text"]
    assert serve.act(env.ctx, "report_accept", {"id": "r1"}) == "scout ho zpracuje"
    env.tick()
    assert len(calls) == 2 and env.ctx.state["reports"][0]["why"].startswith("scout nevrátil výsledek")
    assert serve.act(env.ctx, "report_reject", {"id": "r1"}) == "zahozeno"
    assert env.ctx.state["reports"] == [] and not any(k.startswith("demo-ai-") for k in env.statuses())


def test_dashboard_answer_and_third_question_rejected(tmp_path):
    q = answer({"question": "Kde?"})
    env, calls = env_with(tmp_path, [RAW | {"trusted": True}], [q, q, q])
    env.tick()
    assert serve.snapshot(env.ctx)["reports"][0]["question"] == "Kde?"
    assert serve.act(env.ctx, "report_answer", {"id": "r1", "text": " "}) == "Prázdná odpověď, napiš ji znovu."
    for text in ("Detail", "Mobil"):
        assert serve.act(env.ctx, "report_answer", {"id": "r1", "text": text}).startswith("👍")
        env.tick()
    assert len(calls) == 3 and "You must not ask again" in calls[2]["instructions"]
    r = env.ctx.state["reports"][0]
    assert r["status"] == "rejected" and r["why"] == "scout se ptá potřetí: Kde?"
    assert serve.act(env.ctx, "report_answer", {"id": "r1", "text": "x"}) == "Dotaz k hlášení už není otevřený."


def test_relay_error_reported_once_and_restart_requeues_scouting(tmp_path):
    env, calls = env_with(tmp_path, [], [])
    env.ctx.relay.fail = True
    env.tick()
    later(env)
    env.tick()
    assert sum(t == "⚠️ BugFinder: OSError: relay down" for t in env.bot.texts()) == 1
    r = serve.bugfinder.new_report(RAW, None, {"demo"}) | {"status": "scouting"}
    env.ctx.state["reports"] = [r]
    env.ctx.report_model = lambda *a: calls.append(a) or answer({"reject": "x"})
    env.tick()
    assert len(calls) == 1 and r["status"] == "rejected"


def test_dashboard_serves_screenshot_only_to_itself(tmp_path):
    from fastapi.testclient import TestClient

    from harness.dashboard.app import create_app
    env, _ = env_with(tmp_path, [RAW | {"shot_url": "https://blob/x.jpg"}], [])
    env.tick()
    (tmp_path / "home").mkdir()
    client = TestClient(create_app(tmp_path / "home", control=env.ctx))
    assert client.get("/reports/r1/shot").content == b"jpeg"
    assert client.get("/reports/r1/shot", headers={"referer": "https://evil.example/"}).status_code == 403
    assert client.get("/reports/nope/shot").status_code == 404


def test_no_relay_no_pull(tmp_path):
    env, _ = env_with(tmp_path, [RAW], [])
    env.ctx.relay = None
    env.tick()
    assert env.ctx.state["reports"] == [] and json.dumps(serve.snapshot(env.ctx)["reports"]) == "[]"

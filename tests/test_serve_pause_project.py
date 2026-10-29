from datetime import timedelta

from harness import serve

from tests.test_serve import NOON, Env


def _env(tmp_path):
    return Env(tmp_path, "parallel-projects", now=NOON)


def test_pause_project_keeps_it_out_of_autorun_but_not_others(tmp_path):
    env = _env(tmp_path)
    env.tick(env.msg("/pause alpha"))
    assert "alpha" in env.bot.texts()[-1] and env.ctx.state["paused_projects"] == ["alpha"]
    env.tick()
    assert env.statuses() == {"sim-a1": " ", "sim-b1": "x", "sim-a2": " "}


def test_status_marks_paused_project_and_resume_restarts_it(tmp_path):
    env = _env(tmp_path)
    env.tick(env.msg("/pause alpha"))
    env.tick(env.msg("/status"))
    lines = {l.split(":")[0]: l for l in env.bot.texts()[-1].splitlines()}
    assert "pozastaveno" in lines["alpha"] and "pozastaveno" not in lines["beta"]
    assert any("pozastaveno" in l for l in serve.snapshot(env.ctx)["limits"])
    env.tick(env.msg("/resume alpha"))
    assert env.ctx.state["paused_projects"] == []
    env.clock[0] += timedelta(seconds=serve.CHECK_EVERY)
    env.tick()
    assert env.statuses()["sim-a1"] == "x"


def test_manual_run_still_starts_paused_project(tmp_path):
    env = _env(tmp_path)
    env.tick(env.msg("/pause alpha"))
    serve.act(env.ctx, "run", {"projects": ["alpha"]})
    assert env.statuses()["sim-a1"] == "x"


def test_unknown_project_is_an_error(tmp_path):
    env = _env(tmp_path)
    env.tick(env.msg("/pause neznamy"))
    assert "Neznámý projekt" in env.bot.texts()[-1] and env.ctx.state["paused_projects"] == []
    assert "Neznámý projekt" in serve.act(env.ctx, "resume", {"project": "neznamy"})


def test_dashboard_actions_take_project_and_plain_pause_still_global(tmp_path):
    env = _env(tmp_path)
    serve.act(env.ctx, "pause", {"project": "beta"})
    assert env.ctx.state["paused_projects"] == ["beta"] and env.ctx.state["paused"] is False
    serve.act(env.ctx, "resume", {"project": "beta"})
    assert env.ctx.state["paused_projects"] == []
    serve.act(env.ctx, "pause", {})
    assert env.ctx.state["paused"] is True


def test_paused_projects_survive_save_and_load(tmp_path):
    p = tmp_path / "serve_state.json"
    st = serve.load_state(p, NOON)
    assert st["paused_projects"] == []
    st["paused_projects"] = ["alpha"]
    serve.save_state(p, st)
    assert serve.load_state(p, NOON)["paused_projects"] == ["alpha"]

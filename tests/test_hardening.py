"""Regression tests for findings from the final branch review."""
import os
import subprocess
import time
from pathlib import Path

import pytest

from harness import cli
from harness.backlog import parse, set_status
from harness.nodes import real
from harness.nodes.real import ensure_worktree, git, sh, staged_changes
from harness.projects import Config, Project
from harness.sdk import READ_TOOLS, worker_options
from tests.test_real_git import make_repo, project


def test_sh_timeout_kills_grandchildren():
    start = time.time()
    sleep = "ping -n 15 127.0.0.1 > nul" if os.name == "nt" else "sleep 15 | cat"
    ok, _ = sh(sleep, Path.cwd(), timeout=2)
    assert not ok
    assert time.time() - start < 8


def test_run_refuses_api_key_on_resume(tmp_path, monkeypatch, capsys):
    (tmp_path / "projects.yaml").write_text("billing: subscription\nprojects: {}\n", encoding="utf-8")
    (tmp_path / "BACKLOG.md").write_text("", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert cli.main(["--home", str(tmp_path), "run", "--resume", "run-x"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_guard_sees_moves_out_of_forbidden_paths(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "Thoughts").mkdir()
    (repo / "Thoughts" / "j.md").write_text("x\n")
    git(["add", "-A"], repo)
    git(["commit", "-q", "-m", "vault"], repo)
    git(["push", "-q", "origin", "main"], repo)
    wt = ensure_worktree(project(repo), "t-9", "harness/t-9")
    (wt / "bot").mkdir()
    git(["mv", "Thoughts/j.md", "bot/j.md"], wt)
    (wt / "bot" / "příliš.py").write_text("a\n", encoding="utf-8")
    paths, lines = staged_changes(wt)
    assert "Thoughts/j.md" in paths and "bot/j.md" in paths
    assert "bot/příliš.py" in paths
    assert lines >= 3


def test_open_task_title_keeps_em_dash():
    text = "## p\n- [ ] P1 a-1 Galerie — lazy loading\n"
    [t] = parse(text)
    assert t.title == "Galerie — lazy loading"
    assert "- [x] P1 a-1 Galerie — lazy loading → https://x/pull/1\n" in set_status(text, "a-1", "x", " → https://x/pull/1")


def test_open_pr_passes_body_via_file(tmp_path, monkeypatch):
    calls = []

    class Done:
        stdout = "https://github.com/o/r/pull/9\n"

    def fake_run(args, **kw):
        calls.append(args)
        if "--body-file" in args:
            assert len(Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")) > 40000
        return Done() if args[:3] == ["gh", "pr", "create"] else type("R", (), {"stdout": ""})()

    monkeypatch.setattr(real.subprocess, "run", fake_run)
    monkeypatch.setattr(real, "git", lambda args, cwd: "")
    p = Project("demo", tmp_path, "main", (), ("true",), (), 3, 800)
    nodes = real.real_nodes(Config("subscription", 3, 0.7, {"demo": p}))
    state = {"run_id": "r", "task": {"id": "a-1", "title": "T", "project": "demo"}, "branch": "harness/a-1",
             "worktree": str(tmp_path), "plan": "x" * 50000, "attempts": 1, "usage": []}
    assert nodes["open_pr"](state)["pr_url"] == "https://github.com/o/r/pull/9"
    create = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert "--body" not in create and max(len(a) for a in create) < 8000


def test_worker_cannot_push_or_use_gh(tmp_path):
    env = worker_options(cwd=tmp_path, model="haiku", tools=READ_TOOLS).env
    repo = make_repo(tmp_path)
    (repo / "c.txt").write_text("c\n")
    git(["add", "-A"], repo)
    git(["commit", "-q", "-m", "c"], repo)
    p = subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=repo, capture_output=True, text=True,
                       env={**os.environ, **env})
    assert p.returncode != 0
    assert env["GH_TOKEN"] == "" and Path(env["GH_CONFIG_DIR"]).is_dir()


@pytest.mark.skipif(os.name != "nt", reason="CREATE_NO_WINDOW exists only on Windows")
def test_hide_child_consoles_adds_no_window_flag(monkeypatch):
    seen = []
    orig = subprocess.Popen.__init__

    def spy(self, *a, creationflags=0, **kw):
        seen.append(creationflags)
        orig(self, *a, creationflags=creationflags, **kw)
    monkeypatch.setattr(subprocess.Popen, "__init__", spy)
    cli.hide_child_consoles()
    assert subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    subprocess.run(["git", "--version"], capture_output=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    assert seen == [subprocess.CREATE_NO_WINDOW,
                    subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP]

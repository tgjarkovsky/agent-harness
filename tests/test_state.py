import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from langgraph.store.sqlite import SqliteStore

from harness import backlog, cli, lessons, state

BACKLOG = "# Backlog\n\n## demo\n\n- [~] P2 demo-1 První návrh\n- [ ] P2 demo-2 Druhý úkol\n"


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def store_at(home: Path) -> SqliteStore:
    s = SqliteStore(sqlite3.connect(str(home / "harness.db"), check_same_thread=False, isolation_level=None))
    s.setup()
    return s


@pytest.fixture
def machines(tmp_path):
    """Two harness homes (code checkouts) sharing one bare origin; `a` has the live BACKLOG.md."""
    origin = tmp_path / "origin.git"
    git(["init", "-q", "--bare", "-b", "main", str(origin)], tmp_path)
    homes = []
    for name in ("a", "b"):
        home = tmp_path / name
        git(["clone", "-q", str(origin), str(home)], tmp_path)
        git(["config", "user.email", "t@t"], home)
        git(["config", "user.name", "t"], home)
        homes.append(home)
    a, b = homes
    (a / "BACKLOG.md").write_text("# Backlog\n", encoding="utf-8")
    git(["add", "-A"], a)
    git(["commit", "-q", "-m", "code"], a)
    git(["push", "-q", "origin", "main"], a)
    git(["pull", "-q"], b)
    (a / "BACKLOG.md").write_text(BACKLOG, encoding="utf-8")  # live, uncommitted, like on the Windows desktop
    return a, b


def test_backlog_path_falls_back_before_init(tmp_path):
    assert state.backlog_path(tmp_path) == tmp_path / "BACKLOG.md"
    (tmp_path / "state" / ".git").mkdir(parents=True)
    assert state.backlog_path(tmp_path) == tmp_path / "state" / "BACKLOG.md"


def test_init_refuses_without_branch_unless_create(machines):
    _, b = machines
    with pytest.raises(state.StateError, match="--create"):
        state.init(b)
    assert not state.enabled(b)


def test_create_then_join_moves_live_backlog_and_lessons(machines):
    a, b = machines
    sa = store_at(a)
    key = lessons.propose(sa, "demo", "Piš testy první.", ["rejected:demo-9"])
    lessons.decide(sa, "demo", key, "active")
    sa.put(lessons.SEEN, "rejected:demo-9", {"at": 1})
    state.init(a, sa, create=True)
    assert (a / "state" / "BACKLOG.md").read_text(encoding="utf-8") == BACKLOG
    assert (a / "BACKLOG.md").read_text(encoding="utf-8") == "# Backlog\n"  # old file restored, no longer used
    with pytest.raises(state.StateError, match="už na origin"):
        state.init(b, create=True)  # a stale machine must not overwrite the live state
    sb = store_at(b)
    state.init(b, sb)
    assert state.backlog_path(b).read_text(encoding="utf-8") == BACKLOG
    assert lessons.active_texts(sb, "demo") == ["Piš testy první."]
    assert sb.get(lessons.SEEN, "rejected:demo-9") is not None


def test_edits_on_both_machines_merge(machines):
    a, b = machines
    sa, sb = store_at(a), store_at(b)
    state.init(a, sa, create=True)
    state.init(b, sb)
    backlog.edit(state.backlog_path(b), lambda t: t.replace("[~] P2 demo-1", "[ ] P2 demo-1"))
    backlog.edit(state.backlog_path(a), lambda t: t.replace("[ ] P2 demo-2", "[x] P2 demo-2"))
    state.sync(b, sb)
    state.sync(a, sa)
    state.sync(b, sb)
    for home in (a, b):
        text = state.backlog_path(home).read_text(encoding="utf-8")
        assert "- [ ] P2 demo-1" in text and "- [x] P2 demo-2" in text


def test_same_line_later_sync_wins(machines):
    a, b = machines
    state.init(a, create=True)
    state.init(b)
    backlog.edit(state.backlog_path(a), lambda t: t.replace("[~] P2 demo-1", "[-] P2 demo-1"))
    backlog.edit(state.backlog_path(b), lambda t: t.replace("[~] P2 demo-1", "[ ] P2 demo-1"))
    state.sync(a)
    state.sync(b)  # b merges a's change with its own local line winning
    state.sync(a)
    for home in (a, b):
        assert "- [ ] P2 demo-1" in state.backlog_path(home).read_text(encoding="utf-8")


def test_lesson_status_only_moves_forward(machines):
    a, b = machines
    sa, sb = store_at(a), store_at(b)
    key = lessons.propose(sa, "demo", "Krátké PR.", [])
    state.init(a, sa, create=True)
    state.init(b, sb)
    lessons.decide(sb, "demo", key, "active")  # approved on b
    state.sync(b, sb)
    state.sync(a, sa)
    assert lessons.lessons(sa, "demo")[0]["status"] == "active"
    lessons.decide(sa, "demo", key, "retired")
    state.sync(a, sa)
    state.sync(b, sb)
    assert lessons.lessons(sb, "demo")[0]["status"] == "retired"
    lines = [json.loads(x) for x in (b / "state" / "lessons.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["value"]["status"] for x in lines if x["ns"][0] == "lessons"] == ["retired"]


def test_offline_sync_raises_and_keeps_local_commit(machines, tmp_path):
    a, _ = machines
    state.init(a, create=True)
    git(["remote", "set-url", "origin", str(tmp_path / "gone.git")], a / "state")
    backlog.edit(state.backlog_path(a), lambda t: t + "- [ ] P2 demo-3 Offline\n")
    with pytest.raises(state.StateError):
        state.sync(a)
    assert "demo-3" in git(["show", "HEAD:BACKLOG.md"], a / "state")


def test_syncer_pushes_soon_after_backlog_edit(machines):
    a, b = machines
    state.init(a, create=True)
    state.init(b)
    syncer = state.Syncer(a, every=60, debounce=0.1).start()
    try:
        backlog.edit(state.backlog_path(a), lambda t: t + "- [ ] P2 demo-4 Rychle\n")
        remote = lambda: git(["ls-remote", "origin", "harness-state"], a / "state").split()[0]
        for _ in range(50):
            time.sleep(0.1)
            if remote() == git(["rev-parse", "HEAD"], a / "state") and "demo-4" in git(["show", "HEAD:BACKLOG.md"], a / "state"):
                break
    finally:
        syncer.stop()
    state.sync(b)
    assert "demo-4" in state.backlog_path(b).read_text(encoding="utf-8")


def test_cli_commands_sync_around_the_run(machines, monkeypatch, capsys):
    a, b = machines
    state.init(a, create=True)
    state.init(b)
    backlog.edit(state.backlog_path(a), lambda t: t.replace("Druhý úkol", "Druhý úkol (z a)"))
    state.sync(a)
    (b / "projects.yaml").write_text("billing: subscription\nprojects: {}\n", encoding="utf-8")
    assert cli.main(["--home", str(b), "queue"]) == 0  # pulls before reading
    assert "Druhý úkol (z a)" in state.backlog_path(b).read_text(encoding="utf-8")


def test_merge_backlog_per_task():
    base = "# Backlog\n\n## demo\n- [~] P2 d-1 A\n  popis A\n- [ ] P2 d-2 B\n## web\n- [ ] P1 w-1 C\n"
    ours = base.replace("[~] P2 d-1", "[ ] P2 d-1") + "- [~] P2 w-2 Nový náš\n"
    theirs = base.replace("[ ] P2 d-2 B", "[x] P2 d-2 B → pr/1").replace("- [ ] P1 w-1 C\n", "") \
        .replace("## web\n", "## web\n- [~] P3 w-3 Nový jejich\n")
    merged = state.merge_backlog(base, ours, theirs)
    assert merged == ("# Backlog\n\n## demo\n- [ ] P2 d-1 A\n  popis A\n- [x] P2 d-2 B → pr/1\n## web\n"
                      "- [~] P3 w-3 Nový jejich\n- [~] P2 w-2 Nový náš\n")
    both = state.merge_backlog(base, base.replace("[~] P2 d-1", "[-] P2 d-1"), base.replace("[~] P2 d-1", "[ ] P2 d-1"))
    assert "- [-] P2 d-1 A\n  popis A\n" in both  # same task changed on both sides: ours


def test_new_lessons_on_both_machines_survive(machines):
    a, b = machines
    sa, sb = store_at(a), store_at(b)
    state.init(a, sa, create=True)
    state.init(b, sb)
    lessons.propose(sa, "demo", "Lekce z a.", [])
    lessons.propose(sb, "demo", "Lekce z b.", [])
    state.sync(a, sa)
    state.sync(b, sb)
    state.sync(a, sa)
    for s in (sa, sb):
        assert sorted(x["text"] for x in lessons.lessons(s, "demo")) == ["Lekce z a.", "Lekce z b."]

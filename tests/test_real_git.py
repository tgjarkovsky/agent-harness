import subprocess
from pathlib import Path

from harness.nodes.real import commit_if_dirty, ensure_worktree, git, numstat_lines, worktree_path
from harness.projects import Project


def make_repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    repo = tmp_path / "projects" / "demo"
    repo.mkdir(parents=True)
    git(["init", "-q", "-b", "main"], repo)
    git(["config", "user.email", "t@t"], repo)
    git(["config", "user.name", "t"], repo)
    (repo / "a.txt").write_text("a\n")
    git(["add", "-A"], repo)
    git(["commit", "-q", "-m", "init"], repo)
    git(["remote", "add", "origin", str(remote)], repo)
    git(["push", "-q", "origin", "main"], repo)
    return repo


def project(repo: Path) -> Project:
    return Project("demo", repo, "main", (), ("python -c pass",), (), 3, 800)


def test_worktree_created_from_origin_and_reused(tmp_path):
    repo = make_repo(tmp_path)
    git(["checkout", "-q", "-b", "user-wip"], repo)
    (repo / "wip.txt").write_text("user work\n")
    p = project(repo)
    wt = ensure_worktree(p, "t-1", "harness/t-1")
    assert wt == worktree_path(p, "t-1") == tmp_path / "projects" / ".harness-worktrees" / "demo" / "t-1"
    assert (wt / "a.txt").exists() and not (wt / "wip.txt").exists()
    assert git(["branch", "--show-current"], wt) == "harness/t-1"
    assert ensure_worktree(p, "t-1", "harness/t-1") == wt
    assert git(["branch", "--show-current"], repo) == "user-wip"


def test_commit_if_dirty_is_idempotent(tmp_path):
    repo = make_repo(tmp_path)
    wt = ensure_worktree(project(repo), "t-2", "harness/t-2")
    git(["config", "user.email", "t@t"], wt)
    git(["config", "user.name", "t"], wt)
    (wt / "b.txt").write_text("b\n")
    assert commit_if_dirty(wt, "harness: t-2") is True
    assert commit_if_dirty(wt, "harness: t-2") is False
    assert git(["log", "-1", "--format=%s"], wt) == "harness: t-2"


def test_numstat_lines_skips_binary():
    assert numstat_lines("3\t1\ta.py\n-\t-\timg.png\n10\t0\tb.py\n") == 14
    assert numstat_lines("") == 0


def test_merge_pr_readies_draft_then_drops_worktree_and_branches(tmp_path, monkeypatch):
    from harness import traces
    repo = make_repo(tmp_path)
    wt = ensure_worktree(project(repo), "t-1", "harness/t-1")
    git(["push", "-q", "-u", "origin", "harness/t-1"], wt)
    calls = []

    def gh(args, cwd):
        calls.append(args[:2])
        out = '{"state": "OPEN", "isDraft": true, "headRefName": "harness/t-1"}' if args[1] == "view" else ""
        return subprocess.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(traces, "_gh", gh)
    assert traces.merge_pr(repo, 5) == "PR #5 zmergován (squash)."
    assert calls == [["pr", "view"], ["pr", "ready"], ["pr", "merge"]]
    assert not wt.exists()
    assert git(["branch", "--list", "harness/t-1"], repo) == ""
    assert git(["ls-remote", "--heads", "origin", "harness/t-1"], repo) == ""


def test_merge_pr_refuses_closed_and_keeps_user_worktree(tmp_path, monkeypatch):
    import pytest

    from harness import traces
    repo = make_repo(tmp_path)
    user_wt = tmp_path / "mine"
    git(["worktree", "add", "-q", "-b", "feat", str(user_wt)], repo)
    view = {"state": "CLOSED"}
    monkeypatch.setattr(traces, "_gh", lambda args, cwd: subprocess.CompletedProcess(
        args, 0, '{"state": "%s", "isDraft": false, "headRefName": "feat"}' % view["state"], ""))
    with pytest.raises(RuntimeError, match="closed"):
        traces.merge_pr(repo, 5)
    view["state"] = "OPEN"
    traces.merge_pr(repo, 5)
    assert user_wt.exists()  # only harness worktrees are force-removed



def _changelog_conflict(tmp_path: Path, other: str | None = None) -> Path:
    """origin/main and harness/t-1 both append an entry to CHANGELOG.md (and both change `other`)."""
    repo = make_repo(tmp_path)

    def commit(files: dict, msg: str, branch: str) -> None:
        for name, text in files.items():
            (repo / name).write_text(text)
        git(["add", "-A"], repo)
        git(["commit", "-q", "-m", msg], repo)
        git(["push", "-q", "origin", branch], repo)
    commit({"CHANGELOG.md": "## old\n"}, "changelog", "main")
    git(["checkout", "-q", "-b", "harness/t-1"], repo)
    commit({"CHANGELOG.md": "## old\n## t-1\n", "b.txt": "b\n"} | ({other: "t-1\n"} if other else {}), "t-1",
           "harness/t-1")
    git(["checkout", "-q", "main"], repo)
    commit({"CHANGELOG.md": "## old\n## other PR\n"} | ({other: "main\n"} if other else {}), "other", "main")
    return repo


def _conflicting_gh(repo: Path, calls: list):
    """gh: the PR is CONFLICTING, then UNKNOWN while GitHub recomputes, then MERGEABLE; merge records the branch."""
    views = iter(["CONFLICTING", "UNKNOWN", "MERGEABLE"])

    def gh(args, cwd):
        calls.append(args[:2])
        if args[1] == "view":
            out = ('{"state": "OPEN", "isDraft": false, "headRefName": "harness/t-1", "baseRefName": "main", '
                   '"mergeable": "%s"}' % next(views))
            return subprocess.CompletedProcess(args, 0, out, "")
        if args[1] == "merge":
            git(["fetch", "-q", "origin"], repo)
            calls.append({"changelog": git(["show", "origin/harness/t-1:CHANGELOG.md"], repo).splitlines(),
                          "parent_is_main": git(["rev-parse", "origin/harness/t-1~1"], repo)
                          == git(["rev-parse", "origin/main"], repo)})
        return subprocess.CompletedProcess(args, 0, "", "")
    return gh


def test_merge_pr_resolves_changelog_only_conflict(tmp_path, monkeypatch):
    from harness import traces
    repo = _changelog_conflict(tmp_path)
    calls = []
    monkeypatch.setattr(traces, "_gh", _conflicting_gh(repo, calls))
    monkeypatch.setattr(traces.time, "sleep", lambda s: None)
    assert traces.merge_pr(repo, 5) == "PR #5 zmergován (squash)."
    assert calls == [["pr", "view"], ["pr", "view"], ["pr", "view"], ["pr", "merge"],
                     {"changelog": ["## old", "## other PR", "## t-1"], "parent_is_main": True}]
    assert len(git(["worktree", "list"], repo).splitlines()) == 1  # throwaway worktree removed


def test_merge_pr_leaves_other_conflicts_alone(tmp_path, monkeypatch):
    import pytest

    from harness import traces
    repo = _changelog_conflict(tmp_path, other="a.txt")
    before = git(["ls-remote", "origin", "harness/t-1"], repo)
    calls = []
    monkeypatch.setattr(traces, "_gh", _conflicting_gh(repo, calls))
    with pytest.raises(RuntimeError, match="a.txt"):
        traces.merge_pr(repo, 5)
    assert calls == [["pr", "view"]]
    assert git(["ls-remote", "origin", "harness/t-1"], repo) == before
    assert len(git(["worktree", "list"], repo).splitlines()) == 1


def _resolver(tmp_path, monkeypatch, write: str | None, verify: str = "python -c pass"):
    """Content conflict in a.txt; the fake agent writes `write` into a.txt (None = leaves the markers)."""
    from harness.nodes import real
    from harness.sdk import WorkerResult
    repo = _changelog_conflict(tmp_path, other="a.txt")
    prompts = []

    def agent(prompt, *, cwd, **kw):
        prompts.append(prompt)
        if write is not None:
            (Path(cwd) / "a.txt").write_text(write)
        return WorkerResult("ok", {}, None, None)
    monkeypatch.setattr(real, "run_agent", agent)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = Project("demo", repo, "main", (), (verify,), (), 3, 800)
    return repo, p, prompts


def test_agent_resolves_content_conflict_and_pushes(tmp_path, monkeypatch):
    from harness.nodes.real import resolve_conflict_with_agent
    repo, p, prompts = _resolver(tmp_path, monkeypatch, "main\nt-1\n")
    assert "a.txt" in resolve_conflict_with_agent(p, "harness/t-1", "subscription")
    assert len(prompts) == 1 and "a.txt" in prompts[0] and "CHANGELOG.md" not in prompts[0]
    git(["fetch", "-q", "origin"], repo)
    assert git(["show", "origin/harness/t-1:a.txt"], repo) == "main\nt-1"
    assert git(["show", "origin/harness/t-1:CHANGELOG.md"], repo).splitlines() == ["## old", "## t-1", "## other PR"]
    assert git(["show", "origin/harness/t-1:b.txt"], repo) == "b"
    git(["merge-base", "--is-ancestor", "origin/main", "origin/harness/t-1"], repo)  # raises if main is not merged in
    assert len(git(["worktree", "list"], repo).splitlines()) == 1


def test_agent_leaving_markers_or_failing_verify_pushes_nothing(tmp_path, monkeypatch):
    import pytest

    from harness.nodes.real import resolve_conflict_with_agent
    repo, p, _ = _resolver(tmp_path / "markers", monkeypatch, None)
    before = git(["ls-remote", "origin", "harness/t-1"], repo)
    with pytest.raises(RuntimeError, match="a.txt"):
        resolve_conflict_with_agent(p, "harness/t-1", "subscription")
    assert git(["ls-remote", "origin", "harness/t-1"], repo) == before
    assert len(git(["worktree", "list"], repo).splitlines()) == 1

    repo, p, _ = _resolver(tmp_path / "verify", monkeypatch, "x\n", verify="exit 1")
    before = git(["ls-remote", "origin", "harness/t-1"], repo)
    with pytest.raises(RuntimeError, match="exit 1"):
        resolve_conflict_with_agent(p, "harness/t-1", "subscription")
    assert git(["ls-remote", "origin", "harness/t-1"], repo) == before


def test_agent_resolver_pushes_clean_merge(tmp_path, monkeypatch):
    from harness.nodes import real
    repo = make_repo(tmp_path)

    def commit(name: str, branch: str) -> None:
        (repo / name).write_text(name)
        git(["add", "-A"], repo)
        git(["commit", "-q", "-m", name], repo)
        git(["push", "-q", "origin", branch], repo)
    git(["checkout", "-q", "-b", "harness/t-1"], repo)
    commit("b.txt", "harness/t-1")
    git(["checkout", "-q", "main"], repo)
    commit("c.txt", "main")

    def agent(*a, **kw):
        raise AssertionError("agent se nemá volat")
    monkeypatch.setattr(real, "run_agent", agent)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = Project("demo", repo, "main", (), ("python -c pass",), (), 3, 800)
    assert real.resolve_conflict_with_agent(p, "harness/t-1", "subscription")
    git(["fetch", "-q", "origin"], repo)
    git(["merge-base", "--is-ancestor", "origin/main", "origin/harness/t-1"], repo)


def _ci_fixer(tmp_path, monkeypatch, write: bool, verify: str = "python -c pass"):
    from harness import traces
    from harness.nodes import real
    from harness.sdk import WorkerResult
    repo = _changelog_conflict(tmp_path)
    prompts = []

    def agent(prompt, *, cwd, **kw):
        prompts.append(prompt)
        if write:
            (Path(cwd) / "fix.txt").write_text("fixed")
        return WorkerResult("test was broken", {}, None, None)
    monkeypatch.setattr(real, "run_agent", agent)
    monkeypatch.setattr(traces, "_gh_stdout", lambda args, cwd, label: "FAILED test_x" if args[1] == "view" else "7")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return repo, Project("demo", repo, "main", (), (verify,), (), 3, 800), prompts


def test_agent_fixes_red_ci_and_pushes(tmp_path, monkeypatch):
    from harness.nodes.real import fix_ci_with_agent
    repo, p, prompts = _ci_fixer(tmp_path, monkeypatch, write=True)
    assert "CI opraveno" in fix_ci_with_agent(p, "harness/t-1", "subscription")
    assert "FAILED test_x" in prompts[0]
    git(["fetch", "-q", "origin"], repo)
    assert git(["show", "origin/harness/t-1:fix.txt"], repo) == "fixed"
    assert git(["log", "-1", "--format=%s", "origin/harness/t-1"], repo) == "fix: failing CI"
    assert len(git(["worktree", "list"], repo).splitlines()) == 1


def test_ci_fix_without_change_or_with_failing_verify_pushes_nothing(tmp_path, monkeypatch):
    import pytest

    from harness.nodes.real import fix_ci_with_agent
    for sub, write, verify, error in (("idle", False, "python -c pass", "nic nezměnil"), ("verify", True, "exit 1", "exit 1")):
        repo, p, _ = _ci_fixer(tmp_path / sub, monkeypatch, write, verify)
        before = git(["ls-remote", "origin", "harness/t-1"], repo)
        with pytest.raises(RuntimeError, match=error):
            fix_ci_with_agent(p, "harness/t-1", "subscription")
        assert git(["ls-remote", "origin", "harness/t-1"], repo) == before
        assert len(git(["worktree", "list"], repo).splitlines()) == 1

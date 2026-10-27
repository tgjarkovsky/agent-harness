from harness.backlog import parse
from harness.nodes.real import git
from harness.projects import Config
from harness.scan import new_proposals, proposal_lines, scan_all, scan_project
from tests.test_real_git import make_repo, project


def commit_files(repo, files: dict[str, str]):
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(["add", "-A"], repo)
    git(["commit", "-q", "-m", "todos"], repo)
    git(["push", "-q", "origin", "main"], repo)


def test_scan_reads_origin_base_not_working_copy(tmp_path):
    repo = make_repo(tmp_path)
    commit_files(repo, {"src/a.ts": "x\n// TODO: handle empty album\n", "lib/b.py": "# FIXME broken on Windows\n",
                        "c.txt": "TODOS are not markers\n", "docs/plan.md": "- TODO: list\n",
                        "d.py": 'GREP = "TODO|FIXME"\n"""All TODO markers"""\n# scan TODO daily\n'})
    (repo / "wip.ts").write_text("// TODO: uncommitted\n", encoding="utf-8")
    props = scan_project(project(repo))
    assert [(p.path, p.line, p.title) for p in props] == [
        ("lib/b.py", 1, "FIXME: broken on Windows"), ("src/a.ts", 2, "TODO: handle empty album")]
    assert all(p.id.startswith("demo-todo-") and len(p.id) == len("demo-todo-") + 6 for p in props)


def test_scan_ids_stable_when_line_moves(tmp_path):
    repo = make_repo(tmp_path)
    commit_files(repo, {f"f{i}.py": f"# TODO item {i}\n" for i in range(8)})
    first = scan_project(project(repo))
    assert len(first) == 8
    commit_files(repo, {"f0.py": "\n\n# TODO item 0\n"})
    assert scan_project(project(repo))[0].id == first[0].id


def test_scan_without_markers_is_empty(tmp_path):
    assert scan_project(project(make_repo(tmp_path))) == []


def test_new_proposals_skip_ids_in_backlog_and_lines_parse(tmp_path):
    repo = make_repo(tmp_path)
    commit_files(repo, {"a.py": "# TODO one\n# TODO two\n"})
    props = scan_project(project(repo))
    text = "## demo\n" + "\n".join(proposal_lines(props[0])) + "\n"
    [left] = new_proposals(props, parse(text))
    assert left == props[1]
    [t] = parse("## demo\n" + "\n".join(proposal_lines(left)) + "\n")
    assert (t.status, t.priority, t.title) == ("~", 3, "TODO: two")
    assert "a.py:2" in t.description


def test_scan_all_limits_after_skipping_known(tmp_path):
    repo = make_repo(tmp_path)
    commit_files(repo, {f"f{i}.py": f"# TODO item {i}\n" for i in range(7)})
    props = scan_project(project(repo))
    lines = [line for p in props[:5] for line in proposal_lines(p)]
    lines[0] = lines[0].replace("[~]", "[-]")  # a rejected proposal stays known too
    text = "## demo\n" + "\n".join(lines) + "\n"
    cfg = Config("subscription", 3, 0.7, {"demo": project(repo)})
    new_text, found = scan_all(cfg, text, limit=5)
    assert found == props[5:]
    tasks = parse(new_text)
    assert len(tasks) == 7
    assert [(t.id, t.status) for t in tasks[5:]] == [(p.id, "~") for p in props[5:]]


def test_daemon_scan_appends_new_proposals_once(tmp_path):
    from harness.cli import _scan_into
    from harness.projects import Config
    repo = make_repo(tmp_path)
    commit_files(repo, {"src/a.ts": "// TODO: handle empty album\n"})
    cfg = Config("subscription", 3, 0.7, {"demo": project(repo)})
    backlog_path = tmp_path / "BACKLOG.md"
    backlog_path.write_bytes("## demo\r\n- [ ] P1 d-1 Existující\r\n".encode("utf-8"))
    assert _scan_into(cfg, backlog_path) == 1
    assert _scan_into(cfg, backlog_path) == 0
    tasks = parse(backlog_path.read_bytes().decode("utf-8"))
    assert [t.status for t in tasks] == [" ", "~"]


def test_scan_marker_only_at_comment_start(tmp_path):
    repo = make_repo(tmp_path)
    commit_files(repo, {"prose.py": "# sken TODO 1× denně\n# harness serve: … sken TODO 1× denně na začátku okna\n",
                        "a.py": "# TODO: opravit\n", "b.js": "// FIXME y\n", "c.js": "/*\n * TODO z\n */\n"})
    props = scan_project(project(repo))
    assert [(p.path, p.title) for p in props] == [
        ("a.py", "TODO: opravit"), ("b.js", "FIXME: y"), ("c.js", "TODO: z")]


def test_scan_title_uses_safe_title_so_rejection_keeps_it(tmp_path):
    from harness.backlog import set_status
    repo = make_repo(tmp_path)
    commit_files(repo, {"a.py": "# TODO: a — b → c\n"})
    [p] = scan_project(project(repo))
    assert p.title == "TODO: a – b -> c"
    text = set_status("## demo\n" + "\n".join(proposal_lines(p)) + "\n", p.id, "-", " — zamítnuto")
    [t] = parse(text)
    assert (t.title, t.note) == ("TODO: a – b -> c", "zamítnuto")


def test_scan_duplicate_marker_in_one_file_gives_one_proposal(tmp_path):
    repo = make_repo(tmp_path)
    commit_files(repo, {"a.py": "# TODO: fix\nx = 1\n# TODO: fix\n"})
    [p] = scan_project(project(repo))
    assert (p.path, p.line) == ("a.py", 1)
    cfg = Config("subscription", 3, 0.7, {"demo": project(repo)})
    text, found = scan_all(cfg, "## demo\n")
    assert len(found) == 1 and len(parse(text)) == 1


def test_daemon_scan_duplicate_marker_parses(tmp_path):
    from harness.cli import _scan_into
    repo = make_repo(tmp_path)
    commit_files(repo, {"a.py": "# TODO: fix\nx = 1\n# TODO: fix\n"})
    cfg = Config("subscription", 3, 0.7, {"demo": project(repo)})
    backlog_path = tmp_path / "BACKLOG.md"
    backlog_path.write_bytes(b"## demo\n")
    assert _scan_into(cfg, backlog_path) == 1
    assert len(parse(backlog_path.read_bytes().decode("utf-8"))) == 1

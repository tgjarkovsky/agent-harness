import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness import events
from harness.dashboard.app import create_app

ROOT = Path(__file__).parent.parent


@pytest.fixture
def client(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    return TestClient(create_app(tmp_path))


def wait_for(client, run_id, kind, timeout=10):
    deadline = time.time() + timeout
    kinds = []
    while time.time() < deadline:
        kinds = [e["kind"] for e in client.get(f"/runs/{run_id}/events").json()]
        if kind in kinds:
            return kinds
        time.sleep(0.05)
    raise AssertionError(f"{kind} not reached: {kinds}")


def test_graph(client):
    g = client.get("/graph").json()
    assert "pick_tasks" in g["mermaid"] and "verify" in g["worker"]


def test_scenarios_listed(client):
    names = {s["name"] for s in client.get("/scenarios").json()}
    assert {"happy-path", "flaky-tests", "rate-limit-pause-resume"} <= names


def test_simulate_runs_to_end(client):
    run_id = client.post("/simulate", json={"scenario": "happy-path", "delay": 0}).json()["run_id"]
    wait_for(client, run_id, "run_end")
    assert any(r["run_id"] == run_id for r in client.get("/runs").json())
    assert client.get(f"/backlog?sim={run_id}").json()[0]["status"] == "x"
    assert client.get("/usage").json()["summary"]


def test_unknown_or_traversal_scenario_404(client):
    assert client.post("/simulate", json={"scenario": "../../etc/passwd"}).status_code == 404
    assert client.post("/simulate", json={"scenario": "nope"}).status_code == 404


def test_step_mode(client):
    run_id = client.post("/simulate", json={"scenario": "happy-path", "step": True, "delay": 0}).json()["run_id"]
    deadline = time.time() + 5
    while client.get(f"/runs/{run_id}/state").json()["next"] != ["pick_tasks"]:
        assert time.time() < deadline
        time.sleep(0.05)
    assert client.post(f"/runs/{run_id}/step").json() == {"ok": True}
    deadline = time.time() + 5
    while client.get(f"/runs/{run_id}/state").json()["next"] != ["preflight"]:
        assert time.time() < deadline
        time.sleep(0.05)


def test_state_reports_running_step(client):
    run_id = client.post("/simulate", json={"scenario": "happy-path", "step": True, "delay": 0.5}).json()["run_id"]
    deadline = time.time() + 5
    while client.get(f"/runs/{run_id}/state").json()["next"] != ["pick_tasks"]:
        assert time.time() < deadline
        time.sleep(0.05)
    client.post(f"/runs/{run_id}/step")  # pick_tasks is instant (not a fake node)
    while client.get(f"/runs/{run_id}/state").json()["next"] != ["preflight"]:
        assert time.time() < deadline
        time.sleep(0.05)
    client.post(f"/runs/{run_id}/step")  # preflight sleeps 0.5 s
    time.sleep(0.1)
    assert client.get(f"/runs/{run_id}/state").json()["running"] is True


def test_state_unknown_run_404(client):
    assert client.get("/runs/nope/state").status_code == 404


def wait_next(client, run_id, expected, timeout=5):
    deadline = time.time() + timeout
    while (st := client.get(f"/runs/{run_id}/state").json())["next"] != expected or st["running"]:
        assert time.time() < deadline, st
        time.sleep(0.05)


def test_step_mode_shows_parallel_workers(client):
    run_id = client.post("/simulate", json={"scenario": "parallel-projects", "step": True, "delay": 0}).json()["run_id"]
    wait_next(client, run_id, ["pick_tasks"])
    client.post(f"/runs/{run_id}/step")
    wait_next(client, run_id, ["preflight", "preflight"])
    client.post(f"/runs/{run_id}/step")
    wait_next(client, run_id, ["make_worktree", "make_worktree"])
    workers = {e["worker"] for e in client.get(f"/runs/{run_id}/events").json() if e["worker"]}
    assert workers == {"sim-a1", "sim-b1"}


def test_answer_unclear_question(client):
    run_id = client.post("/simulate", json={"scenario": "unclear-task", "delay": 0}).json()["run_id"]
    wait_for(client, run_id, "waiting")
    deadline = time.time() + 5
    while (st := client.get(f"/runs/{run_id}/state").json())["running"]:
        assert time.time() < deadline
        time.sleep(0.05)
    [q] = st["questions"]
    assert q["question"] == "Co přesně měřit?"
    assert client.post(f"/runs/{run_id}/answer", json={"id": "nope", "answer": "x"}).status_code == 404
    assert client.post(f"/runs/{run_id}/answer", json={"id": q["id"], "answer": ""}).json() == {"ok": True}
    wait_for(client, run_id, "run_end")
    assert client.get(f"/backlog?sim={run_id}").json()[0]["status"] == "?"


def test_second_answer_while_resuming_is_rejected(client, monkeypatch):
    import threading
    from harness.dashboard import app as app_module
    run_id = client.post("/simulate", json={"scenario": "unclear-task", "delay": 0}).json()["run_id"]
    wait_for(client, run_id, "waiting")
    deadline = time.time() + 5
    while (st := client.get(f"/runs/{run_id}/state").json())["running"]:
        assert time.time() < deadline
        time.sleep(0.05)
    started = []
    monkeypatch.setattr(app_module.threading, "Thread", lambda target, args, daemon: type(
        "T", (), {"start": lambda self: started.append(1)})())
    q = st["questions"][0]
    assert client.post(f"/runs/{run_id}/answer", json={"id": q["id"], "answer": "a"}).status_code == 200
    assert client.post(f"/runs/{run_id}/answer", json={"id": q["id"], "answer": "b"}).status_code == 409
    assert started == [1]


def test_calls_steps_and_costs(tmp_path):
    from harness import events, traces
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    conn = events.connect(tmp_path / "harness.db")
    cid = traces.record(conn, {"run_id": "go-1", "worker": "ak-01", "node": "plan", "model": "opus",
                               "input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 3, "cache_write_tokens": 4,
                               "cost_usd": 0.5, "status": "ok"},
                        [{"kind": "tool_use", "tool": "Read", "summary": "{}", "is_error": False}])
    client = TestClient(create_app(tmp_path))
    calls = client.get("/runs/go-1/calls").json()
    assert [(c["id"], c["node"], c["steps"]) for c in calls] == [(cid, "plan", 1)]
    assert client.get(f"/calls/{cid}/steps").json()[0]["tool"] == "Read"
    costs = client.get("/costs").json()
    assert costs["tasks"][0]["task_id"] == "ak-01" and costs["tasks"][0]["cost_usd"] == 0.5


def test_overview_reads_projects_yaml_every_request(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    (tmp_path / "a").mkdir()
    yaml_path = tmp_path / "projects.yaml"
    yaml_path.write_text("projects:\n  alpha: {path: a, base: main, verify: [x]}\n", encoding="utf-8")
    (tmp_path / "BACKLOG.md").write_text("# Backlog\n\n## alpha\n- [ ] P1 a-1 Úkol\n- [~] P2 a-2 Návrh\n  popis\n",
                                         encoding="utf-8")
    calls = []
    prs = lambda path: calls.append(path) or [{"number": 1, "title": "PR", "url": "u", "isDraft": True}]
    client = TestClient(create_app(tmp_path, prs=prs))
    body = client.get("/overview").json()
    [alpha] = body["projects"]
    assert alpha["name"] == "alpha" and alpha["prs"][0]["number"] == 1
    assert alpha["counts"][" "] == 1 and alpha["counts"]["~"] == 1
    assert [p["id"] for p in body["proposals"]] == ["a-2"] and body["serve"] is None
    yaml_path.write_text(yaml_path.read_text(encoding="utf-8") + "  beta: {path: b, base: main, verify: [x]}\n",
                         encoding="utf-8")
    body = client.get("/overview").json()
    assert [p["name"] for p in body["projects"]] == ["alpha", "beta"]
    assert body["projects"][1]["prs"] is None  # missing checkout: no gh call
    assert len(calls) == 1  # alpha cached
    assert body["boot"] == client.get("/overview").json()["boot"]
    assert TestClient(create_app(tmp_path, prs=prs)).get("/overview").json()["boot"] != body["boot"]


def test_overview_counts_merged_prs_since_last_cleanup(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "projects.yaml").write_text("projects:\n  alpha: {path: a, base: main, verify: [x]}\n"
                                            "  beta: {path: a, base: main, verify: [x]}\n", encoding="utf-8")
    conn = events.connect(tmp_path / "harness.db")
    events.log(conn, "go-1", "end", node="discover", worker="alpha", delta={"proposals": 1, "cleanup": True})
    events.log(conn, "go-2", "end", node="discover", worker="alpha", delta={"proposals": 0, "cleanup": False})
    events.log(conn, "go-2", "end", node="discover", worker="beta", delta={"proposals": 0, "cleanup": False})
    (ts,) = events.last_cleanups(conn).values()
    asked = []
    merged = lambda path, since: asked.append(since) or 4
    body = TestClient(create_app(tmp_path, prs=lambda p: [], merged=merged)).get("/overview").json()
    alpha, beta = body["projects"]
    assert alpha["cleanup"] == {"ts": ts, "prs": 4} and asked == [ts]  # go-2 did not run the cleanup lens
    assert beta["cleanup"] is None


def test_control_needs_serve_and_local_origin(client):
    assert client.post("/control/run", json={}).status_code == 503
    assert client.post("/control/run", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/desktop").status_code == 200


def test_merge_pr_endpoint(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "projects.yaml").write_text("projects:\n  alpha: {path: a, base: main, verify: [x]}\n", encoding="utf-8")
    listed, merged = [], []
    prs = lambda path: listed.append(path) or []

    def merge(path, number):
        if number == 13:
            raise RuntimeError("Pull request is not mergeable")
        merged.append((path.name, number))
        return f"PR #{number} zmergován (squash)."
    client = TestClient(create_app(tmp_path, prs=prs, merge=merge))
    client.get("/overview")
    assert client.post("/prs/merge", json={"project": "alpha", "number": 7}).json() == {
        "message": "PR #7 zmergován (squash)."}
    assert merged == [("a", 7)]
    client.get("/overview")
    assert len(listed) == 2  # merge dropped the PR cache
    assert client.post("/prs/merge", json={"project": "alpha", "number": 13}).status_code == 409
    assert client.post("/prs/merge", json={"project": "nope", "number": 7}).status_code == 404
    assert client.post("/prs/merge", json={"project": "alpha", "number": 7},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    assert merged == [("a", 7)]


def test_overview_resolves_conflicting_harness_prs_in_background(tmp_path):
    import threading
    (tmp_path / "a").mkdir()
    (tmp_path / "projects.yaml").write_text("projects:\n  alpha: {path: a, base: main, verify: [x]}\n", encoding="utf-8")
    pr = {"mergeable": "CONFLICTING", "baseRefName": "main", "headRefOid": "s1"}
    listed, resolved, done = [], [], threading.Event()
    prs = lambda path: listed.append(path) or [
        pr | {"number": 1, "headRefName": "harness/t-1"},
        pr | {"number": 2, "headRefName": "harness/t-2"},
        pr | {"number": 3, "headRefName": "user-branch"},
        pr | {"number": 4, "headRefName": "harness/t-4", "mergeable": "MERGEABLE"}]

    def resolve(path, branch, base):
        resolved.append((path.name, branch, base))
        if len(resolved) == 2:
            done.set()
        if branch == "harness/t-2":
            raise RuntimeError("konflikt mimo CHANGELOG.md: a.txt")
    client = TestClient(create_app(tmp_path, prs=prs, resolve=resolve))
    client.get("/overview")
    assert done.wait(5)
    assert sorted(resolved) == [("a", "harness/t-1", "main"), ("a", "harness/t-2", "main")]
    for _ in range(50):  # the successful resolve drops the cache after it returns
        client.get("/overview")
        if len(listed) == 2:
            break
        time.sleep(0.05)
    assert len(listed) == 2
    assert len(resolved) == 2  # same head commits: no second attempt


def test_red_ci_on_harness_pr_fixed_once_per_head_via_shared_pr_cache(tmp_path):
    import threading
    (tmp_path / "a").mkdir()
    (tmp_path / "projects.yaml").write_text("projects:\n  alpha: {path: a, base: main, verify: [x]}\n", encoding="utf-8")
    pr = {"mergeable": "MERGEABLE", "baseRefName": "main", "headRefOid": "s1", "ci": "fail"}
    pulls = [pr | {"number": 1, "headRefName": "harness/t-1"},
             pr | {"number": 2, "headRefName": "user-branch"},
             pr | {"number": 3, "headRefName": "harness/t-3", "ci": "ok"},
             pr | {"number": 4, "headRefName": "harness/t-4", "mergeable": "CONFLICTING"}]  # rebase first
    fixed, done = [], threading.Event()

    def fix_ci(project, branch, billing):
        fixed.append((project.name, branch))
        done.set()
        return "CI opraveno"
    app = create_app(tmp_path, prs=lambda path: pulls, resolve=lambda *a: None, fix_ci=fix_ci)
    path = (tmp_path / "a").resolve()
    app.state.prs(path)  # what serve's autorun calls, no dashboard request needed
    assert done.wait(5)
    for _ in range(50):  # the fix drops the cache: the next read lists again, same head → no second fix
        app.state.prs(path)
        time.sleep(0.02)
    assert fixed == [("alpha", "harness/t-1")]


def _conflict_app(tmp_path, fix):
    """Two harness PRs whose rebase hits a conflict in a.txt; returns the client and the requeue calls."""
    import threading
    from harness.traces import ContentConflict
    (tmp_path / "a").mkdir()
    (tmp_path / "projects.yaml").write_text("projects:\n  alpha: {path: a, base: main, verify: [x]}\n", encoding="utf-8")
    (tmp_path / "BACKLOG.md").write_text(
        "## alpha\n- [x] P2 t-1 [feature] Titulek → https://x/pull/1\n  popis\n", encoding="utf-8")
    pr = {"mergeable": "CONFLICTING", "baseRefName": "main", "headRefOid": "s1"}
    pulls = [pr | {"number": 1, "headRefName": "harness/t-1"}, pr | {"number": 2, "headRefName": "harness/t-2"}]
    seen = threading.Semaphore(0)

    def resolve(path, branch, base):
        seen.release()
        raise ContentConflict(["a.txt"])
    requeued = []
    client = TestClient(create_app(tmp_path, prs=lambda p: pulls, resolve=resolve, fix=fix,
                                   requeue=lambda path, n, branch: requeued.append((path.name, n, branch))))
    client.get("/overview")
    assert seen.acquire(timeout=5) and seen.acquire(timeout=5)
    return client, requeued


def _conflict_of(client, number):
    for _ in range(100):
        pr = next(p for p in client.get("/overview").json()["projects"][0]["prs"] if p["number"] == number)
        if not pr["conflict"] or pr["conflict"]["state"] != "resolving":
            return pr["conflict"]
        time.sleep(0.05)
    raise AssertionError("agent still resolving")


def test_unresolvable_conflict_waits_for_the_owner(tmp_path):
    fixed = []

    def fix(project, branch, billing):
        fixed.append((project.name, branch, billing))
        if branch == "harness/t-2":
            raise RuntimeError("`pytest` po vyřešení konfliktu selhal")
    client, requeued = _conflict_app(tmp_path, fix)
    assert _conflict_of(client, 1) == {"files": ["a.txt"], "state": "open"}
    assert requeued == [] and fixed == []  # nothing happens until the owner picks an action
    post = lambda n, action, **kw: client.post("/prs/conflict", json={"project": "alpha", "number": n, "action": action}, **kw)

    assert post(1, "agent").status_code == 200
    assert _conflict_of(client, 1) is None
    assert post(2, "agent").status_code == 200
    assert _conflict_of(client, 2) == {"files": ["a.txt"], "state": "failed", "error": "`pytest` po vyřešení konfliktu selhal"}
    assert fixed == [("alpha", "harness/t-1", "subscription"), ("alpha", "harness/t-2", "subscription")]

    assert post(2, "keep").status_code == 200
    assert _conflict_of(client, 2)["state"] == "kept"
    assert post(2, "agent", headers={"Origin": "https://evil.example"}).status_code == 403
    assert post(2, "bogus").status_code == 422
    assert post(9, "keep").status_code == 404


def test_reimplement_closes_pr_and_requeues_task(tmp_path):
    client, requeued = _conflict_app(tmp_path, lambda *a: None)
    r = client.post("/prs/conflict", json={"project": "alpha", "number": 1, "action": "reimplement"})
    assert r.status_code == 200, r.text
    assert requeued == [("a", 1, "harness/t-1")]
    assert (tmp_path / "BACKLOG.md").read_text(encoding="utf-8") == "## alpha\n- [ ] P2 t-1 [feature] Titulek\n  popis\n"
    assert client.post("/prs/conflict", json={"project": "alpha", "number": 2, "action": "reimplement"}).status_code == 422
    assert requeued == [("a", 1, "harness/t-1")]  # t-2 is not in the backlog: its PR stays open


def test_progress_tracks_worker_nodes(client):
    run_id = client.post("/simulate", json={"scenario": "flaky-tests", "delay": 0}).json()["run_id"]
    wait_for(client, run_id, "run_end")
    p = client.get(f"/runs/{run_id}/progress").json()
    assert p["stages"] == {"pick_tasks": "done", "worker": "done", "reflect": "done"}
    [w] = p["workers"]
    assert w["id"] == "sim-01" and w["title"] and w["outcome"] == "pr_opened" and w["attempts"] == 3
    assert list(w["nodes"]) == ["preflight", "make_worktree", "plan", "setup", "implement", "verify", "open_pr", "report"]
    assert set(w["nodes"].values()) == {"done"}


def test_progress_waiting_worker_stays_active(client):
    run_id = client.post("/simulate", json={"scenario": "unclear-task", "delay": 0}).json()["run_id"]
    wait_for(client, run_id, "waiting")
    p = client.get(f"/runs/{run_id}/progress").json()
    assert p["stages"]["worker"] == "active" and p["workers"][0]["last"] == "clarify"


def test_progress_go_discover(client):
    run_id = client.post("/simulate", json={"scenario": "go-happy", "delay": 0}).json()["run_id"]
    wait_for(client, run_id, "waiting")
    p = client.get(f"/runs/{run_id}/progress").json()
    assert p["discover"] == {"demo": "done", "web": "done"}
    assert p["stages"] == {"discover": "done", "prioritize": "done", "approve": "done"}


def test_usage_prefers_live_quota_and_falls_back_to_sdk_events(tmp_path):
    from harness import events
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    conn = events.connect(tmp_path / "harness.db")
    events.log(conn, "r", "end", rate_limit={"type": "five_hour", "status": "allowed", "utilization": None,
                                             "resets_at": 100})
    five, week = {"type": "five_hour", "utilization": 0.49}, {"type": "seven_day", "utilization": 0.3}
    live = TestClient(create_app(tmp_path, live_quota=lambda: {"five_hour": five, "seven_day": week})).get("/usage").json()
    assert (live["rate_limit"], live["seven_day"]) == (five, week)
    dead = TestClient(create_app(tmp_path, live_quota=lambda: None)).get("/usage").json()
    assert dead["rate_limit"]["status"] == "allowed" and dead["seven_day"] is None


def test_history_returns_digest_of_a_day(client):
    body = client.get("/history?day=2026-10-02").json()
    assert body["day"] == "2026-10-02" and body["text"].startswith("\U0001F4CB Digest")
    assert client.get("/history?day=vcera").status_code == 400


def test_overview_queue_is_ranked_and_only_holds_queued_tasks(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    (tmp_path / "a").mkdir()
    (tmp_path / "projects.yaml").write_text("projects:\n  alpha: {path: a, base: main, verify: [x]}\n", encoding="utf-8")
    (tmp_path / "BACKLOG.md").write_text(
        "# Backlog\n\n## alpha\n- [ ] P2 a-2 Druhý\n- [ ] P1 a-1 První\n- [~] P1 a-3 Návrh\n- [x] P1 a-4 Hotovo\n",
        encoding="utf-8")
    [alpha] = TestClient(create_app(tmp_path, prs=lambda path: [])).get("/overview").json()["projects"]
    assert [t["id"] for t in alpha["queue"]] == ["a-1", "a-2"]

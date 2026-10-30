"""Agent call ledger (cost, tokens incl. cache, steps) and task outcomes (PR merged/closed/open, failed)."""
import json
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from . import backlog
from .events import _lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, worker TEXT, node TEXT, model TEXT, started REAL,
  duration_ms INTEGER, num_turns INTEGER, input_tokens INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cache_write_tokens INTEGER, cost_usd REAL, status TEXT, error TEXT, session_id TEXT,
  billing TEXT);
CREATE TABLE IF NOT EXISTS agent_steps (
  call_id INTEGER NOT NULL, seq INTEGER NOT NULL, kind TEXT, tool TEXT, summary TEXT, is_error INTEGER);
CREATE TABLE IF NOT EXISTS task_outcomes (
  task_id TEXT PRIMARY KEY, project TEXT, pr_url TEXT, state TEXT, checked REAL);
CREATE INDEX IF NOT EXISTS agent_steps_call ON agent_steps(call_id);
CREATE INDEX IF NOT EXISTS agent_calls_run ON agent_calls(run_id);
"""
CALL_FIELDS = ("run_id", "worker", "node", "model", "started", "duration_ms", "num_turns", "input_tokens",
               "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd", "status", "error", "session_id",
               "billing")
TOKENS = "input_tokens + output_tokens + cache_read_tokens + cache_write_tokens"
# cost_usd is the SDK's list-price figure; only API-billed calls are money really spent (no billing = subscription)
PAID = "ROUND(SUM(CASE WHEN billing = 'api' THEN COALESCE(cost_usd, 0) ELSE 0 END), 6) AS paid_usd"
OVERHEAD_NODES = ("discover", "prioritize")
URL_RE = re.compile(r" → (https?://\S+)\s*$")
LOCAL_STATES = {"!": "failed", "?": "unclear"}
FINAL_STATES = ("merged", "closed")
GH_TIMEOUT = 20
CHANGELOG = "CHANGELOG.md"
MERGEABLE_TRIES, MERGEABLE_WAIT = 10, 2


def ensure(conn) -> None:
    conn.executescript(SCHEMA)
    if "billing" not in {r[1] for r in conn.execute("PRAGMA table_info(agent_calls)")}:
        conn.execute("ALTER TABLE agent_calls ADD COLUMN billing TEXT")


def cost_label(cost: float, paid: float) -> str:
    """Subscription cost is only an estimate; API cost was really paid — never sum them into one number."""
    estimate, paid = (cost or 0) - (paid or 0), paid or 0
    parts = ([f"~${estimate:.2f} odhad (subscription)"] if round(estimate, 2) else []) + \
            ([f"${paid:.2f} zaplaceno (API)"] if round(paid, 2) else [])
    return " + ".join(parts) or "$0.00"


def record(conn, call: dict, steps: list[dict]) -> int:
    with _lock:
        cur = conn.execute(f"INSERT INTO agent_calls ({', '.join(CALL_FIELDS)}) VALUES ({', '.join('?' * len(CALL_FIELDS))})",
                           [call.get(f) for f in CALL_FIELDS])
        conn.executemany("INSERT INTO agent_steps VALUES (?,?,?,?,?,?)",
                         [(cur.lastrowid, i, s["kind"], s["tool"], s["summary"], int(s["is_error"]))
                          for i, s in enumerate(steps)])
        conn.commit()
    return cur.lastrowid


def _rows(conn, sql: str, args=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def calls(conn, run_id: str) -> list[dict]:
    return _rows(conn, "SELECT c.*, (SELECT COUNT(*) FROM agent_steps s WHERE s.call_id = c.id) AS steps "
                       "FROM agent_calls c WHERE run_id = ? ORDER BY id", (run_id,))


def steps(conn, call_id: int) -> list[dict]:
    return [r | {"is_error": bool(r["is_error"])}
            for r in _rows(conn, "SELECT * FROM agent_steps WHERE call_id = ? ORDER BY seq", (call_id,))]


def costs(conn) -> dict:
    placeholders = ", ".join("?" * len(OVERHEAD_NODES))
    # every task with calls or a known outcome (PRs from before the ledger existed have no calls)
    tasks = _rows(conn, f"""
        WITH agg AS (
          SELECT worker, SUM(node = 'implement' AND status != 'rate_limited') AS attempts, COUNT(*) AS calls,
                 ROUND(SUM(COALESCE(cost_usd, 0)), 6) AS cost_usd, {PAID}, SUM({TOKENS}) AS tokens, MIN(id) AS first
          FROM agent_calls WHERE node NOT IN ({placeholders}) AND worker IS NOT NULL GROUP BY worker),
        ids AS (SELECT worker AS task_id FROM agg UNION SELECT task_id FROM task_outcomes)
        SELECT ids.task_id, o.project, o.state, o.pr_url, COALESCE(agg.attempts, 0) AS attempts,
               COALESCE(agg.calls, 0) AS calls, COALESCE(agg.cost_usd, 0) AS cost_usd,
               COALESCE(agg.paid_usd, 0) AS paid_usd,
               COALESCE(agg.tokens, 0) AS tokens
        FROM ids LEFT JOIN agg ON agg.worker = ids.task_id LEFT JOIN task_outcomes o ON o.task_id = ids.task_id
        ORDER BY agg.first IS NULL, agg.first, ids.task_id""", OVERHEAD_NODES)
    overhead = _rows(conn, f"""
        SELECT run_id, node, COUNT(*) AS calls, ROUND(SUM(COALESCE(cost_usd, 0)), 6) AS cost_usd, {PAID},
               SUM({TOKENS}) AS tokens
        FROM agent_calls WHERE node IN ({placeholders}) GROUP BY run_id, node ORDER BY MIN(id)""", OVERHEAD_NODES)
    return {"tasks": tasks, "overhead": overhead}


def summary(conn) -> list[dict]:
    """Tasks grouped by (project, planner model); a task without a plan call (ACCEPT skips it) gets '-'."""
    plan_models = {r["worker"]: r["model"] for r in _rows(conn, """
        SELECT worker, model FROM agent_calls WHERE id IN (
          SELECT MIN(id) FROM agent_calls WHERE node = 'plan' AND worker IS NOT NULL GROUP BY worker)""")}
    groups: dict[tuple, dict] = {}
    for t in costs(conn)["tasks"]:
        key = (t["project"] or "-", plan_models.get(t["task_id"]) or "-")
        g = groups.setdefault(key, {"project": key[0], "plan_model": key[1], "tasks": 0, "merged": 0, "closed": 0,
                                    "failed": 0, "unclear": 0, "open": 0, "cost_usd": 0.0})
        g["tasks"] += 1
        if t["state"] in g:
            g[t["state"]] += 1
        g["cost_usd"] += t["cost_usd"]
    return [g | {"cost_usd": round(g["cost_usd"], 6),
                 "cost_per_merged": round(g["cost_usd"] / g["merged"], 2) if g["merged"] else None}
            for _, g in sorted(groups.items())]


def backfill(conn) -> int:
    """Calls of real runs from before the ledger, rebuilt from event-log usage (no steps, no cache tokens)."""
    with _lock:
        # per event, not per run: a run resumed after phase 4 keeps its earlier, unrecorded calls; an event
        # is covered once a call of its run started no later than it (recorded calls and backfilled ones alike)
        rows = conn.execute("SELECT run_id, ts, node, worker, usage FROM events e WHERE usage IS NOT NULL "
                            "AND simulated = 0 AND NOT EXISTS (SELECT 1 FROM agent_calls a "
                            "WHERE a.run_id = e.run_id AND a.started <= e.ts) ORDER BY id").fetchall()
    n = 0
    for r in rows:
        for u in json.loads(r["usage"]):
            record(conn, {"run_id": r["run_id"], "worker": r["worker"], "node": u.get("node") or r["node"],
                          "model": u.get("model"), "started": r["ts"], "input_tokens": u.get("input_tokens", 0),
                          "output_tokens": u.get("output_tokens", 0), "cache_read_tokens": 0,
                          "cache_write_tokens": 0, "cost_usd": u.get("cost_usd"), "status": "ok"}, [])
            n += 1
    return n


def pr_urls(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = backlog.TASK_RE.match(line.rstrip())
        if m and m["status"] == "x" and (u := URL_RE.search(line)):
            out[m["id"]] = u[1]
    return out


def pr_state(url: str) -> str | None:
    """open / merged / closed from gh; None when gh fails (state is then left as it was)."""
    out = _gh_stdout(["pr", "view", url, "--json", "state", "-q", ".state"], None, f"gh pr view {url}")
    return None if out is None else out.strip().lower() or None


PEOPLE = ("OWNER", "MEMBER", "COLLABORATOR")  # not bots like vercel, which comment on every PR
MAX_REVIEW = 1500


def review_text(data: dict) -> str:
    bodies = [x.get("body", "").strip() for x in (data.get("reviews") or []) + (data.get("comments") or [])
              if x.get("authorAssociation") in PEOPLE]
    return "\n".join(b for b in bodies if b)[:MAX_REVIEW]


def pr_feedback(url: str) -> tuple[str, str] | None:
    """(state, review comments of people) of a PR; None when gh fails."""
    out = _gh_stdout(["pr", "view", url, "--json", "state,reviews,comments"], None, f"gh pr view {url}")
    if out is None:
        return None
    data = json.loads(out or "{}")
    return str(data.get("state", "")).lower(), review_text(data)


def open_prs(repo_dir) -> list[dict] | None:
    """Open PRs of the repo checked out in repo_dir; None when gh fails."""
    out = _gh_stdout(["pr", "list", "--state", "open", "--json",
                      "number,title,url,isDraft,createdAt,mergeable,statusCheckRollup,"
                      "headRefName,baseRefName,headRefOid"], repo_dir, f"gh pr list in {repo_dir}")
    if out is None:
        return None
    return [pr | {"ci": ci_state(pr.pop("statusCheckRollup", None) or [])} for pr in json.loads(out or "[]")]


def recent_prs(repo_dir, limit: int = 40) -> list[dict] | None:
    """Latest PRs of any state (number, title, state); None when gh fails."""
    out = _gh_stdout(["pr", "list", "--state", "all", "--limit", str(limit), "--json", "number,title,state"],
                     repo_dir, f"gh pr list --state all in {repo_dir}")
    return None if out is None else json.loads(out or "[]")


def merged_since(repo_dir, ts: float) -> int | None:
    """Number of PRs merged into the repo after ts; None when gh fails."""
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    out = _gh_stdout(["pr", "list", "--state", "merged", "--search", f"merged:>{since}",
                      "--limit", "1000", "--json", "number"], repo_dir, f"gh pr list --state merged in {repo_dir}")
    return None if out is None else len(json.loads(out or "[]"))


def _gh(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=GH_TIMEOUT)


def _gh_stdout(args: list[str], cwd, label: str) -> str | None:
    """stdout of gh; None (reported on stderr) when gh is missing, hangs or fails."""
    try:
        out = _gh(args, cwd)
    except (OSError, subprocess.TimeoutExpired) as e:  # gh missing or hanging must not block a run start
        print(f"{label}: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    if out.returncode != 0:
        print(f"{label}: {out.stderr.strip()}", file=sys.stderr)
        return None
    return out.stdout


def ci_state(checks: list[dict]) -> str | None:
    """gh statusCheckRollup → "fail" | "pending" | "ok"; None without checks."""
    values = {c.get("conclusion") or c.get("state") or "PENDING" for c in checks}
    if values & {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}:
        return "fail"
    if values - {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        return "pending"
    return "ok" if checks else None


def _must(out: subprocess.CompletedProcess) -> str:
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or out.stdout.strip() or f"exit {out.returncode}")
    return out.stdout


def _drop_branch(repo_dir, branch: str) -> None:
    """After a merge: remove the harness worktree of the branch, the local and the remote branch. Best effort."""
    listing = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo_dir, capture_output=True,
                             text=True, encoding="utf-8").stdout
    paths = [b.splitlines()[0].removeprefix("worktree ") for b in listing.split("\n\n")
             if f"branch refs/heads/{branch}" in b.splitlines()]
    steps = [["worktree", "remove", "--force", p] for p in paths if ".harness-worktrees" in Path(p).parts]
    for args in steps + [["branch", "-D", branch], ["push", "-q", "origin", "--delete", branch]]:
        out = subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True, encoding="utf-8")
        if out.returncode != 0:
            print(f"git {' '.join(args)} in {repo_dir}: {out.stderr.strip()}", file=sys.stderr)


def git_run(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")


@contextmanager
def pr_worktree(repo_dir, branch: str, prefix: str, fetch: tuple[str, ...], run: Callable[[list[str], object], object]):
    """Throwaway detached worktree of origin/<branch>, removed on exit. `run` decides how git is invoked and which
    error it raises."""
    run(["fetch", "-q", "origin", *fetch], repo_dir)
    wt = Path(tempfile.mkdtemp(prefix=prefix))
    run(["worktree", "add", "-q", "--detach", str(wt), f"origin/{branch}"], repo_dir)
    try:
        yield wt
    finally:
        git_run(["worktree", "remove", "--force", str(wt)], repo_dir)


def union_changelog(wt: Path) -> None:
    """Resolve a CHANGELOG.md conflict by keeping the lines of both sides (each PR appends its own entry)."""
    stages = {}
    for n in (1, 2, 3):
        out = subprocess.run(["git", "show", f":{n}:{CHANGELOG}"], cwd=wt, capture_output=True)
        stages[n] = out.stdout if out.returncode == 0 else b""  # no base stage when both sides added the file
    with tempfile.TemporaryDirectory() as d:
        base, theirs = Path(d, "base"), Path(d, "theirs")
        base.write_bytes(stages[1])
        theirs.write_bytes(stages[3])
        (wt / CHANGELOG).write_bytes(stages[2])
        git_run(["merge-file", "--union", CHANGELOG, str(base), str(theirs)], wt)
    _must(git_run(["add", CHANGELOG], wt))


def _unmerged(wt: Path) -> list[str]:
    return git_run(["diff", "--name-only", "--diff-filter=U"], wt).stdout.split()


def settle_changelog(wt: Path) -> list[str]:
    """Union-merge a conflicted CHANGELOG.md and return the other conflicting files."""
    conflicts = _unmerged(wt)
    if CHANGELOG in conflicts:
        union_changelog(wt)
    return [c for c in conflicts if c != CHANGELOG]


class ContentConflict(RuntimeError):
    """A rebase hit a conflict outside CHANGELOG.md: an agent or a fresh implementation has to settle it."""

    def __init__(self, files: list[str]):
        super().__init__(f"konflikt mimo {CHANGELOG}: {', '.join(files)}")
        self.files = files


def _rebase_keeping_changelogs(wt: Path, onto: str) -> None:
    out = git_run(["rebase", onto], wt)
    while out.returncode != 0:
        had_changelog = CHANGELOG in _unmerged(wt)
        others = settle_changelog(wt)
        if others or not had_changelog:
            git_run(["rebase", "--abort"], wt)
            if not others:
                raise RuntimeError(f"rebase selhal: {out.stderr.strip()}")
            raise ContentConflict(others)
        out = git_run(["-c", "core.editor=true", "rebase", "--continue"], wt)


def changelog_fixable(pr: dict) -> bool:
    """A conflicting harness PR: worth a rebase that keeps both CHANGELOG entries (user branches are left alone)."""
    return pr.get("mergeable") == "CONFLICTING" and str(pr.get("headRefName", "")).startswith("harness/")


def resolve_changelog_conflict(repo_dir, branch: str, base: str) -> None:
    """Rebase a PR branch onto origin/base in a throwaway worktree and force-push it; a conflict only in
    CHANGELOG.md keeps both entries, any other conflict raises RuntimeError and pushes nothing."""
    with pr_worktree(repo_dir, branch, "harness-rebase-", (base, branch), lambda a, c: _must(git_run(a, c))) as wt:
        _rebase_keeping_changelogs(wt, f"origin/{base}")
        _must(git_run(["push", "-q", f"--force-with-lease={branch}:origin/{branch}", "origin", f"HEAD:{branch}"], wt))


def requeue_pr(repo_dir, number: int, branch: str) -> None:
    """Close a harness PR that conflicts with main and drop its branch, so the task can be implemented anew."""
    _must(_gh(["pr", "close", str(number), "--comment",
               "Konflikt s main mimo CHANGELOG: úkol se znovu implementuje nad aktuálním main."], repo_dir))
    _drop_branch(repo_dir, branch)


def _wait_mergeable(repo_dir, number: int) -> None:
    """GitHub recomputes mergeability after a push; merging before that fails."""
    for _ in range(MERGEABLE_TRIES):
        time.sleep(MERGEABLE_WAIT)
        state = json.loads(_must(_gh(["pr", "view", str(number), "--json", "mergeable"], repo_dir)))["mergeable"]
        if state != "UNKNOWN":
            return


def merge_pr(repo_dir, number: int) -> str:
    """Squash-merge an open PR (a draft is marked ready first). A harness PR in conflict only in CHANGELOG.md is
    rebased first. RuntimeError carries gh's message."""
    pr = json.loads(_must(_gh(["pr", "view", str(number), "--json",
                               "state,isDraft,headRefName,baseRefName,mergeable"], repo_dir)))
    if pr["state"] != "OPEN":
        raise RuntimeError(f"PR #{number} is {pr['state'].lower()}")
    if changelog_fixable(pr):
        resolve_changelog_conflict(repo_dir, pr["headRefName"], pr["baseRefName"])
        _wait_mergeable(repo_dir, number)
    if pr["isDraft"]:
        _must(_gh(["pr", "ready", str(number)], repo_dir))
    _must(_gh(["pr", "merge", str(number), "--squash"], repo_dir))
    _drop_branch(repo_dir, pr["headRefName"])
    return f"PR #{number} zmergován (squash)."


def _upsert(conn, task_id: str, project: str, url: str | None, state: str) -> None:
    with _lock:
        conn.execute("INSERT INTO task_outcomes VALUES (?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                     "project = excluded.project, pr_url = excluded.pr_url, state = excluded.state, "
                     "checked = excluded.checked", (task_id, project, url, state, time.time()))
        conn.commit()


def sync_outcomes(conn, backlog_text: str, state_of: Callable[[str], str | None] = pr_state) -> None:
    urls, known = pr_urls(backlog_text), outcomes(conn)
    for t in backlog.parse(backlog_text):
        if t.status in LOCAL_STATES:
            _upsert(conn, t.id, t.project, None, LOCAL_STATES[t.status])
        elif t.id in urls and known.get(t.id, (None, None))[1] in FINAL_STATES:
            continue  # merged / closed never change; asking gh again only slows every run start
        elif t.id in urls and (state := state_of(urls[t.id])):
            _upsert(conn, t.id, t.project, urls[t.id], state)


def outcomes(conn) -> dict[str, tuple]:
    return {r["task_id"]: (r["project"], r["state"], r["pr_url"]) for r in _rows(conn, "SELECT * FROM task_outcomes")}

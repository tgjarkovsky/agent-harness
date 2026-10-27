import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .. import lessons, supervisor, traces
from ..backlog import Task
from ..guard import check_diff, new_failures
from ..priority import captures_about
from ..projects import PLAN_FILE, Config, Project
from ..sdk import NO_PUBLISH, READ_TOOLS, WRITE_TOOLS, check_billing, render_prompt, run_agent


def _kill_tree(proc: subprocess.Popen) -> None:
    # subprocess timeout kills only cmd.exe; grandchildren (node/vitest) keep the pipes open forever
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def sh(cmd: str, cwd, timeout: int = 900) -> tuple[bool, str]:
    proc = subprocess.Popen(cmd, cwd=cwd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, _ = proc.communicate()
        return False, f"{out}\n[harness] timeout after {timeout}s: {cmd}"
    return proc.returncode == 0, out


def git(args: list[str], cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


def worktree_path(project: Project, task_id: str) -> Path:
    return project.path.parent / ".harness-worktrees" / project.name / task_id


def ensure_worktree(project: Project, task_id: str, branch: str) -> Path:
    path = worktree_path(project, task_id)
    if not path.exists():
        git(["fetch", "-q", "origin", project.base], project.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        git(["worktree", "add", "-q", "-B", branch, str(path), f"origin/{project.base}"], project.path)
    return path


def commit_if_dirty(worktree, message: str) -> bool:
    git(["add", "-A"], worktree)
    if not git(["diff", "--cached", "--name-only"], worktree):
        return False
    git(["commit", "-q", "-m", message], worktree)
    return True


def staged_changes(worktree) -> tuple[list[str], int]:
    """Stage everything; return changed paths (both sides of moves, unquoted) and diff line count."""
    git(["add", "-A"], worktree)
    names = git(["diff", "--cached", "--no-renames", "-z", "--name-only"], worktree)
    numstat = git(["diff", "--cached", "--no-renames", "--numstat"], worktree)
    return [p for p in names.split("\0") if p], numstat_lines(numstat)


def numstat_lines(text: str) -> int:
    total = 0
    for line in text.splitlines():
        added, deleted, _ = line.split("\t", 2)
        if added != "-":
            total += int(added) + int(deleted)
    return total


def _failed(msg: str) -> dict:
    return {"outcome": "failed", "last_failure": msg}


def _agent_out(node: str, r) -> dict:
    return {"usage": [{"node": node, **r.usage}], "quota": r.quota, "rate_limit": r.rate_limit}


def _pr_body(s: dict) -> str:
    usage = s.get("usage") or []
    tokens = sum((u.get("input_tokens") or 0) + (u.get("output_tokens") or 0) for u in usage)
    baseline = ", ".join(s.get("baseline_failures") or []) or "none"
    return (f"Automated by harness run `{s['run_id']}`.\n\n## Plan\n\n{s.get('plan', '')}\n\n"
            f"## Verify\n\nPassed after {s.get('attempts', 0)} attempt(s). Commands failing on base: {baseline}.\n\n"
            f"Tokens (whole run so far): {tokens}")


def lessons_block(project: str) -> str:
    return lessons.block(lessons.active_texts(lessons.current_store(), project))


def parse_accept(plan: str) -> str:
    lines = [ln.strip() for ln in plan.splitlines() if ln.strip().startswith("ACCEPT:")]
    return lines[-1].removeprefix("ACCEPT:").strip().strip("`").strip() if lines else ""


def replan_block(s: dict) -> str:
    if not s.get("replanned"):
        return ""
    return (f"\nA previous plan for this task failed verification. Find the root cause and plan differently.\n"
            f"Previous plan:\n{s.get('plan', '')}\n\nFailure:\n```\n{s.get('last_failure', '')}\n```\n")


def clarification_block(answers: list[str]) -> str:
    if not answers:
        return ""
    return "\nThe owner answered your earlier questions:\n" + "\n".join(f"- {a}" for a in answers) + "\n"


DISCOVER_WT = "_discover"
RANK_FIELDS = ("id", "project", "title", "kind", "why", "done", "source", "priority")


def _fresh_discover_worktree(p: Project) -> Path:
    """Read-only checkout of origin/<base>; reused between runs, so reset it to the latest origin every time."""
    wt = ensure_worktree(p, DISCOVER_WT, f"harness/{DISCOVER_WT}")
    git(["fetch", "-q", "origin", p.base], p.path)
    git(["reset", "-q", "--hard", f"origin/{p.base}"], wt)
    return wt


MARKER_LINE = re.compile(r"^(<{7}|>{7})( |$)", re.M)


def _merge_base_in(wt: Path, base: str) -> list[str]:
    """Merge origin/base into the checked-out branch; CHANGELOG.md keeps both entries. Returns the files still in conflict."""
    if traces.git_run(["merge", "-q", "--no-edit", f"origin/{base}"], wt).returncode == 0:
        return []
    return traces.settle_changelog(wt)


@contextmanager
def _pr_worktree(p: Project, branch: str, prefix: str, fetch: tuple[str, ...]):
    """Throwaway detached worktree of origin/<branch>, removed on exit."""
    git(["fetch", "-q", "origin", *fetch], p.path)
    wt = Path(tempfile.mkdtemp(prefix=prefix))
    git(["worktree", "add", "-q", "--detach", str(wt), f"origin/{branch}"], p.path)
    try:
        yield wt
    finally:
        traces.git_run(["worktree", "remove", "--force", str(wt)], p.path)


def _verify_or_raise(p: Project, wt: Path, after: str) -> None:
    for cmd in (*p.setup, *p.verify):
        ok, out = sh(cmd, wt)
        if not ok:
            raise RuntimeError(f"`{cmd}` {after} selhal:\n{out[-1500:]}")


def resolve_conflict_with_agent(p: Project, branch: str, billing: str) -> str:
    """Merge origin/base into a conflicting PR branch in a throwaway worktree, let an agent settle the files beyond
    CHANGELOG.md and push only when setup + verify pass. RuntimeError says why nothing was pushed."""
    if err := check_billing(billing, os.environ):
        raise RuntimeError(err)
    with _pr_worktree(p, branch, "harness-conflict-", (p.base, branch)) as wt:
        files = _merge_base_in(wt, p.base)
        if files:
            run_agent(render_prompt("resolve_conflict.md", base=p.base, files="\n".join(files)), cwd=wt,
                      model="sonnet", tools=WRITE_TOOLS, disallowed=NO_PUBLISH, max_turns=60)
            if left := [f for f in files if MARKER_LINE.search((wt / f).read_text(encoding="utf-8", errors="replace"))]:
                raise RuntimeError(f"agent nechal konfliktní značky v: {', '.join(left)}")
        _verify_or_raise(p, wt, "po vyřešení konfliktu")
        git(["add", "-A"], wt)
        merging = traces.git_run(["rev-parse", "-q", "--verify", "MERGE_HEAD"], wt).returncode == 0
        if merging or traces.git_run(["diff", "--cached", "--quiet"], wt).returncode != 0:
            git(["commit", "-q", "--no-edit"], wt)
        git(["push", "-q", "origin", f"HEAD:{branch}"], wt)
        return f"Konflikt vyřešen ({', '.join(files) or traces.CHANGELOG}), verify prošlo, změny pushnuté."


def failed_ci_log(p: Project, branch: str) -> str:
    """Tail of the failed jobs' log of the branch's latest workflow run; empty when gh can't tell."""
    run = traces._gh_stdout(["run", "list", "--branch", branch, "--limit", "1", "--json", "databaseId", "-q",
                             ".[0].databaseId"], p.path, f"gh run list {branch}")
    if not (run or "").strip():
        return ""
    log = traces._gh_stdout(["run", "view", run.strip(), "--log-failed"], p.path, f"gh run view {run.strip()}")
    return (log or "")[-4000:]


def fix_ci_with_agent(p: Project, branch: str, billing: str) -> str:
    """Let an agent fix the failing CI of a PR branch in a throwaway worktree; push only when setup + verify pass
    and the agent changed something. RuntimeError says why nothing was pushed."""
    if err := check_billing(billing, os.environ):
        raise RuntimeError(err)
    with _pr_worktree(p, branch, "harness-ci-", (branch,)) as wt:
        r = run_agent(render_prompt("fix_ci.md", log=failed_ci_log(p, branch) or "(log unavailable, run the tests)"),
                      cwd=wt, model="sonnet", tools=WRITE_TOOLS, disallowed=NO_PUBLISH, max_turns=60)
        git(["add", "-A"], wt)
        if traces.git_run(["diff", "--cached", "--quiet"], wt).returncode == 0:
            raise RuntimeError(f"agent nic nezměnil: {r.text[-300:]}")
        _verify_or_raise(p, wt, "po opravě CI")
        git(["commit", "-q", "-m", "fix: failing CI"], wt)
        git(["push", "-q", "origin", f"HEAD:{branch}"], wt)
        return f"CI opraveno, verify prošlo, změny pushnuté. {r.text[-300:]}"


def recent_block(p: Project, wt: Path, commits: int = 30) -> str:
    """Recent PRs (any state) and base commits, so the scout does not propose work that already exists."""
    prs = traces.recent_prs(wt) or []
    lines = [f"  #{x['number']} [{x['state'].lower()}] {x['title']}" for x in prs]
    log = traces.git_run(["log", f"origin/{p.base}", f"-{commits}", "--format=%s"], wt)
    if log.returncode == 0:
        lines += [f"  {s}" for s in log.stdout.splitlines() if s.strip()]
    return "\n".join(lines) or "  (none)"


def real_explore(cfg: Config) -> Callable[[str, list[Task], str], tuple[str, dict]]:
    def explore(name: str, known: list[Task], lens: str) -> tuple[str, dict]:
        p = cfg.projects[name]
        wt = _fresh_discover_worktree(p)
        notes = captures_about(cfg.secondbrain[0], name, cfg.secondbrain[1]) if cfg.secondbrain else ""
        prompt = render_prompt(f"discover_{lens}.md", project=name, max_diff_lines=p.max_diff_lines,
                               verify="\n".join(f"  {c}" for c in p.verify),
                               known="\n".join(f"  [{t.status}] {t.title}" for t in known) or "  (none)",
                               notes=notes or "(none)", lessons=lessons_block(name),
                               recent=recent_block(p, wt), instructions=p.idle.prompt if p.idle else "",
                               plan=read_plan(wt) or "(none)")
        r = run_agent(prompt, cwd=wt, model="opus", tools=READ_TOOLS, permission_mode="dontAsk", max_turns=40)
        return r.text, _agent_out("discover", r)
    return explore


def read_plan(root: Path) -> str:
    plan = root / PLAN_FILE
    return plan.read_text(encoding="utf-8", errors="replace").strip() if plan.exists() else ""


def real_merge(cfg: Config) -> Callable[[str, list[Task]], str]:
    def merge(name: str, proposals: list[Task]) -> str:
        rows = "\n".join(json.dumps({"id": t.id, "kind": t.kind, "title": t.title, "description": t.description},
                                    ensure_ascii=False) for t in proposals)
        prompt = render_prompt("merge_proposals.md", project=name, proposals=rows,
                               max_diff_lines=cfg.projects[name].max_diff_lines)
        return run_agent(prompt, cwd=tempfile.gettempdir(), model="sonnet", tools=[], permission_mode="dontAsk",
                         max_turns=1).text
    return merge


def real_rank(cands: list[dict], n: int, focus: dict, weights: dict,
              history_of: Callable[[], dict] | None = None) -> tuple[str, dict]:
    rows = "\n".join(json.dumps({k: c[k] for k in RANK_FIELDS}, ensure_ascii=False) for c in cands)
    history = json.dumps(history_of() if history_of else {}, ensure_ascii=False)
    prompt = render_prompt("prioritize.md", n=n, candidates=rows, history=history,
                           focus=json.dumps(focus, ensure_ascii=False), weights=json.dumps(weights, ensure_ascii=False))
    r = run_agent(prompt, cwd=tempfile.gettempdir(), model="opus", tools=[], permission_mode="dontAsk", max_turns=1)
    return r.text, _agent_out("prioritize", r)


def real_reflect(items: list[dict], known: list[dict]) -> tuple[str, dict]:
    prompt = render_prompt("reflect.md", feedback="\n".join(json.dumps(i, ensure_ascii=False) for i in items),
                           known="\n".join(json.dumps(k, ensure_ascii=False) for k in known) or "(none)")
    r = run_agent(prompt, cwd=tempfile.gettempdir(), model="opus", tools=[], permission_mode="dontAsk", max_turns=1)
    return r.text, _agent_out("reflect", r)


def real_nodes(cfg: Config) -> dict:
    def proj(s) -> Project:
        return cfg.projects[s["task"]["project"]]

    def preflight(s):
        if err := check_billing(cfg.billing, os.environ):
            return _failed(err)
        ok, out = sh("gh auth status", proj(s).path)
        if not ok:
            return _failed(f"gh not authenticated: {out[-300:]}")
        branch = f"harness/{s['task']['id']}"
        if git(["ls-remote", "--heads", "origin", branch], proj(s).path):
            return _failed(f"branch {branch} already exists on origin")
        return {"branch": branch}

    def make_worktree(s):
        return {"worktree": str(ensure_worktree(proj(s), s["task"]["id"], s["branch"]))}

    def plan(s):
        t = s["task"]
        if not s.get("replanned") and (accept := parse_accept(t["description"])):
            return {"plan": t["description"], "accept": accept}  # discover already explored: its description is the plan
        prompt = render_prompt("plan.md", title=t["title"], description=t["description"],
                               clarifications=clarification_block(s.get("clarifications") or []),
                               replan=replan_block(s), lessons=lessons_block(t["project"]))
        # shell hidden: in dontAsk mode the planner kept trying it and getting denied
        r = run_agent(prompt, cwd=s["worktree"], model=cfg.plan_model, tools=READ_TOOLS, disallowed=["Bash", "PowerShell"],
                      permission_mode="dontAsk", max_turns=30)
        out = _agent_out("plan", r)
        first = r.text.strip().splitlines()[0] if r.text.strip() else ""
        if first.startswith("UNCLEAR:"):
            return out | {"outcome": "needs_clarification", "last_failure": first.removeprefix("UNCLEAR:").strip()}
        return out | {"plan": r.text, "accept": parse_accept(r.text)}

    def setup(s):
        p, wt = proj(s), s["worktree"]
        for cmd in p.setup:
            ok, out = sh(cmd, wt)
            if not ok:
                return _failed(f"setup `{cmd}` failed: {out[-1500:]}")
        return {"baseline_failures": [c for c in p.verify if not sh(c, wt)[0]]}

    def implement(s):
        t = s["task"]
        failure = (f"The previous attempt failed verification. Fix this:\n```\n{s['last_failure']}\n```"
                   if s.get("last_failure") else "")
        prompt = render_prompt("implement.md", title=t["title"], description=t["description"],
                               plan=s.get("plan", ""), failure=failure, lessons=lessons_block(t["project"]))
        watch = supervisor.Watch(t, s.get("plan", ""), supervisor.jev_judge, s["worktree"])
        r = run_agent(prompt, cwd=s["worktree"], model="sonnet", tools=WRITE_TOOLS, disallowed=NO_PUBLISH, max_turns=80,
                      supervise=watch.hooks)
        return _agent_out("implement", r) | {"supervisor_stop": watch.stop}

    def verify(s):
        p, wt = proj(s), s["worktree"]
        accept = [s["accept"]] if s.get("accept") else []
        results = [(cmd, *sh(cmd, wt)) for cmd in [*p.verify, *accept]]
        # baseline covers p.verify only, so it never excuses the acceptance command
        new = new_failures(s.get("baseline_failures") or [], [c for c, ok, _ in results if not ok])
        changed, lines = staged_changes(wt)
        failures = [f"$ {c}\n{out[-1500:]}" for c, ok, out in results if c in new]
        if violation := check_diff(changed, lines, p.allowed, p.max_diff_lines):
            failures.insert(0, f"guard: {violation}")
        return {"verify_ok": not failures, "last_failure": "\n\n".join(failures)[-4000:],
                "attempts": s.get("attempts", 0) + 1}

    def open_pr(s):
        p, wt, t = proj(s), s["worktree"], s["task"]
        commit_if_dirty(wt, f"harness: {t['id']} {t['title']}")
        git(["push", "-q", "-u", "origin", s["branch"]], wt)
        existing = subprocess.run(["gh", "pr", "view", s["branch"], "--json", "url", "-q", ".url"],
                                  cwd=wt, capture_output=True, text=True, encoding="utf-8").stdout.strip()
        if existing.startswith("http"):
            return {"outcome": "pr_opened", "pr_url": existing}
        # body via file: Windows caps a command line at 32k chars and a long plan would exceed it
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text(_pr_body(s), encoding="utf-8")
            out = subprocess.run(["gh", "pr", "create", "--draft", "--base", p.base, "--head", s["branch"],
                                  "--title", f"[harness] {t['title']}", "--body-file", str(body)],
                                 cwd=wt, check=True, capture_output=True, text=True, encoding="utf-8").stdout
        return {"outcome": "pr_opened", "pr_url": out.strip().splitlines()[-1]}

    return {"preflight": preflight, "make_worktree": make_worktree, "plan": plan, "setup": setup,
            "implement": implement, "verify": verify, "open_pr": open_pr}

import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from .. import backlog, serve, traces
from ..projects import load_config

PR_CACHE_SECONDS = 300


class NoConflict(LookupError):
    pass


class Busy(RuntimeError):
    pass


def make_pr_care(home: Path, control: serve.Ctx | None, prs, merged, resolve, fix, fix_ci, requeue) -> SimpleNamespace:
    """Background care of open PRs (rebase, red CI, conflicts) with the state it shares."""
    pr_cache: dict[Path, tuple[float, list | None]] = {}
    merged_cache: dict[tuple, tuple[float, int | None]] = {}
    tried: set[tuple] = set()  # (repo, PR, head commit): a failed rebase is not retried until the branch moves
    resolve_lock = threading.Lock()
    conflicts: dict[tuple, dict] = {}  # (repo, PR) → files a rebase could not settle and what the owner chose

    def cached_prs(path: Path) -> list | None:
        hit = pr_cache.get(path)
        if hit is None or time.time() - hit[0] > PR_CACHE_SECONDS:
            hit = pr_cache[path] = (time.time(), prs(path) if path.exists() else None)
            resolve_conflicts(path, hit[1] or [])
        return hit[1]

    def cleanup_of(path: Path, ts: float | None) -> dict | None:
        """Last cleanup review of a project and how many PRs merged since; None = never reviewed."""
        if ts is None:
            return None
        hit = merged_cache.get((path, ts))
        if hit is None or time.time() - hit[0] > PR_CACHE_SECONDS:
            hit = merged_cache[(path, ts)] = (time.time(), merged(path, ts) if path.exists() else None)
        return {"ts": ts, "prs": hit[1]}

    def resolve_conflicts(path: Path, pulls: list) -> None:
        """Every harness PR appends to CHANGELOG.md, so the others conflict whenever one merges (from here or
        GitHub); rebase them in the background so the conflict tag clears without a merge click."""
        for pr in pulls:
            key = (path, pr["number"], pr.get("headRefOid"))
            if traces.changelog_fixable(pr) and key not in tried:
                tried.add(key)
                threading.Thread(target=resolve_one, args=(path, pr), daemon=True).start()
            elif ci_fixable(pr) and ("ci", *key) not in tried:
                tried.add(("ci", *key))
                threading.Thread(target=fix_ci_one, args=(path, pr), daemon=True).start()

    def ci_fixable(pr: dict) -> bool:
        return pr.get("ci") == "fail" and pr.get("mergeable") != "CONFLICTING" \
            and str(pr.get("headRefName", "")).startswith("harness/")

    def fix_ci_one(path: Path, pr: dict) -> None:
        """Red CI on a harness PR: an agent fixes it once per head commit; the owner hears how it went."""
        cfg = load_config(home / "projects.yaml")
        project = next((p for p in cfg.projects.values() if p.path == path), None)
        if project is None:
            return
        with resolve_lock:  # shares the repo's fetch and worktree list with the rebases
            try:
                note = f"🔧 PR #{pr['number']} ({project.name}): {fix_ci(project, pr['headRefName'], cfg.billing)}"
            except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
                detail = getattr(e, "stderr", None) or str(e)
                note = f"🔴 PR #{pr['number']} ({project.name}): CI se nepodařilo opravit: {detail[-500:]}"
        pr_cache.pop(path, None)
        if control is not None and control.state["dev"]:  # dev off: the PR list in the dashboard shows CI
            serve.say(control, note)

    def resolve_one(path: Path, pr: dict) -> None:
        with resolve_lock:  # one rebase per serve at a time: they share the repo's fetch and worktree list
            try:
                resolve(path, pr["headRefName"], pr["baseRefName"])
            except traces.ContentConflict as e:
                conflicts[(path, pr["number"])] = {"files": e.files, "state": "open"}
                return
            except (RuntimeError, OSError) as e:
                print(f"PR #{pr['number']} in {path}: {e}", file=sys.stderr)
                return
        pr_cache.pop(path, None)

    def fix_one(project, pr: dict, billing: str) -> None:
        key = (project.path, pr["number"])
        try:
            fix(project, pr["headRefName"], billing)
        except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
            detail = getattr(e, "stderr", None) or str(e)
            conflicts[key] |= {"state": "failed", "error": detail[-2000:]}
            return
        conflicts.pop(key, None)
        pr_cache.pop(project.path, None)

    def reimplement(path: Path, pr: dict) -> str:
        """Close the PR and put its task back to [ ] for a fresh run on the current base."""
        task_id = pr["headRefName"].removeprefix("harness/")
        bl = home / "BACKLOG.md"
        if task_id not in {t.id for t in backlog.parse(backlog.read(bl))}:
            raise ValueError(f"úkol {task_id} není v BACKLOG.md")
        requeue(path, pr["number"], pr["headRefName"])
        backlog.edit(bl, lambda text: backlog.set_status(text, task_id, " "))
        conflicts.pop((path, pr["number"]), None)
        pr_cache.pop(path, None)
        return f"PR #{pr['number']} zavřen, úkol {task_id} je zpět ve frontě."

    def with_conflicts(path: Path, pulls: list | None) -> list | None:
        if pulls is None:
            return None
        return [pr | {"conflict": conflicts.get((path, pr["number"]))} if pr.get("mergeable") == "CONFLICTING" else pr
                for pr in pulls]

    def settle(project, billing: str, number: int, action: str) -> str:
        """The owner's answer to a conflict the background rebase could not settle.
        Raises NoConflict, Busy, or ValueError (bad action, or reimplement impossible)."""
        entry = conflicts.get((project.path, number))
        pr = next((p for p in cached_prs(project.path) or [] if p["number"] == number), None)
        if entry is None or pr is None:
            raise NoConflict(f"PR #{number} nemá nevyřešený konflikt")
        if entry["state"] == "resolving":
            raise Busy("agent už konflikt řeší")
        if action == "keep":
            entry["state"] = "kept"
            return f"PR #{number} nechán s konfliktem."
        if action == "agent":
            entry |= {"state": "resolving", "error": ""}
            threading.Thread(target=fix_one, args=(project, pr, billing), daemon=True).start()
            return f"Agent řeší konflikt v PR #{number}…"
        if action == "reimplement":
            return reimplement(project.path, pr)
        raise ValueError(f"unknown action {action!r}")

    def forget(path: Path) -> None:
        pr_cache.pop(path, None)

    return SimpleNamespace(cached_prs=cached_prs, cleanup_of=cleanup_of, with_conflicts=with_conflicts,
                           settle=settle, reimplement=reimplement, forget=forget)

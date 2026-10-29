from dataclasses import asdict
from pathlib import Path
from typing import Callable

from langgraph.types import interrupt

from .. import backlog, lessons
from ..backlog import Task
from ..discover import DEFAULT_LENSES, apply_approval, candidates, fallback_plan, json_array, parse_proposals, parse_ranking
from ..graph import RateLimited
from ..priority import config_focus, config_weights, pick_batch, rank_by_config
from ..projects import Config

# parallel workers report into the same BACKLOG.md (read-modify-write); re-exported for existing importers
edit_backlog = backlog.edit


def make_pick_tasks(backlog_path: Path, cfg: Config):
    def pick_tasks(state: dict) -> dict:
        left = state.get("max_tasks", cfg.max_tasks_per_run) - state.get("tasks_done", 0)
        if left <= 0 or (state.get("quota") or 0) >= state.get("quota_threshold", cfg.quota_threshold):
            return {"batch": []}
        with backlog.LOCK:
            tasks = backlog.parse(backlog.read(backlog_path))
        planned = state.get("planned")
        if planned is None:
            ranked = rank_by_config(tasks, cfg, state.get("only"))
        else:
            order = {tid: i for i, tid in enumerate(planned)}
            ranked = sorted((t for t in tasks if t.status == " " and t.id in order), key=lambda t: order[t.id])
        batch = pick_batch(ranked, min(left, state.get("max_parallel", cfg.max_parallel)))
        return {"batch": [{"task": asdict(t), "max_attempts": cfg.projects[t.project].max_attempts} for t in batch]}
    return pick_tasks


def _note(state: dict) -> tuple[str, str]:
    outcome = state.get("outcome") or "failed"
    detail = (state.get("last_failure") or "").replace("\r", " ").replace("\n", " ")
    if outcome == "pr_opened":
        return "x", f" → {state['pr_url']}"
    if outcome == "needs_clarification":
        return "?", f" — UNCLEAR: {detail}"[:200]
    return "!", f" — {outcome}: {detail}"[:200]


def make_report(backlog_path: Path):
    def report(state: dict) -> dict:
        status, note = _note(state)
        edit_backlog(backlog_path, lambda text: backlog.set_status(text, state["task"]["id"], status, note))
        return {}
    return report


def _error(where: str, e: Exception) -> str:
    return f"{where}: {type(e).__name__}: {e}"


def _spent(agent: dict) -> dict:
    """Usage and quota of an agent call; quota feeds pick_tasks' reserve check before workers start."""
    return {"usage": agent.get("usage") or [], "quota": agent.get("quota")}


def make_discover(backlog_path: Path, explore: Callable[[str, list[Task], str], tuple[str, dict]]):
    """Proposals of one project (Send payload {"project", "lenses"}), one explore call per lens (fixes, features, cleanup).
    A failure costs only that lens of this project. `explore` returns the model's answer and {"usage", "quota"}."""
    def discover(state: dict) -> dict:
        name = state["project"]
        with backlog.LOCK:
            known = [t for t in backlog.parse(backlog.read(backlog_path)) if t.project == name]
        out: dict = {"proposals": [], "go_errors": [], "usage": [], "quota": None}
        for lens in state.get("lenses") or DEFAULT_LENSES:
            seen = known + _as_tasks(out["proposals"])
            try:
                text, agent = explore(name, seen, lens)
            except RateLimited:
                raise
            except Exception as e:
                out["go_errors"].append(_error(f"{name}/{lens}", e))
                continue
            if json_array(text) is None:
                out["go_errors"].append(f"{name}/{lens}: odpověď bez JSON pole")
            props = parse_proposals(text, name, seen, lens)
            spent = _spent(agent)
            out |= {"proposals": out["proposals"] + props, "usage": out["usage"] + spent["usage"],
                    "quota": spent["quota"] if spent["quota"] is not None else out["quota"]}
        return out
    return discover


def _as_tasks(proposals: list[dict]) -> list[Task]:
    """Proposals of an earlier lens as known tasks, so the next lens cannot repeat them."""
    return [Task(p["id"], p["project"], 2, p["title"], " ", "") for p in proposals]


def make_prioritize(backlog_path: Path, cfg: Config,
                    rank_fn: Callable[[list[dict], int, dict, dict], tuple[str, dict]]):
    weights = config_weights(cfg)

    def prioritize(state: dict) -> dict:
        with backlog.LOCK:
            tasks = backlog.parse(backlog.read(backlog_path))
        cands = candidates(state.get("proposals") or [], tasks, set(state.get("only") or cfg.projects))
        if not cands or state.get("auto"):
            return {"plan": []}
        n, focus = state.get("max_tasks", cfg.max_tasks_per_run), config_focus(cfg)
        try:
            text, agent = rank_fn(cands, n, focus, weights)
            errors = []
        except RateLimited:
            raise
        except Exception as e:
            text, agent, errors = "", {}, [_error("prioritize", e)]
        plan = parse_ranking(text, cands, n) or fallback_plan(cands, focus, weights, n)
        return {"plan": plan, "go_errors": errors} | _spent(agent)
    return prioritize


def make_approve(backlog_path: Path):
    """Owner approves the plan once (interrupt → list of kept indices or "q"); proposals land in BACKLOG."""
    def approve(state: dict) -> dict:
        plan, proposals = state.get("plan") or [], state.get("proposals") or []
        answer = interrupt({"plan": plan}) if plan else "q"
        kept = None if answer == "q" else [plan[i]["id"] for i in answer]
        edit_backlog(backlog_path, lambda text: apply_approval(text, proposals, plan, kept))
        return {"planned": kept or []}
    return approve


def make_reflect(backlog_path: Path, reflect_fn: Callable[[list[dict], list[dict]], tuple[str, dict]],
                 pr_info: Callable[[str], tuple[str, str] | None]):
    """End of every run: new owner feedback → proposed lessons (the owner approves them) and prompt changes
    as [~] proposals for the harness. Feedback counts as consumed only after a successful call, so a failed
    reflection retries next run. No new feedback → no model call."""
    def reflect(state: dict) -> dict:
        store = lessons.current_store()
        if store is None:
            return {}
        with backlog.LOCK:
            text = backlog.read(backlog_path)
        seen = {i.key for i in store.search(lessons.SEEN, limit=100_000)}
        items, quiet = lessons.gather_feedback(text, seen, pr_info)
        mark = {"run": state.get("run_id")}
        for key in quiet:
            store.put(lessons.SEEN, key, mark)
        if not items:
            return {}
        projects = {i["project"] for i in items} | {lessons.ALL}
        known = [{"project": x["project"], "text": x["text"], "status": x["status"]}
                 for p in sorted(projects) for x in lessons.lessons(store, p)]
        try:
            answer, agent = reflect_fn(items, known)
        except RateLimited:
            raise
        except Exception as e:
            return {"go_errors": [_error("reflect", e)]}
        found = lessons.parse_reflection(answer, items)
        for f in found:
            if f["scope"] == "project":
                lessons.propose(store, f["project"], f["text"], f["source"])
        if prompts := [lessons.harness_task(f) for f in found if f["scope"] == "prompt"]:
            edit_backlog(backlog_path, lambda t: backlog.add_new(t, [(pid, lessons.HARNESS, lines) for pid, lines in prompts]))
        for i in items:
            store.put(lessons.SEEN, i["key"], mark)
        return _spent(agent)
    return reflect

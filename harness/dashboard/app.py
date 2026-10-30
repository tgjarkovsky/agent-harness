import asyncio
import json
import subprocess
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .. import backlog, events, quota, serve, traces
from ..graph import graph_mermaid, worker_mermaid
from ..nodes.real import fix_ci_with_agent, read_plan, resolve_conflict_with_agent
from ..projects import PLAN_FILE, load_config
from ..sdk import render_prompt
from ..runner import execute, pending, questions
from ..simulate import Simulation, prepare
from .prcare import NoConflict, make_pr_care

STATIC = Path(__file__).parent / "static"
LOCAL_ORIGINS = ("http://127.0.0.1:", "http://localhost:")


class AnswerRequest(BaseModel):
    id: str
    answer: str


class MergeRequest(BaseModel):
    project: str
    number: int


class ConflictRequest(MergeRequest):
    action: str  # agent | reimplement | keep


class SimulateRequest(BaseModel):
    scenario: str
    step: bool = False
    delay: float | None = None


def progress(evs: list[dict]) -> dict:
    """Fold a run's events into node states (active/done/error): run-level stages, discover per project,
    and per worker its nodes in order of first visit, title, outcome and attempts."""
    stages: dict[str, str] = {}
    discover: dict[str, str] = {}
    workers: dict[str, dict] = {}
    titles: dict[str, str] = {}
    for e in evs:
        node, kind, delta = e["node"], e["kind"], e["delta"] or {}
        if kind not in ("start", "end", "error") or not node:
            continue
        state = {"start": "active", "end": "done", "error": "error"}[kind]
        if node == "pick_tasks" and kind == "end":
            titles |= {b["task"]["id"]: b["task"].get("title", "") for b in delta.get("batch") or []}
        if node == "discover":
            discover[e["worker"] or "?"] = state
        elif e["worker"] is None:
            stages[node] = state
        else:
            w = workers.setdefault(e["worker"], {"id": e["worker"], "nodes": {}, "outcome": None, "attempts": 0})
            w["nodes"][node] = state
            w["last"] = node
            w["outcome"] = delta.get("outcome", w["outcome"])
            w["attempts"] = delta.get("attempts", w["attempts"])
            w["pr_url"] = delta.get("pr_url", w.get("pr_url"))
    if discover:
        stages["discover"] = "active" if "active" in discover.values() else "done"
    if workers:
        stages["worker"] = "active" if any(w["nodes"].get("report") != "done" for w in workers.values()) else "done"
    return {"stages": stages, "discover": discover,
            "workers": [w | {"title": titles.get(w["id"], "")} for w in workers.values()]}


def require_local_origin(request: Request) -> None:
    """Control endpoints spend quota: refuse cross-site requests (a page on the internet posting to localhost)."""
    origin = request.headers.get("origin")
    if origin is not None and not origin.startswith(LOCAL_ORIGINS):
        raise HTTPException(403, "cross-origin request")


def create_app(home: Path, control: serve.Ctx | None = None, prs=traces.open_prs, merge=traces.merge_pr,
               live_quota=quota.fetch, resolve=traces.resolve_changelog_conflict,
               merged=traces.merged_since, requeue=traces.requeue_pr,
               fix=resolve_conflict_with_agent, fix_ci=fix_ci_with_agent) -> FastAPI:
    from ..cli import open_db

    app = FastAPI(title="harness")
    boot = uuid.uuid4().hex  # changes on every serve restart → open dashboards reload their page
    conn, saver = open_db(home)
    sims: dict[str, Simulation] = {}
    mermaid = {"mermaid": graph_mermaid(), "worker": worker_mermaid()}

    def scenarios() -> dict[str, Path]:
        return {p.stem: p for p in sorted((home / "scenarios").glob("*.yaml"))}

    care = make_pr_care(home, control, prs, merged, resolve, fix, fix_ci, requeue)
    app.state.prs = care.cached_prs  # serve's autorun reads PRs through here, so it also triggers the PR care

    def _project(name: str):
        cfg = load_config(home / "projects.yaml")
        project = cfg.projects.get(name)
        if project is None:
            raise HTTPException(404, f"unknown project {name!r}")
        return cfg, project

    def run_sim(sim: Simulation, inputs) -> None:
        with sim.lock:
            execute(sim.app, inputs, sim.config, conn, simulated=True)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/desktop")
    def desktop():
        return FileResponse(STATIC / "desktop.html")

    @app.get("/overview")
    def overview():
        cfg = load_config(home / "projects.yaml")  # every request: new projects show up without a restart
        path = home / "BACKLOG.md"
        tasks = backlog.parse(backlog.read(path))
        last = events.last_cleanups(conn)
        paths = [p.path for p in cfg.projects.values()]
        with ThreadPoolExecutor() as pool:
            pulls = dict(zip(cfg.projects, pool.map(care.cached_prs, paths)))
            cleanups = dict(zip(cfg.projects, pool.map(care.cleanup_of, paths, [last.get(n) for n in cfg.projects])))
        pulls = {name: care.with_conflicts(cfg.projects[name].path, pulls[name]) for name in cfg.projects}
        projects = [{"name": name, "prs": pulls[name], "cleanup": cleanups[name],
                     "plan": (cfg.projects[name].path / PLAN_FILE).exists(),
                     "counts": {s: sum(t.project == name and t.status == s for t in tasks) for s in " ~x?-"}}
                    for name in cfg.projects]
        proposals = [asdict(t) for t in tasks if t.status == "~"]
        return {"projects": projects, "proposals": proposals, "serve": control and serve.snapshot(control),
                "boot": boot, "limits": {"max_tasks": cfg.max_tasks_per_run, "max_parallel": cfg.max_parallel}}

    @app.post("/control/{action}", dependencies=[Depends(require_local_origin)])
    def control_action(action: str, body: dict | None = None):
        if control is None:
            raise HTTPException(503, "start `harness serve` to control runs")
        try:
            return {"message": serve.act(control, action, body or {})}
        except KeyError:
            raise HTTPException(404, f"unknown action {action!r}") from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.get("/projects/{name}/plan")
    def project_plan(name: str):
        text = read_plan(_project(name)[1].path)  # the local checkout: shows edits not pushed yet
        if not text:
            raise HTTPException(404, f"{name}: no {PLAN_FILE}")
        return {"text": text}

    @app.post("/projects/{name}/plan/session", dependencies=[Depends(require_local_origin)])
    def plan_session(name: str):
        path = _project(name)[1].path
        prompt = " ".join(render_prompt("plan_session.md").split())  # one line, no ";" (wt splits commands on it)
        try:
            subprocess.Popen(["wt", "-d", str(path), "claude", prompt])
        except OSError as e:
            raise HTTPException(500, f"Windows Terminal nejde spustit: {e}") from None
        return {"message": f"Otevírám Claude Code v {path.name}."}

    @app.post("/prs/conflict", dependencies=[Depends(require_local_origin)])
    def settle_conflict(req: ConflictRequest):
        """The owner's answer to a conflict the background rebase could not settle."""
        cfg, project = _project(req.project)
        try:
            return {"message": care.settle(project, cfg.billing, req.number, req.action)}
        except NoConflict as e:
            raise HTTPException(404, str(e)) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        except (RuntimeError, OSError) as e:
            raise HTTPException(409, str(e)) from None

    @app.post("/prs/merge", dependencies=[Depends(require_local_origin)])
    def merge_pr(req: MergeRequest):
        _, project = _project(req.project)
        try:
            message = merge(project.path, req.number)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            raise HTTPException(409, str(e)) from None
        finally:
            care.forget(project.path)
        return {"message": message}

    @app.get("/graph")
    def graph():
        return mermaid

    @app.get("/runs")
    def runs():
        return events.runs(conn)

    @app.get("/runs/{run_id}/events")
    def run_events(run_id: str, after: int = 0):
        return events.events_after(conn, after, run_id)

    @app.get("/runs/{run_id}/progress")
    def run_progress(run_id: str):
        return progress(events.events_after(conn, 0, run_id))

    @app.get("/runs/{run_id}/state")
    def run_state(run_id: str):
        sim = sims.get(run_id)
        if sim is None:
            raise HTTPException(404, "only simulations started from this dashboard have live state")
        running = sim.lock.locked()
        return {"next": pending(sim.app, sim.config), "running": running, "simulated": True,
                "questions": [] if running else questions(sim.app, sim.config)}

    @app.get("/events/stream")
    async def stream(after: int = 0):
        async def gen():
            last = after
            while True:
                for e in events.events_after(conn, last):
                    last = e["id"]
                    yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/backlog")
    def get_backlog(sim: str | None = None):
        path = sims[sim].backlog_path if sim in sims else home / "BACKLOG.md"
        return [asdict(t) for t in backlog.parse(backlog.read(path))]

    @app.get("/history")
    def get_history(day: str):
        try:
            text = serve.history(conn, day, backlog.read(home / "BACKLOG.md"))
        except ValueError:
            raise HTTPException(400, f"day must be YYYY-MM-DD, got {day!r}") from None
        return {"day": day, "text": text}

    @app.get("/runs/{run_id}/calls")
    def run_calls(run_id: str):
        return traces.calls(conn, run_id)

    @app.get("/calls/{call_id}/steps")
    def call_steps(call_id: int):
        return traces.steps(conn, call_id)

    @app.get("/costs")
    def costs():
        return traces.costs(conn) | {"summary": traces.summary(conn)}

    @app.get("/usage")
    def usage():
        live = live_quota() or {}
        return {"summary": events.usage_summary(conn),
                "rate_limit": live.get("five_hour") or events.latest_rate_limit(conn), "seven_day": live.get("seven_day")}

    @app.get("/scenarios")
    def list_scenarios():
        return [{"name": name, "title": (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("name", name)}
                for name, p in scenarios().items()]

    @app.post("/simulate")
    def simulate(req: SimulateRequest):
        path = scenarios().get(req.scenario)
        if path is None:
            raise HTTPException(404, f"unknown scenario {req.scenario!r}")
        sim = prepare(path, Path(tempfile.mkdtemp(prefix="harness-sim-")), saver, step=req.step, delay=req.delay)
        sims[sim.run_id] = sim
        threading.Thread(target=run_sim, args=(sim, sim.inputs), daemon=True).start()
        return {"run_id": sim.run_id}

    @app.post("/runs/{run_id}/step")
    def step(run_id: str):
        sim = sims.get(run_id)
        if sim is None:
            raise HTTPException(404, "unknown simulation")
        if sim.lock.locked():
            raise HTTPException(409, "step already running")
        threading.Thread(target=run_sim, args=(sim, None), daemon=True).start()
        return {"ok": True}

    @app.post("/runs/{run_id}/answer")
    def answer(run_id: str, req: AnswerRequest):
        sim = sims.get(run_id)
        if sim is None:
            raise HTTPException(404, "unknown simulation")
        # take the lock here, not in the thread: two quick answers must not both resume
        if not sim.lock.acquire(blocking=False):
            raise HTTPException(409, "run is busy")
        if req.id not in {q["id"] for q in questions(sim.app, sim.config)}:
            sim.lock.release()
            raise HTTPException(404, "unknown question")

        def resume() -> None:
            try:
                execute(sim.app, None, sim.config, conn, simulated=True, answers={req.id: req.answer})
            finally:
                sim.lock.release()
        threading.Thread(target=resume, args=(), daemon=True).start()
        return {"ok": True}

    return app

"""`harness serve`: the autonomous daemon, controlled from Telegram.

One run at a time, executed by `spawn` (a thread in production). The main loop long-polls Telegram and makes
every decision, so the daemon state is only touched from one thread; the run thread just hands back its result."""
import json
import sys
import threading
import time as _time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import backlog, events, lessons, traces
from .discover import LENSES, apply_merge, parse_merges, safe_title, task_lines
from .priority import config_focus, rank
from .projects import Config, Idle
from .runner import approvals, execute, questions, resets_at, run_config
from .telegram import Buttons, TelegramError

POLL_SECONDS = 20
UNKNOWN_RESET_WAIT = 15 * 60  # rate limit without a reset time: don't hammer it every tick
ACTIVE = ("running", "waiting", "paused", "approving")
# autorun: the state of a project decides what runs, not the hour
MAX_OPEN_PRS = 6  # the queue of a project waits while it has this many open PRs
MAX_PROPOSALS = 10  # discover proposes while a project has fewer [~]
RETRY_EVERY = 24 * 3600  # one automatic discover / cleanup attempt per project and day
CLEANUP_DAYS, CLEANUP_PRS = 14, 10  # cleanup review when the last one is older or this many PRs merged since
CHECK_EVERY = 5 * 60  # gh is asked about PRs at most this often
AUTORUN_QUOTA_CAP = 0.85  # no automatic start while the 5h or weekly window is at or above this
TASK_USAGE = "/task <projekt> <titulek> — přidat úkol (další řádky = popis)"
HELP = ("Příkazy:\n/status — stav démona\n/queue — fronta úkolů\n/proposals — znovu poslat návrhy ke schválení\n"
        "/run — spustit běh hned\n/pause, /resume — vypnout/zapnout autorun\n/digest — přehled teď\n"
        f"{TASK_USAGE}\n\n"
        "Na dotaz k nejasnému úkolu odpověz reply na jeho zprávu.")


def default_state(now: datetime) -> dict:
    return {"paused": False, "run": None, "seq": 0, "offset": 0, "asked": {}, "answers": {}, "approval": None,
            "proposals_sent": [], "lessons_sent": [], "rejected_msgs": {}, "auto_tried": {}, "next_check": 0,
            "last_scan": None,
            "merge_checked": {}, "merges": [], "merge_ignored": [], "last_digest": now.date().isoformat(), "digest_since": now.timestamp()}


def load_state(path: Path, now: datetime) -> dict:
    try:
        return default_state(now) | json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_state(now)


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


@dataclass(frozen=True)
class Facts:
    """What autorun knows about one project; None = gh failed (the project then waits)."""
    queued: int
    open_prs: int | None
    proposals: int
    discover: bool
    last_cleanup: float | None  # None = never reviewed
    merged_since_cleanup: int | None
    idle: Idle | None = None


def _due(tried: dict, key: str, now: float, every: float = RETRY_EVERY) -> bool:
    return now - tried.get(key, 0) >= every


def _idle_due(p: str, f: Facts, now: float, tried: dict) -> bool:
    """A scout prompt adds proposals, so it respects their limit; a shell command does not."""
    return f.idle is not None and _due(tried, f"idle:{p}", now, f.idle.every) \
        and (f.idle.cmd is not None or f.proposals < MAX_PROPOSALS)


def _cleanup_due(f: Facts, now: float) -> bool:
    return f.last_cleanup is None or now - f.last_cleanup > CLEANUP_DAYS * 86400 \
        or (f.merged_since_cleanup or 0) >= CLEANUP_PRS


def next_job(facts: dict[str, Facts], now: float, max_tasks: int, tried: dict) -> dict | None:
    """The next autorun job: queue work first, then a cleanup review, new proposals, the projects' own idle job."""
    free = {p: MAX_OPEN_PRS - f.open_prs for p, f in facts.items()
            if f.queued and f.open_prs is not None and f.open_prs < MAX_OPEN_PRS}
    if free:
        return {"kind": "queue", "only": sorted(free), "max_tasks": min(max_tasks, sum(free.values()))}
    for p, f in facts.items():
        if f.discover and _cleanup_due(f, now) and _due(tried, f"cleanup:{p}", now):
            return {"kind": "cleanup", "only": [p]}
    propose = [p for p, f in facts.items()
               if f.discover and f.proposals < MAX_PROPOSALS and _due(tried, f"propose:{p}", now)]
    if propose:
        return {"kind": "propose", "only": propose}
    return next(({"kind": "idle", "only": [p]} for p, f in facts.items() if _idle_due(p, f, now, tried)), None)


def why_idle(p: str, f: Facts, now: float) -> str:
    """One /status line: the limits of a project as autorun sees them."""
    prs = "?" if f.open_prs is None else f.open_prs
    line = f"{p}: fronta {f.queued}, PR {prs}/{MAX_OPEN_PRS}"
    if f.discover:
        line += f", návrhy {f.proposals}/{MAX_PROPOSALS}"
        cleanup = "nikdy" if f.last_cleanup is None else f"před {int((now - f.last_cleanup) // 86400)} d"
        line += f", cleanup {cleanup}"
    return line


@dataclass
class Ctx:
    cfg: Config
    bot: object  # Telegram-like: chat_id, send, edit, ack
    app: object
    backlog_path: Path
    conn: object
    new_inputs: Callable[[str], dict]
    state: dict
    spawn: Callable[[Callable[[], None]], None]
    now: Callable[[], datetime]
    scan: Callable[[], int]
    simulated: bool = False
    state_path: Path | None = None
    live_quota: Callable[[], dict | None] = lambda: None  # quota.fetch in the real serve
    # handoff: the run thread only ever sets `result`; `busy` is set and cleared by the main loop alone
    busy: bool = False
    result: tuple | None = None  # (run_id, status | Exception)
    last_error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)  # main loop vs dashboard requests
    store: object = None  # lessons (phase 5); None = feature off
    prs: Callable[[Path], list | None] = lambda path: []  # traces.open_prs in the real serve
    merged: Callable[[Path, float], int | None] = lambda path, ts: 0  # traces.merged_since in the real serve
    sh: Callable[[str, Path], tuple[bool, str]] = lambda cmd, cwd: (False, "no shell")  # nodes.real.sh in the real serve
    merge: Callable[[str, list], str] = lambda name, proposals: "[]"  # nodes.real.real_merge in the real serve
    facts: dict = field(default_factory=dict)  # project → Facts of the last autorun check, for /status


def say(ctx: Ctx, text: str, buttons: Buttons | None = None) -> int | None:
    try:
        return ctx.bot.send(text, buttons)
    except TelegramError as e:
        print(f"telegram: {e}", file=sys.stderr)
        return None


def _quiet(fn: Callable, *args) -> None:
    try:
        fn(*args)
    except TelegramError as e:
        print(f"telegram: {e}", file=sys.stderr)


def _backlog_text(ctx: Ctx) -> str:
    return backlog.read(ctx.backlog_path)


def _tasks(ctx: Ctx) -> list[backlog.Task]:
    return backlog.parse(_backlog_text(ctx))


# --- runs -------------------------------------------------------------------------------------------------

def _start(ctx: Ctx, run_id: str, inputs, answers: dict[str, str] | None = None) -> None:
    ctx.busy = True
    ctx.state["run"] = {"id": run_id, "status": "running"}

    def job() -> None:
        try:
            outcome = execute(ctx.app, inputs, run_config(run_id), ctx.conn, ctx.simulated, answers=answers)
        except Exception as e:
            outcome = e
        ctx.result = (run_id, outcome)  # one atomic assignment
    ctx.spawn(job)


def _collect(ctx: Ctx) -> None:
    if ctx.result is None:
        return
    run_id, outcome = ctx.result
    ctx.result, ctx.busy = None, False
    if isinstance(outcome, Exception):
        ctx.state["run"] = {"id": run_id, "status": "crashed"}
        ctx.state["paused"] = True  # a deterministic crash would otherwise restart forever
        say(ctx, f"💥 Běh {run_id} spadl: {type(outcome).__name__}: {str(outcome)[:500]}\n"
                 "Automatický start je vypnutý, /resume ho zapne.")
        return
    run = {"id": run_id, "status": outcome}
    if outcome == "paused":
        run["resets_at"] = resets_at(ctx.app, run_config(run_id)) or ctx.now().timestamp() + UNKNOWN_RESET_WAIT
    ctx.state["run"] = run


def _over(info: dict | None, limit: float, now: float, window: float) -> bool:
    if info is None or info["utilization"] < limit:
        return False
    return now < (info.get("resets_at") or info.get("ts", now) + window)


def _quota_reserved(ctx: Ctx) -> bool:
    """A run stops at quota_threshold, but its quota dies with it: without this a new run would start on the
    next tick and eat the owner's reserve. Hold automatic starts until that window resets; either window at
    AUTORUN_QUOTA_CAP holds them regardless of quota_threshold."""
    now = ctx.now().timestamp()
    live = ctx.live_quota() or {}
    # live endpoint first: the SDK reports utilization only near the limit, so its last value may be stale
    five = live.get("five_hour") or events.latest_utilization(ctx.conn, ctx.simulated, types=("five_hour", None))
    week = live.get("seven_day") or events.latest_utilization(ctx.conn, ctx.simulated, types=("seven_day",))
    return _over(five, min(ctx.cfg.quota_threshold, AUTORUN_QUOTA_CAP), now, 5 * 3600) \
        or _over(week, AUTORUN_QUOTA_CAP, now, 7 * 86400)


def _only(ctx: Ctx, projects) -> list[str] | None:
    if projects is None:
        return None
    if not isinstance(projects, list) or not projects or not set(projects) <= set(ctx.cfg.projects):
        raise ValueError(f"projects must be a non-empty subset of {sorted(ctx.cfg.projects)}")
    return projects


def _lenses(lenses) -> list[str] | None:
    if lenses is None:
        return None
    if not isinstance(lenses, list) or not lenses or not set(lenses) <= set(LENSES):
        raise ValueError(f"lenses must be a non-empty subset of {list(LENSES)}")
    return lenses


def _limits(data: dict) -> dict:
    """Per-run overrides of max_tasks / max_parallel from the dashboard; absent → projects.yaml applies."""
    out = {}
    for key in ("max_tasks", "max_parallel"):
        if (v := data.get(key)) is None:
            continue
        if type(v) is not int or v < 1:
            raise ValueError(f"{key} must be a positive int, got {v!r}")
        out[key] = v
    return out


def _start_new(ctx: Ctx, discover: bool = False, only: list[str] | None = None,
               lenses: list[str] | None = None, limits: dict | None = None, auto: bool = False) -> str:
    run = ctx.state["run"]
    if ctx.busy or (run and run["status"] in ACTIVE):
        return "Běh už probíhá nebo čeká — /status."
    if not discover and not rank(_tasks(ctx), set(only or ctx.cfg.projects)):
        return "Fronta je prázdná."
    ctx.state["seq"] += 1
    run_id = f"{'go' if discover else 'run'}-{ctx.now():%Y%m%d-%H%M%S}-s{ctx.state['seq']}"
    _start(ctx, run_id, ctx.new_inputs(run_id) | {"discover": discover, "only": only, "lenses": lenses, "auto": auto}
           | (limits or {}))
    return f"▶️ Spouštím {run_id}."


def _gather_facts(ctx: Ctx) -> dict[str, Facts]:
    tasks, last = _tasks(ctx), events.last_cleanups(ctx.conn, ctx.simulated)
    queued = [t.project for t in rank(tasks, set(ctx.cfg.projects))]
    out = {}
    for name, p in ctx.cfg.projects.items():
        prs = ctx.prs(p.path)
        ts = last.get(name)
        out[name] = Facts(queued.count(name), None if prs is None else len(prs),
                          sum(t.project == name and t.status == "~" for t in tasks), p.discover,
                          ts, None if ts is None else ctx.merged(p.path, ts), p.idle)
    return out


def _idle_cmd(ctx: Ctx, name: str, cmd: str) -> None:
    """The project's own shell job, beside any run (it touches the project's checkout, not a harness worktree).
    The result lands in the event log, so the digest and the history show it; a failure also goes to Telegram."""
    run_id = f"idle-{name}-{ctx.now():%Y%m%d-%H%M%S}"

    def job() -> None:
        ok, out = ctx.sh(cmd, ctx.cfg.projects[name].path)
        events.log(ctx.conn, run_id, "end", node="idle", worker=name, simulated=ctx.simulated,
                   delta={"outcome": "ok" if ok else "failed", "cmd": cmd, "last_failure": "" if ok else out[-1500:]})
        if not ok:
            say(ctx, f"⚙️ Idle [{name}] `{cmd}` selhal:\n{out[-500:]}")
    ctx.spawn(job)


def _autorun(ctx: Ctx) -> None:
    now = ctx.now().timestamp()
    if ctx.state["paused"] or now < ctx.state["next_check"] or _quota_reserved(ctx):
        return
    ctx.state["next_check"] = now + CHECK_EVERY
    ctx.facts = _gather_facts(ctx)
    job = next_job(ctx.facts, now, ctx.cfg.max_tasks_per_run, ctx.state["auto_tried"])
    if job is None:
        return
    if job["kind"] == "queue":
        _start_new(ctx, only=job["only"], limits={"max_tasks": job["max_tasks"]})
        return
    for p in job["only"]:  # recorded at the start: a crashing discover must not retry every check
        ctx.state["auto_tried"][f"{job['kind']}:{p}"] = now
    if job["kind"] == "idle" and (cmd := ctx.cfg.projects[job["only"][0]].idle.cmd):
        _idle_cmd(ctx, job["only"][0], cmd)
        return
    lenses = {"cleanup": ["cleanup"], "idle": ["idle"]}.get(job["kind"])
    _start_new(ctx, discover=True, only=job["only"], lenses=lenses, auto=True)


def _resume(ctx: Ctx, run_id: str) -> None:
    answers, ctx.state["answers"] = ctx.state["answers"], {}
    for iid in answers:  # a late reply to these must not look accepted (_answer)
        if iid in ctx.state["asked"]:
            ctx.state["asked"][iid]["answered"] = True
    _start(ctx, run_id, None, answers)


def _advance(ctx: Ctx) -> None:
    run = ctx.state["run"]
    status = run["status"] if run else None
    if status == "running":  # the daemon died mid-run: continue from the last checkpoint
        _start(ctx, run["id"], None)
    elif status == "paused":
        # answers wait for the reset too: any resume re-runs the rate-limited node, and the quota is shared
        if ctx.now().timestamp() >= run["resets_at"]:
            _resume(ctx, run["id"])
    elif status == "waiting":
        if ctx.state["answers"]:
            _resume(ctx, run["id"])
    elif status == "approving":
        if ctx.state["approval"]:
            approval, ctx.state["approval"] = ctx.state["approval"], None
            _start(ctx, run["id"], None, approval)
    else:
        _autorun(ctx)


# --- questions --------------------------------------------------------------------------------------------

def _sync_questions(ctx: Ctx) -> None:
    """Send new questions, time out old ones, forget closed ones. Only while no run thread is busy."""
    run, st = ctx.state["run"], ctx.state
    qs = questions(ctx.app, run_config(run["id"])) if run and run["status"] in ("waiting", "paused") else []
    open_ids = {q["id"] for q in qs}
    st["asked"] = {k: v for k, v in st["asked"].items() if k in open_ids}
    st["answers"] = {k: v for k, v in st["answers"].items() if k in open_ids}
    now, limit = ctx.now().timestamp(), ctx.cfg.serve.question_timeout_hours
    for q in qs:
        asked = st["asked"].get(q["id"])
        if asked is None:
            text = (f"❓ [{q['project']}] {q['task']} {q['title']}\n\n{q['question']}\n\n"
                    "Odpověz reply na tuto zprávu.")
            if (mid := say(ctx, text, [[("Přeskočit", f"sk:{q['id']}")]])) is not None:
                st["asked"][q["id"]] = {"message_id": mid, "task": q["task"], "at": now}
        elif q["id"] not in st["answers"] and now - asked["at"] >= limit * 3600:
            st["answers"][q["id"]] = ""
            say(ctx, f"⏱ {q['task']}: bez odpovědi {limit:g} h, přeskakuji → [?]")


def _answer(ctx: Ctx, msg: dict, text: str) -> str:
    asked, answers = ctx.state["asked"], ctx.state["answers"]
    reply_id = (msg.get("reply_to_message") or {}).get("message_id")
    if (task_id := ctx.state["rejected_msgs"].pop(str(reply_id), None)) is not None:
        return _reason(ctx, task_id, text)
    iid = next((i for i, a in asked.items() if a["message_id"] == reply_id), None)
    if iid is None and reply_id is None:
        unanswered = [i for i, a in asked.items() if i not in answers and not a.get("answered")]
        iid = unanswered[0] if len(unanswered) == 1 else None
    if iid is None:
        return "Na který dotaz odpovídáš? Dej reply na jeho zprávu." if asked else HELP
    if asked[iid].get("answered"):
        return f"{asked[iid]['task']}: odpověď už mám, úkol pokračuje — tahle se nepoužije."
    answers[iid] = text
    run = ctx.state["run"]
    if run and run["status"] == "paused":
        return f"Uloženo. Běh čeká na reset limitu ({datetime.fromtimestamp(run['resets_at']):%H:%M}), pak pokračuje."
    return f"👍 {asked[iid]['task']}: odpověď uložena, pokračuji."


# --- proposals --------------------------------------------------------------------------------------------

def _notify_proposals(ctx: Ctx) -> None:
    sent = ctx.state["proposals_sent"]
    for t in _tasks(ctx):
        if t.status != "~" or t.id in sent:
            continue
        text = f"💡 Návrh [{t.project}] P{t.priority} {t.id}\n{t.title}" + (f"\n\n{t.description}" if t.description else "")
        if say(ctx, text, [[("✅ Schválit", f"ap:{t.id}"), ("❌ Zamítnout", f"rj:{t.id}")]]) is None:
            return  # Telegram down: retry next tick
        sent.append(t.id)


def _maybe_merge_check(ctx: Ctx) -> None:
    """Whenever a project's set of proposals changes, a cheap agent looks for overlapping ones to merge."""
    if _quota_reserved(ctx):
        return
    tasks = _tasks(ctx)
    for name in ctx.cfg.projects:
        props = [t for t in tasks if t.project == name and t.status == "~"]
        key = ",".join(sorted(t.id for t in props))
        if len(props) < 2 or ctx.state["merge_checked"].get(name) == key:
            continue
        ctx.state["merge_checked"][name] = key  # at the start: a failing agent must not retry every tick

        def job(name=name, props=props) -> None:
            try:
                text = ctx.merge(name, props)
            except Exception as e:  # the check is optional, the next proposal change tries again
                print(f"merge check {name}: {type(e).__name__}: {e}", file=sys.stderr)
                return
            with ctx.lock:
                known = [t for t in _tasks(ctx) if t.project == name]
                ignored = ctx.state["merge_ignored"] + [m["ids"] for m in ctx.state["merges"]]
                ctx.state["merges"] += parse_merges(text, known, ignored)
        ctx.spawn(job)


def _open_merges(ctx: Ctx) -> list[dict]:
    """Suggestions whose proposals all still wait; the rest went stale (approved, rejected, edited)."""
    waiting = {t.id for t in _tasks(ctx) if t.status == "~"}
    ctx.state["merges"] = [m for m in ctx.state["merges"] if set(m["ids"]) <= waiting]
    return ctx.state["merges"]


def _decide_merge(ctx: Ctx, merge_id: str, accept: bool) -> str:
    merge = next((m for m in _open_merges(ctx) if m["id"] == merge_id), None)
    if merge is None:
        return "Návrh sloučení už neplatí."
    ctx.state["merges"].remove(merge)
    if not accept:
        ctx.state["merge_ignored"].append(merge["ids"])
        return "Sloučení ignorováno."

    def change(text: str) -> tuple[str, str]:
        known = {t.id: t.status for t in backlog.parse(text)}
        if merge["id"] in known or any(known.get(i) != "~" for i in merge["ids"]):
            return text, "Návrh sloučení už neplatí."
        return apply_merge(text, merge), f"Sloučeno do {merge['id']}."
    return backlog.edit_with(ctx.backlog_path, change)


def _rejected(reason: str) -> str:
    reason = " ".join(reason.split())[:300]
    return f" — zamítnuto: {reason}" if reason else " — zamítnuto"


def _find(text: str, task_id: str) -> backlog.Task | None:
    return next((t for t in backlog.parse(text) if task_id == t.id), None)


def _clean_title_priority(title: str, priority: int) -> str:
    title = safe_title(title)[:100]
    if not title:
        raise ValueError("prázdný titulek")
    if priority not in (1, 2, 3):
        raise ValueError("priorita musí být 1–3")
    return title


def _task_fields(data: dict) -> tuple[str, str, str, int]:
    return (str(data.get("title") or ""), str(data.get("description") or ""),
            str(data.get("done") or ""), int(data.get("priority") or 2))


def _decide(ctx: Ctx, task_id: str, approve: bool, reason: str = "") -> str:
    def change(text: str) -> tuple[str, str]:
        t = _find(text, task_id)
        if t is None or t.status != "~":
            return text, "už vyřízeno"
        new = backlog.set_status(text, task_id, " " if approve else "-", "" if approve else _rejected(reason))
        return new, "schváleno → fronta" if approve else "zamítnuto"
    return backlog.edit_with(ctx.backlog_path, change)


def _reason(ctx: Ctx, task_id: str, reason: str) -> str:
    """A reason sent after the rejection (Telegram reply); the reflection reads it from the task's note."""
    def change(text: str) -> str:
        t = _find(text, task_id)
        return backlog.set_status(text, task_id, "-", _rejected(reason)) if t and t.status == "-" else text
    backlog.edit(ctx.backlog_path, change)
    return f"📝 {task_id}: důvod uložen, reflexe se z něj poučí."


def _add_task(ctx: Ctx, project: str, title: str, description: str = "", done: str = "", priority: int = 2) -> str:
    if project not in ctx.cfg.projects:
        raise ValueError(f"neznámý projekt: {project or '(prázdný)'}")
    title = _clean_title_priority(title, priority)
    task_id = backlog.task_id(project, "m", title)
    def change(text: str) -> tuple[str, str]:
        if any(task_id == t.id or (t.project == project and t.title.casefold() == title.casefold()) for t in backlog.parse(text)):
            return text, f"➖ {task_id}: už existuje."
        lines = task_lines({"id": task_id, "title": title, "description": description, "done": " ".join(done.split())}, " ")
        lines[0] = f"- [ ] P{priority} {task_id} {title}"
        return backlog.add_tasks(text, project, lines), f"➕ {task_id} přidán do fronty [{project}]."
    return backlog.edit_with(ctx.backlog_path, change)


def _edit_task(ctx: Ctx, task_id: str, title: str, description: str, done: str, priority: int, approve: bool) -> str:
    title = _clean_title_priority(title, priority)
    note = []

    def change(text: str) -> str:
        t = _find(text, task_id)
        if t is None or t.status != "~":
            note.append("už vyřízeno")
            return text
        accept = [r for r in t.description.splitlines() if r.startswith("ACCEPT:")]
        body = [r.strip() for r in description.splitlines() if r.strip()]
        body += [f"Hotovo = {' '.join(done.split())}"] if done.strip() else []
        kind = f"[{t.kind}] " if t.kind else ""
        note.append("upraveno → fronta" if approve else "upraveno")
        return backlog.replace_task(text, task_id, priority, " " if approve else "~", kind + title, body + accept)
    backlog.edit(ctx.backlog_path, change)
    return note[0]


def _task_cmd(ctx: Ctx, text: str) -> str:
    first, _, rest = text.partition("\n")
    parts = first.split(maxsplit=2)
    try:
        return _add_task(ctx, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "", rest)
    except ValueError as e:
        return f"❌ {e}\nPoužití: {TASK_USAGE}\nProjekty: {', '.join(ctx.cfg.projects)}"


# --- lessons ----------------------------------------------------------------------------------------------

def _notify_lessons(ctx: Ctx) -> None:
    if ctx.store is None:
        return
    sent = ctx.state["lessons_sent"]
    for x in lessons.lessons(ctx.store, status="proposed"):
        ref = f"{x['project']}/{x['id']}"
        if ref in sent:
            continue
        if say(ctx, f"🧠 Lekce [{x['project']}]\n{x['text']}",
               [[("✅ Platí", f"la:{ref}"), ("❌ Zahodit", f"lr:{ref}")]]) is None:
            return
        sent.append(ref)


def _decide_lesson(ctx: Ctx, ref: str, approve: bool) -> str:
    if ctx.store is None:
        return "lekce nejsou k dispozici"
    project, _, key = ref.partition("/")
    return lessons.decide(ctx.store, project, key, "active" if approve else "retired")


# --- digest -----------------------------------------------------------------------------------------------

def digest_text(conn, since: float, backlog_text: str, simulated: bool = False, until: float = float("inf")) -> str:
    runs, tokens, cost, paid = set(), 0, 0.0, 0.0
    tasks: dict[str, dict] = defaultdict(dict)
    idle: list[str] = []
    for e in events.since(conn, since, simulated, until):
        if e["node"] == "idle":
            d = e["delta"] or {}
            fail = (d.get("last_failure") or "").replace("\n", " ")[-150:]
            idle.append(f"⚙️ {e['worker']} `{d.get('cmd')}` — " + ("ok" if d.get("outcome") == "ok" else f"selhalo: {fail}"))
            continue
        runs.add(e["run_id"])
        for u in e["usage"] or []:
            tokens += (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
            cost += u.get("cost_usd") or 0.0
            paid += (u.get("cost_usd") or 0.0) if u.get("billing") == "api" else 0.0
        if e["worker"] and e["kind"] == "end" and e["node"] != "discover":  # discover's worker is a project
            d = e["delta"] or {}
            tasks[e["worker"]].update({k: d[k] for k in ("outcome", "pr_url", "last_failure") if k in d})
            if e["node"] == "report":
                tasks[e["worker"]]["reported"] = True
    lines = [f"📋 Digest — běhů: {len(runs)}"]
    for task_id, t in tasks.items():
        outcome = t.get("outcome") or ("failed" if t.get("reported") else None)
        detail = (t.get("last_failure") or "").replace("\n", " ")[:150]
        if outcome == "pr_opened":
            lines.append(f"✅ {task_id} → {t.get('pr_url')}")
        elif outcome == "needs_clarification":
            lines.append(f"❓ {task_id} — nejasné: {detail}")
        elif outcome:
            lines.append(f"❌ {task_id} — {outcome}: {detail}")
        else:
            lines.append(f"⏳ {task_id} — rozpracováno")
    if not tasks:
        lines.append("Žádné úkoly.")
    lines += idle
    open_tasks = backlog.parse(backlog_text)
    lines.append(f"Tokeny: {tokens}, {traces.cost_label(cost, paid)}")
    lines.append(f"Fronta: {sum(t.status == ' ' for t in open_tasks)} · "
                 f"návrhy ke schválení: {sum(t.status == '~' for t in open_tasks)}")
    return "\n".join(lines)


def _send_digest(ctx: Ctx) -> None:
    now = ctx.now()
    text = digest_text(ctx.conn, ctx.state["digest_since"], _backlog_text(ctx), ctx.simulated)
    if say(ctx, text) is not None:
        ctx.state["last_digest"], ctx.state["digest_since"] = now.date().isoformat(), now.timestamp()


def _maybe_digest(ctx: Ctx) -> None:
    now = ctx.now()
    if ctx.state["last_digest"] != now.date().isoformat() and now.time() >= ctx.cfg.serve.digest_at:
        _send_digest(ctx)


def history(conn, day: str, backlog_text: str, simulated: bool = False) -> str:
    """Digest of one calendar day (YYYY-MM-DD), for browsing what autorun did."""
    start = datetime.fromisoformat(day)
    return digest_text(conn, start.timestamp(), backlog_text, simulated, (start + timedelta(days=1)).timestamp())


def _maybe_scan(ctx: Ctx) -> None:
    today = ctx.now().date().isoformat()
    if ctx.cfg.serve.scan and ctx.state["last_scan"] != today:
        ctx.state["last_scan"] = today
        try:
            ctx.scan()
        except Exception as e:
            print(f"scan failed: {e}", file=sys.stderr)


# --- Telegram input ---------------------------------------------------------------------------------------

def _status(ctx: Ctx) -> str:
    run, tasks = ctx.state["run"], _tasks(ctx)
    lines = [f"Autorun: {'vypnutý' if ctx.state['paused'] else 'zapnutý'}"]
    if run:
        line = f"Běh: {run['id']} — {'running' if ctx.busy else run['status']}"
        if run["status"] == "paused" and not ctx.busy:
            line += f", reset limitu {datetime.fromtimestamp(run['resets_at']):%H:%M}"
        lines.append(line)
    else:
        lines.append("Běh: žádný")
    lines.append(f"Fronta: {len(rank(tasks, set(ctx.cfg.projects)))} · návrhy: {sum(t.status == '~' for t in tasks)}"
                 f" · Dotazy: {sum(1 for i in ctx.state['asked'] if i not in ctx.state['answers'])}")
    now = ctx.now().timestamp()
    lines += [why_idle(p, f, now) for p, f in ctx.facts.items()]
    return "\n".join(lines)


def _queue(ctx: Ctx) -> str:
    ranked = rank(_tasks(ctx), set(ctx.cfg.projects), config_focus(ctx.cfg),
                  {n: p.weight for n, p in ctx.cfg.projects.items()})
    if not ranked:
        return "Fronta je prázdná."
    return "\n".join(f"{i}. P{t.priority} {t.id} [{t.project}] {t.title}" for i, t in enumerate(ranked[:10], 1))


def _command(ctx: Ctx, cmd: str) -> str:
    if cmd == "status":
        return _status(ctx)
    if cmd == "queue":
        return _queue(ctx)
    if cmd == "proposals":
        pending = {t.id for t in _tasks(ctx) if t.status == "~"}
        ctx.state["proposals_sent"] = [i for i in ctx.state["proposals_sent"] if i not in pending]
        return f"Posílám {len(pending)} návrh(ů)." if pending else "Žádné návrhy ke schválení."
    if cmd == "run":
        return _start_new(ctx)
    if cmd == "pause":
        ctx.state["paused"] = True
        return "⏸ Autorun vypnut. Běžící běh doběhne."
    if cmd == "resume":
        ctx.state["paused"] = False
        ctx.state["next_check"] = 0
        return "▶️ Autorun zapnut."
    if cmd == "digest":
        _send_digest(ctx)
        return ""
    return HELP


def _callback(ctx: Ctx, cb: dict) -> None:
    kind, _, key = (cb.get("data") or "").partition(":")
    if kind == "sk":
        note = "přeskočeno" if key in ctx.state["asked"] else "dotaz už není otevřený"
        if key in ctx.state["asked"]:
            ctx.state["answers"][key] = ""
    elif kind in ("ap", "rj"):
        note = _decide(ctx, key, kind == "ap")
        if kind == "rj" and note == "zamítnuto":
            note += " (důvod můžeš poslat reply na tuto zprávu)"
            rejected = ctx.state["rejected_msgs"]
            rejected[str(cb["message"]["message_id"])] = key
            ctx.state["rejected_msgs"] = dict(list(rejected.items())[-50:])
    elif kind in ("la", "lr"):
        note = _decide_lesson(ctx, key, kind == "la")
    else:
        note = "neznámá akce"
    _quiet(ctx.bot.ack, cb["id"], note)
    msg = cb["message"]
    _quiet(ctx.bot.edit, msg["message_id"], f"{msg.get('text', '')}\n\n→ {note}")


def handle(ctx: Ctx, update: dict) -> None:
    """Only the configured chat may control the daemon: commands start runs and spend quota."""
    if cb := update.get("callback_query"):
        if (cb.get("message") or {}).get("chat", {}).get("id") == ctx.bot.chat_id:
            _callback(ctx, cb)
        return
    msg = update.get("message") or {}
    if msg.get("chat", {}).get("id") != ctx.bot.chat_id or not (text := (msg.get("text") or "").strip()):
        return
    if text.startswith("/"):
        cmd = text.split()[0][1:].split("@")[0].lower()
        reply = _task_cmd(ctx, text) if cmd == "task" else _command(ctx, cmd)
    else:
        reply = _answer(ctx, msg, text)
    if reply:
        say(ctx, reply)


# --- dashboard --------------------------------------------------------------------------------------------

def _open(ctx: Ctx) -> dict | None:
    """Config of the run when it sits on interrupts the owner can answer (the run thread is idle)."""
    run = ctx.state["run"]
    if ctx.busy or not run or run["status"] not in ("waiting", "paused", "approving"):
        return None
    return run_config(run["id"])


def snapshot(ctx: Ctx) -> dict:
    with ctx.lock:
        _collect(ctx)
        run, config = ctx.state["run"], _open(ctx)
        plans = approvals(ctx.app, config) if config and not ctx.state["approval"] else []
        now = ctx.now().timestamp()
        return {"paused": ctx.state["paused"], "limits": [why_idle(p, f, now) for p, f in ctx.facts.items()],
                "run": run and {**run, "status": "running" if ctx.busy else run["status"]},
                "questions": [q | {"answered": q["id"] in ctx.state["answers"]}
                              for q in (questions(ctx.app, config) if config else [])],
                "plan": plans[0] if plans else None, "merges": _open_merges(ctx),
                "lessons": [x for x in lessons.lessons(ctx.store) if x["status"] != "retired"]}


def _approve_plan(ctx: Ctx, keep) -> str:
    config = _open(ctx)
    plans = approvals(ctx.app, config) if config else []
    if not plans or ctx.state["approval"]:
        return "Žádný plán ke schválení."
    n = len(plans[0]["plan"])
    if keep != "q" and not (isinstance(keep, list) and all(isinstance(i, int) and 0 <= i < n for i in keep)):
        raise ValueError(f"keep must be 'q' or indices 0–{n - 1}")
    ctx.state["approval"] = {plans[0]["id"]: keep if keep == "q" else sorted(set(keep))}
    return "Běh ukončen." if keep == "q" else f"Schváleno {len(set(keep))} z {n}."


def _web_answer(ctx: Ctx, iid: str, text: str) -> str:
    config = _open(ctx)
    if not config or iid in ctx.state["answers"] or iid not in {q["id"] for q in questions(ctx.app, config)}:
        return "Dotaz už není otevřený."
    ctx.state["answers"][iid] = text
    return "Odpověď uložena."


def act(ctx: Ctx, action: str, data: dict) -> str:
    """Dashboard buttons. Same thread rules as Telegram: state changes only under the lock."""
    with ctx.lock:
        _collect(ctx)
        if action in ("run", "go"):
            reply = _start_new(ctx, discover=action == "go", only=_only(ctx, data.get("projects")),
                               lenses=_lenses(data.get("lenses")), limits=_limits(data))
        elif action in ("approve", "reject"):
            reply = _decide(ctx, str(data.get("task")), action == "approve", str(data.get("reason") or ""))
        elif action == "merge":
            reply = _decide_merge(ctx, str(data.get("id")), bool(data.get("accept")))
        elif action == "lesson":
            reply = _decide_lesson(ctx, f"{data.get('project')}/{data.get('id')}", bool(data.get("approve")))
        elif action == "plan":
            reply = _approve_plan(ctx, data.get("keep"))
        elif action == "answer":
            reply = _web_answer(ctx, str(data.get("id")), str(data.get("text") or ""))
        elif action == "add":
            reply = _add_task(ctx, str(data.get("project") or ""), *_task_fields(data))
        elif action == "edit":
            reply = _edit_task(ctx, str(data.get("task")), *_task_fields(data), bool(data.get("approve")))
        elif action in ("pause", "resume"):
            reply = _command(ctx, action)
        else:
            raise KeyError(action)
        if action in ("plan", "answer") and not ctx.busy:
            _advance(ctx)
        if ctx.state_path:
            save_state(ctx.state_path, ctx.state)
        return reply


# --- loop -------------------------------------------------------------------------------------------------

def tick(ctx: Ctx, updates: list[dict]) -> None:
    for u in updates:
        ctx.state["offset"] = max(ctx.state["offset"], u["update_id"] + 1)
        try:
            handle(ctx, u)
        except Exception as e:  # one bad update must not stop the daemon
            print(f"update {u.get('update_id')}: {type(e).__name__}: {e}", file=sys.stderr)
    try:
        _maybe_scan(ctx)
        # collect right before every busy check: the run thread may finish at any moment (e.g. during the scan)
        _collect(ctx)
        if not ctx.busy:
            _sync_questions(ctx)
            _advance(ctx)
        _collect(ctx)
        if not ctx.busy:
            _sync_questions(ctx)
        _notify_proposals(ctx)
        _maybe_merge_check(ctx)
        _notify_lessons(ctx)
        _maybe_digest(ctx)
    finally:  # the offset too: a handled update must not be delivered again after a failure
        if ctx.state_path:
            save_state(ctx.state_path, ctx.state)


def loop(ctx: Ctx) -> None:
    while True:
        try:
            updates = ctx.bot.updates(ctx.state["offset"], POLL_SECONDS)
        except TelegramError as e:
            print(f"telegram: {e}", file=sys.stderr)
            updates = []
            _time.sleep(5)
        try:
            with ctx.lock:
                tick(ctx, updates)
        except Exception as e:  # e.g. a hand-edited BACKLOG.md that no longer parses: keep running, tell once
            error = f"{type(e).__name__}: {e}"
            print(f"tick: {error}", file=sys.stderr)
            if error != ctx.last_error and say(ctx, f"⚠️ Démon narazil na chybu: {error[:500]}") is not None:
                ctx.last_error = error
        else:
            ctx.last_error = None

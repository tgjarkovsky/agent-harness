import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from functools import partial
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore

from . import backlog, events, lessons, quota, serve, trace, traces
from .graph import build_graph, initial_state
from .nodes.common import (make_approve, make_discover, make_pick_tasks, make_prioritize, make_reflect,
                           make_report)
from .nodes.real import git, real_explore, real_merge, sh, real_nodes, real_rank, real_reflect, worktree_path
from .projects import load_config
from .priority import config_focus, rank
from .discover import format_plan, parse_approval
from .runner import approvals, execute, pending, questions, run_config
from .scan import proposal_lines, scan_all
from .sdk import check_billing
from .simulate import prepare
from .telegram import NoTelegram, Telegram


def open_db(home: Path):
    db = home / "harness.db"
    conn = events.connect(db)
    saver = SqliteSaver(sqlite3.connect(str(db), check_same_thread=False))
    return conn, saver


def open_store(home: Path) -> SqliteStore:
    """Lessons and consumed feedback (phase 5). Autocommit connection: the store runs its own transactions."""
    store = SqliteStore(sqlite3.connect(str(home / "harness.db"), check_same_thread=False, isolation_level=None))
    store.setup()
    return store


def _scenario_path(home: Path, name: str) -> Path | None:
    p = Path(name)
    if p.suffix == ".yaml" and p.exists():
        return p
    p = home / "scenarios" / f"{name}.yaml"
    return p if p.exists() else None


def ask_in_terminal(app, config: dict) -> dict[str, str]:
    return {q["id"]: input(f"{q['task']}: {q['question']} (Enter = přeskočit) > ").strip()
            for q in questions(app, config)}


def ask_approval(app, config: dict) -> dict:
    out = {}
    for a in approvals(app, config):
        print(format_plan(a["plan"]))
        while True:
            try:
                out[a["id"]] = parse_approval(input("Enter = vše, čísla = vyškrtnout (2,5), q = konec > "),
                                              len(a["plan"]))
                break
            except ValueError as e:
                print(e)
    return out


def answer_in_terminal(app, config: dict, status: str) -> dict:
    return ask_approval(app, config) if status == "approving" else ask_in_terminal(app, config)


def cmd_simulate(args) -> int:
    path = _scenario_path(args.home, args.scenario)
    if path is None:
        print(f"scenario not found: {args.scenario}", file=sys.stderr)
        return 2
    conn, saver = open_db(args.home)
    sim = prepare(path, Path(tempfile.mkdtemp(prefix="harness-sim-")), saver, step=args.step, delay=args.delay)
    print(f"run {sim.run_id}")
    status = execute(sim.app, sim.inputs, sim.config, conn, simulated=True)
    while status in ("interrupted", "waiting", "approving"):
        if status in ("waiting", "approving"):
            status = execute(sim.app, None, sim.config, conn, simulated=True,
                             answers=answer_in_terminal(sim.app, sim.config, status))
            continue
        nxt = ", ".join(pending(sim.app, sim.config))
        if input(f"další uzel: {nxt} — Enter = krok, q = konec > ").strip().lower() == "q":
            break
        status = execute(sim.app, None, sim.config, conn, simulated=True)
    print(status)
    print(sim.backlog_path.read_text(encoding="utf-8"))
    return 0


def _real_app(home: Path, cfg):
    backlog_path = home / "BACKLOG.md"
    conn, saver = open_db(home)
    store = open_store(home)
    trace.set_sink(lambda call, steps: traces.record(conn, call, steps))
    rank_fn = partial(real_rank, history_of=lambda: lessons.owner_history(backlog.parse(_read_backlog(home))))
    nodes = real_nodes(cfg) | {"pick_tasks": make_pick_tasks(backlog_path, cfg), "report": make_report(backlog_path),
                               "discover": make_discover(backlog_path, real_explore(cfg)),
                               "prioritize": make_prioritize(backlog_path, cfg, rank_fn),
                               "approve": make_approve(backlog_path),
                               "reflect": make_reflect(backlog_path, real_reflect, traces.pr_feedback)}
    return build_graph(nodes, saver, store=store), backlog_path, conn, store


def _run(args, discover: bool) -> int:
    cfg = load_config(args.home / "projects.yaml")
    # preflight checks this per task, but --resume jumps straight back into implement
    if err := check_billing(cfg.billing, os.environ):
        print(err, file=sys.stderr)
        return 2
    app, backlog_path, conn, _ = _real_app(args.home, cfg)
    _sync_outcomes(conn, args.home)
    resume = getattr(args, "resume", None)
    run_id = resume or f"{'go' if discover else 'run'}-{time.strftime('%Y%m%d-%H%M%S')}"
    inputs = None if resume else initial_state(run_id, cfg, simulated=False, discover=discover)
    config = run_config(run_id)
    status = execute(app, inputs, config, conn, simulated=False)
    while status in ("waiting", "approving"):
        status = execute(app, None, config, conn, simulated=False, answers=answer_in_terminal(app, config, status))
    for err in app.get_state(config).values.get("go_errors") or []:
        print(f"go: {err}", file=sys.stderr)
    print(f"{run_id} {status}")
    if status == "paused":
        print(f"resume: harness run --resume {run_id}")
    return 0


def _sync_outcomes(conn, home: Path) -> None:
    try:
        traces.sync_outcomes(conn, _read_backlog(home), traces.pr_state)
    except Exception as e:  # outcomes are bookkeeping; never block a run on them
        print(f"outcome sync failed: {e}", file=sys.stderr)


def _money(v) -> str:
    return f"{v or 0:.2f}"


def cmd_stats(args) -> int:
    conn, _ = open_db(args.home)
    traces.backfill(conn)
    _sync_outcomes(conn, args.home)
    c = traces.costs(conn)
    print(f"{'úkol':<28} {'projekt':<14} {'stav':<8} {'pokusy':>6} {'volání':>6} {'tokeny':>10} {'~$ sub':>7} "
          f"{'$ API':>7}  PR")
    for t in c["tasks"]:
        print(f"{t['task_id']:<28} {t['project'] or '-':<14} {t['state'] or '-':<8} {t['attempts']:>6} "
              f"{t['calls']:>6} {t['tokens'] or 0:>10} {_money(t['cost_usd'] - t['paid_usd']):>7} "
              f"{_money(t['paid_usd']):>7}  {t['pr_url'] or ''}")
    for o in c["overhead"]:
        print(f"režie {o['run_id']} {o['node']}: {o['calls']}× {o['tokens'] or 0} tokenů "
              f"{traces.cost_label(o['cost_usd'], o['paid_usd'])}")
    if c["tasks"]:
        print("souhrn (projekt × model plánovače)")
        for g in traces.summary(conn):
            per = "-" if g["cost_per_merged"] is None else _money(g["cost_per_merged"])
            print(f"{g['project']:<14} {g['plan_model']:<10} úkoly {g['tasks']:>3} merged {g['merged']:>3} "
                  f"closed {g['closed']:>3} [!] {g['failed']:>3} [?] {g['unclear']:>3} open {g['open']:>3} "
                  f"~${_money(g['cost_usd'])} ~$/merged {per}")
    rows = c["tasks"] + c["overhead"]
    total, paid = (sum(r[k] or 0 for r in rows) for k in ("cost_usd", "paid_usd"))
    print(f"celkem {traces.cost_label(total, paid)}" if rows else "žádná zaznamenaná volání")
    return 0


def cmd_run(args) -> int:
    return _run(args, discover=False)


def cmd_go(args) -> int:
    return _run(args, discover=True)


def _scan_props(cfg, backlog_path: Path, names=None, limit=5, write=True) -> list:
    """Scan outside the backlog lock (git fetch is slow), then append only proposals still unknown."""
    _, props = scan_all(cfg, backlog.read(backlog_path), names, limit)
    if write:
        backlog.edit(backlog_path, lambda text: backlog.add_new(text, [(p.id, p.project, proposal_lines(p)) for p in props]))
    return props


def _scan_into(cfg, backlog_path: Path, names=None, limit=5) -> int:
    return len(_scan_props(cfg, backlog_path, names, limit))


def hide_child_consoles() -> None:
    """Without a console every git/gh child would flash its own window; default them to CREATE_NO_WINDOW."""
    init = subprocess.Popen.__init__

    def no_window(self, *a, creationflags=0, **kw):
        init(self, *a, creationflags=creationflags | subprocess.CREATE_NO_WINDOW, **kw)
    subprocess.Popen.__init__ = no_window


def cmd_serve(args) -> int:
    import uvicorn

    from .dashboard.app import create_app
    if sys.stdout is None:  # pythonw (autostart without a console window)
        sys.stdout = sys.stderr = open(args.home / "serve.log", "a", encoding="utf-8", buffering=1)
        hide_child_consoles()
    token, chat = os.environ.get("HARNESS_TELEGRAM_TOKEN"), os.environ.get("HARNESS_TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("HARNESS_TELEGRAM_TOKEN/CHAT_ID not set: dashboard only (see README → serve)", file=sys.stderr)
    cfg = load_config(args.home / "projects.yaml")
    if err := check_billing(cfg.billing, os.environ):
        print(err, file=sys.stderr)
        return 2
    app, backlog_path, conn, store = _real_app(args.home, cfg)
    state_path = args.home / "serve_state.json"
    bot = Telegram(token, int(chat)) if token and chat else NoTelegram()
    ctx = serve.Ctx(cfg=cfg, bot=bot, app=app, backlog_path=backlog_path, conn=conn,
                    new_inputs=lambda rid: initial_state(rid, cfg, simulated=False),
                    state=serve.load_state(state_path, datetime.now()),
                    spawn=lambda job: threading.Thread(target=job, daemon=True).start(),
                    now=datetime.now, scan=lambda: _scan_into(cfg, backlog_path), state_path=state_path,
                    store=store, live_quota=quota.fetch, merged=traces.merged_since, sh=sh,
                    merge=real_merge(cfg))
    web = create_app(args.home, control=ctx)
    ctx.prs = web.state.prs  # shared cache: conflict rebases and CI fixes run even with no dashboard open
    threading.Thread(target=uvicorn.run, args=(web,), kwargs={"host": "127.0.0.1", "port": args.port},
                     daemon=True).start()
    print(f"harness serve: autorun, dashboard http://127.0.0.1:{args.port}/desktop, Ctrl+C to stop")
    serve.say(ctx, "🟢 harness serve běží. /help")
    try:
        serve.loop(ctx)
    except KeyboardInterrupt:  # a running run continues from its checkpoint on the next start
        serve.save_state(state_path, ctx.state)
    return 0


def cmd_lessons(args) -> int:
    store = open_store(args.home)
    if args.retire:
        project, _, key = args.retire.partition("/")
        print(lessons.decide(store, project, key, "retired"))
        return 0
    rows = lessons.lessons(store, args.project)
    for x in rows:
        print(f"{x['project']}/{x['id']} [{x['status']}] {x['text']}")
    if not rows:
        print("no lessons")
    return 0


def cmd_prune(args) -> int:
    cfg = load_config(args.home / "projects.yaml")
    done = {t.id for t in backlog.parse(backlog.read(args.home / "BACKLOG.md")) if t.status == "x"}
    for project in cfg.projects.values():
        for task_id in done:
            path = worktree_path(project, task_id)
            if path.exists():
                git(["worktree", "remove", "--force", str(path)], project.path)
                print(f"removed {path}")
    return 0


def _read_backlog(home: Path) -> str:
    return backlog.read(home / "BACKLOG.md")


def cmd_queue(args) -> int:
    cfg = load_config(args.home / "projects.yaml")
    focus = config_focus(cfg)
    ranked = rank(backlog.parse(_read_backlog(args.home)), set(cfg.projects), focus,
                  {n: p.weight for n, p in cfg.projects.items()})
    print("focus (secondbrain): " + (", ".join(f"{n}={c}" for n, c in focus.items()) if focus else "off"))
    for i, t in enumerate(ranked, 1):
        print(f"{i:>2}. P{t.priority} {t.id} [{t.project}] {t.title}")
    if not ranked:
        print("no open tasks")
    return 0


def cmd_scan(args) -> int:
    cfg = load_config(args.home / "projects.yaml")
    names = [args.project] if args.project else [n for n, p in cfg.projects.items() if p.discover]
    if unknown := [n for n in names if n not in cfg.projects]:
        print(f"unknown project: {', '.join(unknown)}", file=sys.stderr)
        return 2
    props = _scan_props(cfg, args.home / "BACKLOG.md", names, args.limit, write=bool(args.write))
    for name in names:
        mine = [p for p in props if p.project == name]
        for p in mine:
            print(f"{name}: {p.id} {p.title} ({p.path}:{p.line})")
        if not mine:
            print(f"{name}: no new proposals")
    if args.write and props:
        print(f"{len(props)} proposal(s) written as [~]; change to [ ] to approve")
    return 0


def cmd_dashboard(args) -> int:
    import uvicorn

    from .dashboard.app import create_app
    uvicorn.run(create_app(args.home), host="127.0.0.1", port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252; BACKLOG uses → and —
        sys.stdout.reconfigure(errors="replace")
    p = argparse.ArgumentParser(prog="harness")
    p.add_argument("--home", type=Path, default=Path.cwd())
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("simulate")
    s.add_argument("scenario")
    s.add_argument("--step", action="store_true")
    s.add_argument("--delay", type=float)
    r = sub.add_parser("run")
    r.add_argument("--resume", metavar="RUN_ID")
    d = sub.add_parser("dashboard")
    d.add_argument("--port", type=int, default=8765)
    sub.add_parser("go")
    sub.add_parser("stats")
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=8765)
    sub.add_parser("prune")
    ls = sub.add_parser("lessons")
    ls.add_argument("--project")
    ls.add_argument("--retire", metavar="PROJECT/ID")
    sub.add_parser("queue")
    sc = sub.add_parser("scan")
    sc.add_argument("--write", action="store_true")
    sc.add_argument("--project")
    sc.add_argument("--limit", type=int, default=5)
    args = p.parse_args(argv)
    handlers = {"simulate": cmd_simulate, "run": cmd_run, "dashboard": cmd_dashboard, "prune": cmd_prune,
                "queue": cmd_queue, "scan": cmd_scan, "serve": cmd_serve, "go": cmd_go, "stats": cmd_stats,
                "lessons": cmd_lessons}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

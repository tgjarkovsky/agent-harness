import re
import sys
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

import yaml

REQUIRED = ("path", "base", "verify")
TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
EVERY_RE = re.compile(r"^\s*(\d+)\s*([mhd])\s*$")
UNIT = {"m": 60, "h": 3600, "d": 86400}
PLAN_FILE = "ROADMAP.md"  # the project's mid-term product plan in its repo root; the feature scout follows it


@dataclass(frozen=True)
class Idle:
    """What the harness runs for a project when autorun has nothing else to do: a shell command or a scout prompt."""
    cmd: str | None
    prompt: str | None
    every: int  # seconds
    web: bool = False  # the scout may also search and read the web (WebSearch, WebFetch)


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    base: str
    setup: tuple[str, ...]
    verify: tuple[str, ...]
    allowed: tuple[str, ...]
    max_attempts: int
    max_diff_lines: int
    weight: int = 0
    discover: bool = True  # false: `go` discover and the marker scan skip it (tasks only from reflection / by hand)
    idle: Idle | None = None


@dataclass(frozen=True)
class Serve:
    digest_at: time = time(7)  # daily Telegram digest of the last 24 h
    question_timeout_hours: float = 24
    scan: bool = True


@dataclass(frozen=True)
class Agenda:
    """Personal work: tasks in an Obsidian Tasks file; meetings from Google Calendar (HARNESS_GCAL_ICS)."""
    todo: Path
    work_hours: tuple[time, time] = (time(9), time(18))
    block_minutes: int = 90
    suggest_at: tuple[time, ...] = (time(7), time(14))  # task suggestions from new e-mails and calendar events
    remind_today_at: time | None = time(15)  # deadlines of today still open
    remind_tomorrow_at: time | None = time(20)  # deadlines of tomorrow
    soon_days: int = 3  # the day plan's "coming up" reaches this far


@dataclass(frozen=True)
class Config:
    billing: str
    max_tasks_per_run: int
    quota_threshold: float
    projects: dict[str, Project]
    max_parallel: int = 2
    secondbrain: tuple[Path, int] | None = None  # (vault path, look-back days)
    serve: Serve = field(default_factory=Serve)
    plan_model: str = "opus"
    agenda: Agenda | None = None


def load_config(path: Path) -> Config:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    billing = raw.get("billing", "subscription")
    if billing not in ("subscription", "api"):
        raise ValueError(f"billing must be subscription|api, got {billing!r}")
    defaults = raw.get("defaults", {})
    projects = {
        name: _project(name, spec, defaults, path.parent)
        for name, spec in (raw.get("projects") or {}).items()
    }
    max_parallel = raw.get("max_parallel", 2)
    if not isinstance(max_parallel, int) or max_parallel < 1:
        raise ValueError(f"max_parallel must be a positive int, got {max_parallel!r}")
    brain = raw.get("secondbrain")
    secondbrain = ((path.parent / brain["path"]).resolve(), brain.get("days", 14)) if brain else None
    return Config(billing, raw.get("max_tasks_per_run", 3), raw.get("quota_threshold", 0.7), projects,
                  max_parallel, secondbrain, _serve(raw.get("serve") or {}), raw.get("plan_model", "opus"),
                  _agenda(raw["agenda"], path.parent) if raw.get("agenda") else None)


def _time(text: str, key: str = "serve.digest_at") -> time:
    m = TIME_RE.match(str(text))
    try:
        return time(int(m[1]), int(m[2]))
    except (TypeError, ValueError):  # no match / hour 25
        raise ValueError(f"{key} must look like 07:00, got {text!r}") from None


def _agenda(raw: dict, root: Path) -> Agenda:
    if not raw.get("todo"):
        raise ValueError("agenda.todo: path to the TODO.md is required")
    start, _, end = str(raw.get("work_hours", "09:00-18:00")).partition("-")
    hours = (_time(start, "agenda.work_hours"), _time(end, "agenda.work_hours"))
    if hours[0] >= hours[1]:
        raise ValueError(f"agenda.work_hours must look like 09:00-18:00, got {raw.get('work_hours')!r}")
    at = raw.get("suggest_at", ["07:00", "14:00"])
    remind = {k: (None if raw.get(k, d) is None else _time(raw.get(k, d), f"agenda.{k}"))
              for k, d in (("remind_today_at", "15:00"), ("remind_tomorrow_at", "20:00"))}
    return Agenda((root / raw["todo"]).resolve(), hours, int(raw.get("block_minutes", 90)),
                  tuple(sorted(_time(t, "agenda.suggest_at") for t in (at if isinstance(at, list) else [at]))),
                  remind["remind_today_at"], remind["remind_tomorrow_at"], int(raw.get("soon_days", 3)))


def _serve(raw: dict) -> Serve:
    if "window" in raw:
        print("projects.yaml: serve.window is ignored, autorun runs any time (limits in serve.py)", file=sys.stderr)
    return Serve(_time(raw.get("digest_at", "07:00")), raw.get("question_timeout_hours", 24), raw.get("scan", True))


def _project(name: str, spec: dict, defaults: dict, root: Path) -> Project:
    missing = [k for k in REQUIRED if k not in spec]
    if missing:
        raise ValueError(f"project {name}: missing {', '.join(missing)}")
    return Project(
        name=name,
        path=(root / spec["path"]).resolve(),
        base=spec["base"],
        setup=tuple(spec.get("setup", [])),
        verify=tuple(spec["verify"]),
        allowed=tuple(spec.get("allowed", [])),
        max_attempts=spec.get("max_attempts", defaults.get("max_attempts", 3)),
        max_diff_lines=spec.get("max_diff_lines", defaults.get("max_diff_lines", 800)),
        weight=spec.get("weight", 0),
        discover=spec.get("discover", True),
        idle=_idle(name, spec["idle"]) if spec.get("idle") else None,
    )


def _idle(name: str, raw: dict) -> Idle:
    if not isinstance(raw, dict) or bool(raw.get("cmd")) == bool(raw.get("prompt")):
        raise ValueError(f"project {name}: idle needs exactly one of cmd / prompt")
    m = EVERY_RE.match(str(raw.get("every", "24h")))
    if not m or int(m[1]) == 0:
        raise ValueError(f"project {name}: idle.every must look like 30m, 24h or 7d, got {raw.get('every')!r}")
    return Idle(raw.get("cmd"), raw.get("prompt"), int(m[1]) * UNIT[m[2]], bool(raw.get("web")))

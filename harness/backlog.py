import hashlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")
LOCK = threading.Lock()  # serve, dashboard and graph nodes edit BACKLOG.md from different threads
ON_CHANGE: list[Callable[[], None]] = []  # called after every write (state sync schedules a push)

HEADER_RE = re.compile(r"^## (?P<project>\S+)\s*$")
# id becomes a git branch name and CLI argument → restrict to safe characters; task_id() is the only creator
TASK_RE = re.compile(r"^- \[(?P<status>[ x!?~-])\] P(?P<prio>[1-3]) (?P<id>[A-Za-z0-9._-]+) (?P<rest>.+)$")
SUFFIX_RE = re.compile(r" (?:→|—) ")
# proposal kind as a title prefix: "- [~] P2 id [feature] Title"; set_status keeps it untouched
KIND_RE = re.compile(r"^\[(?P<kind>test|bug|debt|feature|docs|refactor)\] (?P<title>.+)$")


def task_id(project: str, kind: str, seed: str) -> str:
    """kind: "ai" (discover), "m" (manual), "todo" (scan)."""
    digest = hashlib.sha1(seed.encode()).hexdigest()[:6]
    return f"{re.sub(r'[^A-Za-z0-9._-]', '-', project)}-{kind}-{digest}"


@dataclass(frozen=True)
class Task:
    id: str
    project: str
    priority: int
    title: str
    status: str
    description: str
    note: str = ""  # suffix of a finished task: PR url, failure, rejection reason
    kind: str = ""  # test|bug|debt|feature|docs|refactor; "" = written by hand or before kinds existed


def _split(rest: str, status: str) -> tuple[str, str]:
    # only finished tasks carry a " → url" / " — note" suffix; an open/proposed title may contain "—" itself
    if status in " ~":
        return rest.strip(), ""
    title, note = (SUFFIX_RE.split(rest, maxsplit=1) + [""])[:2]
    return title.strip(), note.strip()


def _title(rest: str, status: str) -> str:
    return _split(rest, status)[0]


def parse(text: str) -> list[Task]:
    raw: list[dict] = []
    project, current = None, None
    for line in text.splitlines():
        if m := HEADER_RE.match(line):
            project, current = m["project"], None
        elif (m := TASK_RE.match(line)) and project:
            title, note = _split(m["rest"], m["status"])
            k = KIND_RE.match(title)
            current = {"id": m["id"], "project": project, "priority": int(m["prio"]),
                       "title": k["title"] if k else title, "status": m["status"], "description": [], "note": note,
                       "kind": k["kind"] if k else ""}
            raw.append(current)
        elif current is not None and line.startswith("  ") and line.strip():
            current["description"].append(line.strip())
        elif line.strip():
            current = None
    seen: set[str] = set()
    for t in raw:
        if t["id"] in seen:
            raise ValueError(f"duplicate task id: {t['id']}")
        seen.add(t["id"])
    return [Task(**{**t, "description": "\n".join(t["description"])}) for t in raw]


def set_status(text: str, task_id: str, status: str, note: str = "") -> str:
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        m = TASK_RE.match(body)
        if m and m["id"] == task_id:
            eol = line[len(body):]
            lines[i] = f"- [{status}] P{m['prio']} {task_id} {_title(m['rest'], m['status'])}{note}{eol}"
            return "".join(lines)
    raise KeyError(task_id)


def replace_task(text: str, task_id: str, priority: int, status: str, title: str, body: list[str]) -> str:
    """Rewrite the task's row and its indented description; every other line stays byte for byte."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        row = line.rstrip("\r\n")
        m = TASK_RE.match(row)
        if m and m["id"] == task_id:
            eol = line[len(row):]
            end = i + 1
            while end < len(lines) and lines[end].startswith("  ") and lines[end].strip():
                end += 1
            new = [f"- [{status}] P{priority} {task_id} {title}", *(f"  {r}" for r in body)]
            lines[i:end] = [r + eol for r in new]
            return "".join(lines)
    raise KeyError(task_id)


def add_tasks(text: str, project: str, lines: list[str]) -> str:
    """Append task lines at the end of the project's section (created at the end of the file if missing)."""
    eol = "\r\n" if "\r\n" in text else "\n"
    rows = text.splitlines()
    start = next((i for i, r in enumerate(rows) if (m := HEADER_RE.match(r)) and m["project"] == project), None)
    if start is None:
        head = rows + [""] if rows else []
        return eol.join(head + [f"## {project}", *lines]) + eol
    end = next((i for i in range(start + 1, len(rows)) if HEADER_RE.match(rows[i])), len(rows))
    while end > start + 1 and not rows[end - 1].strip():
        end -= 1
    return eol.join(rows[:end] + lines + rows[end:]) + eol


def add_new(text: str, items: list[tuple[str, str, list[str]]]) -> str:
    """Add tasks whose id is not in the backlog yet; items are (id, project, lines)."""
    known = {t.id for t in parse(text)}
    for task_id, project, lines in items:
        if task_id not in known:
            text = add_tasks(text, project, lines)
            known.add(task_id)
    return text


def read(path: Path) -> str:
    return path.read_bytes().decode("utf-8") if path.exists() else ""


def edit_with(path: Path, fn: Callable[[str], tuple[str, T]]) -> T:
    """Read-modify-write under the lock; fn returns (new text, result) and the result is passed on."""
    with LOCK:
        new, result = fn(read(path))
        path.write_bytes(new.encode("utf-8"))
        for fn_changed in list(ON_CHANGE):
            fn_changed()
        return result


def edit(path: Path, fn: Callable[[str], str]) -> None:
    edit_with(path, lambda text: (fn(text), None))

import re
import subprocess
import sys
from dataclasses import dataclass

from . import backlog
from .backlog import Task
from .discover import safe_title
from .nodes.real import git
from .projects import Config, Project

# marker must directly follow a comment prefix, so code and prose merely mentioning it don't count
MARKER_RE = re.compile(r"(?:#|//|/\*|<!--|--|;|^\s*\*)\s*\b(TODO|FIXME)\b[:\s-]*(.*)")


@dataclass(frozen=True)
class Proposal:
    id: str
    project: str
    path: str
    line: int
    title: str


def _proposal(project: str, path: str, line: int, text: str) -> Proposal | None:
    m = MARKER_RE.search(text)
    if not m:
        return None
    # id ignores the line number so a proposal survives code moving around it
    title = f"{m[1]}: {m[2].strip()}" if m[2].strip() else f"{m[1]} v {path}"
    return Proposal(backlog.task_id(project, "todo", f"{path}\0{text.strip()}"), project, path, line, safe_title(title)[:100])


def scan_project(project: Project) -> list[Proposal]:
    """All TODO/FIXME markers on origin/<base> (never the user's working copy), sorted by path."""
    ref = f"origin/{project.base}"
    git(["fetch", "-q", "origin", project.base], project.path)
    # markdown excluded: plans/specs/notes quote TODOs as prose, not as work left in code
    out = subprocess.run(["git", "grep", "-n", "-I", "-w", "-E", "TODO|FIXME", ref, "--", ".", ":!*.md"], cwd=project.path,
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode not in (0, 1):  # 1 = no match
        raise RuntimeError(f"git grep failed in {project.name}: {out.stderr.strip()}")
    props = []
    for row in out.stdout.splitlines():
        # ponytail: a path containing ":" is mis-split and skipped; use -z if that ever matters
        path, line, text = row.removeprefix(f"{ref}:").split(":", 2)
        if line.isdigit() and (p := _proposal(project.name, path, int(line), text)):
            props.append(p)
    props.sort(key=lambda p: (p.path, p.line))
    # the same comment twice in one file has one id; keep its first occurrence
    return list({p.id: p for p in reversed(props)}.values())[::-1]


def new_proposals(props: list[Proposal], tasks: list[Task]) -> list[Proposal]:
    known = {t.id for t in tasks}
    return [p for p in props if p.id not in known]


def proposal_lines(p: Proposal) -> list[str]:
    return [f"- [~] P3 {p.id} {p.title}",
            f"  Návrh ze skenu: {p.path}:{p.line}. Vyřeš poznámku a odstraň ji; ověř existujícími testy."]


def scan_all(cfg: Config, text: str, names: list[str] | None = None, limit: int = 5) -> tuple[str, list[Proposal]]:
    """New proposals of the given projects and the backlog text with them appended as [~]."""
    found: list[Proposal] = []
    for name in names or [n for n, p in cfg.projects.items() if p.discover]:
        try:
            props = new_proposals(scan_project(cfg.projects[name]), backlog.parse(text))[:limit]
        except Exception as e:  # one unreachable repo must not hide the others
            print(f"{name}: scan failed: {e}", file=sys.stderr)
            continue
        if props:
            text = backlog.add_tasks(text, name, [line for p in props for line in proposal_lines(p)])
        found += props
    return text, found

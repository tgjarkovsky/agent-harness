"""Pure helpers of `harness go`: parse model answers, build the plan, write the owner's approval to BACKLOG."""
import json
import re

from . import backlog
from .backlog import Task
from .priority import rank

FIX_KINDS = ("test", "bug", "debt", "docs")
LENSES = ("fix", "feature", "cleanup")  # one discover prompt each: discover_<lens>.md
DEFAULT_LENSES = ("fix", "feature")  # cleanup (whole-repo review) runs only on request
MAX_PROPOSALS = 5  # per lens
NEW_PRIORITY = 2
MERGED_NOTE = "sloučeno do"  # note of a proposal folded into another one; not a rejection
FENCE_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)


def _arrays(text: str) -> list[list]:
    decoder, out = json.JSONDecoder(), []
    for i, ch in enumerate(text):
        if ch == "[":
            try:
                data, _ = decoder.raw_decode(text, i)
            except ValueError:
                continue
            if isinstance(data, list):
                out.append(data)
    return out


def json_array(text: str) -> list | None:
    """The JSON array of a model answer: a ```json fence, else the first array of objects in the text
    (prose around it may hold `[README.md]` or `[1]`), else the first array at all (`[]` = nothing to do)."""
    m = FENCE_RE.search(text)
    arrays = _arrays(m[1] if m else text)
    return next((a for a in arrays if a and all(isinstance(x, dict) for x in a)), arrays[0] if arrays else None)


def _line(value) -> str:
    return " ".join(str(value or "").split())


def safe_title(value) -> str:
    """Title without " — " / " → ", which BACKLOG reads as the start of a finished task's note."""
    return _line(value).replace(" — ", " – ").replace(" → ", " -> ")


def proposal_id(project: str, title: str) -> str:
    return backlog.task_id(project, "ai", title)


def _kind(item: dict, lens: str) -> str:
    if lens not in ("fix", "idle"):  # idle = the project's own scout prompt (projects.yaml), kinds as for fixes
        return {"feature": "feature", "cleanup": "refactor"}[lens]
    return item.get("kind") if item.get("kind") in FIX_KINDS else "debt"


def parse_proposals(text: str, project: str, known: list[Task], lens: str = "fix") -> list[dict]:
    """Up to MAX_PROPOSALS new proposals. `known` = backlog tasks in any status, so rejected ones never return."""
    ids, titles = {t.id for t in known}, {t.title.casefold() for t in known}
    out: list[dict] = []
    for item in json_array(text) or []:
        title = safe_title(item.get("title"))[:100] if isinstance(item, dict) else ""
        pid = proposal_id(project, title)
        if not title or pid in ids or title.casefold() in titles:
            continue
        ids.add(pid)
        titles.add(title.casefold())
        out.append({"id": pid, "project": project, "title": title,
                    "kind": _kind(item, lens),
                    "description": str(item.get("description") or "").strip(),
                    "why": _line(item.get("why")), "done": _line(item.get("done")),
                    "accept": _line(item.get("accept")).strip("`").strip(), "source": "new"})
    return out[:MAX_PROPOSALS]


def candidates(proposals: list[dict], tasks: list[Task], projects: set[str]) -> list[dict]:
    """New proposals (stable by project — parallel discover finishes in any order) + open backlog tasks."""
    new = [p | {"priority": NEW_PRIORITY} for p in sorted(proposals, key=lambda p: p["project"])]
    old = [{"id": t.id, "project": t.project, "title": t.title, "kind": t.kind, "description": t.description,
            "why": "", "done": "", "source": "backlog", "priority": t.priority}
           for t in tasks if t.status == " " and t.project in projects]
    return new + old


def parse_ranking(text: str, cands: list[dict], n: int) -> list[dict]:
    by_id = {c["id"]: c for c in cands}
    plan: list[dict] = []
    for item in json_array(text) or []:
        cid = item.get("id") if isinstance(item, dict) else None
        if isinstance(cid, str) and cid in by_id and all(p["id"] != cid for p in plan):
            plan.append(by_id[cid] | {"why": _line(item.get("why")) or by_id[cid]["why"]})
    return plan[:n]


def fallback_plan(cands: list[dict], focus: dict[str, int], weights: dict[str, int], n: int) -> list[dict]:
    tasks = [Task(c["id"], c["project"], c["priority"], c["title"], " ", c["description"]) for c in cands]
    by_id = {c["id"]: c for c in cands}
    ranked = rank(tasks, {c["project"] for c in cands}, focus, weights)
    return [by_id[t.id] | {"why": "fallback: pořadí podle priority"} for t in ranked[:n]]


def parse_approval(answer: str, n: int) -> list[int] | str:
    """"" = keep all, "2,5" = strike 2 and 5, "q" = quit. Anything else raises ValueError."""
    a = answer.strip().lower()
    if a == "q":
        return "q"
    drop = set()
    for part in filter(None, re.split(r"[,\s]+", a)):
        if not part.isdigit() or not 1 <= int(part) <= n:
            raise ValueError(f"neplatná volba: {part} (čísla 1–{n}, Enter, q)")
        drop.add(int(part) - 1)
    return [i for i in range(n) if i not in drop]


def task_lines(p: dict, status: str) -> list[str]:
    kind = f"[{p['kind']}] " if p.get("kind") else ""
    lines = [f"- [{status}] P{NEW_PRIORITY} {p['id']} {kind}{p['title']}"]
    lines += [f"  {row.strip()}" for row in p["description"].splitlines() if row.strip()]
    lines += [f"  Hotovo = {p['done']}"] if p["done"] else []
    # the scout already explored the code: with an acceptance command the worker skips the planner
    return lines + ([f"  ACCEPT: {p['accept']}"] if p.get("accept") else [])


def _status(pid: str, planned: set[str], kept: list[str] | None) -> str:
    if kept is None:
        return "~"
    if pid in kept:
        return " "
    return "-" if pid in planned else "~"


def apply_approval(text: str, proposals: list[dict], plan: list[dict], kept: list[str] | None) -> str:
    """New proposals into BACKLOG once: approved [ ], struck out [-], not planned / quit [~]."""
    planned = {p["id"] for p in plan}
    return backlog.add_new(text, [(p["id"], p["project"], task_lines(p, _status(p["id"], planned, kept)))
                                  for p in proposals])


def format_plan(plan: list[dict]) -> str:
    rows: list[str] = []
    for i, p in enumerate(plan, 1):
        src = "nový" if p["source"] == "new" else "z BACKLOGu"
        rows.append(f"{i}. [{p['project']}] {p['kind'] or '-'} {p['title']} ({src})")
        if p["why"]:
            rows.append(f"     {p['why']}")
    return "\n".join(rows)


def parse_merges(text: str, known: list[Task], ignored: list[list[str]]) -> list[dict]:
    """Groups of 2+ overlapping [~] proposals, each id in at most one group, ignored groups skipped.
    `known` = backlog tasks in any status: the merged title must be new, like a discover proposal."""
    by_id = {t.id: t for t in known if t.status == "~"}
    used, skip, taken = set(), {frozenset(g) for g in ignored}, {t.id for t in known}
    out: list[dict] = []
    for item in json_array(text) or []:
        ids = list(dict.fromkeys(item.get("ids") or [])) if isinstance(item, dict) else []
        title = safe_title(item.get("title"))[:100] if isinstance(item, dict) else ""
        if not title or len(ids) < 2 or not all(i in by_id for i in ids) or used & set(ids) or frozenset(ids) in skip:
            continue
        kinds = {by_id[i].kind for i in ids}
        if len(kinds) > 1:  # a bug and a refactor are separate changes, even in the same file
            continue
        project = by_id[ids[0]].project
        if proposal_id(project, title) in taken:
            continue
        used |= set(ids)
        out.append({"id": proposal_id(project, title), "project": project, "ids": ids,
                    "titles": [by_id[i].title for i in ids], "title": title, "kind": kinds.pop(),
                    "description": str(item.get("description") or "").strip(),
                    "why": _line(item.get("why")), "done": _line(item.get("done")), "accept": ""})
    return out


def apply_merge(text: str, merge: dict) -> str:
    """The merged proposal as a new [~], its originals [-] with a note the reflection skips."""
    text = backlog.add_tasks(text, merge["project"], task_lines(merge, "~"))
    for tid in merge["ids"]:
        text = backlog.set_status(text, tid, "-", f" — {MERGED_NOTE} {merge['id']}")
    return text

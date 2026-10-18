"""Phase 5: owner feedback → lessons in the LangGraph Store → prompts.

Feedback is derived from BACKLOG.md (rejected / failed / unclear tasks) and from PRs of finished tasks; the
Store remembers which feedback a reflection already consumed. A lesson starts `proposed` and reaches prompts
only after the owner approves it (`active`); rejected or retired lessons keep their key so they never return."""
import hashlib
import time
from collections import Counter
from typing import Callable

from langgraph.config import get_store

from . import backlog
from .discover import MERGED_NOTE, json_array, proposal_id, safe_title, task_lines
from .traces import pr_urls

LESSONS = "lessons"
SEEN = ("feedback",)
ALL = "*"  # lessons for every project
HARNESS = "harness"
MAX_ACTIVE = 15
MAX_TEXT = 300
FEEDBACK_KIND = {"-": "rejected", "!": "failure", "?": "unclear"}
STATUS_WORD = {"x": "hotovo", "-": "zamítnuto", "!": "selhalo", "?": "nejasné", "~": "čeká", " ": "ve frontě"}


def current_store():
    """The Store of the running graph; None outside a run (node unit tests, CLI helpers)."""
    try:
        return get_store()
    except RuntimeError:
        return None


# --- lessons --------------------------------------------------------------------------------------------

def lessons(store, project: str | None = None, status: str | None = None) -> list[dict]:
    if store is None:
        return []
    ns = (LESSONS,) if project is None else (LESSONS, project)
    return [{"id": i.key, "project": i.namespace[1], **i.value} for i in store.search(ns, limit=10_000)
            if status is None or i.value["status"] == status]


def active_texts(store, project: str) -> list[str]:
    return [lesson["text"] for p in (project, ALL) for lesson in lessons(store, p, "active")]


def block(texts: list[str]) -> str:
    if not texts:
        return ""
    return "\nLessons from earlier runs, approved by the owner. Follow them:\n" + "\n".join(f"- {t}" for t in texts) + "\n"


def propose(store, project: str, text: str, source: list[str]) -> str | None:
    key = hashlib.sha1(f"{project}\n{text}".encode()).hexdigest()[:8]
    if store.get((LESSONS, project), key) is not None:
        return None
    store.put((LESSONS, project), key, {"text": text, "source": source, "status": "proposed", "created": time.time()})
    return key


def decide(store, project: str, key: str, status: str) -> str:
    """status: active (approve) or retired (reject a proposal / retire an active lesson)."""
    item = store.get((LESSONS, project), key)
    if item is None:
        return "lekce neexistuje"
    if item.value["status"] == status:
        return "už vyřízeno"
    if status == "active" and len(lessons(store, project, "active")) >= MAX_ACTIVE:
        return f"{project} už má {MAX_ACTIVE} aktivních lekcí, nejdřív nějakou vyřaď"
    store.put((LESSONS, project), key, {**item.value, "status": status})
    return "lekce platí" if status == "active" else "lekce vyřazena"


# --- feedback -------------------------------------------------------------------------------------------

def gather_feedback(text: str, seen: set[str], pr_info: Callable[[str], tuple[str, str] | None]
                    ) -> tuple[list[dict], list[str]]:
    """New feedback items and keys checked without anything to learn (merged PR, no comments).
    `pr_info(url)` → (state, review text) or None when gh fails; open PRs are checked again next time."""
    items, quiet = [], []
    urls = pr_urls(text)
    for t in backlog.parse(text):
        if t.status == "-" and t.note.startswith(MERGED_NOTE):
            continue  # folded into a merged proposal, not rejected
        if t.status in FEEDBACK_KIND:
            key = f"{FEEDBACK_KIND[t.status]}:{t.id}"
            if key not in seen:
                items.append({"key": key, "project": t.project, "task": t.id, "kind": FEEDBACK_KIND[t.status],
                              "text": f"{t.title} — {t.note}" if t.note else t.title})
        elif t.status == "x" and (url := urls.get(t.id)) and (key := f"pr:{t.id}") not in seen:
            info = pr_info(url)
            if info is None or info[0] not in ("merged", "closed"):
                continue
            state, comments = info
            if state == "merged" and not comments:
                quiet.append(key)
                continue
            items.append({"key": key, "project": t.project, "task": t.id,
                          "kind": "closed" if state == "closed" else "review",
                          "text": f"{t.title} — PR {state}" + (f": {comments}" if comments else "")})
    return items, quiet


def parse_reflection(text: str, items: list[dict]) -> list[dict]:
    """Model answer → lessons for projects that had feedback (or `*`), and prompt changes for the harness."""
    projects, keys = {i["project"] for i in items} | {ALL}, {i["key"] for i in items}
    out = []
    for x in json_array(text) or []:
        if not isinstance(x, dict):
            continue
        body, scope = " ".join(str(x.get("text") or "").split())[:MAX_TEXT], x.get("scope", "project")
        project = x.get("project") if scope == "project" else HARNESS
        if not isinstance(scope, str) or not isinstance(project, str):
            continue
        if not body or scope not in ("project", "prompt") or project not in projects | {HARNESS}:
            continue
        out.append({"project": project, "scope": scope, "text": body,
                    "title": " ".join(str(x.get("title") or body).split())[:80],
                    "source": [k for k in _as_list(x.get("source")) if isinstance(k, str) and k in keys]})
    return out


def _as_list(raw) -> list:
    """Model may send a string or garbage where a list is expected."""
    return [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []


def harness_task(item: dict) -> tuple[str, list[str]]:
    """A change to the harness itself (prompts or code) as a [~] proposal in its BACKLOG section."""
    title = safe_title(item["title"])
    pid = proposal_id(HARNESS, title)
    p = {"id": pid, "title": title, "description": f"{item['text']}\nZdroj: {', '.join(item['source']) or '-'}",
         "done": "Změna v promptech nebo kódu harnessu (ne guard/sdk/quota/projects.yaml), pytest zelený."}
    return pid, task_lines(p, "~")


def owner_history(tasks: list[backlog.Task]) -> dict[str, dict[str, int]]:
    """Per project: how the owner treated tasks so far (input for prioritize, no formula)."""
    out: dict[str, Counter] = {}
    for t in tasks:
        out.setdefault(t.project, Counter())[STATUS_WORD[t.status]] += 1
    return {p: dict(c) for p, c in out.items()}

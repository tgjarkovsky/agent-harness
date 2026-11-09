"""Task suggestions from new e-mails and calendar events: the prompt for a tool-less model and its answer
turned into Obsidian Tasks lines. Links in a suggestion are only ever ones found in the source e-mail."""
import hashlib
from datetime import date

from . import agenda
from .discover import json_array, safe_title
from .gcal import Event
from .mail import Mail
from .sdk import render_prompt

WEEKDAYS = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]
PRIORITY_SIGN = {"high": " ⏫", "low": " 🔽"}
MAX_TASKS = 50
TITLE_STRIP = str.maketrans("", "", "#📅⏳✅🛫➕🔁🔺⏫🔼🔽")


def event_key(e: Event) -> str:
    return f"cal:{e.start.isoformat(timespec='minutes')}:{e.title}"


def _mail_block(i: int, m: Mail) -> str:
    links = "".join(f"\n  {n}) {u}" for n, u in enumerate(m.links, 1))
    return (f"[m{i}] {m.date:%Y-%m-%d %H:%M} · Od: {m.sender} · Předmět: {m.subject}\n{m.body}"
            + (f"\nOdkazy:{links}" if links else ""))


def _event_line(i: int, e: Event) -> str:
    when = "celý den" if e.all_day else f"{e.start:%H:%M}–{e.end:%H:%M}"
    return f"[c{i}] {e.start:%Y-%m-%d} {when} {e.title}"


def prompt(mails: list[Mail], events: list[Event], tasks: list[agenda.Item], today: date, projects) -> str:
    return render_prompt(
        "suggest_tasks.md", today=today.isoformat(), weekday=WEEKDAYS[today.weekday()],
        projects=", ".join(projects) or "(none)",
        mails="\n\n".join(_mail_block(i, m) for i, m in enumerate(mails, 1)) or "(none)",
        events="\n".join(_event_line(i, e) for i, e in enumerate(events, 1)) or "(none)",
        tasks="\n".join(f"- {t.title}" + (f" (📅 {t.due})" if t.due else "") for t in tasks[:MAX_TASKS]) or "(none)")


def _date(value, today: date) -> date | None:
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        return None
    return d if d >= today else None


def _source(item: dict, mails: list[Mail], events: list[Event]) -> tuple[str, Mail | None, Event | None] | None:
    src = str(item.get("source") or "")
    try:
        n = int(src[1:]) - 1
    except ValueError:
        return None
    if src.startswith("m") and 0 <= n < len(mails):
        return f"mail:{mails[n].msgid}", mails[n], None
    if src.startswith("c") and 0 <= n < len(events):
        return event_key(events[n]), None, events[n]
    return None


def _picked_links(item: dict, m: Mail | None) -> list[str]:
    if m is None or not isinstance(item.get("links"), list):
        return []
    picked = [m.links[n - 1] for n in item["links"] if isinstance(n, int) and 1 <= n <= len(m.links)]
    return list(dict.fromkeys(picked))[:3]


def parse(text: str, mails: list[Mail], events: list[Event], today: date, projects) -> list[dict]:
    """Valid suggestions as {id, source, line, title, why, origin, links}; anything malformed is dropped."""
    out, seen = [], set()
    for item in json_array(text) or []:
        if not isinstance(item, dict) or not (src := _source(item, mails, events)):
            continue
        key, m, e = src
        # nothing in the title may read as a Tasks field, a tag or a link
        title = " ".join(safe_title(item.get("title")).translate(TITLE_STRIP).replace("[", "(").replace("]", ")").split())[:100]
        if not title:
            continue
        sid = hashlib.sha1(f"{key}|{title}".encode()).hexdigest()[:10]
        if sid in seen:
            continue
        seen.add(sid)
        project = item.get("project") if item.get("project") in projects else None
        due, scheduled = _date(item.get("due"), today), _date(item.get("scheduled"), today)
        links = _picked_links(item, m)
        line = (title + (f" #{project}" if project else "") + (f" ⏳ {scheduled}" if scheduled else "")
                + (f" 📅 {due}" if due else "") + PRIORITY_SIGN.get(item.get("priority"), "")
                + (f" [✉️ mail]({m.url})" if m else "") + "".join(f" [odkaz {i}]({u})" for i, u in enumerate(links, 1)))
        origin = f"✉️ {m.sender} · {m.subject}" if m else f"📅 {e.start:%d. %m.}{'' if e.all_day else f' {e.start:%H:%M}'} {e.title}"
        out.append({"id": sid, "source": key, "line": line, "title": title, "why": " ".join(str(item.get("why") or "").split())[:300],
                    "origin": origin, "mail_url": m.url if m else None})
    return out

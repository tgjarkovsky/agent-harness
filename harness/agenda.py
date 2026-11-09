"""Personal tasks in the secondbrain TODO.md, Obsidian Tasks format.

Only the fields harness needs are parsed; the rest of a line is left untouched, so Obsidian and harness can
both edit the file."""
import calendar
import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

TASK_RE = re.compile(r"^\s*[-*] \[(.)\] (.*)$")
DATE_FIELD = {"due": "📅", "scheduled": "⏳", "done_on": "✅"}
PRIORITY = {"🔺": 0, "⏫": 1, "🔼": 2, "🔽": 4}  # no sign = 3, like the Tasks plugin
NO_PRIORITY = 3
RECUR_RE = re.compile(r"🔁\s*(every\b[^📅⏳✅🛫➕🔺⏫🔼🔽#]*)")
RECUR_STEP_RE = re.compile(r"every\s+(?:(\d+)\s+)?(day|week|month|year)s?\b", re.IGNORECASE)
TAG_RE = re.compile(r"(?<!\S)#([\w/-]+)")
META_RE = re.compile(r"[📅⏳✅🛫➕]\s*\d{4}-\d{2}-\d{2}|🔁\s*every\b[^📅⏳✅🛫➕🔺⏫🔼🔽#]*|[🔺⏫🔼🔽]")
LINK_RE = re.compile(r"\[([^\]\n]*)\]\((https?://[^)\s]+)\)")
HEADER = "# TODO\n\n"


@dataclass(frozen=True)
class Item:
    line_no: int
    line: str
    status: str  # " " open, "x" done, "-" cancelled
    title: str  # without dates, priority, recurrence and tags
    tags: tuple[str, ...]
    due: date | None
    scheduled: date | None
    done_on: date | None
    priority: int
    recur: str | None
    links: tuple[tuple[str, str], ...] = ()  # markdown [label](https://…) in the line, e.g. the source e-mail

    @property
    def key(self) -> str:
        """Stable id of the line for buttons: line numbers shift when the file is edited in Obsidian."""
        return hashlib.sha1(self.line.strip().encode()).hexdigest()[:10]

    @property
    def when(self) -> date | None:
        return self.due or self.scheduled

    def project(self, projects) -> str | None:
        return next((t for t in self.tags if t in projects), None)


def _date(body: str, sign: str) -> date | None:
    m = re.search(sign + r"\s*(\d{4}-\d{2}-\d{2})", body)
    return date.fromisoformat(m[1]) if m else None


def parse_line(line_no: int, line: str) -> Item | None:
    m = TASK_RE.match(line)
    if not m:
        return None
    body = m[2]
    recur = RECUR_RE.search(body)
    links = tuple((label, url) for label, url in LINK_RE.findall(body))
    plain = LINK_RE.sub("", body)
    title = " ".join(TAG_RE.sub("", META_RE.sub("", plain)).split())
    return Item(line_no, line, m[1].lower(), title, tuple(TAG_RE.findall(plain)),
                *(_date(body, s) for s in DATE_FIELD.values()),
                next((p for s, p in PRIORITY.items() if s in body), NO_PRIORITY),
                recur[1].strip() if recur else None, links)


def parse(text: str) -> list[Item]:
    return [i for n, line in enumerate(text.splitlines()) if (i := parse_line(n, line))]


def find(text: str, key: str) -> Item | None:
    return next((i for i in parse(text) if i.key == key), None)


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def next_date(d: date, recur: str) -> date | None:
    m = RECUR_STEP_RE.search(recur)
    if not m:
        return None  # e.g. "every weekday": left to Obsidian
    n, unit = int(m[1] or 1), m[2].lower()
    if unit == "day":
        return d + timedelta(days=n)
    if unit == "week":
        return d + timedelta(weeks=n)
    return _add_months(d, n * (12 if unit == "year" else 1))


def _next_occurrence(item: Item, today: date) -> str | None:
    """The new open line Obsidian Tasks would add for a recurring task, or None."""
    if not item.recur:
        return None
    line = item.line
    for field, sign in (("due", "📅"), ("scheduled", "⏳")):
        old = getattr(item, field)
        base = today if "when done" in item.recur else old
        new = old and next_date(base, item.recur)
        if old and new:
            line = line.replace(f"{sign} {old.isoformat()}", f"{sign} {new.isoformat()}")
    if not (item.due or item.scheduled) and (new := next_date(today, item.recur)):
        line = line.rstrip() + f" ⏳ {new.isoformat()}"
    return line if line != item.line else None


def complete(text: str, key: str, today: date) -> tuple[str, Item | None]:
    """Mark the task done (✅ today); a recurring one gets its next occurrence above, as in Obsidian Tasks."""
    item = find(text, key)
    if item is None or item.status != " ":
        return text, None
    lines = text.splitlines(keepends=True)
    eol = lines[item.line_no][len(lines[item.line_no].rstrip("\r\n")):] or "\n"
    done = re.sub(r"\[ \]", "[x]", item.line, count=1).rstrip() + f" ✅ {today.isoformat()}"
    new = [done + eol]
    if nxt := _next_occurrence(item, today):
        new.insert(0, nxt + eol)
    lines[item.line_no:item.line_no + 1] = new
    return "".join(lines), item


def add(text: str, body: str) -> str:
    text = text or HEADER
    return text + ("" if text.endswith("\n") else "\n") + f"- [ ] {body.strip()}\n"


def today_items(items: list[Item], today: date) -> list[Item]:
    """Open tasks for today: overdue, due or scheduled today, and the high-priority undated ones."""
    picked = [i for i in items if i.status == " "
              and ((i.when and i.when <= today) or (not i.when and i.priority <= PRIORITY["⏫"]))]
    return sorted(picked, key=lambda i: (i.when or today, i.priority, i.line_no))


def due_between(items: list[Item], first: date, last: date) -> list[Item]:
    """Open tasks with a deadline (📅) from `first` to `last`, soonest first."""
    return sorted((i for i in items if i.status == " " and i.due and first <= i.due <= last),
                  key=lambda i: (i.due, i.priority, i.line_no))



@dataclass(frozen=True)
class Block:
    start: datetime
    end: datetime
    item: Item


def plan_blocks(items: list[Item], slots: list[tuple[datetime, datetime]], length: timedelta,
                min_len: timedelta = timedelta(minutes=30)) -> list[Block]:
    """Tasks in the given order, each into the earliest free time, at most `length` long."""
    # ponytail: greedy fill without per-task estimates; let a model pick when this stops fitting the day
    free, blocks = list(slots), []
    for item in items:
        for n, (start, end) in enumerate(free):
            if end - start >= min_len:
                stop = min(start + length, end)
                blocks.append(Block(start, stop, item))
                free[n] = (stop, end)
                break
    return blocks

# --- capture from chat ------------------------------------------------------------------------------------

WEEKDAYS = {"po": 0, "pondělí": 0, "út": 1, "úterý": 1, "st": 2, "středa": 2, "středu": 2, "čt": 3, "čtvrtek": 3,
            "pá": 4, "pátek": 4, "so": 5, "sobota": 5, "sobotu": 5, "ne": 6, "neděle": 6, "neděli": 6}
RELATIVE = {"dnes": 0, "zítra": 1, "pozítří": 2}
WORD_RE = re.compile(r"(?<!\S)(\d{4}-\d{2}-\d{2}|[^\W\d_]+)(?!\S)")


def _word_date(word: str, today: date) -> date | None:
    w = word.lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", w):
        try:
            return date.fromisoformat(w)
        except ValueError:
            return None
    if w in RELATIVE:
        return today + timedelta(days=RELATIVE[w])
    if w in WEEKDAYS:
        return today + timedelta(days=(WEEKDAYS[w] - today.weekday() - 1) % 7 + 1)  # the next one, never today
    return None


def capture(text: str, today: date) -> str:
    """Chat text → task body: the last date word (dnes, zítra, pá, 2026-11-12) becomes 📅, the rest stays."""
    if "📅" in text:
        return " ".join(text.split())
    hits = [(m, d) for m in WORD_RE.finditer(text) if (d := _word_date(m[1], today))]
    if not hits:
        return " ".join(text.split())
    m, d = hits[-1]
    return " ".join((text[:m.start()] + text[m.end():]).split()) + f" 📅 {d.isoformat()}"


# --- file ---------------------------------------------------------------------------------------------------

def read(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write(path: Path, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8", newline="")  # keep LF: Obsidian and git see no churn

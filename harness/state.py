"""Owner state shared between machines: BACKLOG.md and lessons on the git branch `harness-state`.

The branch is cloned into `<home>/state/` (ignored by the code checkout). History, traces, costs and checkpoints stay
in each machine's harness.db. Sync = commit local changes, fetch, merge (same line changed on both sides: the local
side wins), import lessons into the Store, push. Offline or failing git never stops the harness: it retries later."""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import backlog, lessons

BRANCH = "harness-state"
DIR = "state"
BACKLOG = "BACKLOG.md"
LESSONS_FILE = "lessons.jsonl"
NAMESPACES = ((lessons.LESSONS,), lessons.SEEN)
RANK = {"proposed": 0, "active": 1, "retired": 2}  # lessons only move forward
EVERY = 60  # s between background syncs
DEBOUNCE = 5  # s after a BACKLOG edit


class StateError(RuntimeError):
    pass


def state_dir(home: Path) -> Path:
    return Path(home) / DIR


def enabled(home: Path) -> bool:
    return (state_dir(home) / ".git").exists()


def backlog_path(home: Path) -> Path:
    """state/BACKLOG.md once `harness state init` ran, the old BACKLOG.md in the code checkout before that."""
    return state_dir(home) / BACKLOG if enabled(home) else Path(home) / BACKLOG


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120)
    if check and r.returncode:
        raise StateError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[-500:]}")
    return r


# --- lessons <-> lessons.jsonl ----------------------------------------------------------------------------

def _store_items(store) -> dict[tuple, dict]:
    out = {}
    for ns in NAMESPACES:
        for i in store.search(ns, limit=1_000_000):
            out[(tuple(i.namespace), i.key)] = i.value
    return out


def _read_file(path: Path) -> dict[tuple, dict]:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            out[(tuple(r["ns"]), r["key"])] = r["value"]
        except (ValueError, KeyError, TypeError):
            continue  # a broken line must not lose the rest
    return out


def _newer(a: dict, b: dict) -> dict:
    """Pick the further lesson status; feedback marks (no status) are equal, keep the first."""
    ra, rb = RANK.get(a.get("status"), -1), RANK.get(b.get("status"), -1)
    return b if rb > ra else a


def merge_lessons(store, path: Path) -> int:
    """File → Store for missing or advanced items, then write the union back. Returns items imported."""
    mine, theirs = _store_items(store), _read_file(path)
    imported = 0
    for (ns, key), value in theirs.items():
        if (ns, key) not in mine or _newer(mine[(ns, key)], value) is not mine[(ns, key)]:
            store.put(ns, key, value)
            mine[(ns, key)] = value
            imported += 1
    text = _lesson_lines(mine)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8", newline="\n")
    return imported


# --- merge drivers (git calls them per file; line merges lose edits on adjacent tasks) ----------------------

ATTRIBUTES = f"{BACKLOG} merge=harness-backlog\n{LESSONS_FILE} merge=harness-lessons\n"


def _blocks(text: str) -> list[tuple[str, str]]:
    """Ordered (key, text): `pre` preamble, `h:<project>` header, `t:<id>` task with its description lines."""
    out: list[tuple[str, str]] = []
    key, lines = "pre", []
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        if (m := backlog.HEADER_RE.match(bare)) or (m := backlog.TASK_RE.match(bare)):
            out.append((key, "".join(lines)))
            key, lines = (f"h:{m['project']}" if "project" in m.groupdict() else f"t:{m['id']}"), []
        lines.append(line)
    out.append((key, "".join(lines)))
    return [(k, t) for k, t in out if t or k == "pre"]


def merge_backlog(base: str, ours: str, theirs: str) -> str:
    """Three-way merge per task: a side's change wins over no change, both changed → ours, edit beats delete."""
    b, o, t = dict(_blocks(base)), _blocks(ours), _blocks(theirs)
    od, td = dict(o), dict(t)

    def pick(k: str) -> str | None:
        bv, ov, tv = b.get(k), od.get(k), td.get(k)
        if ov == tv or tv == bv:
            return ov
        if ov == bv:
            return tv
        return ov if ov is not None else tv

    keys = [k for k, _ in o]
    pos = -1
    for k, _ in t:  # their new blocks go after their predecessor
        if k in keys:
            pos = keys.index(k)
        else:
            pos += 1
            keys.insert(pos, k)
    merged = [pick(k) for k in keys]
    text = "".join(x for x in merged if x)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def _lesson_lines(items: dict[tuple, dict]) -> str:
    lines = [json.dumps({"ns": list(ns), "key": key, "value": value}, ensure_ascii=False, sort_keys=True)
             for (ns, key), value in sorted(items.items(), key=lambda kv: (kv[0][0], kv[0][1]))]
    return "\n".join(lines) + ("\n" if lines else "")


def merge_lessons_files(ours: Path, theirs: Path) -> str:
    items = _read_file(ours)
    for k, v in _read_file(theirs).items():
        items[k] = _newer(items[k], v) if k in items else v
    return _lesson_lines(items)


def _driver_main(argv: list[str]) -> int:
    kind, base, ours, theirs = argv
    o = Path(ours)
    if kind == "backlog":
        text = merge_backlog(*(Path(x).read_text(encoding="utf-8") for x in (base, ours, theirs)))
    else:
        text = merge_lessons_files(o, Path(theirs))
    o.write_bytes(text.encode("utf-8"))
    return 0


def _ensure_drivers(d: Path) -> None:
    py = Path(sys.executable).as_posix()
    for name in ("backlog", "lessons"):
        _git(["config", f"merge.harness-{name}.driver", f'"{py}" -m harness.state {name} %O %A %B'], d)
    attrs = d / ".gitattributes"
    if not attrs.exists() or attrs.read_text(encoding="utf-8") != ATTRIBUTES:
        attrs.write_text(ATTRIBUTES, encoding="utf-8", newline="\n")


# --- sync -------------------------------------------------------------------------------------------------

def _commit(d: Path, message: str) -> bool:
    _git(["add", "-A"], d)
    if not _git(["status", "--porcelain"], d).stdout.strip():
        return False
    _git(["-c", "user.name=harness", "-c", "user.email=harness@localhost", "commit", "-q", "-m", message], d)
    return True


def sync(home: Path, store=None, host: str = "") -> str:
    """One round trip. Returns a short summary; raises StateError when git fails (caller logs and retries)."""
    d = state_dir(home)
    if not enabled(home):
        return "state: not initialised (harness state init)"
    lessons_path = d / LESSONS_FILE
    with backlog.LOCK:
        _ensure_drivers(d)
        if store is not None:
            merge_lessons(store, lessons_path)
        _commit(d, f"state: {host or 'local'} {time.strftime('%Y-%m-%d %H:%M:%S')}")
    _git(["fetch", "-q", "origin", BRANCH], d)
    with backlog.LOCK:
        _commit(d, f"state: {host or 'local'} {time.strftime('%Y-%m-%d %H:%M:%S')}")  # edits made during fetch
        if _git(["rev-list", "--count", f"HEAD..origin/{BRANCH}"], d).stdout.strip() != "0":
            r = _git(["merge", "-q", "--no-edit", "-X", "ours", f"origin/{BRANCH}"], d, check=False)
            if r.returncode:
                _git(["merge", "--abort"], d, check=False)
                raise StateError(f"merge failed: {(r.stderr or r.stdout).strip()[-500:]}")
        if store is not None:
            merge_lessons(store, lessons_path)
            _commit(d, f"state: lessons merged on {host or 'local'}")
    ahead = _git(["rev-list", "--count", f"origin/{BRANCH}..HEAD"], d).stdout.strip()
    if ahead != "0":
        _git(["push", "-q", "origin", f"HEAD:{BRANCH}"], d)
    return f"state: synced ({ahead} pushed)"


class Syncer:
    """Background sync: every EVERY seconds and DEBOUNCE seconds after a BACKLOG edit."""

    def __init__(self, home: Path, store=None, every: float = EVERY, debounce: float = DEBOUNCE):
        self.home, self.store, self.every, self.debounce = Path(home), store, every, debounce
        self._dirty_at: float | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    def touch(self) -> None:
        if self._dirty_at is None:
            self._dirty_at = time.monotonic()
        self._wake.set()

    def run_once(self) -> None:
        self._dirty_at = None
        try:
            sync(self.home, self.store, _host())
            if self.last_error:
                print("state: sync ok again", file=sys.stderr)
            self.last_error = None
        except (StateError, OSError, subprocess.SubprocessError) as e:
            if str(e) != self.last_error:
                print(f"state: {e}", file=sys.stderr)
            self.last_error = str(e)

    def _loop(self) -> None:
        next_at = time.monotonic() + self.every
        while not self._stop.is_set():
            now = time.monotonic()
            due = next_at if self._dirty_at is None else min(next_at, self._dirty_at + self.debounce)
            self._wake.wait(max(0.0, due - now))
            self._wake.clear()
            if self._stop.is_set():
                break
            now = time.monotonic()
            if now >= next_at or (self._dirty_at is not None and now >= self._dirty_at + self.debounce):
                self.run_once()
                next_at = time.monotonic() + self.every

    def start(self) -> "Syncer":
        backlog.ON_CHANGE.append(self.touch)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="state-sync")
        self._thread.start()
        return self

    def stop(self) -> None:
        if self.touch in backlog.ON_CHANGE:
            backlog.ON_CHANGE.remove(self.touch)
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)


def _host() -> str:
    import platform
    return platform.node().split(".")[0]


@contextmanager
def session(home: Path, store=None):
    """Pull before a command, sync in the background while it runs, push at the end. No-op before init."""
    if not enabled(home):
        yield None
        return
    syncer = Syncer(home, store)
    syncer.run_once()
    syncer.start()
    try:
        yield syncer
    finally:
        syncer.stop()
        syncer.run_once()


# --- init -------------------------------------------------------------------------------------------------

def _remote_url(home: Path) -> str:
    return _git(["remote", "get-url", "origin"], home).stdout.strip()


def remote_has_branch(url: str, cwd: Path) -> bool:
    return bool(_git(["ls-remote", "--heads", url, BRANCH], cwd).stdout.strip())


def init(home: Path, store=None, create: bool = False, url: str | None = None) -> str:
    """Clone the state branch into state/; with create=True start it from this machine's BACKLOG.md and lessons."""
    home = Path(home)
    d = state_dir(home)
    if enabled(home):
        return f"{d} už existuje, synchronizuju: " + sync(home, store, _host())
    if d.exists() and any(d.iterdir()):
        raise StateError(f"{d} existuje a není prázdná")
    url = url or _remote_url(home)
    if remote_has_branch(url, home):
        if create:
            raise StateError(f"větev {BRANCH} už na origin je; spusť `harness state init` bez --create")
        _git(["clone", "-q", "--single-branch", "--branch", BRANCH, url, str(d)], home)
        imported = merge_lessons(store, d / LESSONS_FILE) if store is not None else 0
        sync(home, store, _host())
        return f"stav stažen do {d} (lekcí a značek importováno: {imported})"
    if not create:
        raise StateError(f"větev {BRANCH} na origin ještě není. Založ ji na stroji s aktuálním BACKLOG.md "
                         f"příkazem `harness state init --create`.")
    old = home / BACKLOG
    if not old.exists():
        raise StateError(f"{old} neexistuje, není z čeho založit stav")
    tmp = Path(tempfile.mkdtemp(prefix="harness-state-"))
    try:
        _git(["init", "-q", "-b", BRANCH], tmp)
        _ensure_drivers(tmp)
        shutil.copyfile(old, tmp / BACKLOG)
        if store is not None:
            merge_lessons(store, tmp / LESSONS_FILE)
        _commit(tmp, f"state: založeno z {_host() or 'local'}")
        _git(["remote", "add", "origin", url], tmp)
        _git(["push", "-q", "-u", "origin", BRANCH], tmp)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    shutil.rmtree(tmp, ignore_errors=True)
    _git(["clone", "-q", "--single-branch", "--branch", BRANCH, url, str(d)], home)
    restored = _git(["checkout", "--", BACKLOG], home, check=False).returncode == 0
    note = " BACKLOG.md v kódu vrácen na verzi z gitu (už se nepoužívá)." if restored else ""
    return f"stav založen ve větvi {BRANCH} a stažen do {d}.{note}"


if __name__ == "__main__":  # git merge driver: python -m harness.state backlog|lessons %O %A %B
    sys.exit(_driver_main(sys.argv[1:]))

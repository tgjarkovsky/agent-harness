import json
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path

SCHEMA = """CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  ts REAL NOT NULL,
  node TEXT,
  worker TEXT,
  kind TEXT NOT NULL,
  delta TEXT,
  usage TEXT,
  rate_limit TEXT,
  simulated INTEGER NOT NULL DEFAULT 0
)"""
JSON_FIELDS = ("delta", "usage", "rate_limit")
_lock = threading.Lock()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(SCHEMA)
    if "worker" not in {r["name"] for r in conn.execute("PRAGMA table_info(events)")}:
        conn.execute("ALTER TABLE events ADD COLUMN worker TEXT")  # phase-1 database
    from . import traces  # traces uses this module's lock
    traces.ensure(conn)
    conn.commit()
    return conn


def _dump(value):
    return None if value is None else json.dumps(value, default=str, ensure_ascii=False)


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in JSON_FIELDS:
        d[k] = json.loads(d[k]) if d[k] else None
    d["simulated"] = bool(d["simulated"])
    return d


def log(conn, run_id, kind, node=None, delta=None, usage=None, rate_limit=None, simulated=False, worker=None) -> None:
    with _lock:
        conn.execute(
            "INSERT INTO events (run_id, ts, node, worker, kind, delta, usage, rate_limit, simulated) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, time.time(), node, worker, kind, _dump(delta), _dump(usage), _dump(rate_limit), int(simulated)),
        )
        conn.commit()


def events_after(conn, after_id: int = 0, run_id: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM events WHERE id > ?", [after_id]
    if run_id:
        sql, args = sql + " AND run_id = ?", args + [run_id]
    with _lock:
        rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    return [_row(r) for r in rows]


def since(conn, ts: float, simulated: bool, until: float = float("inf")) -> list[dict]:
    with _lock:
        rows = conn.execute("SELECT * FROM events WHERE ts >= ? AND ts < ? AND simulated = ? ORDER BY id",
                            (ts, until, int(simulated))).fetchall()
    return [_row(r) for r in rows]


def latest_utilization(conn, simulated: bool, types: tuple[str | None, ...] | None = None) -> dict | None:
    """Newest rate-limit info that carries a utilization (pause events only carry resets_at), with its event ts.
    `types` filters by window type; None in it stands for records without a type (old events, treated as five_hour)."""
    with _lock:
        rows = conn.execute("SELECT ts, rate_limit FROM events WHERE rate_limit IS NOT NULL AND simulated = ? "
                            "ORDER BY id DESC LIMIT 50", (int(simulated),)).fetchall()
    for r in rows:
        info = json.loads(r["rate_limit"])
        if info.get("utilization") is not None and (types is None or info.get("type") in types):
            return {**info, "ts": r["ts"]}
    return None


def last_cleanups(conn, simulated: bool = False) -> dict[str, float]:
    """Project → ts of its last discover whose cleanup lens went through (runner logs delta.cleanup)."""
    with _lock:
        rows = conn.execute("SELECT worker, MAX(ts) AS ts FROM events WHERE node = 'discover' AND kind = 'end' "
                            "AND simulated = ? AND json_extract(delta, '$.cleanup') = 1 GROUP BY worker",
                            (int(simulated),)).fetchall()
    return {r["worker"]: r["ts"] for r in rows}


def runs(conn) -> list[dict]:
    with _lock:
        rows = conn.execute("""
          SELECT run_id, MIN(ts) AS started, MAX(ts) AS last, MAX(simulated) AS simulated,
                 (SELECT kind FROM events e2 WHERE e2.run_id = e.run_id ORDER BY id DESC LIMIT 1) AS last_kind
          FROM events e GROUP BY run_id ORDER BY started DESC""").fetchall()
    return [{**dict(r), "simulated": bool(r["simulated"])} for r in rows]


def usage_summary(conn) -> list[dict]:
    with _lock:
        rows = conn.execute("SELECT ts, usage, simulated FROM events WHERE usage IS NOT NULL").fetchall()
    totals: dict[tuple, dict] = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0, "cache_tokens": 0,
                                                     "cost_usd": 0.0, "paid_usd": 0.0})
    for r in rows:
        day = time.strftime("%Y-%m-%d", time.localtime(r["ts"]))
        for u in json.loads(r["usage"]):
            t = totals[(day, u.get("node"), u.get("model"), bool(r["simulated"]))]
            t["input_tokens"] += u.get("input_tokens") or 0
            t["output_tokens"] += u.get("output_tokens") or 0
            t["cache_tokens"] += (u.get("cache_read_tokens") or 0) + (u.get("cache_write_tokens") or 0)
            t["cost_usd"] += u.get("cost_usd") or 0.0
            if u.get("billing") == "api":  # the rest is a subscription estimate, not money spent
                t["paid_usd"] += u.get("cost_usd") or 0.0
    return [{"day": d, "node": n, "model": m, "simulated": s, **v} for (d, n, m, s), v in sorted(totals.items())]


def latest_rate_limit(conn) -> dict | None:
    """Newest real quota info. The SDK sends utilization only with allowed_warning, so a later plain `allowed`
    in the same window inherits the last known one; pause events (only resets_at) are skipped."""
    with _lock:
        rows = conn.execute("SELECT rate_limit FROM events WHERE rate_limit IS NOT NULL AND simulated = 0 "
                            "ORDER BY id DESC LIMIT 50").fetchall()
    infos = [i for i in (json.loads(r["rate_limit"]) for r in rows) if set(i) != {"resets_at"}]
    if not infos:
        return None
    latest = infos[0]
    if latest.get("utilization") is None:
        same = (i["utilization"] for i in infos if i.get("utilization") is not None
                and (i.get("type"), i.get("resets_at")) == (latest.get("type"), latest.get("resets_at")))
        latest = {**latest, "utilization": next(same, None)}
    return latest

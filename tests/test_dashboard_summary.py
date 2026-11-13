import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from harness import events, traces
from harness.dashboard.app import create_app

ROOT = Path(__file__).resolve().parent.parent


def test_costs_returns_summary(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    conn = events.connect(tmp_path / "harness.db")
    traces.record(conn, {"run_id": "go-1", "worker": "ak-01", "node": "plan", "model": "opus",
                         "input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 0, "cache_write_tokens": 0,
                         "cost_usd": 0.5, "status": "ok"}, [])
    traces._upsert(conn, "ak-01", "akano", "http://pr/1", "merged")
    body = TestClient(create_app(tmp_path)).get("/costs").json()
    assert body["summary"] == traces.summary(conn)
    assert body["summary"] == [{"project": "akano", "plan_model": "opus", "tasks": 1, "merged": 1, "closed": 0,
                                "failed": 0, "unclear": 0, "open": 0, "cost_usd": 0.5, "cost_per_merged": 0.5}]
    assert body["tasks"] and "overhead" in body

import json
import subprocess
from types import SimpleNamespace

import pytest

from harness import traces
from harness.nodes import real
from harness.sdk import render_prompt


def test_recent_prs_parses_and_none_on_failure(monkeypatch):
    monkeypatch.setattr(traces, "_gh_stdout", lambda *a: json.dumps([{"number": 7, "title": "X", "state": "MERGED"}]))
    assert traces.recent_prs(".") == [{"number": 7, "title": "X", "state": "MERGED"}]
    monkeypatch.setattr(traces, "_gh_stdout", lambda *a: None)
    assert traces.recent_prs(".") is None


def test_recent_block_prs_and_commits(monkeypatch):
    monkeypatch.setattr(traces, "recent_prs", lambda d: [{"number": 7, "title": "X", "state": "MERGED"}])
    monkeypatch.setattr(traces, "git_run", lambda a, c: subprocess.CompletedProcess(a, 0, "fix y\n", ""))
    out = real.recent_block(SimpleNamespace(base="main"), ".")
    assert "#7 [merged] X" in out and "fix y" in out


def test_recent_block_gh_failure_keeps_commits(monkeypatch):
    monkeypatch.setattr(traces, "recent_prs", lambda d: None)
    monkeypatch.setattr(traces, "git_run", lambda a, c: subprocess.CompletedProcess(a, 0, "fix y\n", ""))
    assert real.recent_block(SimpleNamespace(base="main"), ".").strip() == "fix y"


@pytest.mark.parametrize("name", ["fix", "feature", "cleanup"])
def test_prompts_contain_recent(name):
    text = render_prompt(f"discover_{name}.md", project="p", max_diff_lines=800, verify="x", known="-",
                         notes="-", lessons="", recent="#7 [merged] X", plan="-")
    assert "#7 [merged] X" in text

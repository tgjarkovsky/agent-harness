import json

from langgraph.store.memory import InMemoryStore

from harness import lessons
from harness.backlog import parse

BACKLOG = """## demo
- [-] P2 d-1 Testy pro lib — zamítnuto: nechci testy bez bugu
- [!] P1 d-2 Oprava — failed: npm test 2 failed
- [?] P1 d-3 Výkon — UNCLEAR: co měřit?
- [x] P1 d-4 Merged tiše → https://github.com/o/demo/pull/4
- [x] P1 d-5 Merged s review → https://github.com/o/demo/pull/5
- [x] P1 d-6 Zavřený → https://github.com/o/demo/pull/6
- [x] P1 d-7 Otevřený → https://github.com/o/demo/pull/7
- [x] P1 d-8 gh selže → https://github.com/o/demo/pull/8
- [ ] P1 d-9 Ve frontě
- [~] P2 d-10 Návrh
"""
PRS = {"4": ("merged", ""), "5": ("merged", "příště menší diff"), "6": ("closed", "duplicitní"), "7": ("open", "")}


def pr_info(url):
    return PRS.get(url.rsplit("/", 1)[-1])


def test_gather_feedback_kinds_and_texts():
    items, quiet = lessons.gather_feedback(BACKLOG, set(), pr_info)
    assert {i["key"]: i["text"] for i in items} == {
        "rejected:d-1": "Testy pro lib — zamítnuto: nechci testy bez bugu",
        "failure:d-2": "Oprava — failed: npm test 2 failed",
        "unclear:d-3": "Výkon — UNCLEAR: co měřit?",
        "pr:d-4": "Merged tiše — PR merged",
        "pr:d-5": "Merged s review — PR merged: příště menší diff",
        "pr:d-6": "Zavřený — PR closed: duplicitní"}
    assert [i["kind"] for i in items if i["key"].startswith("pr:")] == ["merged", "review", "closed"]
    assert quiet == []  # open PR and failed gh: checked again next run
    merged = next(i for i in items if i["key"] == "pr:d-4")
    assert merged["kind"] == "merged" and merged["text"].startswith("Merged tiše — PR merged")


def test_gather_feedback_skips_seen():
    items, quiet = lessons.gather_feedback(BACKLOG, {"rejected:d-1", "pr:d-4", "pr:d-6"}, pr_info)
    assert "rejected:d-1" not in {i["key"] for i in items} and "pr:d-6" not in {i["key"] for i in items}
    assert quiet == []


def test_parse_reflection_filters():
    items = [{"key": "rejected:d-1", "project": "demo"}]
    text = json.dumps([
        {"project": "demo", "text": "  Nenavrhuj\n testy ", "source": ["rejected:d-1", "nope"]},
        {"project": "other", "text": "projekt bez zpětné vazby"},
        {"project": "*", "text": "pro všechny"},
        {"project": "demo", "text": "", "scope": "project"},
        {"project": "demo", "text": "x", "scope": "weird"},
        {"scope": "prompt", "text": "plan.md: číst CLAUDE.md", "title": "Plán čte CLAUDE.md"}])
    out = lessons.parse_reflection(f"Tady:\n```json\n{text}\n```", items)
    assert [(o["project"], o["scope"], o["text"]) for o in out] == [
        ("demo", "project", "Nenavrhuj testy"), ("*", "project", "pro všechny"),
        ("harness", "prompt", "plan.md: číst CLAUDE.md")]
    assert out[0]["source"] == ["rejected:d-1"] and out[2]["title"] == "Plán čte CLAUDE.md"


def test_parse_reflection_tolerates_bad_types():
    items = [{"key": "rejected:x", "project": "demo"}]
    text = json.dumps([
        {"project": "demo", "text": "a", "source": 5},
        {"project": "demo", "text": "b", "source": "rejected:x"},
        {"project": ["a"], "text": "c"},
        {"project": {"a": 1}, "text": "c2"},
        {"project": "demo", "scope": ["project"], "text": "e"},
        {"project": "demo", "text": "d", "source": ["rejected:x", {"k": 1}]}])
    out = lessons.parse_reflection(text, items)
    assert [o["text"] for o in out] == ["a", "b", "d"]
    assert [o["source"] for o in out] == [[], ["rejected:x"], ["rejected:x"]]


def test_propose_decide_and_active_texts():
    store = InMemoryStore()
    key = lessons.propose(store, "demo", "A", ["rejected:d-1"])
    assert lessons.propose(store, "demo", "A", []) is None  # same lesson never twice
    assert lessons.active_texts(store, "demo") == []  # proposed lessons stay out of prompts
    assert lessons.decide(store, "demo", key, "active") == "lekce platí"
    assert lessons.decide(store, "demo", key, "active") == "už vyřízeno"
    lessons.decide(store, "*", lessons.propose(store, "*", "G", []), "active")
    assert lessons.active_texts(store, "demo") == ["A", "G"]
    assert lessons.decide(store, "demo", key, "retired") == "lekce vyřazena"
    assert lessons.active_texts(store, "demo") == ["G"]
    assert lessons.decide(store, "demo", "missing", "active") == "lekce neexistuje"


def test_active_limit():
    store = InMemoryStore()
    for i in range(lessons.MAX_ACTIVE):
        lessons.decide(store, "demo", lessons.propose(store, "demo", f"L{i}", []), "active")
    extra = lessons.propose(store, "demo", "one more", [])
    assert "nejdřív nějakou vyřaď" in lessons.decide(store, "demo", extra, "active")


def test_block_and_harness_task_and_history():
    assert lessons.block([]) == ""
    assert "- A\n" in lessons.block(["A"])
    pid, lines = lessons.harness_task({"title": "T", "text": "popis", "source": ["failure:x"]})
    assert pid.startswith("harness-ai-") and lines[0] == f"- [~] P2 {pid} T"
    assert "  Zdroj: failure:x" in lines
    assert lessons.owner_history(parse(BACKLOG))["demo"] == {"zamítnuto": 1, "selhalo": 1, "nejasné": 1, "hotovo": 5,
                                                           "ve frontě": 1, "čeká": 1}


def test_current_store_outside_graph_is_none():
    assert lessons.current_store() is None


def test_review_text_ignores_bots():
    from harness.traces import review_text
    data = {"reviews": [{"authorAssociation": "OWNER", "body": "menší diff"}, {"authorAssociation": "OWNER", "body": " "}],
            "comments": [{"authorAssociation": "NONE", "body": "[vc]: deploy"}, {"authorAssociation": "OWNER", "body": "díky"}]}
    assert review_text(data).splitlines() == ["menší diff", "díky"]
    assert review_text({}) == ""


def test_parse_reflection_skill():
    items = [{"key": "pr:d-4", "project": "demo"}]
    ok = {"scope": "skill", "project": "demo", "name": "pridani-stranky", "text": "Postup", "source": ["pr:d-4"]}
    bad = [{**ok, "name": n} for n in ("Pridani Stranky", "x" * 41, "", None)] + [{**ok, "project": "*"}, {**ok, "project": "other"}]
    out = lessons.parse_reflection(json.dumps([ok, *bad]), items)
    assert [(o["project"], o["scope"], o["name"], o["title"]) for o in out] == [
        ("demo", "skill", "pridani-stranky", "Skill pridani-stranky")]


def test_make_reflect_skill_proposal(tmp_path, monkeypatch):
    from harness.nodes.common import make_reflect
    path = tmp_path / "BACKLOG.md"
    path.write_bytes(BACKLOG.encode())
    store = InMemoryStore()
    monkeypatch.setattr(lessons, "current_store", lambda: store)
    answer = json.dumps([{"scope": "skill", "project": "demo", "name": "pridani-stranky",
                          "text": "Přidej stránku takto.", "source": ["pr:d-4"]}])
    make_reflect(path, lambda items, known: (answer, {}), pr_info)({"run_id": "r"})
    task = next(t for t in parse(path.read_text(encoding="utf-8")) if t.title == "Skill pridani-stranky")
    assert task.status == "~" and task.project == "demo"
    assert ".claude/skills/pridani-stranky/SKILL.md" in task.description
    assert lessons.lessons(store, "demo") == []

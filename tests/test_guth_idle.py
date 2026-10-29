import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("guth_idle", Path(__file__).parent.parent / "scripts" / "guth_idle.py")
guth_idle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guth_idle)

CS = {"home": {"title": "Ahoj"}, "packages": [{"id": "a1", "price": "2500", "name": "Základní", "time": "90"}]}


def test_texts_skips_data_keys_and_numbers():
    assert guth_idle.texts(CS) == {"home.title": "Ahoj", "packages.0.name": "Základní"}


def test_with_texts_keeps_czech_skeleton_and_data():
    got = guth_idle.with_texts(CS, {"home.title": "Hi", "packages.0.name": "Basic", "packages.0.id": "zz"})
    assert got == {"home": {"title": "Hi"}, "packages": [{"id": "a1", "price": "2500", "name": "Basic", "time": "90"}]}


def test_stale_is_changed_czech_or_missing_english():
    cs = {"a": "Nový", "b": "Stejný", "c": "Portfolio", "d": "Další"}
    prev = {"a": "Starý", "b": "Stejný", "c": "Portfolio", "d": "Další"}
    en = {"a": "Old", "b": "Same", "c": "Portfolio"}
    assert guth_idle.stale(cs, prev, en) == ["a", "d"]
    assert guth_idle.stale(cs, None, en) == ["a", "b", "c", "d"]


def test_needs_text_by_template():
    assert guth_idle.needs_text({"template": "C", "headline": "", "words": ["a."]})
    assert not guth_idle.needs_text({"template": "D", "headline": "", "words": ["Světlo."]})

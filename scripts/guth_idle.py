"""Idle job of the guth-pictures project: keeps the English site texts in step with the Czech ones and writes
texts for story drafts that have none.

Runs from the guth-pictures checkout (projects.yaml → guth-pictures.idle.cmd) and writes straight to the production
KV through its REST API. KV_REST_API_URL / KV_REST_API_TOKEN come from guth-pictures' .env.local.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# leaves that are data, not text: always taken from the Czech side
FIXED = {"id", "publicId", "price", "photo", "service", "focal", "x", "y"}
PHOTO_URL = "https://res.cloudinary.com/{cloud}/image/upload/w_800,c_limit/{id}.jpg"

EN_PROMPT = """Jsi překladatel webu portrétního fotografa Guth Pictures (Praha). Zdrojem pravdy je čeština.
Každá položka níže má cestu (path), český text (cs) a dosavadní anglický text na stejné cestě (en, může být null).
Pro každou položku vrať anglický text: když en věrně odpovídá cs, vrať ho beze změny; jinak cs přelož do přirozené
angličtiny ve stejném tónu. Jména, názvy a značky nepřekládej, prázdný text nech prázdný. Vrať všechny cesty.

{items}"""
EN_SCHEMA = {"type": "object", "required": ["texts"], "properties": {"texts": {"type": "array", "items": {
    "type": "object", "required": ["path", "en"],
    "properties": {"path": {"type": "string"}, "en": {"type": "string"}}}}}}

STORY_PROMPT = """Píšeš krátké texty na Instagram stories fotografa Guth Pictures (portréty, svatby, eventy, larpy; Praha).
Tón: sebevědomý, úsporný, bez emoji, bez vykřičníků. Prohlédni si fotky každého návrhu (nástroj Read) a napiš texty,
které k nim sedí. Šablona C: headline max 2 krátká slova na řádek (zlom řádku znakem \\n), sub max 8 slov.
Šablona D: words 3–4 jednoslovné údery zakončené tečkou, subHeadline max 8 slov. Pro šablonu C nech words prázdné
a subHeadline "", pro D nech headline a sub "". Jazyk podle pole lang.

Návrhy:
{drafts}"""

STORY_SCHEMA = {"type": "object", "properties": {"drafts": {"type": "array", "items": {
    "type": "object", "required": ["id", "headline", "sub", "words", "subHeadline"],
    "properties": {"id": {"type": "string"}, "headline": {"type": "string"}, "sub": {"type": "string"},
                   "words": {"type": "array", "items": {"type": "string"}}, "subHeadline": {"type": "string"}}}}},
    "required": ["drafts"]}


def env_value(path: Path, key: str) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


def kv(env: dict, cmd: str, key: str, body: str | None = None):
    req = urllib.request.Request(f"{env['url']}/{cmd}/{key}", data=body.encode("utf-8") if body is not None else None,
                                 headers={"Authorization": f"Bearer {env['token']}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["result"]


def kv_json(env: dict, key: str):
    raw = kv(env, "get", key)
    return None if raw is None else json.loads(raw)


def kv_put(env: dict, key: str, value) -> None:
    kv(env, "set", key, json.dumps(value, ensure_ascii=False))


def claude_bin() -> str:
    # serve runs from Task Scheduler, whose PATH may lack the user's ~/.local/bin
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")


def ask(prompt: str, schema: dict, cwd: str, tools: str = "") -> dict:
    """claude -p without the owner's settings and hooks; returns the structured output."""
    out = subprocess.run([claude_bin(), "-p", "--setting-sources", "", "--strict-mcp-config", "--tools", tools,
                          "--allowedTools", tools, "--permission-mode", "dontAsk", "--model", "sonnet",
                          "--output-format", "json", "--json-schema", json.dumps(schema)],
                         input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         cwd=cwd, timeout=1800)
    if out.returncode:
        raise RuntimeError(f"claude -p selhal: {out.stderr[-500:] or out.stdout[-500:]}")
    result = json.loads(out.stdout)
    if result.get("is_error") or "structured_output" not in result:
        raise RuntimeError(f"claude -p nevrátil JSON: {str(result.get('result'))[:500]}")
    return result["structured_output"]


def texts(value, path: str = "") -> dict[str, str]:
    """Translatable string leaves by dotted path; FIXED keys and strings without letters are data."""
    if isinstance(value, dict):
        return {p: t for k, v in value.items() if k not in FIXED for p, t in texts(v, f"{path}.{k}".lstrip(".")).items()}
    if isinstance(value, list):
        return {p: t for i, v in enumerate(value) for p, t in texts(v, f"{path}.{i}").items()}
    return {path: value} if isinstance(value, str) and any(c.isalpha() for c in value) else {}


def with_texts(value, new: dict[str, str], path: str = ""):
    """Copy of value with the string leaves listed in new replaced."""
    if isinstance(value, dict):
        return {k: v if k in FIXED else with_texts(v, new, f"{path}.{k}".lstrip(".")) for k, v in value.items()}
    if isinstance(value, list):
        return [with_texts(v, new, f"{path}.{i}") for i, v in enumerate(value)]
    return new.get(path, value)


def stale(cs: dict, prev: dict | None, en: dict) -> list[str]:
    """Paths whose English text has to be (re)checked: Czech changed since the last sync, or English missing."""
    return [p for p, t in cs.items() if prev is None or prev.get(p) != t or p not in en]


def sync_en(env: dict, tmp: str) -> str:
    cs_doc, en_doc, prev_doc = (kv_json(env, k) for k in ("content:cs", "content:en", "content:cs:synced"))
    if cs_doc is None:
        return "EN: content:cs v KV není, nic k překladu."
    cs, en = texts(cs_doc), texts(en_doc or {})
    todo = stale(cs, None if prev_doc is None else texts(prev_doc), en)
    if not todo:
        return "EN: čeština se od minula nezměnila."
    items = json.dumps([{"path": p, "cs": cs[p], "en": en.get(p)} for p in todo], ensure_ascii=False, indent=1)
    got = {t["path"]: t["en"] for t in ask(EN_PROMPT.format(items=items), EN_SCHEMA, tmp)["texts"] if t["path"] in cs}
    if missing := [p for p in todo if p not in got]:
        raise RuntimeError(f"EN: překlad vynechal {len(missing)} textů (např. {missing[0]}), nic jsem nezapsal")
    # the Czech document is the skeleton; English keeps its translation where the Czech text did not change
    new = with_texts(cs_doc, {**{p: en[p] for p in cs if p in en}, **got})
    if en_doc is not None:
        kv_put(env, "content:en:backup", en_doc)
    kv_put(env, "content:en", new)
    kv_put(env, "content:cs:synced", cs_doc)
    changed = sum(got[p] != en.get(p) for p in todo)
    return f"EN: zkontrolováno {len(todo)} textů, změněno {changed} (záloha v content:en:backup)."


def needs_text(d: dict) -> bool:
    return not d.get("headline") if d.get("template") == "C" else not d.get("words")


def download_photos(drafts: list[dict], cloud: str, tmp: str) -> dict[str, list[str]]:
    files = {}
    for d in drafts:
        files[d["id"]] = []
        for i, p in enumerate(d["photos"]):
            path = Path(tmp) / f"{d['id']}-{i}.jpg"
            try:
                urllib.request.urlretrieve(PHOTO_URL.format(cloud=cloud, id=p["publicId"]), path)
            except urllib.error.HTTPError:  # photo deleted from Cloudinary since the draft was made
                continue
            files[d["id"]].append(str(path))
    return {k: v for k, v in files.items() if v}


def fill_stories(env: dict, cloud: str, tmp: str) -> str:
    todo = [d for d in kv_json(env, "stories:drafts") or [] if needs_text(d)]
    if not todo:
        return "Stories: všechny návrhy mají texty."
    files = download_photos(todo, cloud, tmp)
    todo = [d for d in todo if d["id"] in files]
    if not todo:
        return "Stories: návrhům bez textu chybí v Cloudinary všechny fotky."
    listing = "\n".join(f"- id {d['id']}, šablona {d['template']}, lang {d.get('lang', 'cs')}, fotky: "
                        + ", ".join(files[d["id"]]) for d in todo)
    texts = {t["id"]: t for t in ask(STORY_PROMPT.format(drafts=listing), STORY_SCHEMA, tmp, "Read")["drafts"]}
    # re-read: the owner may have edited drafts while Claude was writing
    drafts = kv_json(env, "stories:drafts") or []
    filled = 0
    for d in drafts:
        if needs_text(d) and d["id"] in texts:
            t = texts[d["id"]]
            d.update(headline=t["headline"], sub=t["sub"], words=t["words"], subHeadline=t["subHeadline"], textSource="ai")
            filled += 1
    kv_put(env, "stories:drafts", drafts)
    return f"Stories: doplněny texty u {filled} z {len(todo)} návrhů."


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # serve pipes the output, which on Windows defaults to cp1252
        stream.reconfigure(encoding="utf-8")
    dotenv = Path(".env.local")
    env = {"url": env_value(dotenv, "KV_REST_API_URL"), "token": env_value(dotenv, "KV_REST_API_TOKEN")}
    cloud = env_value(dotenv, "CLOUDINARY_CLOUD_NAME")
    if not (env["url"] and env["token"] and cloud):
        print("KV_REST_API_URL / KV_REST_API_TOKEN / CLOUDINARY_CLOUD_NAME chybí v .env.local", file=sys.stderr)
        return 2
    failed = False
    with tempfile.TemporaryDirectory() as tmp:
        for step in (lambda: sync_en(env, tmp), lambda: fill_stories(env, cloud, tmp)):
            try:
                print(step())
            except Exception as e:  # one failing step must not skip the other
                print(e, file=sys.stderr)
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

# harness

LangGraph orchestrátor: úkol z `BACKLOG.md` → plán (Opus) → implementace (Sonnet) ve worktree → verify → draft PR. Modely běží přes Claude Agent SDK na subscription.

    python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
    .venv/Scripts/harness simulate flaky-tests [--step]
    .venv/Scripts/harness dashboard          # http://127.0.0.1:8765
    .venv/Scripts/harness go                 # Opus navrhne a seřadí úkoly, schválíš plán, workery udělají draft PR
    .venv/Scripts/harness run [--resume RUN_ID]
    .venv/Scripts/harness stats              # cena za úkol (tokeny vč. cache, $), stav PR z gh, režie discover/prioritize
    .venv/Scripts/harness prune              # smaže worktree hotových úkolů
    .venv/Scripts/harness queue              # seřazená fronta (P → fokus ze secondbrainu → weight)
    .venv/Scripts/harness scan [--write]     # TODO/FIXME z origin/<base> → návrhy [~] v BACKLOGu
    .venv/Scripts/harness lessons [--project X] [--retire PROJECT/ID]   # lekce z reflexe (fáze 5)

Fáze 2: `run` zpracovává až `max_parallel` úkolů z různých projektů najednou (LangGraph `Send` → worker subgraf). Návrh `[~]` se nespustí, dokud ho ručně nezměníš na `[ ]`. Běhy z fáze 1 nejdou resumovat novým grafem.

Fáze 3: když plánovač označí úkol za nejasný, worker se zeptá (uzel `clarify`, LangGraph `interrupt`) a běh skončí ve stavu `waiting`. Na dotaz se odpovídá v terminálu (`run` / `simulate`), v dashboardu nebo z Telegramu. Po 2 kolech dotazů nebo po přeskočení se úkol zapíše jako `[?]`. Zamítnutý návrh má stav `[-]`.

Ověření hotovosti: plánovač z „Hotovo =“ v popisu úkolu odvodí řádek `ACCEPT: <příkaz>` (typicky konkrétní nový test). `verify` ho pustí vedle `verify` příkazů projektu a baseline ho nikdy neomluví. Když verify vrátí stejnou chybu jako minule (čísla se ignorují) nebo dojdou pokusy, worker se jednou vrátí do `plan` (uzel `replan`) s minulým plánem a chybou a pokusy začnou znovu. Další neúspěch → `[!]`.

## `harness go`

Jedno kolo bez ručního psaní úkolů. Pro každý projekt souběžně běží `discover`: Opus si v čerstvém read-only worktree z `origin/<base>` projde kód, dokumentaci projektu a poznámky ze secondbrainu (Thoughts/, Inbox/) a navrhne nejvýš 5 úkolů velikosti jednoho PR. `prioritize` pak z návrhů a otevřených úkolů v BACKLOGu sestaví plán o délce `max_tasks_per_run`. Když volání selže, plán sestaví klasické pořadí podle priority.

Plán se ukáže v terminálu. Enter schválí všechno, `2,5` vyškrtne úkoly 2 a 5, `q` běh ukončí. Schválené úkoly se zapíšou jako `[ ]` a workery z nich udělají draft PR. Vyškrtnuté se zapíšou jako `[-]` a už se znovu nenavrhnou. Nevybrané a po `q` se zapíšou jako `[~]` na později. Po rate limitu se pokračuje přes `harness run --resume go-…` a průzkum projektů, které už doběhly, se znovu nespouští. Bez modelu jde vyzkoušet přes `harness simulate go-happy`.

## Observabilita (fáze 4)

Každé volání agenta se zapíše do `harness.db`, i když selže nebo narazí na rate limit:
- tabulka `agent_calls`: uzel, úkol, model, počet tahů, tokeny včetně cache, odhad ceny, stav a `session_id`,
- tabulka `agent_steps`: kroky volání, tedy nástroje a výsledky, zkrácené na 300 znaků, a text agenta na 1 000 znaků.

Plný transkript volání najdeš podle `session_id` v `~/.claude/projects/`. Ke kterému běhu, uzlu a úkolu volání patří, se zjistí z `config["metadata"]` LangGraphu, takže stav grafu kroky neobsahuje.

`harness stats`, stejně jako začátek každého `run`/`go`, si přes `gh` zjistí stav PR úkolů a uloží ho do tabulky `task_outcomes`. Dashboard ukazuje volání agentů vybraného běhu (po kliknutí kroky) a cenu za úkol. Ceny jsou u subscription jen odhad SDK. Pod tabulkou úkolů vypíše `harness stats` souhrn po projektech a modelech plánovače (merged/closed/[!]/[?]/open a cena za zmergovaný PR), z něhož se čte výsledek A/B testu `plan_model`.

## Self-improvement (fáze 5)

Na konci každého běhu proběhne uzel `reflect`. Model se volá, jen když od minulé reflexe přibyla zpětná vazba:
- zamítnuté návrhy `[-]` včetně důvodu,
- selhané `[!]` a nejasné `[?]` úkoly,
- PR zavřené bez merge,
- review komentáře lidí (ne botů).

Opus z toho navrhne krátké lekce pro jednotlivé projekty. Každou schvaluješ ✅/❌ v dashboardu nebo Telegramu. Do promptů `discover`, `plan` a `implement` se dostanou jen schválené lekce, nejvýš 15 na projekt. Lekce i zpracovaná zpětná vazba se ukládají do LangGraph `Store` v `harness.db`. Spravuje je `harness lessons`.

Důvod zamítnutí je nepovinný. V dashboardu se k němu dostaneš kliknutím na ❌, v Telegramu jako reply na zprávu zamítnutého návrhu.

Když chyba leží v promptu harnessu (worker by selhal v jakémkoli projektu), reflexe zapíše návrh `[~]` do sekce `## harness`. Harness je v `projects.yaml` běžný projekt, `discover` nevypíná, takže ho prochází `harness go` i sken TODO. `allowed` jen vylučuje pojistky (`!harness/guard.py`, `!harness/sdk.py`, `!harness/quota.py`, `!projects.yaml`); zbytek kódu včetně promptů worker měnit smí. Výstup je draft PR a merge děláš ty.

`prioritize` navíc dostane, jak jsi dosud naložil s úkoly každého projektu (hotovo, zamítnuto, selhalo, čeká).

## Revize čistoty kódu

Tlačítko 🧹 v dashboardu spustí `go` jen s lensem `cleanup` (prompt `discover_cleanup.md`) pro vybrané projekty. Opus projde celé repo a hledá zamotaný kód, duplicity, špatné hranice modulů, mrtvý kód a zbytečnou složitost. Navrhne nejvýš 5 refaktorů bez změny chování, každý na jeden PR. Návrhy mají v BACKLOGu typ `[refactor]` a plán schvaluješ stejně jako u `go`. Běžné `go` lens `cleanup` nespouští.

Discover, ve kterém lens `cleanup` doběhl bez chyby, uloží do eventu `delta.cleanup = true`. Dashboard u projektu podle toho ukazuje, kolik dní uplynulo od poslední revize a kolik PR se od té doby zmergovalo (`gh pr list --state merged`, cache jako u otevřených PR).

## Dozor nad implement (Jev)

Implementujícího agenta hlídá přes hooky Agent SDK model Jev (TypeSafe, `POST /v1/systemone`, klíč v env `TYPESAFE_API_KEY` nebo `JEV_API_KEY`):
- **Úprava existujícího testu:** před každým Edit/Write testovacího souboru se Jev zeptá, jestli úprava oslabuje kontrolu (smazaný assert, `.skip`, očekávaná hodnota přepsaná na výsledek kódu). Při p ≥ 0.7 se úprava zakáže a agent dostane důvod.
- **Diff testů na konci:** agenti často píšou soubory skriptem přes Bash, tím Edit/Write hook obejdou. Když agent končí, Jev stejnou otázkou posoudí každý existující testový soubor změněný od začátku volání (`git diff`, i commitnuté). Při oslabení se agent jednou vrátí test obnovit. Když oslabení zůstane, zastaví se a úkol jde na `replan` (ani prošlé verify PR neotevře).
- **Postup:** každých 8 nástrojů Jev posoudí poslední kroky: postupuje / opakuje stejnou chybu / mimo plán / obchází problém. Při prvním problémovém verdiktu (confidence ≥ 0.6) agent dostane varování, při druhé se zastaví a úkol jde rovnou na `replan` s důvodem.

- **Stejné volání:** když agent 3× za sebou zavolá stejný nástroj se stejným vstupem a dostane stejný výsledek, platí to jako zacyklení (varování, pak stop). Funguje i bez Jev.

Verdikty jsou kroky `supervisor` u volání v dashboardu. Bez klíče nebo při chybě Jev mlčí a harness běží jako dřív.

Model plánovače nastavuje `plan_model` v `projects.yaml` (výchozí `opus`). Plánovač nemá Bash ani PowerShell, jen Read/Glob/Grep.

Discover (lensy fix a feature) ke každému návrhu přidá `accept`, příkaz, který ověří „Hotovo =“. V BACKLOGu je to řádek `ACCEPT: …` v popisu. Úkol s tímto řádkem plánovač přeskočí: plánem je popis ze scouta, který už kód prošel. Plánovač se zavolá až při `replan`. Ruční úkol může `ACCEPT:` dostat taky.

## Autonomní běh: `harness serve`

    .venv/Scripts/harness serve

Jde o jeden proces: Telegram bot (long polling, nepotřebuje veřejnou URL) a plánovač. Autorun běží kdykoli, nejen v noci. Co spustí, určuje stav projektu (limity jsou konstanty v `harness/serve.py`, kontrola nejvýš jednou za 5 minut):
1. frontu projektu zpracuje, jen když má projekt méně než 6 otevřených PR,
2. cleanup review pustí, když poslední proběhl před víc než 14 dny, od té doby se mergnulo aspoň 10 PR, nebo ještě nikdy neproběhl,
3. nové úkoly navrhuje, jen když má projekt méně než 10 návrhů `[~]`, nejvýš jednou za 24 h,
4. když nic z toho nečeká, pustí vlastní idle činnost projektu z `projects.yaml`:

```yaml
projects:
  kytky:
    idle: {cmd: "npm run refresh-data", every: 24h}     # příkaz ve složce projektu
  web:
    idle: {prompt: "projdi logy chyb a navrhni opravy", every: 7d}   # vlastní scout
```

`cmd` běží ve složce projektu vedle běhů. Výsledek se objeví v digestu a historii a chyba přijde hned do Telegramu. `prompt` je zadání pro read-only scouta. Jeho nálezy se stanou návrhy `[~]` a platí pro ně limit 10 návrhů. `every` se zadává jako `30m`, `24h` nebo `7d`, výchozí hodnota je `24h`.
S `web: true` smí scout navíc hledat a číst na webu (WebSearch, WebFetch). Harness to používá k týdenní rešerši novinek o agentních harnessech: nové poznatky navrhne zapsat do `docs/poznatky.md` s datem zdroje a technologií a k tomu navrhne doporučení pro sebe.

Skripty pro `cmd` jsou v `scripts/` a zapisují rovnou do produkce daného projektu:
- `kytky_fill.py` doplní u kytek bez druhu druh a intervaly péče podle fotky (přes MCP kytek),
- `guth_idle.py` přeloží do angličtiny texty webu guth-pictures, které se v češtině od minula změnily, a doplní texty návrhům stories bez textu podle jejich fotek. Původní anglický obsah uloží do `content:en:backup` v KV.

Discover z autorunu nečeká na schválení plánu. Všechny návrhy skončí jako `[~]` a schvalují se jednotlivě. Kvóta (`quota_threshold`) brzdí dál. `/status` u každého projektu ukáže, kde je vůči limitům. Démon dál:
- dotazy k nejasným úkolům posílá hned, odpovídá se reply na zprávu (po `question_timeout_hours` se dotaz přeskočí),
- návrhy `[~]` posílá s tlačítky ✅/❌,
- jednou denně v `serve.digest_at` (výchozí `07:00`) pošle digest (PR, chyby, tokeny). Digest libovolného dne je v dashboardu v panelu Historie autorunu,
- jednou denně spustí `scan`,
- PR harnessu s konfliktem v CHANGELOG.md rebasuje sám i bez otevřeného dashboardu. U PR s červeným CI nechá agenta opravu jednou za head commit (push jen když projde verify) a výsledek pošle do Telegramu,
- po rate limitu sám pokračuje,
- po restartu naváže na rozběhnutý běh,
- když běh spadne, vypne automatiku a pošle zprávu (zapne se příkazem `/resume`).

Příkazy: `/status /queue /proposals /run /pause /resume /digest /help`, `/task <projekt> <titulek>` (další řádky = popis úkolu). Updaty z jiného chatu se ignorují.

Nastavení:
1. Přes [@BotFather](https://t.me/BotFather) založ **nový** bot (`/newbot`). Token secondbrain bota nepoužívej, dva pollery na jednom tokenu si navzájem kradou zprávy.
2. Napiš botovi a z `https://api.telegram.org/bot<TOKEN>/getUpdates` zjisti `chat.id`.
3. Nastav proměnné prostředí `HARNESS_TELEGRAM_TOKEN` a `HARNESS_TELEGRAM_CHAT_ID`, například `setx HARNESS_TELEGRAM_TOKEN ...`.
4. Pro autostart přidej v Task Scheduleru úlohu „Při přihlášení“ → `C:\Users\TGJ\Projects\harness\.venv\Scripts\pythonw.exe -m harness.cli serve` se „Spustit v“ = složka harnessu (bez okna konzole, log jde do `serve.log`).

Stav démona se ukládá do `serve_state.json` (je v gitignore).

### Dashboard na ploše

`serve` spouští i dashboard: `http://127.0.0.1:8765/desktop` (`--port` ho změní). Bez `HARNESS_TELEGRAM_*` běží serve jen s dashboardem. Stránka ukazuje:
- projekty z `projects.yaml`, které se čtou při každém načtení, takže nový projekt se ukáže bez restartu (běhy na něm ale potřebují restart serve),
- otevřené PR přes `gh` (cache 5 min),
- návrhy `[~]` s ✅/❌,
- kvótu subscription a tokeny a cenu po dnech.

Tlačítka spustí frontu (`run`) nebo `harness go` a plán z `go` se tam i schvaluje. Chipy projektů pod hlavičkou omezí obě akce na vybrané projekty (výběr si pamatuje prohlížeč). Autorun chipy ignoruje a řídí se limity projektů. U PR je stav CI a tlačítko Merge (dvě kliknutí): draft označí jako ready, udělá squash merge a smaže větev a worktree harnessu. Pozastavují automatiku a odpovídají na dotazy. Akce jdou přes hlavní smyčku serve, takže nikdy neběží dva běhy naráz.

Tapeta: [Lively Wallpaper](https://github.com/rocksdanister/lively) (`winget install rocksdanister.LivelyWallpaper`), tapeta typu URL na `/desktop`. Kliká se na odkryté ploše (Win+D). Psaní odpovědí vyžaduje v Lively nastavení „Input → Mouse & Keyboard“.

Subscription: `ANTHROPIC_API_KEY` nesmí být nastavený. Izolaci workerů ověří `.venv/Scripts/python scripts/check_isolation.py`.

Design: `docs/specs/`, plán: `docs/plans/`.

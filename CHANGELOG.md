## [2026-10-01] - Design spec fáze 1 (jedna smyčka, dashboard, simulace)
## [2026-10-01] - Implementační plán fáze 1
## [2026-10-01] - Scaffold harness balíčku a načítání projects.yaml
## [2026-10-01] - BACKLOG.md parser a zápis stavu
## [2026-10-01] - Guard pro diff a baseline selhání
## [2026-10-01] - SQLite event log
## [2026-10-01] - LangGraph graf a sdílené uzly pick_task/report
## [2026-10-01] - Simulace: falešné uzly, 8 scénářů, runner s event logem
## [2026-10-01] - CLI: harness simulate (plynulý i krokovací režim)
## [2026-10-01] - Agent SDK wrapper (subscription, rate limit), prompty, izolační spike: apiKeySource=none, bez globálních pluginů, claude.ai konektory vypnuté přes strict_mcp_config
## [2026-10-01] - Skutečné uzly (worktree, Agent SDK, verify, draft PR), harness run/prune
## [2026-10-01] - Dashboard API (FastAPI, SSE, spouštění simulací)
## [2026-10-01] - Dashboard frontend (graf, běhy, backlog, spotřeba, krokování); /state vrací running, tlačítko kroku čeká na doběhnutí
## [2026-10-01] - Pilotní konfigurace (ověřené verify příkazy), BACKLOG s gp-01/gp-02, README
## [2026-10-01] - První reálný běh: PR guth-pictures#6 a #7 (pauza na limit + resume, kill + resume)
## [2026-10-01] - Opravy z finálního review: timeout zabíjí strom procesů, billing kontrola i při --resume, guard vidí přesuny, název s — se nezkracuje, tělo PR přes soubor, worker nemůže push/gh
## [2026-10-01] - Design spec a plán fáze 2 (paralelní workers, priority napříč projekty, sken)
## [2026-10-01] - Fáze 2: worker subgraf + Send fan-out (max_parallel, 1 úkol/projekt/dávku), pauza na rate limit přes interrupt (sourozenci doběhnou), ranking P → fokus secondbrainu → weight, harness queue/scan, návrhy [~], sloupec worker v event logu, dashboard se dvěma diagramy
## [2026-10-01] - První reálný paralelní běh fáze 2: PR guth-pictures#8 a akano3#1 souběžně, verify zelené napoprvé
## [2026-10-02] - Design spec fáze 3 (Telegram, dotazy přes interrupt, démon serve)
## [2026-10-02] - Backlog: stav [-] (zamítnutý návrh), edit_backlog pod zámkem
## [2026-10-02] - Uzel clarify: nejasný úkol se ptá přes interrupt (max 2 kola), stav běhu waiting, open_interrupts ignoruje zodpovězené interrupty hotových workerů
## [2026-10-02] - Odpověď na dotaz nejasného úkolu z terminálu (run/simulate) a z dashboardu (POST /runs/{id}/answer)
## [2026-10-02] - Telegram klient (stdlib urllib: getUpdates, sendMessage s tlačítky, editMessageText, answerCallbackQuery)
## [2026-10-02] - harness serve: démon s Telegram ovládáním (okno pro automatický start, dotazy s reply/timeoutem, schvalování návrhů, digest po okně, resume po rate limitu a restartu, pád = pauza), denní sken, serve v projects.yaml
## [2026-10-02] - README: harness serve (nastavení bota, Task Scheduler), screenshot dotazu v dashboardu
## [2026-10-02] - Opravy z review fáze 3: démon drží rezervu kvóty mezi běhy, předávka výsledku běhu bez závodu, výjimka v tiku démona neshodí, sken 1× na okno, /answer v dashboardu atomicky, pozdní odpověď na už zodpovězený dotaz se ohlásí
## [2026-10-07] - Design spec harness go (objev úkolů modelem, prioritizace, schválení plánu v terminálu)
## [2026-10-07] - Implementační plán harness go
## [2026-10-07] - harness go: čisté funkce (parsování návrhů a pořadí, schválení, zápis do BACKLOGu), poznámky secondbrainu k projektu
## [2026-10-07] - harness go: graf (Send discover → prioritize → approve interrupt), pick_tasks podle schváleného plánu
## [2026-10-07] - harness go v terminálu: schválení plánu, simulace (go-happy, go-reject-all, go-discover-rate-limit)
## [2026-10-08] - harness go: reálný průzkum projektů (Opus, čerstvý worktree z origin, poznámky secondbrainu) a prioritizace
## [2026-10-08] - README: harness go
## [2026-10-08] - Opravy z review harness go: kvóta z discover/prioritize hlídá start workerů, robustní hledání JSON v odpovědi modelu, nehashovatelné id v pořadí neshodí běh
## [2026-10-08] - První reálný harness go: 14 návrhů ze 3 projektů (BACKLOG [~]), plán zatím neschválen
## [2026-10-08] - Druhý reálný harness go: draft PR akano3#2 a guth-pictures#14 (pauza na rate limit + resume), 10 návrhů [~]
## [2026-10-08] - Design spec fáze 4 (observabilita: trace volání agentů, cena za PR, výsledky PR)
## [2026-10-08] - Fáze 4: zachycení kroků agenta a cache tokenů v run_agent, kontext trace z config metadat LangGraphu, záznam i neúspěšných volání
## [2026-10-08] - Fáze 4: ledger volání agentů (agent_calls, agent_steps), výsledky úkolů z gh (task_outcomes), harness stats s cenou za úkol a režií, doplnění starých běhů z event logu
## [2026-10-08] - Fáze 4: dashboard (volání agentů běhu → kroky, cena za úkol s odkazem na PR, cache tokeny ve spotřebě); oprava: end události discover nesly prázdný worker
## [2026-10-08] - README: observabilita, harness stats
## [2026-10-08] - Opravy z review fáze 4: chyba zápisu trace nepřepíše výsledek ani pauzu na rate limit, gh s timeoutem a bez dotazů na merged/closed PR, backfill po eventech (obnovené běhy), indexy, Ctrl+C se nezapíše jako ok, pokusy bez rate-limited volání
## [2026-10-08] - Dashboard: výraznější hierarchie nadpisů (h1 20 px, h2 15 px)
## [2026-10-08] - Ostrý běh s trace: draft PR secondbrain#1 (0,32 $, 2 pokusy — první zablokoval guard na CHANGELOG.md mimo allowed)
## [2026-10-08] - secondbrain: CHANGELOG.md v allowed (worker ho podle konvence projektu upravuje)
## [2026-10-09] - Desktop dashboard v serve: projekty, PR, návrhy, kvóta, spouštění run/go a schválení plánu
## [2026-10-09] - Desktop dashboard: plné pozadí místo dekorativního gradientu
## [2026-10-09] - Projekt linkedin v projects.yaml (verify ověřeno v čerstvém worktree z origin/main)
## [2026-10-09] - Fáze 5: reflexe zpětné vazby → lekce (LangGraph Store, schvaluje vlastník) v promptech, důvod zamítnutí, harness jako projekt jen pro vlastní prompty
## [2026-10-09] - Dashboard se po restartu serve sám přenačte; fáze 5 kritérium 4 ověřeno ostře
## [2026-10-09] - Dashboard: výběr projektů pro frontu a go, merge PR (squash) s úklidem větve a worktree, stav CI u PR
## [2026-10-09] - Desktop dashboard: živá pipeline běhu, akční karty místo „harness go“, přepínač automatického startu místo Pauzy
## [2026-10-09] - Dashboard: kvóta subscription ukazuje stav i bez % (SDK hlásí utilization jen u varování)
## [2026-10-09] - Ceny: odhad u subscription (~/≈) oddělen od skutečně zaplaceného API (billing z apiKeySource)
## [2026-10-09] - Živá kvóta subscription (5h + 7 dní) z endpointu /usage Claude Code; serve se jí řídí, při chybě záloha na SDK
## [2026-10-09] - Harness upravuje sám sebe: discover zapnutý, allowed s ! výjimkami (guard/sdk/quota/projects.yaml), reflexe navrhuje i změny kódu
## [2026-10-10] - Discover hledá zvlášť opravy a nové features (dva prompty, mix v prioritizaci)
## [2026-10-10] - Druh návrhu v BACKLOGu ([feature] Titulek); dashboard dělí návrhy na features a opravy a před během volí, co hledat
## [2026-10-10] - Merge z dashboardu: harness PR v konfliktu jen v CHANGELOG.md se nejdřív rebasne (oba záznamy zůstanou), jiný konflikt dál chybou
## [2026-10-10] - Dashboard při načtení PR sám na pozadí rebasne harness PR v konfliktu jen v CHANGELOG.md (i po merge z GitHubu)
## [2026-10-10] - titulky návrhů bez „ — “/„ → “ (po zamítnutí se neořezávaly)
## [2026-10-10] - serve pod pythonw: child procesy (git/gh) bez blikajících konzolových oken
## [2026-10-10] - Worker: plánovač z „Hotovo =“ odvodí ACCEPT příkaz, který verify pouští; opakovaná stejná chyba nebo vyčerpané pokusy → jeden replan s kontextem selhání
## [2026-10-10] - parse_reflection toleruje nečekané typy v odpovědi modelu
## [2026-10-10] - Sken TODO: limit až po vyřazení návrhů známých z BACKLOGu
## [2026-10-11] - Dashboard: max úkolů a paralelismus nastavitelné pro jeden běh (výchozí z projects.yaml)
## [2026-10-11] - ruční zadání úkolu (dashboard "+ úkol", Telegram /task)
## [2026-10-11] - Dozor Jev nad implement: zákaz oslabení testů, varování/stop při zacyklení → replan
## [2026-10-11] - Dozor Jev: kontrola diffu testů při konci implement (zápis přes Bash obcházel Edit hook)
## [2026-10-11] - Revize čistoty kódu: tlačítko v dashboardu (discover lens cleanup → návrhy refaktorů), u projektu dny a zmergované PR od poslední revize
## [2026-10-11] - Dozor: 3× stejné volání se stejným výsledkem → varování, pak stop (i bez Jev); plan bez Bash/PowerShell; `plan_model` v projects.yaml (podle arXiv 2609.20804)
## [2026-10-11] - Úkoly z discover s ACCEPT přeskočí plánovač (plán = popis scouta, plán až při replan); plan_model: sonnet
## [2026-10-12] - Desktop dashboard: sloupce scrollují vlastním scrollbarem (Lively nepředává kolečko)
## [2026-10-12] - Konflikt harness PR mimo CHANGELOG: dashboard nabídne Vyřešit agentem / Přeimplementovat / Nechat
## [2026-10-12] - traces.py: jeden helper _gh_stdout místo čtyř kopií try/except kolem gh
## [2026-10-12] - README: popis projektu harness v projects.yaml odpovídá skutečné konfiguraci
## [2026-10-12] - Dashboard: úprava návrhu před schválením (akce edit, ✏️)
## [2026-10-12] - traces.py: veřejné settle_changelog/git_run/union_changelog, real.py už nesahá na privátní jména traces
## [2026-10-12] - Dashboard: kontrola origin jako závislost require_local_origin a helper _project
## [2026-10-12] - Discover: scout dostane seznam nedávných PR a commitů, aby nenavrhoval hotové úkoly
## [2026-10-12] - harness stats: souhrn úspěšnosti a ceny podle projektu a modelu plánovače
## [2026-10-12] - Desktop dashboard: přepínač „Mlékárna“ (parodický vzhled SAMM) a přepínač rozmazání obsahu PR a úkolů
## [2026-10-12] - SAMM: kreslené tlačítka a okna, pasoucí se kráva v pozadí, méně přejmenovaných textů
## [2026-10-12] - SAMM: stádo tří krav, které se toulají po celé louce
## [2026-10-12] - SAMM: výraznější rysy krav a občasná idle animace s logem jejich firmy
## [2026-10-12] - Rozmazání skrývá i jména projektů
## [2026-10-12] - Nový projekt kytky (private repo, base master, verify s next typegen)
## [2026-10-12] - Discover: oprava volání traces._git → git_run (přehled nedávných commitů padal)
## [2026-10-12] - Animované vysvětlující video SAMM (docs/video: HTML scéna + render do MP4)
## [2026-10-13] - Autorun kdykoli místo nočního okna: limity 6 PR / 10 návrhů / cleanup po 14 dnech nebo 10 PR, denní digest v digest_at, historie v dashboardu
## [2026-10-13] - Démon řeší konflikty PR i bez otevřeného dashboardu a červené CI harness PR opravuje agentem
## [2026-10-13] - Idle činnost projektu (idle.cmd / idle.prompt v projects.yaml) jako 4. krok autorunu; digest už nehlásí discover jako rozpracovaný úkol
## [2026-10-13] - Kytky: idle cmd scripts/kytky_fill.py 1× denně doplní přes MCP kytky bez druhu a intervalů
## [2026-10-13] - Desktop dashboard: scrollbar levého sloupce na levém okraji, scroll tažením obsahu myší
## [2026-10-13] - Desktop dashboard: tažení scrolluje i v Lively (setPointerCapture tam vyhazoval výjimku)
## [2026-10-13] - Autorun nestartuje, když je 5h nebo týdenní limit na 85 % a víc
## [2026-10-13] - Péče o PR (rebase, CI, konflikty) z create_app do dashboard/prcare.py
## [2026-10-18] - Slučování překrývajících se návrhů (výzva v dashboardu) a projektový plán ROADMAP.md pro feature scouta
## [2026-10-27] - Projekty faktury a discord-noter v projects.yaml

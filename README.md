# AI OpenAPI Refactoring & Validation Engine

Applicazione locale (Python 3.12+, FastAPI, Ollama) che prende una specifica **Swagger 2.0 / OpenAPI 3.x
esistente**, la porta alla versione OpenAPI target e la rifattorizza secondo regole di governance, sia
meccaniche (ruleset Spectral) sia scritte in linguaggio naturale (file Markdown). Il risultato è validato in
modo deterministico e rivisto da un Critic separato. Gli errori residui vengono corretti automaticamente, con
un numero massimo di iterazioni. L'originale non viene mai sovrascritto.

```text
AI transforms.        Refactor Agent / Correction Engine propongono operazioni tipizzate
Validators verify.    openapi-spec-validator + controlli globali + Spectral + regole compilate
AI critic reviews.    Critic Agent (prompt e modello distinti)
Validators decide.    anche i claim fattuali del Critic sono verificati sul semantic diff
```

Principio guida: *preserve behavior unless a rule explicitly requires a behavioral change.*
Non è un generatore di API da zero.

---

## 1. Requisiti

### Python (gestito con `uv`, fallback `pip`)

Dipendenze dirette (versioni bloccate; l'albero completo, dipendenze transitive incluse, è in `uv.lock`):

| Pacchetto | Versione | Uso |
| --- | --- | --- |
| `fastapi` | 0.141.1 | servizio REST opzionale (`app/api.py`), stessa logica della CLI |
| `pydantic` | 2.13.5 | Object Model, operazioni tipizzate, output strutturato dell'LLM |
| `openapi-spec-validator` | 0.9.0 | validazione OpenAPI 3.0/3.1 deterministica |
| `prance` | 26.7.19.0 | risoluzione dei `$ref` verso file esterni (bundling) |
| `ollama` | 0.6.2 | client ufficiale Ollama (`ollama-python`) |
| `pyyaml` | 6.0.3 | parsing/serializzazione YAML |
| `uvicorn` | 0.53.0 | *(extra `server`)* avvio del servizio FastAPI |
| `pytest` | 9.1.1 | *(dev)* test |
| `pytest-asyncio` | 1.4.0 | *(dev)* test async |
| `httpx` | 0.28.1 | *(dev)* `TestClient` FastAPI e trasporto finto per il test del provider Ollama |

`instructor` **non** è usato. Ollama accetta direttamente il JSON Schema pydantic nel parametro `format`
(structured outputs) e la risposta viene validata con pydantic. I retry con feedback dell'errore di
validazione sono implementati in `app/llm/structured.py`. Una dipendenza in meno, stesso risultato.

### Dipendenze esterne (non Python)

| Tool | Versione testata | Perché |
| --- | --- | --- |
| Node.js | ≥ 18 (testato con 20.19) | runtime per i due CLI sotto |
| `@stoplight/spectral-cli` | 6.16.3 | governance meccanica (ruleset `.spectral.yaml`) |
| `swagger2openapi` | 7.0.8 | upgrade Swagger 2.0 → OpenAPI 3.0 |
| Ollama | recente | LLM locale (nessuna chiamata cloud, nessuna GPU richiesta) |

Installazione dei tool Node, a scelta:

```bash
# globale
npm install -g @stoplight/spectral-cli swagger2openapi

# oppure locale al progetto (consigliato: versioni bloccate in package.json / package-lock.json)
npm install
```

I wrapper cercano i comandi prima nel `PATH` e poi in `./node_modules/.bin` (su Windows anche i file `.cmd`).
I nomi dei comandi sono configurabili in `config.yaml` (`spectralCommand`, `swagger2openapiCommand`).

### Ollama e modelli

1. Installa Ollama da <https://ollama.com/download> (Windows 11 / macOS / Linux).
2. Avvia il server, se non parte già come servizio: `ollama serve`
3. Scarica i modelli di default:

   ```bash
   ollama pull qwen3-coder:30b     # Refactor Agent, Correction Engine, Rule Interpreter
   ollama pull deepseek-r1:14b     # Critic Agent
   ```

4. Se Ollama non ascolta su `http://localhost:11434`, imposta `ollamaHost` in `config.yaml`.

I modelli sono configurabili (`refactorModel`, `criticModel`). Per esempio, per un Critic più accurato:
`ollama pull deepseek-r1:32b` e poi `criticModel: "deepseek-r1:32b"`. Il codice non va toccato.

---

## 2. Installazione

Un solo comando crea `.venv`, installa le dipendenze Python (incluse quelle di test) e i tool Node locali.

```bash
# macOS / Linux
./setup.sh
```

```powershell
# Windows 11 (PowerShell)
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Gli script usano `uv` se è installato (`uv sync --extra server`), altrimenti ripiegano su
`python -m venv .venv` + `pip install -r requirements-dev.txt`.

In alternativa, a mano:

```bash
uv sync --extra server && npm install                     # con uv (https://docs.astral.sh/uv/)
# oppure
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pip install -e . --no-deps && npm install
```

Verifica dell'ambiente (Ollama raggiungibile, modelli presenti, Spectral e swagger2openapi trovati):

```bash
uv run python -m app.cli preflight
```

---

## 3. Avvio

Comando esatto, dalla root del progetto:

```bash
uv run python -m app.cli refactor --input apis/case-003-no-problem-details.yaml --output output/ --rules rules/
```

Senza `uv`, con il virtualenv creato da `setup.sh` / `setup.ps1`:

```bash
.venv/bin/python -m app.cli refactor --input apis/case-003-no-problem-details.yaml --output output/ --rules rules/
```

```powershell
.\.venv\Scripts\python.exe -m app.cli refactor --input apis\case-003-no-problem-details.yaml --output output\ --rules rules\
```

Opzioni utili (ognuna sovrascrive il valore di `config.yaml` solo per quella run):

| Flag | Effetto |
| --- | --- |
| `--verbose` / `--log-level debug` | stampa ogni frammento inviato all'LLM e la risposta ricevuta (verifica del context slicing) |
| `--max-iterations N` | iterazioni massime del feedback loop |
| `--target-version 3.0\|3.1` | versione OpenAPI target |
| `--run-timeout SECONDI` | budget complessivo della run |
| `--refactor-model`, `--critic-model`, `--ollama-host` | override dei modelli e dell'host |
| `--config path/config.yaml` | file di configurazione alternativo |
| `--api-lifecycle draft\|published` | override di `apiLifecycle` |
| `--resume` | riprende una run interrotta dall'ultimo checkpoint (`output/<api-name>/.checkpoint/`) |

Altri comandi:

```bash
uv run python -m app.cli compile-rules     # compila (o legge dalla cache) le regole .md e mostra il risultato
uv run --extra server uvicorn app.api:app  # servizio REST opzionale: GET /health, POST /refactor
uv run python -m app.cli status --output output/<nome-api>          # stato di una run (anche da un altro terminale)
uv run python -m app.cli status --output output/<nome-api> --watch  # aggiornato ogni 10 secondi
```

Exit code della CLI: `0` SUCCESS (oppure specifica senza path), `1` NEEDS_REVIEW, `2` FAILED,
`4` SUCCESS_WITH_BREAKING_CHANGES (il messaggio finale elenca le modifiche breaking), `3` errore di input o
configurazione (file non parsabile, Ollama non raggiungibile, tool mancante, collisione
di `ruleId`, …). Gli errori attesi producono un messaggio leggibile, mai uno stack trace Python.

---

## 4. Output

Ogni run scrive `output/<api-name>/`, dove `<api-name>` è il nome del file di input senza estensione:

```text
output/<api-name>/
    original/<file>                  copia byte per byte del sorgente (mai modificato)
    refactored/<api-name>.yaml|json  candidato finale, stesso formato dell'input
    reports/
        refactoring-plan.json        piano iniziale + piani di correzione: operazioni (con `proposedBy`:
                                     deterministic / refactor / correction), frammento di origine, rationale
                                     dell'LLM, problemi del piano (operazioni senza violazioni, SET_FIELD non
                                     motivati, target normalizzati, conflitti)
        validation-report.json       validazione OpenAPI per iterazione e finale + warning di swagger2openapi
        governance-report.json       violazioni Spectral + regole compilate per iterazione e finali; regole
                                     compilate, conflitti tra regole, avvisi, uso della cache
        critic-report.json           verdetti del Critic per iterazione, con l'esito della verifica
                                     deterministica di ogni claim; `skipped`/`skipReason` quando non è
                                     stato invocato; stato per frammento
        changes.json                 AppliedChange (tipo, ruleId, STRUCTURAL/GOVERNANCE/SEMANTIC, posizioni,
                                     before/after, `inFinalOutput`), elenco dei cambi SEMANTIC dell'output,
                                     modifiche dei candidati scartati (`rejected`), operazioni non applicabili
        semantic-diff.json           diff semantico original → refactored (breaking / expected)
        progress.json                stato della run, riscritto a ogni frammento (vedi "Avanzamento")
        renames.json / renames.csv   mappa delle rinomine nell'output: elemento, posizione nel documento
                                     finale, nome vecchio → nuovo, ruleId, chi l'ha proposta; `needsReview`
                                     per i nomi originali con acronimi
        summary.json                 stato finale e motivazioni; `output` (iterazione scelta, `isBaseline`,
                                     nota); `baselineCounts` / `finalCounts` (ERROR, WARNING); condizioni di
                                     uscita e iterazioni scartate (`rejectedIterations`); `compileFailedRules`;
                                     `timings` per fase (compile, plan, engine, validation, critic,
                                     correction, total) e `llmByRole` (chiamate, secondi, fallimenti,
                                     token stimati inviati); `apiLifecycle`; `resume` (ripresa e risposte
                                     LLM riutilizzate dal checkpoint)
    .checkpoint/                     giornale delle risposte LLM per `--resume` (run.json + llm-journal.jsonl)
```

Stato finale:

- **SUCCESS**: il candidato finale è OpenAPI valido, non ha ERROR di governance, non ha change di
  contratto non tracciati, il Critic l'ha accettato, nessuna operazione o chiamata LLM è fallita, tutte le
  regole in linguaggio naturale sono state compilate in modo conforme e l'output non contiene modifiche
  breaking.
- **SUCCESS_WITH_BREAKING_CHANGES** (exit code `4`): come SUCCESS, ma l'output contiene almeno una modifica
  breaking, anche se attesa e motivata da una regola. Per esempio un header reso obbligatorio, una proprietà
  rinominata o una security aggiunta a un'operation pubblica. Il risultato è corretto, ma i client esistenti
  vanno informati o aggiornati, quindi non è un successo pieno. `summary.json` elenca queste modifiche in
  testa, nel campo `breakingChanges` (`location`, `type`, `ruleId`, `expected`), e la CLI le stampa nel
  messaggio finale. Ho preferito uno stato dedicato a un flag dentro SUCCESS, perché lo stato e l'exit code
  sono ciò che una pipeline CI controlla: con un flag, un "successo" breaking passerebbe inosservato.
- **NEEDS_REVIEW**: il candidato è valido, ma qualcosa resta aperto: `maxIterations` o `runTimeoutSeconds`
  raggiunti, violazioni residue, Critic non convinto, frammenti LLM falliti, oppure almeno una regola finita
  `compileFailed`. Una regola `compileFailed` resta attiva solo come giudizio e quindi non è verificata
  meccanicamente. In quel caso `summary.json` elenca le regole in `compileFailedRules` (`ruleId`, `file`,
  `line`, `reason`) e il motivo compare anche in `reasons`. Serve una revisione umana.
- **FAILED**: il candidato finale non è una specifica OpenAPI valida.

L'output è il **miglior candidato visto**: prima uno OpenAPI valido, poi quello con meno ERROR, poi quello
con meno WARNING; a parità di questi, quello accettato dal Critic e poi il più recente. Il conteggio include
ERROR e WARNING di validazione, governance e semantic diff. L'output non è mai peggiore della baseline: se
nessun candidato la migliora in senso stretto, l'output coincide con l'originale (convertito alla versione
target, se serviva). `summary.json` lo dice esplicitamente in `output.isBaseline` e `output.note`, e in quel
caso lo stato è NEEDS_REVIEW, se la baseline ha violazioni.

---

## 5. Architettura

```text
InputLoader → SpecificationParser → RuleLoader → RuleInterpreter → RefactoringPlanner → RefactoringEngine
      → [ OpenAPIValidator → GovernanceValidator → CriticEngine → CorrectionEngine ] × maxIterations
      → FinalValidator → OutputWriter
```

| Modulo | File |
| --- | --- |
| InputLoader + SpecificationParser | `app/loader.py`, `app/model/document.py`, `app/model/pointer.py` |
| Upgrade di versione | `app/converter.py` (swagger2openapi / 3.0→3.1 in Python) |
| RuleLoader + RuleInterpreter | `app/rules/loader.py`, `app/rules/interpreter.py`, `app/rules/models.py` |
| Registry regole (query per scope, collisioni, conflitti) | `app/rules/registry.py` |
| RefactoringPlanner (+ validazione del piano) | `app/refactor/planner.py` |
| Operazioni tipizzate + RefactoringEngine | `app/refactor/operations.py`, `app/refactor/engine.py` |
| Context slicing | `app/refactor/fragments.py`, `app/model/refs.py` |
| OpenAPIValidator | `app/validators/openapi_validator.py` |
| GovernanceValidator (Spectral + regole compilate) | `app/validators/governance.py`, `spectral.py`, `rule_eval.py` |
| Semantic diff + tabella breaking | `app/diff/semantic_diff.py`, `app/diff/compat.py` |
| CriticEngine (+ verifica dei claim) | `app/critic.py` |
| CorrectionEngine | `app/correction.py` |
| Orchestrazione / feedback loop / FinalValidator | `app/pipeline.py` |
| OutputWriter | `app/output.py` |
| LLM: interfaccia, retry tecnici, Ollama, provider finto | `app/llm/base.py`, `structured.py`, `ollama_provider.py`, `fake.py` |
| Trasporti | `app/service.py` (servizio), `app/cli.py`, `app/api.py` |
| System prompt per ruolo | `app/prompts/*.md` (solo istruzioni di ruolo e formato, nessuna regola di business) |

### Decisioni esplicite

- **Swagger 2.0 → OpenAPI 3.0 con `swagger2openapi` (Node, processo esterno).** In Python non c'è un
  convertitore maturo quanto questo, e Node serve già per Spectral. Il tool gira con
  `--patch --warnOnly`: i suoi warning (estensioni `x-s2o-warning`) e il suo stderr vengono raccolti e
  finiscono nel validation report, e un exit code non nullo è un errore esplicito. L'upgrade 3.0 → 3.1 è un
  passo deterministico in Python (`nullable` → type array). Il downgrade 3.1 → 3.0 non è supportato.
- **Object Model.** Dopo il parsing il documento è un albero `dict`/`list` dentro un modello pydantic
  (`SpecDocument`), con viste tipizzate per operation e schema, indirizzate via JSON Pointer (RFC 6901).
  L'engine lo modifica solo tramite operazioni tipizzate, mai con testo YAML. Conseguenza: i commenti YAML
  del sorgente non compaiono nel file rifattorizzato, ma restano nella copia in `original/`.
- **`openapi-spec-validator` si blocca sui `$ref` rotti** (solleva un'eccezione invece di riportare un
  errore). Per questo i `$ref` rotti vengono individuati prima, con un controllo deterministico, e
  riportati come `OAS-REF-BROKEN`. Il validator gira poi su una copia in cui sono neutralizzati.
- **Regole in linguaggio naturale → DSL chiuso.** L'LLM compila ogni regola in un `CompiledRule` con una
  lista di requisiti tipizzati (`requireHeader`, `requireQueryParameter`, `requireOperationId`, `nameCasing`,
  `errorFormat`, `requireSecurity`, `requireResponse`). Il GovernanceValidator valuta questi requisiti
  **senza LLM**. Se nessun requisito esprime *esattamente* il vincolo, il prompt impone `judgment` e vieta di
  ripiegare sul requisito più simile: un requisito "quasi giusto" produrrebbe violazioni e correzioni
  sbagliate. Per esempio "le GET che restituiscono collezioni devono accettare `limit`" resta `judgment`,
  perché "restituisce una collezione" non si può esprimere con `condition`. Le regole `judgment` restano
  affidate al Critic.
- **Metodi HTTP nominati nel testo della regola.** Se il testo nomina GET, POST, PUT, PATCH o DELETE (anche
  in minuscolo), `condition.methods` deve contenere esattamente quei metodi. Se è null, ne manca uno o ce n'è
  uno in più, la regola meccanica varrebbe per metodi che il testo non prevede. È un bug osservato con
  Ollama reale: "Ogni endpoint POST …" compilata senza `methods` aggiungeva `Idempotency-Key` obbligatorio
  alle GET, una modifica "expected" perché motivata da un ruleId, che né il diff né il Critic potevano
  bloccare. Una compilazione non conforme viene ritentata, passando l'errore al modello; esauriti i retry, la
  regola resta attiva solo come `judgment` marcata `compileFailed`, con un avviso nel governance report. Lo
  stesso controllo si applica alle regole lette dalla cache: se una non è conforme, il file viene
  ricompilato. Le regole solo-giudizio non vengono controllate, perché non si applicano meccanicamente.
- **`requireHeader` e `requireQueryParameter`: `required` senza default.** Il modello deve sempre
  dichiararlo. `required: true` si usa solo se la regola dice esplicitamente che il parametro è obbligatorio
  o va sempre inviato; verbi come "supportare", "accettare", "gestire" significano che l'API deve accettare
  il parametro, non pretenderlo, quindi `required: false`. È il caso di HTTP-IDEMPOTENCY-001, che con il
  vecchio default `true` aggiungeva un `Idempotency-Key` obbligatorio (breaking) a POST /cards e chiudeva la
  run in SUCCESS. Il nome dell'header si confronta senza distinguere maiuscole e minuscole.
  `DSL_VERSION` passa a `3`, così tutte le regole in cache vengono ricompilate. Non si può invalidare solo
  `requireHeader`: in cache il valore `required: true` è salvato allo stesso modo sia quando veniva dal
  vecchio default sia quando era una scelta del modello, quindi non si possono distinguere. Inoltre lo schema
  inviato al modello è cambiato, perché `required` è diventato obbligatorio.
- **`requireQueryParameter`** (`name`, `required`, più `condition.methods`, per esempio `["get"]`): con
  `required: false` il parametro deve solo **esistere**, dichiarato a livello di operation, di path o via `$ref`.
  La sua obbligatorietà non viene verificata né modificata: una regola di paginazione non deve rendere
  opzionale un `limit` che l'API dichiara obbligatorio (preserve behavior). Con `required: true` deve anche
  essere obbligatorio. È lo stesso comportamento di `requireHeader`. La correzione corrispondente è
  l'operazione `ADD_QUERY_PARAMETER`:
  - con `required: false` aggiunge il parametro come opzionale solo se manca (GOVERNANCE, non-breaking) e
    non tocca mai un parametro esistente;
  - con `required: true` lo aggiunge obbligatorio o rende obbligatorio quello esistente (SEMANTIC,
    breaking). Se il parametro è definito a livello di path o via `$ref` come opzionale, l'operazione
    fallisce in modo esplicito invece di creare un duplicato sulla singola operation.
- **Cache delle regole compilate** in `.cache/compiled-rules/` (configurabile), una per file `.md`. La
  cache è invalidata dallo SHA-256 del file, dal modello usato e dalla versione del DSL: se un file cambia,
  viene ricompilato per intero. Le compilazioni fallite non vengono salvate, quindi alla run successiva si
  ritenta. Nel frattempo la regola resta attiva come regola di giudizio, marcata `compileFailed`.
- **Correzioni deterministiche** (`app/refactor/deterministic.py`). Le violazioni dei requisiti meccanici
  con una correzione nota vengono corrette senza LLM, con operazioni tracciate dal loro `ruleId`
  (`proposedBy: deterministic`):

  | Requisito | Operazioni |
  | --- | --- |
  | `errorFormat` | `ADD_COMPONENT` Problem (solo se non c'è già uno schema conforme) + `CONVERT_ERROR_RESPONSE` |
  | `requireOperationId` | `ADD_OPERATION_ID` |
  | `requireHeader` | `ADD_HEADER` |
  | `requireQueryParameter` | `ADD_QUERY_PARAMETER` |
  | `requireResponse` | `ADD_RESPONSE` |
  | `requireSecurity` (http) | `ADD_SECURITY_SCHEME` (se manca) + `SET_SECURITY_REQUIREMENT` |
  | `nameCasing` e regole Spectral di casing | `RENAME_PROPERTY`, `RENAME_PARAMETER`, `RENAME_PATH`, `RENAME_SCHEMA`, `ADD_OPERATION_ID` |

  Il Refactor Agent e il Correction Engine ricevono solo il resto: violazioni Spectral e del validator senza
  mapping, e le rinomine in collisione. Non ricevono nemmeno le violazioni che cadono su una response già riscritta da una
  correzione deterministica, per esempio DE-STATUS-002 sulla stessa response di ERR-001, che altrimenti
  porterebbero a modifiche concorrenti. Restano all'LLM, per scelta, i casi in cui la correzione non è
  deducibile:
  - security scheme `apiKey`, `oauth2` e `openIdConnect` (servono nome dell'header, flussi o URL);
  - operation che hanno già altri security requirement, perché sostituirli cambierebbe chi può accedere;
  - response definite via `$ref`.

  Valori generati:
  - **operationId:** `<metodo><SegmentiStatici>[By<Parametri>]`, per esempio GET `/accounts/{account-id}` →
    `getAccountsByAccountId`, con suffisso numerico se è già usato;
  - **description di una response aggiunta:** la reason phrase HTTP, per esempio `Not Found`;
  - **schema Problem (RFC 9457):** `type` (uri), `title`, `status` (integer int32), `detail`, `instance`
    (uri-reference), più le proprietà richieste dalla regola, come string e in `required`. Si chiama
    `Problem`, oppure `ProblemDetails` se esiste già un `Problem` non conforme;
  - **nome del security scheme:** quello di uno scheme conforme già definito, altrimenti `<scheme>Auth`;
  - **header e query parameter aggiunti:** schema `{"type": "string"}`.
- **Naming deterministico.** Si correggono in modo deterministico le violazioni `nameCasing` e quelle delle
  regole Spectral di casing: proprietà, parametri (query, header, path), segmenti di path, nomi di schema e
  operationId. Il casing di destinazione delle regole Spectral viene dedotto dal ruleset (funzioni `casing`,
  `kebabCasePath`, pattern Train-Case degli header): nessun ruleId è scritto nel codice.
  - **Conversione** (`app/validators/casing.py`): il nome viene scomposto in parole e ricomposto.

    | Nome | Casing | Risultato |
    | --- | --- | --- |
    | `payer_IBAN` | camel | `payerIban` (separatori misti, acronimo) |
    | `IBANCode` / `HTTPStatusCode` | camel | `ibanCode` / `httpStatusCode` (acronimo seguito da parola) |
    | `sha256_hash` / `line_2` | camel | `sha256Hash` / `line2` (le cifre restano con la parola precedente) |
    | `Payment_Instructions` / `v2Accounts` | kebab | `payment-instructions` / `v2-accounts` |
    | `x_request_id` | Train | `X-Request-Id` |
    | `payment_order` | Pascal | `PaymentOrder` |
    | `2fa_code` | camel | nessuna rinomina: `2faCode` non è camelCase valido → all'LLM |

  - **Riferimenti aggiornati** (`app/refactor/references.py`):
    - schema: `$ref` e `discriminator.mapping`, nella forma `$ref` o come nome semplice;
    - proprietà: `required`, anche quello degli schemi che includono lo schema via `allOf`, direttamente o in
      modo transitivo, e dei loro membri inline. Non viene toccato se il nome vecchio è dichiarato anche da un
      altro membro, perché lì si riferisce a quella proprietà. Poi `example`/`examples` a qualunque
      profondità, seguendo lo schema (proprietà, `items`, `additionalProperties`, `allOf`, `$ref`, esempi in
      `components/examples` referenziati), e `discriminator.propertyName`;
    - parametro: template del path, `links` che puntano all'operation (chiavi `nome` e `<in>.nome`),
      espressioni `$request.<in>.<nome>`;
    - path e operationId: `links.operationRef` e `links.operationId`.

    Un esempio la cui corrispondenza con lo schema non è determinabile (lo schema passa da `oneOf`/`anyOf`,
    quindi non si sa quale ramo descriva l'esempio, oppure `externalValue`) non viene toccato: viene
    segnalato per revisione in `renames.json` (`needsReview`, `examplesToReview`).
  - **`required` orfani** (`STRUCT-REQUIRED-ORPHAN`, ERROR): è un controllo deterministico, sempre attivo,
    eseguito dal GovernanceValidator dopo ogni applicazione. Ogni nome in un `required` deve esistere tra le
    proprietà dello schema o degli schemi inclusi (`allOf`, e i rami `oneOf`/`anyOf`); un membro inline di un
    `allOf` vede anche i suoi fratelli. Gli schemi aperti, senza proprietà né composizioni, non vengono
    controllati. È un ERROR di governance e non di validità OpenAPI, perché il documento resta valido: blocca
    il SUCCESS e va in correzione, invece di far finire la run in FAILED.
  - **Collisioni:** se il nuovo nome esiste già, o se due nomi dello stesso ambito diventerebbero uguali (per
    esempio `tax_amount` e `tax__amount`), non si rinomina nulla e la violazione passa all'LLM.
  - **Parametri via `$ref`:** i parametri definiti in `components/parameters` non vengono rinominati in modo
    deterministico, perché il componente è condiviso.
  - **Tracciabilità:** le rinomine sono tracciate con il loro `ruleId` e, quando cambiano il wire, sono
    SEMANTIC e breaking.
  - **Mappa delle rinomine** (`reports/renames.json` e `renames.csv`, totali in `summary.json`): una riga per
    ogni rinomina inclusa nell'output. Contiene tipo di elemento (`schema`, `property`,
    `query|header|path parameter`, `path`, `operationId`), posizione nel documento finale, nome vecchio →
    nuovo, `ruleId`, `proposedBy` e iterazione. Le rinomine di nomi con un acronimo, cioè due o più maiuscole
    consecutive come `payer_IBAN` → `payerIban`, sono marcate `needsReview` con il motivo: la conversione
    ricompone le parole, e va verificato che la forma scelta per l'acronimo sia quella voluta.
  - **Path parameter:** una rinomina cambia la chiave del path, quindi nel piano viene eseguita dopo le altre
    operazioni sullo stesso path e prima di `RENAME_PATH`.
- **Validazione del piano.**
  - Un'operazione proposta dall'LLM deve citare il `ruleId` di una violazione mostrata per il suo frammento
    di origine; altrimenti viene scartata e riportata (`RULE_WITHOUT_VIOLATION`). Niente modifiche "per
    scrupolo", come un security scheme senza violazioni di sicurezza. Ne segue che le regole di giudizio non
    generano operazioni: restano al Critic.
  - Un `SET_FIELD` che sovrascrive un oggetto non vuoto con un contenuto diverso richiede una violazione di
    quella regola su quell'elemento (`SET_FIELD_UNJUSTIFIED`) ed è sempre classificato SEMANTIC.
- **Target robusti.** Se un target non esiste, si prova a leggere i `/` non escapati come parte dei nomi, per
  esempio `.../content/application/json` → `.../content/application~1json`. La correzione si usa solo se
  esiste un'unica lettura esistente; altrimenti l'operazione fallisce in modo esplicito, come prima. La
  normalizzazione avviene già nella validazione del piano, così due operazioni scritte con escape diversi
  vengono riconosciute come duplicate, e viene riportata nel piano e nella descrizione della modifica.
  Per le operazioni sulle response ho valutato campi strutturati (path, metodo, status, media type) al posto
  dei puntatori, e ho scelto di non introdurli: `CONVERT_ERROR_RESPONSE` ora è deterministica, quindi l'LLM
  punta raramente alle response; la normalizzazione copre l'errore osservato; e una seconda forma di target
  renderebbe più grande lo schema delle operazioni da generare, cosa che su CPU pesa.
- **Critic solo sulle modifiche dell'LLM.** Il Critic rivede un frammento solo se contiene almeno una
  modifica proposta dall'LLM (Refactor Agent o Correction Engine), o un change non tracciato del semantic
  diff, cioè una possibile regressione. Un frammento modificato solo da correzioni deterministiche è già
  coperto da validatori e semantic diff: viene saltato e `critic-report.json` lo elenca in
  `skippedFragments` con il motivo. Ogni `AppliedChange` riporta in `proposedBy` chi l'ha proposto.
- **Critic solo quando serve.** Il Critic viene invocato solo su candidati OpenAPI validi e senza ERROR del
  GovernanceValidator; altrimenti si passa direttamente alla correzione, e `critic-report.json` riporta
  `skipped` con il motivo. Gli ERROR del semantic diff (`DIFF-UNTRACED-CHANGE`) non escludono il Critic,
  perché individuare regressioni come una response persa è proprio il suo compito (CASE 005).
- **Nessuna regressione tra iterazioni.** Una correzione che aumenta gli ERROR, o che introduce violazioni
  nuove (ERROR o WARNING) su elementi prima conformi, viene scartata: il candidato corrente non cambia.
  L'iterazione è registrata come `rejected`, con i motivi e le violazioni nuove, e la correzione successiva
  riceve il tentativo scartato (`previousAttemptRejected`) per non ripeterlo. Il gate vale per le correzioni;
  la prima proposta (V1) contro la baseline è coperta dalla scelta del miglior candidato. Una correzione
  viene scartata per intero, anche se solo una parte delle sue operazioni peggiora il candidato.
- **Precedenza nei conflitti.** Una regola Spectral (deterministica) prevale su una regola compilata
  dall'LLM che dà un'indicazione contraria sullo stesso elemento: per esempio `DE-JSON-001` (camelCase)
  contro una regola `.md` che chiede snake_case. Il conflitto è sempre riportato in `governance-report.json`
  e la regola perdente non viene valutata. A livello di piano vale lo stesso: tra due operazioni in
  conflitto vince quella motivata da una regola deterministica; se nessuna lo è, vengono scartate entrambe.
- **Collisione di `ruleId`** tra file `.md` o tra `.md` e Spectral: errore di configurazione all'avvio.
- **Nessuna regola presente**: la pipeline esegue solo la validazione OpenAPI di base e lo segnala con un
  avviso ben visibile nel log e nel report.

### Context slicing

- Il documento viene lavorato per frammenti (livello documento, path, operation, componente). Un frammento
  di operation contiene solo quell'operation, non l'intero path item, più il contesto minimo: i parametri
  a livello di path e gli schemi referenziati.
- Al Refactor Agent e al Correction Engine arrivano solo le regole citate dalle violazioni del frammento; al
  Critic, le regole di giudizio applicabili e quelle coinvolte nel frammento. Prima arrivavano tutte le
  regole applicabili, circa l'80% del prompt. Riduzione misurata sui prompt di pianificazione (frammenti con
  violazioni dei casi in `apis/` e della spec di test del ruleset): da 25.528 a 14.607 token stimati, −43%
  (da −32% a −57% per spec). `llmByRole.promptTokens` in `summary.json` misura i token inviati nelle run
  reali.
- I `$ref` sono indicizzati una volta sola (`RefIndex`). Al modello arrivano solo le definizioni referenziate
  direttamente, entro `llmContextTokenBudget`; le altre compaiono solo per nome. I cicli (`User → Manager →
  User`) vengono rilevati: la risoluzione si ferma sul ciclo, che è riportato come `OAS-REF-CIRCULAR` (INFO).
- I controlli globali (operationId univoci, `$ref` rotti, security verso schemi non definiti) sono passi
  deterministici separati.
- Ollama riceve `num_ctx = llmContextTokenBudget + 4096`: il default di Ollama troncherebbe i frammenti.
- Il Critic rivede solo i frammenti cambiati rispetto all'originale. Un frammento già accettato, con lo
  stesso contenuto (fingerprint), non viene rivisto nelle iterazioni successive. Il CorrectionEngine lavora
  solo sui frammenti che hanno problemi.
- Con `--verbose` il log mostra ogni frammento inviato, con una stima dei token.

In pratica, una specifica più grande richiede **più chiamate**, non chiamate più lunghe.

### Semantic diff

Il diff (`semantic-diff.json`) è una lista piatta di change tipizzati: path, operation, parametri, request
body, media type, response, schema, proprietà, `required`, valori enum, security requirement, security
scheme. Ogni change ha due flag:

- `breaking`: calcolato con la tabella `BREAKING_RULES` in `app/diff/compat.py`, sensibile al contesto
  request/response. Per esempio, un valore enum rimosso rompe i client solo nelle request.
- `expected`: vale `true` solo se il change ricade dentro la posizione di un `AppliedChange`, e in quel caso
  riporta il `ruleId`. Un change con `expected: false` non è riconducibile ad alcuna operazione pianificata:
  diventa la violazione `DIFF-UNTRACED-CHANGE` (ERROR se breaking), cioè "nessuna modifica silenziosa del
  contratto".

I rename di schema e di path vengono accoppiati (`SCHEMA_RENAMED` / `PATH_RENAMED`) invece di comparire come
rimozione più aggiunta.

### Critic: cosa decide e cosa no

Ogni issue del Critic passa da una verifica deterministica prima di poter bloccare un'iterazione:

| Tipo di issue | Verifica | Blocca se |
| --- | --- | --- |
| `ELEMENT_LOST`, `INVENTED_ELEMENT`, `SEMANTIC_ALTERATION`, `UNNECESSARY_CHANGE`, `REGRESSION` (o con `claim`) | incrocio con il semantic diff | `VERIFIED`: il change esiste ed è **non** tracciato. `TRACED` (esiste ma è motivato da un `ruleId`) e `REFUTED` non bloccano |
| `BROKEN_REFERENCE` | `RefIndex.broken_refs()` | confermato |
| `RULE_NOT_APPLIED` su una regola deterministica | violazioni del GovernanceValidator | confermato |
| `NAMING_QUALITY`, `SCHEMA_INCONSISTENCY`, `MIGRATION_ERROR`, `OTHER`, regole di giudizio | nessuna (giudizio semantico) | severity ERROR |

### Avanzamento e riepilogo della baseline

- **Riepilogo della baseline per regola.** Subito dopo la validazione della baseline, e prima delle
  chiamate LLM di pianificazione, il log mostra una tabella con una riga per `ruleId`, ordinata per numero
  di violazioni decrescente. Colonne: severità, totale, violazioni con correzione deterministica, violazioni
  all'LLM, violazioni *rivalutate*. Le rivalutate cadono su elementi che una correzione deterministica
  riscrive, per esempio DE-STATUS-002 sulla response convertita da ERR-001: vengono rivalutate dopo
  l'applicazione. In fondo ci sono i totali e il numero di frammenti che andranno all'LLM. Lo stesso
  riepilogo è in `summary.json` (`baselineByRule`) e dà in pochi secondi l'ordine di grandezza della run.
- **Log per frammento.** Per pianificazione, Critic e correzione:
  - all'inizio della fase, i frammenti da elaborare; per il Critic anche quanti sono saltati perché hanno
    solo modifiche deterministiche;
  - una riga per frammento completato, per esempio
    `[PLAN] 12/73 /paths/~1accounts/get: 48.2s | trascorso 9m40s | stima fine fase 16:52`. La stima usa la
    durata media dei frammenti già completati nella fase;
  - all'inizio di ogni iterazione, `---- Iterazione i/maxIterations ----`.
- **Ripresa.** Con `--resume` le risposte prese dal checkpoint contano come completate ma sono indicate
  (`da checkpoint`) ed escluse dalla media, altrimenti la stima risulterebbe troppo ottimista.
- **`reports/progress.json`.** È riscritto in modo atomico (file temporaneo e rename) a ogni frammento, quindi
  si può leggere mentre la run è in corso. Contiene: fase corrente, iterazione, frammenti completati/totali
  della fase (da checkpoint, falliti, saltati), chiamate LLM fatte, fallite e da checkpoint, tempo trascorso
  (run e fase), stima di fine fase, ultimo aggiornamento. A fine run lo stato diventa quello finale.
- **`python -m app.cli status --output output/<nome-api>`** legge `progress.json` e stampa un riepilogo;
  con `--watch` lo aggiorna ogni 10 secondi finché la run è in corso. Non serve né la configurazione né
  Ollama, quindi funziona da un secondo terminale. Se il file non è aggiornato da più di
  `llmCallTimeoutSeconds`, lo segnala ("nessun avanzamento da X minuti"): la run potrebbe essere bloccata o
  interrotta.

### Resilienza

- **Preflight** all'avvio: Ollama raggiungibile, modelli presenti, Spectral disponibile se ci sono ruleset.
  Se qualcosa manca, la run si ferma subito con il comando da eseguire (per esempio `ollama pull …`).
- **Retry tecnici** (`llmTechnicalRetries`), distinti dai retry di qualità del feedback loop: coprono
  timeout, errori di Ollama e output non conforme allo schema. In quest'ultimo caso l'errore di validazione
  viene passato al tentativo successivo. Esauriti i retry, il frammento è marcato come fallito e riportato,
  e la run non può chiudersi in SUCCESS.
- **Ripresa** (`--resume`). Il checkpoint è il giornale delle risposte LLM riuscite, in
  `output/<api-name>/.checkpoint/`, scritto con fsync dopo ogni frammento elaborato (pianificazione,
  Critic, correzione, compilazione delle regole). A parità di risposte dell'LLM la pipeline è
  deterministica, quindi la ripresa riesegue la run: le chiamate già fatte vengono servite dal giornale senza
  interrogare il modello, e la run prosegue dal punto di interruzione. Validazione e Spectral si rieseguono,
  ma costano secondi. La ripresa è rifiutata, con un messaggio che dice cosa è cambiato, se input, regole o
  configurazione rilevante (modelli, target, iterazioni, lifecycle, …) non coincidono con la run interrotta.
  Senza `--resume` il checkpoint viene ricreato da zero.
- **Durata delle chiamate.** Ogni chiamata LLM viene cronometrata: una riga di log INFO per chiamata, e i
  totali per ruolo in `summary.json` (`llmByRole`).
- **Ragionamento del Critic** (`criticThink`, default `false`). Il parametro `think` di Ollama viene passato
  solo ai modelli che dichiarano la capability `thinking` (verificata con `ollama show`). Con `false` il
  ragionamento esteso dei modelli come `deepseek-r1` viene disattivato esplicitamente, ed è il fattore che
  pesa di più sui tempi del Critic su CPU. Con `true` il Critic ragiona più a lungo, per revisioni più
  accurate.
- **Timeout per chiamata** (`llmCallTimeoutSeconds`) e **budget della run** (`runTimeoutSeconds`). Il
  timeout di ogni chiamata è limitato al tempo residuo della run. Se il budget si esaurisce, la run esce in
  modo pulito con NEEDS_REVIEW, validazione finale deterministica e report delle violazioni residue.
  Unico caso a parte: se il budget scade durante la compilazione delle regole, non esiste ancora un
  candidato e la CLI esce con un messaggio di errore.

---

## 6. Configurazione (`config.yaml`)

Tutti i parametri stanno in un solo file. I flag CLI li sovrascrivono solo per la singola run.

| Chiave | Default | Descrizione |
| --- | --- | --- |
| `targetOpenApiVersion` | `"3.0"` | `"3.0"` o `"3.1"` |
| `apiLifecycle` | `published` | `draft`: API non ancora pubblicata, le modifiche breaking sono il lavoro richiesto: restano in `breakingChanges`, ma lo stato è SUCCESS |
| `maxIterations` | `3` | candidati valutati al massimo nel feedback loop |
| `refactorModel` / `criticModel` | `qwen3-coder:30b` / `qwen3-coder:30b` | modelli Ollama per ruolo (default del codice per il Critic: `deepseek-r1:14b`; `config.yaml` usa lo stesso modello per evitare di caricarne due in memoria su CPU) |
| `criticThink` | `false` | ragionamento esteso del Critic (solo modelli con capability `thinking`) |
| `ollamaHost` | `http://localhost:11434` | |
| `llmCallTimeoutSeconds` | `600` | timeout della singola chiamata (su CPU serve un valore alto) |
| `llmTechnicalRetries` | `2` | retry tecnici per chiamata |
| `runTimeoutSeconds` | `7200` | budget complessivo della run |
| `llmTemperature` | `0.0` | |
| `llmContextTokenBudget` | `12000` | budget indicativo per chiamata (8-16K) |
| `logLevel` | `INFO` | `DEBUG` mostra i frammenti inviati all'LLM |
| `rulesDir` / `outputDir` | `rules` / `output` | |
| `compiledRulesCacheDir` | `.cache/compiled-rules` | cache delle regole compilate |
| `spectralCommand` / `swagger2openapiCommand` | `spectral` / `swagger2openapi` | comandi dei tool Node |
| `externalToolTimeoutSeconds` | `120` | timeout dei processi esterni |

---

## 7. Regole

```text
rules/
    general.md, security.md, naming.md, pagination.md     regole in linguaggio naturale (compilate dall'LLM)
    spectral/
        digital-euro-ruleset.spectral.yaml                ruleset Spectral reale (20 regole), copiato invariato
        functions/*.js                                    7 funzioni custom del ruleset
        non-mechanical-rules.md                           documentazione: cosa non copre Spectral e perché
        README.md                                         README originale del ruleset
rules-input/spectral/                                     materiale fornito, conservato così com'era
```

**Formato dei file `.md`.** Vengono letti solo i file `.md` direttamente dentro `rules/` (le sottocartelle
contengono documentazione). Ogni voce di elenco di primo livello è una regola. Come nel Markdown standard
(CommonMark), la voce continua sulle righe indentate che la seguono, anche dopo una riga vuota, e sulle righe
non indentate che la seguono direttamente, senza riga vuota in mezzo. Un paragrafo non indentato dopo una
riga vuota chiude l'elenco ed è solo testo di contesto: non viene inviato all'LLM. L'ID si indica con `[ID]` oppure `**ID**:` all'inizio della voce. Senza ID esplicito
viene assegnato `<NOMEFILE>-<NNN>`, che però cambia se si riordinano le voci: meglio sempre un ID esplicito.
I titoli (`#`) fanno da sezione e vengono passati all'LLM come contesto.

**Ruleset Spectral.** È un file di configurazione: per aggiungere o modificare una regola basta cambiare il
`.spectral.yaml`, senza toccare il codice. Ogni `*.spectral.yaml` in `rules/spectral/` viene eseguito con
`spectral lint --ruleset <file> --format json`. Due particolarità del ruleset fornito, lasciato invariato:

- il messaggio di `DE-JSON-001` mostra `'[object Object]'` al posto del nome della proprietà (`{{value}}` su
  una selezione di chiavi). Il wrapper ricava comunque il nome dal path (campo `actual` della violazione);
- `DE-STATUS-001` / `DE-STATUS-002` usano `given: $..responses`, quindi colpiscono anche
  `components/responses`: un componente response riusabile genera un falso "manca una response 2xx".

---

## 8. Test

```bash
uv run pytest                        # suite di default: veloce, deterministica, senza Ollama
uv run pytest -m requires_ollama     # integrazione end-to-end con il vero Ollama (lenta su CPU)
uv run pytest -m requires_ollama tests/test_critic_quality.py   # solo la qualità del Critic
```

La suite di default usa un `LlmProvider` finto e deterministico (`app/llm/fake.py` + `tests/fake_agents.py`):
il Rule Interpreter restituisce bozze predefinite, Refactor e Correction sono fixer scriptati sulle
violazioni ricevute, e il Critic accetta salvo override per singolo test. Tutto il resto è reale: parser,
swagger2openapi, engine, openapi-spec-validator, Spectral, semantic diff, critic verification, output.
I test che richiedono i tool Node vengono saltati, con il motivo, se `spectral` o `swagger2openapi` mancano.

| Caso | File | Cosa verifica |
| --- | --- | --- |
| CASE 001 | `apis/case-001-swagger2-legacy.yaml` | upgrade Swagger 2.0 → 3.0: stesse operation, schemi equivalenti, validazione OK, originale intatto |
| CASE 002 | `apis/case-002-naming.yaml` | schema → PascalCase, proprietà → camelCase, `$ref` aggiornati, rename delle proprietà marcati SEMANTIC/breaking ma tracciati |
| CASE 003 | `apis/case-003-no-problem-details.yaml` | errori convertiti in `application/problem+json` (RFC 9457) in modo deterministico, schema `Problem` aggiunto una sola volta, anche con un LLM che propone operazioni sbagliate |
| CASE 004 | `apis/case-004-incomplete-security.yaml` | security scheme bearer aggiunto, requirement su tutte le operation, restrizione marcata breaking e tracciata |
| CASE 005 | `apis/case-005-regression-guard.yaml` | regressione simulata (404 rimossa senza motivo): il Critic la segnala, il claim è verificato sul diff e blocca l'accettazione, la correzione ripristina la 404. Con la regressione persistente nessun candidato migliora la baseline: dopo `maxIterations` lo stato è NEEDS_REVIEW e l'output è l'originale. Un claim falso del Critic viene smentito e non blocca |

Stati attesi: i CASE 001–004 chiudono in `SUCCESS_WITH_BREAKING_CHANGES`. Le loro modifiche breaking sono
attese e motivate dalle regole: nel 001 la security passa da apiKey a bearer, nel 002 le proprietà vengono
rinominate, nel 003 cambia il media type delle response di errore, nel 004 la security viene aggiunta. Il
CASE 005 chiude in `SUCCESS`, perché `Idempotency-Key` viene aggiunto come opzionale.

Altri test coprono: errori di parsing con riga e colonna, specifica vuota, `$ref` circolari, cache delle
regole e sua invalidazione, collisioni di `ruleId`, conflitti tra regole e tra operazioni del piano, target
mancanti nell'engine, tabella breaking, retry tecnici, timeout per chiamata e per run, preflight,
pipeline senza regole, formato della richiesta a Ollama (con trasporto HTTP simulato, incluso `think`).
**Qualità del Critic** (`tests/test_critic_quality.py`, `requires_ollama`). I candidati difettosi sono
costruiti in modo deterministico dai casi esistenti, senza Refactor Agent: response 404 rimossa (caso 005),
campo `required` rimosso da uno schema usato in request (caso 002), tipo di una proprietà cambiato da string
a integer (caso 005). Il Critic reale, con il modello di `config.yaml`, viene invocato sul frammento
modificato e deve produrre almeno una issue bloccante pertinente: confermata dal semantic diff, oppure
localizzata sul difetto. C'è anche un caso di controllo senza difetti, in cui il Critic deve accettare.
Esito e durata di ogni caso sono stampati a fine run e salvati in `output/critic-quality.json`.

In `tests/test_headers_and_breaking.py`: semantica di `requireHeader` sui tre livelli di dichiarazione,
Idempotency-Key opzionale in SUCCESS, header obbligatorio → `SUCCESS_WITH_BREAKING_CHANGES` con
`breakingChanges` ed exit code 4 dalla CLI, ricompilazione della cache del DSL precedente.

In `tests/test_feedback_loop.py`: correzioni deterministiche e valori generati, operazioni senza violazioni
scartate, `SET_FIELD` non motivati, target normalizzati o ambigui, Critic non invocato con ERROR di
governance, correzione peggiorativa scartata con output = miglior candidato, output = originale quando
nessun candidato migliora, tempi per fase e chiamate per ruolo in `summary.json`.

---

## 9. Limiti noti

- Un file per run. Supporto solo per OpenAPI 3.0/3.1 come target e solo Ollama come provider
  (l'interfaccia `LlmProvider` permette di aggiungerne altri).
- I prompt e la pipeline sono testati end-to-end con l'LLM finto. Con modelli reali su CPU la qualità delle
  proposte dipende dal modello, ma la correttezza del risultato resta decisa dai validatori: nel caso
  peggiore la run chiude in NEEDS_REVIEW, mai in un falso SUCCESS.
- Il Critic rivede solo i frammenti con modifiche dell'LLM o con change non tracciati. Una regola di
  giudizio non applicata a un frammento che l'LLM non ha toccato non viene segnalata.
# ApiRefactor

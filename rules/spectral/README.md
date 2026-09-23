# Digital Euro API Guidelines — Spectral ruleset

Ruleset Spectral per le regole **meccanicamente verificabili** delle
Digital Euro RESTful API Guidelines. 20 regole, tutte verificate contro
casi di test reali (vedi sotto) — non solo sintatticamente valide.

## Cosa c'è

```
digital-euro-ruleset.spectral.yaml   # il ruleset (file di configurazione)
functions/                           # 7 funzioni custom JS richiamate dal ruleset
non-mechanical-rules.md              # cosa NON è coperto e perché, per trasparenza
test-spec-with-violations.yaml       # spec di test con violazioni deliberate
```

## Uso

Richiede Node.js e il CLI Spectral:

```bash
npm install -g @stoplight/spectral-cli
spectral lint --ruleset digital-euro-ruleset.spectral.yaml --format json <spec.yaml>
```

L'output JSON (`code`, `path`, `message`, `severity`) è pensato per essere
parsato direttamente dal wrapper Python del `GovernanceValidator` e
convertito nel formato di violazione unificato della pipeline (`ruleId`,
`severity`, `path`, `message`, `expected`, `actual`, `suggestedFix`).

## Copertura

20 regole implementate, raggruppate per sezione del documento originale:

| Sezione | Regole |
|---|---|
| Specification Info | `DE-INFO-001`, `DE-INFO-002` |
| Data Formats | `DE-FMT-001`, `DE-FMT-002`, `DE-FMT-003` |
| URLs & Paths | `DE-PATH-001` … `DE-PATH-005` |
| JSON Payloads | `DE-JSON-001` … `DE-JSON-005` |
| HTTP Semantics | `DE-HTTP-001` |
| HTTP Status Codes | `DE-STATUS-001`, `DE-STATUS-002` |
| HTTP Headers | `DE-HDR-001` |
| Compatibility | `DE-COMPAT-001` |

Tutte e 20 sono state eseguite contro `test-spec-with-violations.yaml`
(e un secondo file di edge case per le 3 regole non coperte dal primo
test) e hanno prodotto la violazione attesa — non è un ruleset scritto
e mai eseguito.

Quello che **non** è qui — perché richiede giudizio semantico, non è
verificabile da una singola specifica statica, o è organizzativo — è
elencato punto per punto in `non-mechanical-rules.md`, con la
motivazione. Quelle regole restano competenza del RuleInterpreter (LLM)
+ CriticEngine della pipeline, non di Spectral.

## Design decisions rilevanti

- **`extends: []`**: questo ruleset non estende `spectral:oas`
  deliberatamente. La validità OpenAPI di base (sintassi, `$ref`, schema)
  è già responsabilità dell'`OpenAPIValidator` deterministico della
  pipeline (`openapi-spec-validator` in Python) — validarla due volte con
  due strumenti diversi avrebbe solo aggiunto rumore e messaggi duplicati.
- **Severity**: le regole MUST del documento originale sono `error`, le
  SHOULD sono `warn` — coerente con la distinzione INFO/WARNING/ERROR già
  usata nel resto della pipeline.
- **Alcune regole sono euristiche dichiarate tali** (es. `DE-JSON-004`
  pluralizzazione array basata su "termina con s", `DE-FMT-003` naming
  date/time): possono avere falsi positivi su casi irregolari (es. plurali
  irregolari in inglese). Sono severity `warn`, non `error`, proprio per
  questo — pensate per essere riviste, non applicate ciecamente.

## Estendere il ruleset

Essendo un file di configurazione separato dal codice dell'applicativo,
aggiungere una regola non richiede toccare la pipeline Python: basta
aggiungere una entry sotto `rules:` (funzione custom in `functions/` solo
se serve logica oltre a `pattern`/`casing`/`truthy`/`schema` nativi di
Spectral) e la nuova regola viene raccolta automaticamente al prossimo
`spectral lint`.

## Possibili estensioni future (non incluse ora)

Elencate anche in `non-mechanical-rules.md` come basso-priorità/v2:
- `MUST use 429 with headers` (Retry-After / X-RateLimit-*)
- `MUST define collection format for parameters` (style/explode)
- `MUST use official HTTP status codes` (enumerazione IANA)

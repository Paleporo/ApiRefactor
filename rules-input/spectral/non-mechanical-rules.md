# Regole NON coperte da Spectral (e perché)

Questo documento esiste per trasparenza: elenca ogni regola delle "Digital
Euro RESTful API Guidelines" che **non** è implementata in
`digital-euro-ruleset.spectral.yaml`, con la ragione. Niente del documento
originale viene "perso silenziosamente" — o è qui, o è coperta altrove
nella pipeline (RuleInterpreter/LLM, CriticEngine, semantic diff).

Legenda:
- **(A) Semantica** — richiede giudizio contestuale/di dominio. Va gestita
  dal RuleInterpreter (LLM) + CriticEngine, come regola in linguaggio
  naturale in `rules/*.md`.
- **(B) Non verificabile da spec statica** — riguarda comportamento a
  runtime, dati effettivi delle richieste, o confronto fra più versioni
  della specifica (compatibilità). Non è un compito per un linter su un
  singolo documento OpenAPI.
- **(C) Processo/organizzativo** — riguarda comunicazione con i client,
  governance, pubblicazione. Non è espresso nella specifica OpenAPI stessa.

## General
- MUST follow the API-first approach — (C)
- MUST provide OpenAPI YAML 3.x — garantito dalla pipeline stessa (step di
  conversione al target OpenAPI), non da una regola di linting
- MUST use ECB English — (A)

## Data Formats
- MUST use standard formats for country/language/currency — (A): servirebbe
  riconoscere semanticamente quali proprietà rappresentano paese/lingua/
  valuta, non solo il loro tipo
- SHOULD use standard formats for durations/intervals — (A)
- MUST use the common money object — (A): richiede riconoscere lo schema
  "money" per struttura, non solo per tipo

## URLs & Paths
- MUST pluralize resource names (segmenti di path) — (A): la pluralizzazione
  corretta richiede un dizionario linguistico, non solo regex (a differenza
  del pluralize sulle property array, dove l'euristica "termina con s" è
  accettabile ed è coperta da `DE-JSON-004`)
- MUST use URL-friendly resource identifiers — (B): riguarda i valori
  effettivi degli ID nelle richieste, non la specifica
- SHOULD model resources, keep URLs verb-free — (A)
- MUST use domain-specific resource names — (A)
- MUST identify resources/sub-resources via path segments — (A)
- SHOULD model complete business processes — (A)
- MUST use conventional query parameters (q, sort, offset, cursorId,
  cursorValue, limit) — (A): la regola ammette esplicitamente alias
  specifici del dominio, quindi un controllo rigido produrrebbe troppi
  falsi positivi

## JSON Payloads
- SHOULD use a unified schema for read/write (writeOnly/readOnly) — (A)
- SHOULD sanitize unsupported Unicode — (B)
- MUST use standard media types — coperto solo parzialmente (vedi
  `DE-STATUS-002` per gli errori); l'enforcement generale su
  `application/json` per i payload di successo non è incluso: basso
  valore aggiunto, rischio di falsi positivi con media type legittimi
  (file upload, ecc.)
- SHOULD define maps using additionalProperties con schema del valore — (A)
- MUST treat null e absent properties identicamente — (B)
- SHOULD not use null for empty arrays — (B)
- SHOULD use common field names (id, {something}Id, amount) — (A)

## HTTP Semantics
- MUST fulfil HTTP method properties (safe/idempotent/cacheable) — (A)
- SHOULD design idempotent POST operations — (A): gestita come regola
  separata in linguaggio naturale (Idempotency-Key) in `rules/*.md`
- MAY support asynchronous request processing — opzionale (MAY), non
  enforceable
- MUST define collection format for parameters (style/explode) — non
  implementata in questa versione: casi limitati nella pratica, da
  valutare come v2 se emergono violazioni reali
- SHOULD design query languages using query parameters — (A)
- MUST document implicit response filtering — (A)

## HTTP Status Codes
- MUST use official, specific, common status codes (registro IANA) — non
  implementata: richiederebbe un'enumerazione lunga e va aggiornata nel
  tempo; rischio di falsi positivi più alto del beneficio in questa fase
- SHOULD use 207 for batch/bulk requests — (A)
- MUST use 429 with headers (Retry-After o X-RateLimit-*) — non
  implementata in questa versione, da aggiungere come v2

## HTTP Headers
- MUST use Content-* headers correctly — (B)
- MAY support ETag — opzionale
- MUST support traceparent/tracestate — (B): comportamento del server a
  runtime, non verificabile dalla sola specifica

## Pagination
- MUST support pagination for list endpoints — (A): richiede riconoscere
  quali operation "restituiscono liste"
- SHOULD use pagination response page object (cursorValue/cursorId) — (A)
- SHOULD use pagination links — (A)
- SHOULD avoid total result counts — (A)

## Performance & Caching
- Tutte le regole di questa sezione sono SHOULD e riguardano
  comportamento a runtime o scelte implementative — (B)/(C)

## Compatibility
- MUST not break backward compatibility — (B): questo è il compito del
  **semantic diff** della pipeline (original vs refactored), non di un
  linter su un singolo documento
- SHOULD prefer compatible extensions — (A)
- SHOULD design APIs conservatively (reject unknown fields) — (B)
- MUST prepare clients for compatible extensions — (C): riguarda il
  comportamento dei client, non la specifica server
- SHOULD avoid versioning / MUST use URL versioning quando necessario —
  (A): la necessità di versionare è una decisione contestuale
- SHOULD use open-ended lists for extensible enums — (A)

## Deprecation
- MUST obtain client approval before shutdown — (C)
- MUST collect external partner consent on deprecation timeline — (C)
- MUST monitor deprecated APIs — (C)
- SHOULD add Deprecation/Sunset headers in responses — (B): comportamento
  runtime del server, non della specifica
- SHOULD monitor Deprecation/Sunset headers — (C), lato client
- MUST not start using deprecated APIs — (C), lato client

## Operation
- MUST publish OpenAPI specifications — (C)
- SHOULD monitor API usage — (C)

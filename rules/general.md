# Regole generali di refactoring

Ogni voce di elenco di primo livello è una regola. L'ID tra parentesi quadre è stabile:
compare nei report e nelle operazioni di refactoring che la regola genera. Senza ID esplicito
viene assegnato `<NOMEFILE>-<NNN>` in base alla posizione.

- [HTTP-IDEMPOTENCY-001] Ogni endpoint POST deve supportare Idempotency-Key: l'header di richiesta
  `Idempotency-Key` deve essere dichiarato come obbligatorio.
- [ERR-001] Gli errori (status 4xx e 5xx) devono usare RFC 9457 Problem Details: media type
  `application/problem+json` e uno schema con le proprietà `type`, `title`, `status` e `code`.
- [OPID-001] Tutte le operation devono avere un operationId univoco.
- [NAMING-001] Gli schema names devono essere PascalCase, le properties camelCase.
- [COMPAT-001] Il refactoring non deve rimuovere path, operation, parametri o response esistenti,
  né cambiarne il significato, se nessuna regola lo richiede esplicitamente.

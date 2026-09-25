"""Correzioni deterministiche: violazioni di requisiti meccanici con una correzione nota, senza LLM.

L'LLM (Refactor Agent / Correction Engine) riceve solo ciò che resta: nameCasing, regole di giudizio,
violazioni Spectral e del validator senza mapping. Le operazioni generate qui sono tracciate con il ruleId
della violazione, come tutte le altre, con `proposedBy: deterministic`.

Valori generati (scelte documentate anche nel README):
- operationId: `<metodo><SegmentiStatici>[By<Parametri>]` in camelCase, es. GET /accounts/{account-id} ->
  `getAccountsByAccountId`; suffisso numerico se già usato.
- description di una response aggiunta: la reason phrase HTTP dello status (es. 404 -> "Not Found").
- schema Problem (RFC 9457): proprietà type (uri), title, status (integer int32), detail, instance
  (uri-reference) più quelle richieste dalla regola (string); `required` = proprietà richieste dalla regola.
  Si riusa uno schema esistente se contiene già tutte le proprietà richieste; nome "Problem", oppure
  "ProblemDetails", "ProblemDetails2", ... se "Problem" esiste con un contenuto non conforme.
- security scheme: solo `http` (serve solo scheme/bearerFormat); nome di uno scheme conforme già definito,
  altrimenti `<scheme>Auth` (es. bearerAuth). apiKey/oauth2/openIdConnect richiedono dati (nome header,
  flussi, URL) non deducibili: restano all'LLM. Se l'operation ha già altri security requirement, sostituirli
  cambierebbe chi può accedere: resta all'LLM.
- query parameter e header aggiunti: schema `{"type": "string"}`.

Naming (requisiti nameCasing e regole Spectral di casing, dedotte dal ruleset): proprietà, parametri
(query, header, path), segmenti di path, nomi di schema, operationId. Il nome convertito (vedi
app/validators/casing.py) si usa solo se è conforme; in caso di collisione (il nome esiste già, o due nomi
diventerebbero uguali) non si rinomina nulla e la violazione passa all'LLM. Le rinomine aggiornano tutti i
riferimenti (app/refactor/references.py) e sono tracciate con il loro ruleId; quelle sul wire sono breaking.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.model import pointer as jp
from app.model.document import HTTP_METHODS, SpecDocument
from app.model.issues import Violation, ViolationSource
from app.model.refs import RefIndex
from app.refactor import operations as ops
from app.refactor.fragments import Fragment, fragment_for
from app.rules.models import Casing, CompiledRule, NameTarget, SpectralRule
from app.rules.registry import RuleRegistry, spectral_casing
from app.validators.casing import convert, convert_checked, words

PROPOSER = "deterministic"
_PROBLEM_BASE: dict[str, dict[str, Any]] = {
    "type": {"type": "string", "format": "uri"},
    "title": {"type": "string"},
    "status": {"type": "integer", "format": "int32"},
    "detail": {"type": "string"},
    "instance": {"type": "string", "format": "uri-reference"},
}


class DeterministicPlan(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    operations: list[ops.PlannedOperation] = Field(default_factory=list)
    handled: list[Violation] = Field(default_factory=list)
    # elementi (response) che le operazioni deterministiche riscrivono: le altre violazioni su questi elementi
    # non vanno all'LLM, che altrimenti proporrebbe modifiche concorrenti (es. REPLACE_RESPONSE_SCHEMA)
    covered: list[str] = Field(default_factory=list)
    # elementi rinominati: le altre violazioni esattamente su quell'elemento (es. naming date/time su una
    # proprietà che viene rinominata) si rivalutano dopo la rinomina invece di andare all'LLM
    covered_exact: list[str] = Field(default_factory=list)

    def is_llm_visible(self, violation: Violation) -> bool:
        if any(violation is h for h in self.handled):
            return False
        if violation.path in self.covered_exact:
            return False
        return not any(jp.is_prefix(c, violation.path) for c in self.covered)


def _operation_pointer(path: str) -> str | None:
    tokens = jp.split(path)
    if len(tokens) >= 3 and tokens[0] == "paths" and tokens[2] in HTTP_METHODS:
        return jp.join(tokens[:3])
    return None


def generate_operation_id(path: str, method: str, taken: set[str]) -> str:
    static = [s for s in path.strip("/").split("/") if s and not s.startswith("{")]
    params = [s[1:-1] for s in path.strip("/").split("/") if s.startswith("{") and s.endswith("}")]
    parts = [w.capitalize() for s in static for w in words(s)]
    base = method.lower() + "".join(parts)
    if params:
        base += "By" + "And".join("".join(w.capitalize() for w in words(p)) for p in params)
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}{n}", n + 1
    taken.add(candidate)
    return candidate


class DeterministicFixer:
    def __init__(self, doc: SpecDocument, registry: RuleRegistry, fragments: list[Fragment]):
        self.doc = doc
        self.registry = registry
        self.fragments = fragments
        self.refs = RefIndex(doc.data)
        self.plan = DeterministicPlan()
        self._taken_ids = {op.operation_id for op in doc.operations() if op.operation_id}
        self._problem_ref: dict[tuple, str] = {}
        self._scheme_name: dict[tuple, str] = {}
        self._renamed: dict[tuple, str] = {}  # (ambito, nome originale) -> nuovo nome pianificato
        self._claimed: set[tuple] = set()  # (ambito, nuovo nome): per rilevare due nomi che diventano uguali

    # ── API ────────────────────────────────────────────────────────────
    def fix(self, violations: list[Violation]) -> DeterministicPlan:
        for v in violations:
            rule = self.registry.get(v.rule_id)
            if v.source == ViolationSource.SPECTRAL and isinstance(rule, SpectralRule):
                casing = spectral_casing(rule)
                if casing is None:
                    continue  # regola Spectral senza correzione nota: all'LLM
                produced = self._rename(v, *casing)
            elif v.source == ViolationSource.COMPILED_RULE and v.requirement_index is not None \
                    and isinstance(rule, CompiledRule) and v.requirement_index < len(rule.requirements):
                req = rule.requirements[v.requirement_index]
                handler = getattr(self, f"_fix_{req.kind}", None)
                if handler is None:
                    continue  # judgment: al Critic
                produced = handler(v, req)
            else:
                continue
            if produced == ["already-planned"]:
                self.plan.handled.append(v)  # stesso path già rinominato per un'altra violazione
                continue
            if produced:
                self.plan.handled.append(v)
                fragment = fragment_for(v.path, self.fragments).pointer or "/"
                self.plan.operations.extend(
                    ops.PlannedOperation(operation=o, fragment=fragment, proposed_by=PROPOSER) for o in produced)
        return self.plan

    @staticmethod
    def _op(data: dict[str, Any]):
        return ops.OperationsProposal.model_validate({"operations": [data]}).operations[0]

    # ── naming ─────────────────────────────────────────────────────────
    def _fix_nameCasing(self, v: Violation, req) -> list:
        return self._rename(v, req.target, req.casing)

    def _claim(self, scope: tuple, old: str, new: str, existing: set[str], casing: Casing | None = None) -> bool:
        """Prenota la rinomina old -> new nell'ambito; False in caso di collisione.

        Collisione: `new` esiste già, oppure un altro nome dello stesso ambito diventerebbe anch'esso `new`
        (es. order_total e order__total -> orderTotal). In quel caso nessuno dei due viene rinominato.
        """
        key = (*scope, old)
        if key in self._renamed:
            return self._renamed[key] == new  # già pianificata (es. violata da due regole)
        if new in existing or (*scope, new) in self._claimed:
            return False
        if casing is not None and any(n != old and convert(n, casing) == new for n in existing):
            return False
        self._renamed[key] = new
        self._claimed.add((*scope, new))
        return True

    def _rename(self, v: Violation, target: NameTarget, casing: Casing) -> list:
        tokens = jp.split(v.path)
        rid = v.rule_id
        if target == NameTarget.SCHEMA_NAME:
            if len(tokens) != 3 or tokens[:2] != ["components", "schemas"]:
                return []
            old = tokens[2]
            new = convert_checked(old, casing)
            schemas = set(((self.doc.data.get("components") or {}).get("schemas") or {}))
            if not new or not self._claim(("schema",), old, new, schemas, casing):
                return []
            self.plan.covered_exact.append(v.path)
            return [self._op({"type": "RENAME_SCHEMA", "from": old, "to": new, "ruleId": rid})]

        if target == NameTarget.PROPERTY_NAME:
            if len(tokens) < 2 or tokens[-2] != "properties":
                return []
            schema_ptr, old = jp.join(tokens[:-2]), tokens[-1]
            schema = self.doc.get(schema_ptr)
            if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
                return []
            new = convert_checked(old, casing)
            if not new or not self._claim(("property", schema_ptr), old, new, set(schema["properties"]), casing):
                return []
            self.plan.covered_exact.append(v.path)
            return [self._op({"type": "RENAME_PROPERTY", "target": schema_ptr, "from": old, "to": new,
                              "ruleId": rid})]

        if target in (NameTarget.QUERY_PARAMETER, NameTarget.HEADER):
            # .../parameters/<idx>/name nel path item o nell'operation che lo dichiara
            if len(tokens) < 5 or tokens[0] != "paths" or tokens[-3] != "parameters" or tokens[-1] != "name":
                return []
            holder_ptr = jp.join(tokens[:-3])
            param = self.doc.get(jp.join(tokens[:-1]))
            location = "query" if target == NameTarget.QUERY_PARAMETER else "header"
            if not isinstance(param, dict) or param.get("in") != location:
                return []  # parametro via $ref: va rinominato il componente -> LLM
            old = str(param.get("name"))
            new = convert_checked(old, casing)
            siblings = self._declared_names(holder_ptr, location, exclude=old)
            if not new or not self._claim(("parameter", holder_ptr, location), old, new, siblings, casing):
                return []
            self.plan.covered_exact.append(jp.join(tokens[:-1]))
            return [self._op({"type": "RENAME_PARAMETER", "target": holder_ptr, "in": location, "from": old,
                              "to": new, "ruleId": rid})]

        if target == NameTarget.PATH_SEGMENT:
            return self._rename_path(v, casing)

        if target == NameTarget.OPERATION_ID:
            op_ptr = _operation_pointer(v.path)
            old = (self.doc.get(op_ptr) or {}).get("operationId") if op_ptr else None
            new = convert_checked(str(old), casing) if old else None
            if not new or not self._claim(("operationId",), old, new, self._taken_ids, casing):
                return []
            self._taken_ids.add(new)
            return [self._op({"type": "ADD_OPERATION_ID", "target": op_ptr, "operationId": new, "ruleId": rid})]
        return []

    def _declared_names(self, holder_ptr: str, location: str, exclude: str) -> set[str]:
        """Nomi dei parametri `location` visibili dove vive holder_ptr (path item + operation)."""
        tokens = jp.split(holder_ptr)
        holders = [jp.join(tokens[:2])] + ([holder_ptr] if len(tokens) == 3 else
                                             refs_operations(self.doc, tokens[1]))
        names = set()
        for ptr in holders:
            for p in (self.doc.get(ptr) or {}).get("parameters") or []:
                p = self.refs.resolve_ref(p)
                if isinstance(p, dict) and p.get("in") == location and p.get("name") != exclude:
                    names.add(str(p.get("name")))
        # gli header non distinguono maiuscole: "X-Id" e "x-id" collidono
        return names | ({n.lower() for n in names} if location == "header" else set())

    def _rename_path(self, v: Violation, casing: Casing) -> list:
        tokens = jp.split(v.path)
        if len(tokens) != 2 or tokens[0] != "paths":
            return []
        path = tokens[1]
        key = ("path-done", path)
        if key in self._renamed:
            return [] if self._renamed[key] == "" else ["already-planned"]
        segments = path.split("/")
        params = [s[1:-1] for s in segments if s.startswith("{") and s.endswith("}")]
        param_new = {p: convert_checked(p, casing) for p in params}
        param_new = {p: n for p, n in param_new.items() if n}
        static_new = [s if (s.startswith("{") or not s or convert_checked(s, casing) is None)
                      else convert_checked(s, casing) for s in segments]
        if len(set(params)) != len({param_new.get(p, p) for p in params}):
            self._renamed[key] = ""  # due path parameter diventerebbero uguali
            return []
        intermediate = "/".join("{" + param_new.get(s[1:-1], s[1:-1]) + "}" if s.startswith("{") else s
                                for s in segments)
        final = "/".join("{" + param_new.get(s[1:-1], s[1:-1]) + "}" if s.startswith("{") else n
                         for s, n in zip(segments, static_new))
        if final == path or not self._claim(("path",), path, final, set(self.doc.paths())):
            self._renamed[key] = ""
            return []
        self._renamed[key] = final
        ops_out = [self._op({"type": "RENAME_PARAMETER", "target": v.path, "in": "path", "from": p, "to": n,
                             "ruleId": v.rule_id}) for p, n in param_new.items()]
        if intermediate != final:  # applicato dopo le rinomine dei parametri (fase successiva del piano)
            ops_out.append(self._op({"type": "RENAME_PATH", "from": intermediate, "to": final, "ruleId": v.rule_id}))
        self.plan.covered_exact.append(v.path)
        return ops_out

    # ── requisiti ──────────────────────────────────────────────────────
    def _fix_requireHeader(self, v: Violation, req) -> list:
        target = _operation_pointer(v.path)
        if not target:
            return []
        return [self._op({"type": "ADD_HEADER", "target": target, "header": req.header, "required": req.required,
                          "ruleId": v.rule_id})]

    def _fix_requireQueryParameter(self, v: Violation, req) -> list:
        target = _operation_pointer(v.path)
        if not target:
            return []
        return [self._op({"type": "ADD_QUERY_PARAMETER", "target": target, "name": req.name,
                          "required": req.required, "ruleId": v.rule_id})]

    def _fix_requireOperationId(self, v: Violation, req) -> list:
        target = _operation_pointer(v.path)
        if not target:
            return []
        _, path, method = jp.split(target)
        new_id = generate_operation_id(path, method, self._taken_ids)
        return [self._op({"type": "ADD_OPERATION_ID", "target": target, "operationId": new_id, "ruleId": v.rule_id})]

    def _fix_requireResponse(self, v: Violation, req) -> list:
        target = _operation_pointer(v.path)
        if not target:
            return []
        try:
            description = HTTPStatus(int(req.status)).phrase
        except ValueError:
            description = f"Response {req.status}"
        return [self._op({"type": "ADD_RESPONSE", "target": target, "status": req.status,
                          "description": description, "ruleId": v.rule_id})]

    def _fix_requireSecurity(self, v: Violation, req) -> list:
        target = _operation_pointer(v.path)
        if not target or req.scheme_type != "http" or not req.scheme:
            return []
        op_node = self.doc.get(target) or {}
        effective = op_node["security"] if "security" in op_node else self.doc.data.get("security")
        if [n for r in (effective or []) for n in (r or {})]:
            return []  # ha già altri requisiti: sostituirli cambierebbe chi può accedere -> LLM
        wanted = {"type": "http", "scheme": req.scheme, **({"bearerFormat": req.bearer_format} if req.bearer_format else {})}
        out = []
        key = (req.scheme.lower(), req.bearer_format)
        name = self._scheme_name.get(key)
        if name is None:
            schemes = (self.doc.data.get("components") or {}).get("securitySchemes") or {}
            name = next((n for n, s in schemes.items() if isinstance(s, dict) and s.get("type") == "http"
                         and str(s.get("scheme", "")).lower() == req.scheme.lower()), None)
            if name is None:
                base = f"{req.scheme.lower()}Auth"
                name, n = base, 2
                while name in schemes:
                    name, n = f"{base}{n}", n + 1
                out.append(self._op({"type": "ADD_SECURITY_SCHEME", "name": name, "scheme": wanted,
                                     "ruleId": v.rule_id}))
            self._scheme_name[key] = name
        out.append(self._op({"type": "SET_SECURITY_REQUIREMENT", "target": target, "requirements": [{name: []}],
                             "ruleId": v.rule_id}))
        return out

    def _fix_errorFormat(self, v: Violation, req) -> list:
        tokens = jp.split(v.path)
        if "responses" not in tokens or len(tokens) < tokens.index("responses") + 2:
            return []
        response_ptr = jp.join(tokens[: tokens.index("responses") + 2])
        response = self.doc.get(response_ptr)
        if not isinstance(response, dict) or "$ref" in response:
            return []  # response riusabile via $ref: va convertito il componente -> LLM
        out = []
        key = (req.media_type, tuple(req.required_properties))
        ref = self._problem_ref.get(key)
        if ref is None:
            ref, create = self._problem_schema(req.required_properties)
            if create is not None:
                out.append(self._op({"type": "ADD_COMPONENT", "componentType": "schemas", "name": create[0],
                                     "definition": create[1], "ruleId": v.rule_id}))
            self._problem_ref[key] = ref
        out.append(self._op({"type": "CONVERT_ERROR_RESPONSE", "target": response_ptr, "mediaType": req.media_type,
                             "schemaRef": ref, "ruleId": v.rule_id}))
        if response_ptr not in self.plan.covered:
            self.plan.covered.append(response_ptr)
        return out

    def _problem_schema(self, required: list[str]) -> tuple[str, tuple[str, dict] | None]:
        schemas = (self.doc.data.get("components") or {}).get("schemas") or {}
        conforming = [n for n, s in schemas.items() if set(required) <= set(self._properties(s))]
        if conforming:
            name = "Problem" if "Problem" in conforming else conforming[0]
            return f"#/components/schemas/{jp.escape(name)}", None
        props = {**_PROBLEM_BASE, **{p: {"type": "string"} for p in required if p not in _PROBLEM_BASE}}
        definition = {"type": "object", "required": list(required), "properties": props}
        name, n = "Problem", 2
        if name in schemas:
            name = "ProblemDetails"
            while name in schemas:
                name, n = f"ProblemDetails{n}", n + 1
        return f"#/components/schemas/{name}", (name, definition)

    def _properties(self, schema: Any, depth: int = 0) -> list[str]:
        schema = self.refs.resolve_ref(schema)
        if depth > 10 or not isinstance(schema, dict):
            return []
        names = list((schema.get("properties") or {}).keys())
        for part in schema.get("allOf") or []:
            names += self._properties(part, depth + 1)
        return names


def refs_operations(doc: SpecDocument, path: str) -> list[str]:
    item = doc.paths().get(path) or {}
    return [jp.join(["paths", path, m]) for m in HTTP_METHODS if isinstance(item.get(m), dict)]


def deterministic_fixes(doc: SpecDocument, violations: list[Violation], registry: RuleRegistry,
                        fragments: list[Fragment]) -> DeterministicPlan:
    return DeterministicFixer(doc, registry, fragments).fix(violations)

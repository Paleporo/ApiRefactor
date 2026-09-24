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
from app.rules.models import CompiledRule
from app.rules.registry import RuleRegistry
from app.validators.casing import words

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

    def is_llm_visible(self, violation: Violation) -> bool:
        if any(violation is h for h in self.handled):
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

    # ── API ────────────────────────────────────────────────────────────
    def fix(self, violations: list[Violation]) -> DeterministicPlan:
        for v in violations:
            if v.source != ViolationSource.COMPILED_RULE or v.requirement_index is None:
                continue
            rule = self.registry.get(v.rule_id)
            if not isinstance(rule, CompiledRule) or v.requirement_index >= len(rule.requirements):
                continue
            req = rule.requirements[v.requirement_index]
            handler = getattr(self, f"_fix_{req.kind}", None)
            if handler is None:
                continue  # nameCasing, judgment: all'LLM
            produced = handler(v, req)
            if produced:
                self.plan.handled.append(v)
                fragment = fragment_for(v.path, self.fragments).pointer or "/"
                self.plan.operations.extend(
                    ops.PlannedOperation(operation=o, fragment=fragment, proposed_by=PROPOSER) for o in produced)
        return self.plan

    @staticmethod
    def _op(data: dict[str, Any]):
        return ops.OperationsProposal.model_validate({"operations": [data]}).operations[0]

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


def deterministic_fixes(doc: SpecDocument, violations: list[Violation], registry: RuleRegistry,
                        fragments: list[Fragment]) -> DeterministicPlan:
    return DeterministicFixer(doc, registry, fragments).fix(violations)

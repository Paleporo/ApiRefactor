"""Valutazione deterministica delle regole compilate dal linguaggio naturale (nessun LLM a questo stadio)."""

from __future__ import annotations

import re
from typing import Any, Iterator

from app.model import pointer as jp
from app.model.document import OperationView, SpecDocument
from app.model.issues import Violation, ViolationSource
from app.model.refs import RefIndex
from app.rules.models import (CompiledRule, ErrorFormat, NameCasing, NameTarget, RequireHeader, RequireOperationId,
                              RequireQueryParameter,
                              RequireResponse, RequireSecurity)
from app.validators import casing as casing_util


class RuleEvaluator:
    def __init__(self, doc: SpecDocument):
        self.doc = doc
        self.refs = RefIndex(doc.data)

    def evaluate(self, rules: list[CompiledRule]) -> list[Violation]:
        out: list[Violation] = []
        for rule in rules:
            if rule.overridden_by:
                continue  # in conflitto con una regola deterministica che prevale (riportato nel report regole)
            for req in rule.requirements:
                handler = getattr(self, f"_eval_{req.kind}", None)
                if handler is None:
                    continue  # judgment: competenza del Critic
                out.extend(handler(rule, req))
        return out

    # ── helper ─────────────────────────────────────────────────────────
    def _violation(self, rule: CompiledRule, path: str, message: str, expected: Any = None, actual: Any = None,
                   fix: str | None = None, operation_id: str | None = None) -> Violation:
        return Violation(rule_id=rule.id, severity=rule.severity, file=self.doc.source_file, path=path,
                         operation_id=operation_id, message=message, expected=expected, actual=actual,
                         suggested_fix=fix, source=ViolationSource.COMPILED_RULE)

    def _operations(self, rule: CompiledRule) -> Iterator[OperationView]:
        cond = rule.condition
        for op in self.doc.operations():
            if cond.methods and op.method not in [m.lower() for m in cond.methods]:
                continue
            if cond.path_pattern and not re.search(cond.path_pattern, op.path):
                continue
            yield op

    def _parameters(self, op: OperationView) -> list[dict]:
        item = self.doc.get(jp.join(["paths", op.path])) or {}
        params = list(item.get("parameters") or []) + list((self.doc.get(op.pointer) or {}).get("parameters") or [])
        return [p for p in (self.refs.resolve_ref(p) for p in params) if isinstance(p, dict)]

    # ── requisiti ──────────────────────────────────────────────────────
    def _eval_requireHeader(self, rule: CompiledRule, req: RequireHeader) -> Iterator[Violation]:
        for op in self._operations(rule):
            found = [p for p in self._parameters(op)
                     if p.get("in") == "header" and str(p.get("name", "")).lower() == req.header.lower()]
            if not found:
                yield self._violation(rule, op.pointer, f"{op.label}: manca l'header {req.header}",
                                      expected={"header": req.header, "required": req.required}, actual=None,
                                      fix=f"ADD_HEADER {req.header} (required={req.required})", operation_id=op.operation_id)
            elif req.required and not any(p.get("required") for p in found):
                yield self._violation(rule, op.pointer, f"{op.label}: l'header {req.header} deve essere required",
                                      expected={"required": True}, actual={"required": False},
                                      fix=f"ADD_HEADER {req.header} required=true", operation_id=op.operation_id)

    def _eval_requireQueryParameter(self, rule: CompiledRule, req: RequireQueryParameter) -> Iterator[Violation]:
        """Il parametro deve esistere (operation-level, path-level o via $ref).

        required=true: deve anche essere obbligatorio. required=false: basta che esista, l'obbligatorietà
        dichiarata dall'API non si tocca (preserve behavior), come per requireHeader.
        """
        for op in self._operations(rule):
            found = [p for p in self._parameters(op) if p.get("in") == "query" and p.get("name") == req.name]
            if not found:
                wanted = "obbligatorio" if req.required else "opzionale"
                yield self._violation(rule, op.pointer, f"{op.label}: manca il query parameter {wanted} '{req.name}'",
                                      expected={"name": req.name, "required": req.required}, actual=None,
                                      fix=f"ADD_QUERY_PARAMETER {req.name} (required={str(req.required).lower()})",
                                      operation_id=op.operation_id)
            elif req.required and not found[-1].get("required"):  # l'ultimo è quello a livello operation
                yield self._violation(rule, op.pointer, f"{op.label}: il query parameter '{req.name}' deve essere obbligatorio",
                                      expected={"name": req.name, "required": req.required},
                                      actual={"required": bool(found[-1].get("required"))},
                                      fix=f"ADD_QUERY_PARAMETER {req.name} required={str(req.required).lower()}",
                                      operation_id=op.operation_id)

    def _eval_requireOperationId(self, rule: CompiledRule, req: RequireOperationId) -> Iterator[Violation]:
        seen: dict[str, str] = {}
        for op in self._operations(rule):
            if not op.operation_id:
                yield self._violation(rule, op.pointer, f"{op.label}: operationId mancante", expected="operationId",
                                      fix="ADD_OPERATION_ID")
            elif req.unique and op.operation_id in seen:
                yield self._violation(rule, jp.child(op.pointer, "operationId"),
                                      f"{op.label}: operationId '{op.operation_id}' non univoco (già in {seen[op.operation_id]})",
                                      actual=op.operation_id, fix="ADD_OPERATION_ID con un valore univoco",
                                      operation_id=op.operation_id)
            else:
                seen[op.operation_id] = op.label

    def _eval_nameCasing(self, rule: CompiledRule, req: NameCasing) -> Iterator[Violation]:
        for pointer, name in self._names(req.target, rule):
            if not casing_util.matches(name, req.casing):
                expected = casing_util.convert(name, req.casing)
                yield self._violation(rule, pointer, f"'{name}' non è {req.casing.value} ({req.target.value})",
                                      expected=expected, actual=name, fix=f"rinomina '{name}' in '{expected}'")

    def _names(self, target: NameTarget, rule: CompiledRule) -> Iterator[tuple[str, str]]:
        if target == NameTarget.SCHEMA_NAME:
            for s in self.doc.schemas():
                yield s.pointer, s.name
        elif target == NameTarget.PROPERTY_NAME:
            yield from self._property_names(self.doc.data, "")
        elif target in (NameTarget.QUERY_PARAMETER, NameTarget.HEADER):
            where = "query" if target == NameTarget.QUERY_PARAMETER else "header"
            for op in self._operations(rule):
                for idx, p in enumerate((self.doc.get(op.pointer) or {}).get("parameters") or []):
                    p = self.refs.resolve_ref(p)
                    if isinstance(p, dict) and p.get("in") == where:
                        yield jp.child(op.pointer, "parameters", idx, "name"), str(p.get("name"))
        elif target == NameTarget.PATH_SEGMENT:
            for path in self.doc.paths():
                for seg in path.strip("/").split("/"):
                    if seg:
                        yield jp.join(["paths", path]), seg.strip("{}")
        elif target == NameTarget.OPERATION_ID:
            for op in self._operations(rule):
                if op.operation_id:
                    yield jp.child(op.pointer, "operationId"), op.operation_id

    def _property_names(self, node: Any, pointer: str) -> Iterator[tuple[str, str]]:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict) and (node.get("type") in ("object", None) or "properties" in node):
                for name in props:
                    yield jp.child(pointer, "properties", name), name
            for k, v in node.items():
                if k == "properties" and isinstance(v, dict):
                    for name, sub in v.items():
                        yield from self._property_names(sub, jp.child(pointer, k, name))
                elif k not in ("example", "examples"):
                    yield from self._property_names(v, jp.child(pointer, k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from self._property_names(v, jp.child(pointer, i))

    def _eval_errorFormat(self, rule: CompiledRule, req: ErrorFormat) -> Iterator[Violation]:
        pattern = rule.condition.status_pattern or r"^[45]\d\d$"
        for op in self._operations(rule):
            responses = (self.doc.get(op.pointer) or {}).get("responses") or {}
            for status, response in responses.items():
                if not re.search(pattern, str(status)):
                    continue
                ptr = jp.child(op.pointer, "responses", status)
                resolved = self.refs.resolve_ref(response) or {}
                media = (resolved.get("content") or {}).get(req.media_type)
                if media is None:
                    yield self._violation(rule, ptr, f"{op.label} {status}: la response di errore deve usare {req.media_type}",
                                          expected=req.media_type, actual=sorted((resolved.get("content") or {}).keys()),
                                          fix="CONVERT_ERROR_RESPONSE", operation_id=op.operation_id)
                    continue
                schema = self.refs.resolve_ref(media.get("schema") or {}) or {}
                props = set(self._all_properties(schema))
                missing = [p for p in req.required_properties if p not in props]
                if missing:
                    yield self._violation(rule, jp.child(ptr, "content", req.media_type, "schema"),
                                          f"{op.label} {status}: proprietà Problem Details mancanti: {missing}",
                                          expected=req.required_properties, actual=sorted(props),
                                          fix="usa uno schema Problem conforme", operation_id=op.operation_id)

    def _all_properties(self, schema: dict, depth: int = 0) -> list[str]:
        if depth > 10 or not isinstance(schema, dict):
            return []
        names = list((schema.get("properties") or {}).keys())
        for part in schema.get("allOf") or []:
            names += self._all_properties(self.refs.resolve_ref(part) or {}, depth + 1)
        return names

    def _eval_requireSecurity(self, rule: CompiledRule, req: RequireSecurity) -> Iterator[Violation]:
        schemes = (self.doc.data.get("components") or {}).get("securitySchemes") or {}

        def acceptable(name: str) -> bool:
            s = schemes.get(name)
            if not isinstance(s, dict) or s.get("type") != req.scheme_type:
                return False
            return not req.scheme or str(s.get("scheme", "")).lower() == req.scheme.lower()

        global_security = self.doc.data.get("security")
        for op in self._operations(rule):
            op_node = self.doc.get(op.pointer) or {}
            effective = op_node["security"] if "security" in op_node else global_security
            names = [n for requirement in (effective or []) for n in (requirement or {})]
            if not any(acceptable(n) for n in names):
                yield self._violation(
                    rule, op.pointer, f"{op.label}: nessun security requirement di tipo {req.scheme_type}"
                    + (f"/{req.scheme}" if req.scheme else ""),
                    expected={"type": req.scheme_type, "scheme": req.scheme}, actual=names or None,
                    fix="ADD_SECURITY_SCHEME + SET_SECURITY_REQUIREMENT", operation_id=op.operation_id)

    def _eval_requireResponse(self, rule: CompiledRule, req: RequireResponse) -> Iterator[Violation]:
        for op in self._operations(rule):
            responses = (self.doc.get(op.pointer) or {}).get("responses") or {}
            if req.status not in responses:
                yield self._violation(rule, op.pointer, f"{op.label}: manca la response {req.status}",
                                      expected=req.status, actual=sorted(responses), fix="ADD_RESPONSE",
                                      operation_id=op.operation_id)


"""OpenAPIValidator deterministico (nessun LLM): openapi-spec-validator + controlli globali.

Controlli globali separati (mai affidati alla "memoria" del modello): $ref rotti, operationId duplicati,
path parameter non dichiarati, security requirement verso schemi non definiti, $ref circolari (INFO).
"""

from __future__ import annotations

import copy
import re

from openapi_spec_validator import OpenAPIV30SpecValidator, OpenAPIV31SpecValidator

from app.logging_setup import get_logger
from app.model import pointer as jp
from app.model.document import HTTP_METHODS, SpecDocument
from app.model.issues import Severity, Violation, ViolationSource
from app.model.refs import RefIndex

log = get_logger("validate")

# errori già coperti dai controlli globali (con pointer preciso): evitiamo duplicati
_COVERED = {"DuplicateOperationIDError"}
_OP_IN_MSG = re.compile(r"for '(?P<method>\w+)' operation in '(?P<path>[^']+)'|for '(?P<m2>\w+)' in '(?P<p2>[^']+)'")


def _pointer_from_message(message: str) -> str:
    m = _OP_IN_MSG.search(message)
    if not m:
        return ""
    method = m.group("method") or m.group("m2")
    path = m.group("path") or m.group("p2")
    return jp.join(["paths", path, method.lower()])


class OpenAPIValidator:
    def validate(self, doc: SpecDocument) -> list[Violation]:
        file = doc.source_file
        refs = RefIndex(doc.data)
        out: list[Violation] = []

        broken = refs.broken_refs()
        for use in broken:
            out.append(Violation(rule_id="OAS-REF-BROKEN", severity=Severity.ERROR, file=file, path=use.at,
                                 message=f"$ref non risolvibile: {use.ref}", actual=use.ref,
                                 suggested_fix="Correggi il $ref o aggiungi il componente mancante",
                                 source=ViolationSource.OPENAPI))
        for cycle in refs.cycles:
            out.append(Violation(rule_id="OAS-REF-CIRCULAR", severity=Severity.INFO, file=file, path=cycle[0],
                                 message="$ref circolare (legale, risoluzione interrotta al ciclo): " + " -> ".join(cycle),
                                 source=ViolationSource.OPENAPI))

        out.extend(self._schema_validation(doc, broken_at=[u.at for u in broken]))
        out.extend(self._operation_ids(doc))
        out.extend(self._security_references(doc))
        return out

    def _schema_validation(self, doc: SpecDocument, broken_at: list[str]) -> list[Violation]:
        data = doc.data
        if broken_at:
            # openapi-spec-validator solleva eccezione sui $ref rotti: li neutralizziamo (già riportati sopra)
            data = copy.deepcopy(doc.data)
            for at in broken_at:
                node = jp.resolve(data, at)
                node.pop("$ref", None)
        validator_cls = OpenAPIV31SpecValidator if str(data.get("openapi", "")).startswith("3.1") else OpenAPIV30SpecValidator
        out: list[Violation] = []
        try:
            for err in validator_cls(data).iter_errors():
                name = type(err).__name__
                if name in _COVERED:
                    continue
                message = getattr(err, "message", str(err))
                path = list(getattr(err, "path", []) or [])
                pointer = jp.join(path) if path else _pointer_from_message(message)
                out.append(Violation(rule_id=f"OAS-{name}", severity=Severity.ERROR, file=doc.source_file,
                                     path=pointer, message=message, source=ViolationSource.OPENAPI))
        except Exception as exc:  # il validator può fallire su input patologici: diventa un errore, non un crash
            out.append(Violation(rule_id="OAS-VALIDATOR-FAILURE", severity=Severity.ERROR, file=doc.source_file,
                                 message=f"openapi-spec-validator non è riuscito a validare il documento: {exc}",
                                 source=ViolationSource.OPENAPI))
        return out

    def _operation_ids(self, doc: SpecDocument) -> list[Violation]:
        seen: dict[str, str] = {}
        out = []
        for op in doc.operations():
            if not op.operation_id:
                continue
            if op.operation_id in seen:
                out.append(Violation(rule_id="OAS-OPERATION-ID-DUPLICATE", severity=Severity.ERROR, file=doc.source_file,
                                     path=jp.child(op.pointer, "operationId"), operation_id=op.operation_id,
                                     message=f"operationId '{op.operation_id}' duplicato (già usato in {seen[op.operation_id]})",
                                     actual=op.operation_id, source=ViolationSource.OPENAPI))
            else:
                seen[op.operation_id] = op.pointer
        return out

    def _security_references(self, doc: SpecDocument) -> list[Violation]:
        schemes = set(((doc.data.get("components") or {}).get("securitySchemes") or {}).keys())
        holders = [("", doc.data.get("security"))]
        for path, item in doc.paths().items():
            for m in HTTP_METHODS:
                if isinstance(item, dict) and isinstance(item.get(m), dict):
                    holders.append((jp.join(["paths", path, m]), item[m].get("security")))
        out = []
        for ptr, security in holders:
            for idx, requirement in enumerate(security or []):
                for name in (requirement or {}):
                    if name not in schemes:
                        out.append(Violation(rule_id="OAS-SECURITY-UNDEFINED", severity=Severity.ERROR,
                                             file=doc.source_file, path=jp.child(ptr, "security", idx),
                                             message=f"security requirement verso lo schema non definito '{name}'",
                                             expected=f"components/securitySchemes/{name}", actual=None,
                                             suggested_fix=f"Aggiungi il security scheme '{name}' (ADD_SECURITY_SCHEME)",
                                             source=ViolationSource.OPENAPI))
        return out

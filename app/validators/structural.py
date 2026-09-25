"""Controlli strutturali deterministici sul contenuto degli schemi (oltre alla validità OpenAPI).

`required` orfano: ogni nome in un `required` deve esistere tra le proprietà dello schema o di quelli che
include (allOf, e i rami oneOf/anyOf, dove la proprietà può essere dichiarata in un ramo). Un membro inline
di un allOf vede anche le proprietà dei suoi fratelli. Un `required` orfano rende di fatto facoltativo un campo
senza che la validazione OpenAPI lo segnali (tipico dopo una rinomina non propagata).
Gli schemi senza proprietà né composizioni (aperti, `additionalProperties`) non vengono controllati.
"""

from __future__ import annotations

from typing import Any, Iterator

from app.model import pointer as jp
from app.model.document import SpecDocument
from app.model.issues import Severity, Violation, ViolationSource

RULE_ID = "STRUCT-REQUIRED-ORPHAN"
_COMBINERS = ("allOf", "oneOf", "anyOf")
_EXAMPLE_KEYS = ("example", "examples")


def _resolve(data: dict, node: Any) -> Any:
    seen: set[str] = set()
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and node["$ref"].startswith("#"):
        if node["$ref"] in seen:
            return node
        seen.add(node["$ref"])
        node = jp.resolve(data, node["$ref"][1:], None)
    return node


def _available(data: dict, schema: Any, visited: set[int]) -> set[str]:
    schema = _resolve(data, schema)
    if not isinstance(schema, dict) or id(schema) in visited:
        return set()
    visited.add(id(schema))
    names = set((schema.get("properties") or {}).keys())
    for comb in _COMBINERS:
        for member in schema.get(comb) or []:
            names |= _available(data, member, visited)
    return names


def _nodes(node: Any, pointer: str, parent: dict | None) -> Iterator[tuple[str, dict, dict | None]]:
    """(pointer, nodo, schema padre se il nodo è un membro inline di un allOf); salta i valori degli esempi."""
    if isinstance(node, dict):
        yield pointer, node, parent
        for key, value in node.items():
            if key in _EXAMPLE_KEYS:
                continue
            if key == "allOf" and isinstance(value, list):
                for i, member in enumerate(value):
                    yield from _nodes(member, jp.child(pointer, key, i), node)
            else:
                yield from _nodes(value, jp.child(pointer, key), None)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _nodes(value, jp.child(pointer, i), None)


def required_orphans(doc: SpecDocument) -> list[Violation]:
    data = doc.data
    out: list[Violation] = []
    for pointer, node, parent in _nodes(data, "", None):
        required = node.get("required")
        if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
            continue  # `required: true` di parametri e request body
        structured = "properties" in node or any(c in node for c in _COMBINERS)
        if not structured and parent is None:
            continue  # schema aperto: required verso proprietà aggiuntive
        available = _available(data, parent if parent is not None else node, set())
        if parent is not None:
            available |= _available(data, node, set())
        for idx, name in enumerate(required):
            if name not in available:
                out.append(Violation(
                    rule_id=RULE_ID, severity=Severity.ERROR, file=doc.source_file,
                    path=jp.child(pointer, "required", idx),
                    message=f"'{name}' è in required ma non è una proprietà dello schema né degli schemi inclusi: "
                            "il campo è di fatto facoltativo",
                    expected=sorted(available), actual=name,
                    suggested_fix="Allinea il required al nome della proprietà (o rimuovilo se la proprietà non esiste)",
                    source=ViolationSource.STRUCTURAL))
    return out

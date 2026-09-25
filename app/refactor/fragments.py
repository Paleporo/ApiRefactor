"""Context slicing: il documento viene elaborato per frammenti (documento / path / operation / componente).

All'LLM arriva solo il frammento + le definizioni puntuali che referenzia (cicli interrotti) + i nomi dei
componenti esistenti, entro un budget di token. La dimensione della spec fa crescere il numero di chiamate,
non la dimensione del contesto di ciascuna.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from app.logging_setup import get_logger
from app.model import pointer as jp
from app.model.document import ElementKind, SpecDocument
from app.model.issues import Violation
from app.model.refs import RefIndex

log = get_logger("slicing")

DOCUMENT_POINTER = ""


class Fragment(BaseModel):
    kind: ElementKind
    pointer: str
    label: str
    method: str | None = None
    path: str | None = None


def build_fragments(doc: SpecDocument) -> list[Fragment]:
    fragments = [Fragment(kind=ElementKind.DOCUMENT, pointer=DOCUMENT_POINTER, label="document")]
    for path, item in doc.paths().items():
        fragments.append(Fragment(kind=ElementKind.PATH, pointer=jp.join(["paths", path]), label=f"path {path}", path=path))
    for op in doc.operations():
        fragments.append(Fragment(kind=ElementKind.OPERATION, pointer=op.pointer, label=op.label, method=op.method,
                                  path=op.path))
    for ctype, entries in (doc.data.get("components") or {}).items():
        if not isinstance(entries, dict) or ctype == "securitySchemes":
            continue  # gli security scheme sono gestiti a livello documento
        for name in entries:
            kind = ElementKind.SCHEMA if ctype == "schemas" else ElementKind.DOCUMENT
            fragments.append(Fragment(kind=kind, pointer=jp.join(["components", ctype, name]),
                                      label=f"components/{ctype}/{name}"))
    return fragments


def fragment_for(pointer: str, fragments: list[Fragment]) -> Fragment:
    """Frammento più specifico che contiene `pointer`.

    Il nodo /paths/<p> (chiave del path) appartiene al frammento path; tutto sotto /paths/<p>/<method> all'operation.
    """
    best = fragments[0]
    best_len = -1
    for frag in fragments:
        if frag.pointer and jp.is_prefix(frag.pointer, pointer):
            n = len(jp.split(frag.pointer))
            if n > best_len:
                best, best_len = frag, n
    return best


def group_by_fragment(violations: list[Violation], fragments: list[Fragment]) -> dict[str, list[Violation]]:
    grouped: dict[str, list[Violation]] = {}
    for v in violations:
        grouped.setdefault(fragment_for(v.path, fragments).pointer, []).append(v)
    return grouped


def fragment_content(doc: SpecDocument, fragment: Fragment) -> Any:
    if fragment.kind == ElementKind.DOCUMENT and fragment.pointer == DOCUMENT_POINTER:
        # livello documento: tutto tranne paths e components (di cui si danno solo i nomi)
        top = {k: v for k, v in doc.data.items() if k not in ("paths", "components")}
        schemes = (doc.data.get("components") or {}).get("securitySchemes")
        if schemes:
            top["components"] = {"securitySchemes": schemes}
        return top
    if fragment.kind == ElementKind.PATH:
        item = doc.get(fragment.pointer) or {}
        # per il path basta il template + parametri di path-level + i metodi presenti
        return {"path": fragment.path, "parameters": item.get("parameters", []),
                "methods": [m for m in item if isinstance(item[m], dict) and m != "parameters"]}
    return doc.get(fragment.pointer)


def _tokens(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str)) // 4


def build_slice(doc: SpecDocument, fragment: Fragment, refs: RefIndex, budget_tokens: int) -> dict[str, Any]:
    content = fragment_content(doc, fragment)
    definitions, truncated = ({}, [])
    if fragment.pointer != DOCUMENT_POINTER:
        definitions, truncated = refs.closure(fragment.pointer, max_depth=1)
    components = doc.data.get("components") or {}
    payload: dict[str, Any] = {
        "fragment": {"pointer": fragment.pointer or "/", "kind": fragment.kind.value, "label": fragment.label},
        "content": content,
        "referencedDefinitions": {},
        "truncatedReferences": [],
        "existingComponentNames": {k: sorted(v) for k, v in components.items() if isinstance(v, dict)},
    }
    if fragment.kind == ElementKind.OPERATION and fragment.path is not None:
        # contesto minimo dal path item: solo i parametri dichiarati a livello di path (non le altre operation)
        path_params = (doc.paths().get(fragment.path) or {}).get("parameters")
        if path_params:
            payload["pathParameters"] = path_params
            definitions.update({t: refs.definitions[t] for p in path_params if isinstance(p, dict)
                                for t in [refs.target_pointer(p.get("$ref", ""))] if t in refs.definitions})
    used = _tokens(payload)
    if used > budget_tokens:
        log.warning("[SLICE] %s supera da solo il budget (%d > %d token): inviato comunque, senza definizioni",
                    fragment.label, used, budget_tokens)
    for ptr, definition in definitions.items():
        cost = _tokens(definition)
        if used + cost > budget_tokens:
            truncated.append(ptr)
            continue
        payload["referencedDefinitions"][ptr] = definition
        used += cost
    payload["truncatedReferences"] = sorted(set(truncated))
    log.debug("[SLICE] %s: ~%d token, %d definizioni incluse, %d troncate", fragment.label, used,
              len(payload["referencedDefinitions"]), len(payload["truncatedReferences"]))
    return payload


def render(obj: Any) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False, default=str)

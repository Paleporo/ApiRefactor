"""Aggiornamento dei riferimenti quando un nome cambia (rinomine deterministiche o proposte dall'LLM).

Oltre alla chiave rinominata, un nome compare in altri punti del documento:
- schema: `$ref`, `discriminator.mapping` (come `$ref` o come nome semplice);
- proprietà: `required` (anche degli schemi che la ereditano via allOf, direttamente o in modo transitivo),
  `example`/`examples` a qualunque profondità seguendo lo schema, `discriminator.propertyName`;
- parametro: template del path (in=path), `links` che puntano all'operation (chiavi di `parameters`, anche
  nella forma `<in>.<nome>`) ed espressioni `$request.<in>.<nome>` nei link dell'operation stessa;
- path e operationId: `links.operationRef` / `links.operationId`.
Un esempio la cui corrispondenza con lo schema non è determinabile (rami oneOf/anyOf, externalValue) non viene
toccato ma segnalato per revisione.
"""

from __future__ import annotations

from typing import Any, Iterator

from app.model import pointer as jp
from app.model.document import HTTP_METHODS


def _walk(node: Any, pointer: str = "") -> Iterator[tuple[str, Any]]:
    yield pointer, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, jp.child(pointer, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, jp.child(pointer, i))


def rename_key(mapping: dict, old: str, new: str) -> bool:
    """Rinomina una chiave preservando l'ordine; False se la chiave non c'è o la nuova esiste già."""
    if old not in mapping or new in mapping:
        return False
    items = list(mapping.items())
    mapping.clear()
    for k, v in items:
        mapping[new if k == old else k] = v
    return True


# ── schemi ─────────────────────────────────────────────────────────────
def rename_schema_in_discriminators(data: dict, old_name: str, new_name: str) -> int:
    old_ref = f"#/components/schemas/{jp.escape(old_name)}"
    new_ref = f"#/components/schemas/{jp.escape(new_name)}"
    count = 0
    for _, node in _walk(data):
        mapping = (node.get("discriminator") or {}).get("mapping") if isinstance(node, dict) and \
            isinstance(node.get("discriminator"), dict) else None
        if isinstance(mapping, dict):
            for k, v in mapping.items():
                if v == old_ref:
                    mapping[k], count = new_ref, count + 1
                elif v == old_name:  # forma breve: nome dello schema
                    mapping[k], count = new_name, count + 1
    return count


# ── proprietà ──────────────────────────────────────────────────────────
_EXAMPLE_KEYS = ("example", "examples")


def _resolve(data: dict, node: Any, max_hops: int = 20) -> Any:
    seen: set[str] = set()
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and node["$ref"].startswith("#"):
        ref = node["$ref"]
        if ref in seen or max_hops == 0:
            return node
        seen.add(ref)
        max_hops -= 1
        node = jp.resolve(data, ref[1:], None)
    return node


def _reaches(data: dict, schema: Any, target: dict, visited: set[int] | None = None) -> bool:
    """True se `target` compare nell'albero dello schema (ref, proprietà, items, composizioni)."""
    visited = visited if visited is not None else set()
    schema = _resolve(data, schema)
    if schema is target:
        return True
    if not isinstance(schema, dict) or id(schema) in visited:
        return False
    visited.add(id(schema))
    children = list((schema.get("properties") or {}).values())
    children += [schema.get("items"), schema.get("additionalProperties")]
    for comb in ("allOf", "oneOf", "anyOf"):
        children += list(schema.get(comb) or [])
    return any(isinstance(c, dict) and _reaches(data, c, target, visited) for c in children)


def _includes(data: dict, schema: Any, target: dict, visited: set[int] | None = None) -> bool:
    """True se lo schema include `target` via allOf, direttamente o in modo transitivo."""
    visited = visited if visited is not None else set()
    schema = _resolve(data, schema)
    if not isinstance(schema, dict) or id(schema) in visited:
        return False
    visited.add(id(schema))
    for member in schema.get("allOf") or []:
        resolved = _resolve(data, member)
        if resolved is target or _includes(data, resolved, target, visited):
            return True
    return False


def _declared(data: dict, schema: Any, exclude: dict, visited: set[int] | None = None) -> set[str]:
    """Proprietà dichiarate dallo schema e dai suoi membri allOf, escluso lo schema `exclude`."""
    visited = visited if visited is not None else set()
    schema = _resolve(data, schema)
    if not isinstance(schema, dict) or schema is exclude or id(schema) in visited:
        return set()
    visited.add(id(schema))
    names = set((schema.get("properties") or {}).keys())
    for member in schema.get("allOf") or []:
        names |= _declared(data, member, exclude, visited)
    return names


def _schema_nodes(data: dict) -> Iterator[tuple[str, dict]]:
    """Nodi dict del documento, esclusi i valori degli esempi (dati utente, non schemi)."""
    def walk(node: Any, pointer: str) -> Iterator[tuple[str, dict]]:
        if isinstance(node, dict):
            yield pointer, node
            for k, v in node.items():
                if k not in _EXAMPLE_KEYS:
                    yield from walk(v, jp.child(pointer, k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from walk(v, jp.child(pointer, i))
    yield from walk(data, "")


def rename_inherited_required(data: dict, target: dict, old: str, new: str) -> int:
    """`required` degli schemi che includono `target` via allOf (anche transitivamente) e dei loro membri inline."""
    count = 0
    for _, node in _schema_nodes(data):
        if node is target or not node.get("allOf") or not _includes(data, node, target):
            continue
        if old in _declared(data, node, exclude=target):
            continue  # il nome è dichiarato anche da un altro membro: il required si riferisce a quello
        holders = [node] + [m for m in node.get("allOf") or [] if isinstance(m, dict) and "$ref" not in m]
        for holder in holders:
            required = holder.get("required")
            if isinstance(required, list) and old in required:
                holder["required"] = [new if r == old else r for r in required]
                count += 1
    return count


def _walk_example(data: dict, value: Any, schema: Any, target: dict, old: str, new: str, ptr: str,
                  review: list[str], visited: set[tuple[int, int]]) -> int:
    """Rinomina la chiave `old` negli oggetti dell'esempio che corrispondono a `target`, seguendo lo schema."""
    schema = _resolve(data, schema)
    if not isinstance(schema, dict) or (id(value), id(schema)) in visited:
        return 0
    visited.add((id(value), id(schema)))
    count = 0
    if schema is target and isinstance(value, dict):
        count += int(rename_key(value, old, new))
    for member in schema.get("allOf") or []:
        count += _walk_example(data, value, member, target, old, new, ptr, review, visited)
    for comb in ("oneOf", "anyOf"):
        if any(_reaches(data, b, target) for b in schema.get(comb) or []) and ptr not in review:
            review.append(ptr)  # quale ramo descriva l'esempio non è determinabile
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        extra = schema.get("additionalProperties")
        for key, item in value.items():
            sub = props.get(key) if key in props else (extra if isinstance(extra, dict) else None)
            if sub is not None:
                count += _walk_example(data, item, sub, target, old, new, jp.child(ptr, key), review, visited)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            count += _walk_example(data, item, schema["items"], target, old, new, jp.child(ptr, i), review, visited)
    return count


def _example_values(data: dict, holder: dict, pointer: str) -> Iterator[tuple[str, Any, bool]]:
    """(pointer, valore, determinabile) di example/examples di un holder (schema, media type o parametro)."""
    if "example" in holder:
        yield jp.child(pointer, "example"), holder["example"], True
    examples = holder.get("examples")
    if isinstance(examples, list):  # schema 3.1: lista di esempi
        for i, e in enumerate(examples):
            yield jp.child(pointer, "examples", i), e, True
    elif isinstance(examples, dict):  # media type / parametro: {nome: Example Object}
        for name, e in examples.items():
            ptr = jp.child(pointer, "examples", name)
            if isinstance(e, dict) and isinstance(e.get("$ref"), str):
                ptr = e["$ref"][1:]
                e = _resolve(data, e)
            if isinstance(e, dict) and "value" in e:
                yield jp.child(ptr, "value"), e["value"], True
            elif isinstance(e, dict) and "externalValue" in e:
                yield jp.child(ptr, "externalValue"), None, False


def rename_in_examples(data: dict, target: dict, old: str, new: str) -> tuple[int, list[str]]:
    """Aggiorna gli esempi a qualunque profondità. Ritorna (chiavi rinominate, esempi da rivedere)."""
    count, review = 0, []
    visited: set[tuple[int, int]] = set()
    for pointer, node in _schema_nodes(data):
        if not any(k in node for k in _EXAMPLE_KEYS):
            continue
        # media type e parametri descrivono il valore con `schema`; uno schema descrive i propri esempi
        schema = node.get("schema") if isinstance(node.get("schema"), dict) else node
        if not _reaches(data, schema, target):
            continue
        for ptr, value, determinable in _example_values(data, node, pointer):
            if not determinable:
                if ptr not in review:
                    review.append(ptr)  # esempio esterno: non aggiornabile
                continue
            count += _walk_example(data, value, schema, target, old, new, ptr, review, visited)
    return count, review


def rename_property_references(data: dict, schema_ptr: str, old: str, new: str) -> tuple[int, list[str]]:
    """Riferimenti alla proprietà `old` dello schema in `schema_ptr` (già rinominata in `new`).

    Aggiorna: `required` ereditati via allOf, esempi a qualunque profondità, `discriminator.propertyName`.
    Ritorna (riferimenti aggiornati, esempi non aggiornabili da rivedere).
    """
    target = jp.resolve(data, schema_ptr, None)
    if not isinstance(target, dict):
        return 0, []
    count = rename_inherited_required(data, target, old, new)
    examples, review = rename_in_examples(data, target, old, new)
    count += examples
    for _, node in _schema_nodes(data):
        disc = node.get("discriminator")
        if isinstance(disc, dict) and disc.get("propertyName") == old and \
                (node is target or _includes(data, node, target)):
            disc["propertyName"], count = new, count + 1
    return count, review


# ── operation, parametri, link ─────────────────────────────────────────
def _links(data: dict) -> Iterator[dict]:
    for _, node in _walk(data):
        if isinstance(node, dict) and isinstance(node.get("links"), dict):
            for link in node["links"].values():
                if isinstance(link, dict):
                    yield link
    for link in ((data.get("components") or {}).get("links") or {}).values():
        if isinstance(link, dict):
            yield link


def _targets_operation(link: dict, op_ptr: str, operation_id: str | None) -> bool:
    return (operation_id is not None and link.get("operationId") == operation_id) or link.get("operationRef") == "#" + op_ptr


def rename_operation_ref(data: dict, old_op_ptr: str, new_op_ptr: str) -> int:
    count = 0
    for link in _links(data):
        ref = link.get("operationRef")
        if isinstance(ref, str) and (ref == "#" + old_op_ptr or ref.startswith("#" + old_op_ptr + "/")):
            link["operationRef"], count = "#" + new_op_ptr + ref[len(old_op_ptr) + 1:], count + 1
    return count


def rename_path_refs(data: dict, old_path: str, new_path: str) -> int:
    return rename_operation_ref(data, jp.join(["paths", old_path]), jp.join(["paths", new_path]))


def rename_operation_id_refs(data: dict, old: str, new: str) -> int:
    count = 0
    for link in _links(data):
        if link.get("operationId") == old:
            link["operationId"], count = new, count + 1
    return count


def rename_parameter_in_links(data: dict, op_ptrs: list[str], location: str, old: str, new: str) -> int:
    """Link che puntano alle operation `op_ptrs` (chiavi di parameters) + espressioni $request nei loro link."""
    count = 0
    for op_ptr in op_ptrs:
        operation = jp.resolve(data, op_ptr, None)
        operation_id = operation.get("operationId") if isinstance(operation, dict) else None
        for link in _links(data):
            params = link.get("parameters")
            if _targets_operation(link, op_ptr, operation_id) and isinstance(params, dict):
                count += int(rename_key(params, old, new)) + int(rename_key(params, f"{location}.{old}",
                                                                           f"{location}.{new}"))
        # espressioni runtime nei link delle response dell'operation stessa: $request.path.<nome>
        expr_old, expr_new = f"$request.{location}.{old}", f"$request.{location}.{new}"
        for response in ((operation or {}).get("responses") or {}).values() if isinstance(operation, dict) else []:
            for link in ((response or {}).get("links") or {}).values() if isinstance(response, dict) else []:
                if isinstance(link, dict):
                    for key, value in list((link.get("parameters") or {}).items()):
                        if isinstance(value, str) and expr_old in value:
                            link["parameters"][key], count = value.replace(expr_old, expr_new), count + 1
                    body = link.get("requestBody")
                    if isinstance(body, str) and expr_old in body:
                        link["requestBody"], count = body.replace(expr_old, expr_new), count + 1
    return count


def operations_of_path(data: dict, path: str) -> list[str]:
    item = (data.get("paths") or {}).get(path) or {}
    return [jp.join(["paths", path, m]) for m in HTTP_METHODS if isinstance(item.get(m), dict)]


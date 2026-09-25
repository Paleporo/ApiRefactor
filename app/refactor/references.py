"""Aggiornamento dei riferimenti quando un nome cambia (rinomine deterministiche o proposte dall'LLM).

Oltre alla chiave rinominata, un nome compare in altri punti del documento:
- schema: `$ref`, `discriminator.mapping` (come `$ref` o come nome semplice);
- proprietà: `required`, `example`/`examples` dello schema e dei media type che lo usano (chiavi di primo
  livello degli oggetti di esempio), `discriminator.propertyName`;
- parametro: template del path (in=path), `links` che puntano all'operation (chiavi di `parameters`, anche
  nella forma `<in>.<nome>`) ed espressioni `$request.<in>.<nome>` nei link dell'operation stessa;
- path e operationId: `links.operationRef` / `links.operationId`.
Limiti noti: `required` di schemi che includono lo schema via `allOf` e gli esempi annidati oltre il primo
livello non vengono aggiornati.
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
def _rename_in_example(example: Any, old: str, new: str) -> int:
    if isinstance(example, dict):
        return int(rename_key(example, old, new))
    if isinstance(example, list):  # array di oggetti
        return sum(_rename_in_example(item, old, new) for item in example)
    return 0


def _rename_in_example_holder(holder: dict, old: str, new: str) -> int:
    count = _rename_in_example(holder.get("example"), old, new)
    examples = holder.get("examples")
    if isinstance(examples, dict):  # media type: {nome: {value: ...}}
        count += sum(_rename_in_example(e.get("value"), old, new) for e in examples.values() if isinstance(e, dict))
    elif isinstance(examples, list):  # schema 3.1: lista di esempi
        count += sum(_rename_in_example(e, old, new) for e in examples)
    return count


def _schema_uses(schema: Any, ref: str) -> bool:
    if not isinstance(schema, dict):
        return False
    if schema.get("$ref") == ref:
        return True
    return isinstance(schema.get("items"), dict) and schema["items"].get("$ref") == ref


def rename_property_references(data: dict, schema_ptr: str, old: str, new: str) -> int:
    """Esempi e discriminator che citano la proprietà `old` dello schema in `schema_ptr`."""
    schema = jp.resolve(data, schema_ptr, None)
    count = 0
    if isinstance(schema, dict):
        count += _rename_in_example_holder(schema, old, new)
        disc = schema.get("discriminator")
        if isinstance(disc, dict) and disc.get("propertyName") == old:
            disc["propertyName"], count = new, count + 1
    tokens = jp.split(schema_ptr)
    if len(tokens) == 3 and tokens[:2] == ["components", "schemas"]:
        ref = "#" + schema_ptr
        for ptr, node in _walk(data):
            if not isinstance(node, dict):
                continue
            # media type che usano lo schema (anche come items di un array)
            if _schema_uses(node.get("schema"), ref) and ("example" in node or "examples" in node):
                count += _rename_in_example_holder(node, old, new)
            # schemi che lo includono via allOf: il discriminator può stare nel genitore
            if any(isinstance(p, dict) and p.get("$ref") == ref for p in node.get("allOf") or []):
                disc = node.get("discriminator")
                if isinstance(disc, dict) and disc.get("propertyName") == old:
                    disc["propertyName"], count = new, count + 1
    elif "content" in tokens and tokens[-1] == "schema":  # schema inline di un media type
        media = jp.resolve(data, jp.join(tokens[:-1]), None)
        if isinstance(media, dict):
            count += _rename_in_example_holder(media, old, new)
    return count


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


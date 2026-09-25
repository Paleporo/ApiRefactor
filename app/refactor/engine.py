"""RefactoringEngine: applica le RefactorOperation direttamente sull'Object Model (mai riscrittura YAML libera).

Ogni applicazione produce un AppliedChange classificato (STRUCTURAL / GOVERNANCE / SEMANTIC).
Un target mancante è un errore esplicito (ApplyFailure), mai uno skip silenzioso.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Callable

from app.logging_setup import get_logger
from app.model import pointer as jp
from app.model.document import HTTP_METHODS, SpecDocument
from app.refactor import operations as ops
from app.refactor import references as refs
from app.refactor.operations import AppliedChange, ApplyFailure, ChangeCategory

log = get_logger("engine")

# chiavi puramente documentali: modificarle non cambia il contratto
DOC_ONLY_KEYS = {"description", "summary", "title", "example", "examples", "operationId", "tags", "externalDocs",
                 "contact", "license", "termsOfService", "deprecated"}


class ApplyError(Exception):
    pass


def _rename_key(mapping: dict, old: str, new: str) -> None:
    """Rinomina una chiave preservando l'ordine."""
    items = list(mapping.items())
    mapping.clear()
    for k, v in items:
        mapping[new if k == old else k] = v


def _walk_refs(node: Any, pointer: str, fn: Callable[[dict, str], None]) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            fn(node, pointer)
        for k, v in node.items():
            _walk_refs(v, jp.child(pointer, k), fn)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_refs(v, jp.child(pointer, i), fn)


def _doc_only(pointer: str) -> bool:
    tokens = jp.split(pointer)
    if not tokens:
        return False
    return tokens[0] == "info" or tokens[-1] in DOC_ONLY_KEYS or tokens[-1].startswith("x-")


def _require(doc: SpecDocument, pointer: str, what: str) -> Any:
    node = doc.get(pointer)
    if node is None:
        raise ApplyError(f"{what} non trovato: {pointer or '/'} (target inesistente nel documento corrente)")
    return node


def _require_operation(doc: SpecDocument, pointer: str) -> dict:
    tokens = jp.split(pointer)
    if len(tokens) != 3 or tokens[0] != "paths" or tokens[2] not in HTTP_METHODS:
        raise ApplyError(f"il target deve essere una operation (/paths/<path>/<method>), ricevuto: {pointer}")
    node = _require(doc, pointer, "operation")
    if not isinstance(node, dict):
        raise ApplyError(f"{pointer} non è una operation")
    return node


POINTER_FIELDS = ("target", "source")
CREATES_LEAF = {"SET_FIELD"}


def normalize_targets(doc: SpecDocument, op):
    """Corregge i target con `/` non escapati (es. `.../content/application/json`) se la correzione è univoca.

    Ritorna (operazione, nota); se nulla cambia la nota è vuota. Un target non risolvibile resta com'è e
    l'applicazione fallirà in modo esplicito, come prima.
    """
    updates, notes = {}, []
    for field in POINTER_FIELDS:
        value = getattr(op, field, None)
        if not isinstance(value, str) or not value or doc.exists(value):
            continue
        fixed = jp.normalize(doc.data, value, allow_new_leaf=op.type in CREATES_LEAF)
        if fixed and fixed != value:
            updates[field] = fixed
            notes.append(f"{field} normalizzato da {value!r} a {fixed!r}")
    if not updates:
        return op, ""
    return op.model_copy(update=updates), "; ".join(notes)


class RefactoringEngine:
    def apply(
        self, doc: SpecDocument, planned: list[ops.PlannedOperation], iteration: int = 0
    ) -> tuple[SpecDocument, list[AppliedChange], list[ApplyFailure]]:
        """Applica le operazioni in ordine su una copia del documento: il documento in ingresso non viene toccato."""
        result = doc.clone()
        applied: list[AppliedChange] = []
        failures: list[ApplyFailure] = []
        for item in planned:
            op, note = normalize_targets(result, item.operation)
            snapshot = copy.deepcopy(result.data)
            try:
                change = getattr(self, f"_apply_{op.type.lower()}")(result, op)
            except (ApplyError, KeyError, ValueError, IndexError, TypeError) as exc:
                result.data = snapshot  # atomicità per operazione
                target = getattr(op, "target", None) or getattr(op, "from_", None) or getattr(op, "name", "")
                reason = str(exc) if isinstance(exc, ApplyError) else f"{type(exc).__name__}: {exc}"
                failures.append(ApplyFailure(type=op.type, rule_id=op.rule_id, target=str(target), reason=reason,
                                             iteration=iteration))
                log.warning("[ENGINE] %s (%s) NON applicata: %s", op.type, op.rule_id, reason)
                continue
            if change is None:
                log.debug("[ENGINE] %s (%s): nessun effetto (già conforme)", op.type, op.rule_id)
                continue
            change.iteration = iteration
            change.proposed_by = item.proposed_by
            if note:
                change.description += f" ({note})"
            applied.append(change)
            log.debug("[ENGINE] %s [%s] %s", change.category.value, op.rule_id, change.description)
        return result, applied, failures

    # ── singole operazioni ──────────────────────────────────────────────
    def _apply_update_openapi_version(self, doc: SpecDocument, op: ops.UpdateOpenApiVersion) -> AppliedChange | None:
        before = doc.data.get("openapi")
        if before == op.version:
            return None
        doc.data["openapi"] = op.version
        return AppliedChange(type=op.type, rule_id=op.rule_id, category=ChangeCategory.STRUCTURAL, locations=["/openapi"],
                             description=f"openapi {before} -> {op.version}", before=before, after=op.version)

    def _apply_add_header(self, doc: SpecDocument, op: ops.AddHeader) -> AppliedChange | None:
        return self._add_parameter(doc, op, "header", op.header, op.required, op.schema_, op.description,
                                   case_insensitive=True)  # i nomi degli header HTTP non distinguono maiuscole

    def _apply_add_query_parameter(self, doc: SpecDocument, op: ops.AddQueryParameter) -> AppliedChange | None:
        return self._add_parameter(doc, op, "query", op.name, op.required, op.schema_, op.description)

    def _add_parameter(self, doc: SpecDocument, op, location: str, name: str, required: bool, schema: dict,
                       description: str | None, case_insensitive: bool = False) -> AppliedChange | None:
        """required=false: aggiunge il parametro opzionale solo se manca, senza mai toccarne l'obbligatorietà.
        required=true: lo aggiunge obbligatorio o rende obbligatorio quello esistente (SEMANTIC).
        Un parametro definito a livello di path o via $ref non viene duplicato sulla singola operation."""
        operation = _require_operation(doc, op.target)
        kind = "header" if location == "header" else "query parameter"

        def same(p: Any) -> bool:
            if not isinstance(p, dict) or p.get("in") != location:
                return False
            other = str(p.get("name", ""))
            return other.lower() == name.lower() if case_insensitive else other == name

        # rendere obbligatorio un parametro rompe i client esistenti; aggiungerne uno opzionale no
        category = ChangeCategory.SEMANTIC if required else ChangeCategory.GOVERNANCE
        params = operation.get("parameters") or []
        for idx, p in enumerate(params):
            if isinstance(p, dict) and isinstance(p.get("$ref"), str):
                target = doc.get(p["$ref"][1:]) if p["$ref"].startswith("#") else None
                if same(target):
                    if not required or target.get("required"):
                        return None
                    raise ApplyError(f"{kind} '{name}' definito via $ref {p['$ref']}: "
                                     "va modificato il componente, non l'operation")
                continue
            if same(p):
                if not required or p.get("required"):
                    return None
                before = copy.deepcopy(p)
                p["required"] = True
                return AppliedChange(type=op.type, rule_id=op.rule_id, category=category,
                                     locations=[jp.child(op.target, "parameters", idx)],
                                     description=f"{kind} {name} reso obbligatorio su {op.target}",
                                     before=before, after=p)
        path_ptr = jp.parent(op.target)[0]
        for p in (doc.get(path_ptr) or {}).get("parameters") or []:
            if same(p):
                if not required or p.get("required"):
                    return None
                raise ApplyError(f"{kind} '{name}' definito a livello di path in {path_ptr} con "
                                 f"required={bool(p.get('required'))}: va modificato lì, non sulla singola operation")
        param: dict[str, Any] = {"name": name, "in": location, "required": required, "schema": schema}
        if description:
            param["description"] = description
        params = operation.setdefault("parameters", [])  # creato solo quando si aggiunge davvero
        params.append(param)
        return AppliedChange(type=op.type, rule_id=op.rule_id, category=category,
                             locations=[jp.child(op.target, "parameters", len(params) - 1)],
                             description=f"aggiunto {kind} {'obbligatorio' if required else 'opzionale'} "
                                         f"{name} a {op.target}",
                             after=param)

    def _apply_add_operation_id(self, doc: SpecDocument, op: ops.AddOperationId) -> AppliedChange | None:
        operation = _require_operation(doc, op.target)
        before = operation.get("operationId")
        if before == op.operation_id:
            return None
        operation["operationId"] = op.operation_id
        links = refs.rename_operation_id_refs(doc.data, before, op.operation_id) if before else 0
        return AppliedChange(type=op.type, rule_id=op.rule_id, category=ChangeCategory.GOVERNANCE,
                             locations=[jp.child(op.target, "operationId")],
                             description=f"operationId {before!r} -> {op.operation_id!r}"
                                         + (f" ({links} link aggiornati)" if links else ""), before=before,
                             after=op.operation_id)

    def _apply_rename_schema(self, doc: SpecDocument, op: ops.RenameSchema) -> AppliedChange | None:
        schemas = (doc.data.get("components") or {}).get("schemas")
        if not isinstance(schemas, dict) or op.from_ not in schemas:
            raise ApplyError(f"schema '{op.from_}' non trovato in components/schemas")
        if op.from_ == op.to:
            return None
        if op.to in schemas:
            raise ApplyError(f"rename '{op.from_}' -> '{op.to}' produrrebbe un duplicato: '{op.to}' esiste già")
        _rename_key(schemas, op.from_, op.to)
        old_ref = f"#/components/schemas/{jp.escape(op.from_)}"
        new_ref = f"#/components/schemas/{jp.escape(op.to)}"
        updated: list[str] = []

        def fix(node: dict, at: str) -> None:
            ref = node["$ref"]
            if ref == old_ref or ref.startswith(old_ref + "/"):
                node["$ref"] = new_ref + ref[len(old_ref):]
                updated.append(at)

        _walk_refs(doc.data, "", fix)
        refs.rename_schema_in_discriminators(doc.data, op.from_, op.to)
        return AppliedChange(type=op.type, rule_id=op.rule_id, category=ChangeCategory.STRUCTURAL,
                             locations=[jp.join(["components", "schemas", op.from_]),
                                        jp.join(["components", "schemas", op.to])],
                             description=f"schema {op.from_} -> {op.to} ({len(updated)} $ref aggiornati)",
                             before=op.from_, after=op.to)

    def _apply_rename_property(self, doc: SpecDocument, op: ops.RenameProperty) -> AppliedChange | None:
        schema = _require(doc, op.target, "schema")
        props = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(props, dict) or op.from_ not in props:
            raise ApplyError(f"proprietà '{op.from_}' non trovata in {op.target}/properties")
        if op.from_ == op.to:
            return None
        if op.to in props:
            raise ApplyError(f"la proprietà '{op.to}' esiste già in {op.target}")
        _rename_key(props, op.from_, op.to)
        if isinstance(schema.get("required"), list):
            schema["required"] = [op.to if r == op.from_ else r for r in schema["required"]]
        extra = refs.rename_property_references(doc.data, op.target, op.from_, op.to)
        return AppliedChange(type=op.type, rule_id=op.rule_id, category=ChangeCategory.SEMANTIC,
                             locations=[jp.child(op.target, "properties", op.from_),
                                        jp.child(op.target, "properties", op.to)],
                             description=f"proprietà {op.from_} -> {op.to} in {op.target} (nome sul wire cambiato"
                                         + (f"; {extra} riferimenti in esempi/discriminator aggiornati)" if extra else ")"),
                             before=op.from_, after=op.to)

    def _apply_rename_path(self, doc: SpecDocument, op: ops.RenamePath) -> AppliedChange | None:
        paths = doc.data.get("paths") or {}
        if op.from_ not in paths:
            raise ApplyError(f"path '{op.from_}' non trovato")
        if op.from_ == op.to:
            return None
        if op.to in paths:
            raise ApplyError(f"il path '{op.to}' esiste già")
        if sorted(re.findall(r"{([^}]+)}", op.from_)) != sorted(re.findall(r"{([^}]+)}", op.to)):
            raise ApplyError("RENAME_PATH non può cambiare i path parameter: usa RENAME_PARAMETER (in=path)")
        _rename_key(paths, op.from_, op.to)
        refs.rename_path_refs(doc.data, op.from_, op.to)
        return AppliedChange(type=op.type, rule_id=op.rule_id, category=ChangeCategory.SEMANTIC,
                             locations=[jp.join(["paths", op.from_]), jp.join(["paths", op.to])],
                             description=f"path {op.from_} -> {op.to} (URL cambiato)", before=op.from_, after=op.to)

    def _apply_rename_parameter(self, doc: SpecDocument, op: ops.RenameParameter) -> AppliedChange | None:
        """Target: l'operation o il path item che dichiara il parametro. Un path parameter vive nel template del
        path e in tutte le operation del path item. Aggiorna anche i link che citano il parametro."""
        tokens = jp.split(op.target)
        if len(tokens) < 2 or tokens[0] != "paths":
            raise ApplyError(f"target non valido per RENAME_PARAMETER: {op.target}")
        path_key = tokens[1]
        path_item = _require(doc, jp.join(["paths", path_key]), "path")
        if op.from_ == op.to:
            return None
        path_level = len(tokens) == 2
        if op.in_ == "path" or path_level:
            holders = [path_item] + [path_item[m] for m in HTTP_METHODS if isinstance(path_item.get(m), dict)]
            if op.in_ != "path":  # query/header a livello di path: solo la dichiarazione del path item
                holders = [path_item]
            affected = refs.operations_of_path(doc.data, path_key)
        else:
            holders = [_require(doc, op.target, "operation")]
            affected = [op.target]
        for holder in holders:
            if any(isinstance(p, dict) and p.get("in") == op.in_ and p.get("name") == op.to
                   for p in holder.get("parameters") or []):
                raise ApplyError(f"il parametro {op.in_}:{op.to} esiste già: la rinomina produrrebbe un duplicato")
        found: list[tuple[str | None, int]] = []  # (metodo o None per path-level, indice)
        for holder in holders:
            method = next((m for m in HTTP_METHODS if path_item.get(m) is holder), None)
            for idx, p in enumerate(holder.get("parameters") or []):
                if isinstance(p, dict) and p.get("in") == op.in_ and p.get("name") == op.from_:
                    p["name"] = op.to
                    found.append((method, idx))
        if not found:
            raise ApplyError(f"parametro {op.in_}:{op.from_} non trovato in {op.target}")
        links = refs.rename_parameter_in_links(doc.data, affected, op.in_, op.from_, op.to)
        new_key = path_key
        if op.in_ == "path":
            new_key = path_key.replace("{" + op.from_ + "}", "{" + op.to + "}")
            if new_key != path_key:
                if new_key in doc.data["paths"]:
                    raise ApplyError(f"il path '{new_key}' esiste già")
                _rename_key(doc.data["paths"], path_key, new_key)
                refs.rename_path_refs(doc.data, path_key, new_key)
        locations = [jp.join(["paths", key, *([m] if m else []), "parameters", idx])
                     for key in {path_key, new_key} for m, idx in found]
        if new_key != path_key:
            locations += [jp.join(["paths", path_key]), jp.join(["paths", new_key])]
        return AppliedChange(type=op.type, rule_id=op.rule_id, category=ChangeCategory.SEMANTIC,
                             locations=locations,
                             description=f"parametro {op.in_} {op.from_} -> {op.to} ({op.target})"
                                         + (f", {links} riferimenti nei link aggiornati" if links else ""),
                             before=op.from_, after=op.to)

    def _apply_move_component(self, doc: SpecDocument, op: ops.MoveComponent) -> AppliedChange | None:
        node = _require(doc, op.source, "nodo sorgente")
        if isinstance(node, dict) and "$ref" in node:
            raise ApplyError(f"{op.source} è già un $ref")
        dest = jp.join(["components", op.component_type, op.name])
        existing = doc.get(dest)
        if existing is not None and existing != node:
            raise ApplyError(f"{dest} esiste già con un contenuto diverso")
        jp.set_value(doc.data, dest, copy.deepcopy(node), create_parents=True)
        jp.set_value(doc.data, op.source, {"$ref": f"#{dest}"})
        return AppliedChange(type=op.type, rule_id=op.rule_id, category=ChangeCategory.STRUCTURAL,
                             locations=[op.source, dest], description=f"{op.source} spostato in {dest}",
                             before=None, after={"$ref": f"#{dest}"})

    def _apply_add_component(self, doc: SpecDocument, op: ops.AddComponent) -> AppliedChange | None:
        dest = jp.join(["components", op.component_type, op.name])
        existing = doc.get(dest)
        if existing is not None:
            if existing == op.definition:
                return None
            raise ApplyError(f"{dest} esiste già con un contenuto diverso")
        jp.set_value(doc.data, dest, copy.deepcopy(op.definition), create_parents=True)
        return AppliedChange(type=op.type, rule_id=op.rule_id, category=ChangeCategory.STRUCTURAL, locations=[dest],
                             description=f"aggiunto componente {dest}", after=op.definition)

    def _apply_replace_response_schema(self, doc: SpecDocument, op: ops.ReplaceResponseSchema) -> AppliedChange | None:
        response = _require(doc, op.target, "response")
        content = response.setdefault("content", {})
        media = content.setdefault(op.media_type, {})
        before = copy.deepcopy(media.get("schema"))
        if before == op.schema_:
            return None
        media["schema"] = copy.deepcopy(op.schema_)
        return AppliedChange(type=op.type, rule_id=op.rule_id, category=ChangeCategory.SEMANTIC,
                             locations=[jp.child(op.target, "content", op.media_type, "schema")],
                             description=f"schema della response {op.target} ({op.media_type}) sostituito",
                             before=before, after=op.schema_)

    def _apply_convert_error_response(self, doc: SpecDocument, op: ops.ConvertErrorResponse) -> AppliedChange | None:
        response = _require(doc, op.target, "response")
        if not isinstance(response, dict) or "$ref" in response:
            raise ApplyError(f"{op.target} è un $ref: converti il componente referenziato")
        if not op.schema_ref.startswith("#") or doc.get(op.schema_ref[1:]) is None:
            raise ApplyError(f"schemaRef {op.schema_ref} non esiste (aggiungilo prima con ADD_COMPONENT)")
        wanted = {op.media_type: {"schema": {"$ref": op.schema_ref}}}
        before = copy.deepcopy(response.get("content"))
        if before == wanted:
            return None
        response["content"] = wanted
        return AppliedChange(type=op.type, rule_id=op.rule_id, category=ChangeCategory.SEMANTIC,
                             locations=[jp.child(op.target, "content")],
                             description=f"response {op.target} convertita in {op.media_type} ({op.schema_ref})",
                             before=before, after=wanted)

    def _apply_add_security_scheme(self, doc: SpecDocument, op: ops.AddSecurityScheme) -> AppliedChange | None:
        dest = jp.join(["components", "securitySchemes", op.name])
        existing = doc.get(dest)
        if existing is not None:
            if existing == op.scheme:
                return None
            raise ApplyError(f"security scheme '{op.name}' esiste già con una definizione diversa")
        jp.set_value(doc.data, dest, copy.deepcopy(op.scheme), create_parents=True)
        return AppliedChange(type=op.type, rule_id=op.rule_id, category=ChangeCategory.GOVERNANCE, locations=[dest],
                             description=f"aggiunto security scheme {op.name}", after=op.scheme)

    def _apply_set_security_requirement(self, doc: SpecDocument, op: ops.SetSecurityRequirement) -> AppliedChange | None:
        holder = doc.data if op.target == "" else _require_operation(doc, op.target)
        before = copy.deepcopy(holder.get("security"))
        if before == op.requirements:
            return None
        holder["security"] = copy.deepcopy(op.requirements)
        return AppliedChange(type=op.type, rule_id=op.rule_id, category=ChangeCategory.SEMANTIC,
                             locations=[jp.child(op.target, "security")],
                             description=f"security requirement su {op.target or 'livello globale'}: {op.requirements}",
                             before=before, after=op.requirements)

    def _apply_add_response(self, doc: SpecDocument, op: ops.AddResponse) -> AppliedChange | None:
        operation = _require_operation(doc, op.target)
        responses = operation.setdefault("responses", {})
        if op.status in responses:
            raise ApplyError(f"la response {op.status} esiste già in {op.target}")
        response: dict[str, Any] = {"description": op.description}
        if op.media_type and op.schema_ is not None:
            response["content"] = {op.media_type: {"schema": copy.deepcopy(op.schema_)}}
        responses[op.status] = response
        return AppliedChange(type=op.type, rule_id=op.rule_id, category=ChangeCategory.GOVERNANCE,
                             locations=[jp.child(op.target, "responses", op.status)],
                             description=f"aggiunta response {op.status} a {op.target}", after=response)

    def _apply_set_field(self, doc: SpecDocument, op: ops.SetField) -> AppliedChange | None:
        parent_ptr, key = jp.parent(op.target)
        parent = _require(doc, parent_ptr, "nodo parent")
        if not isinstance(parent, (dict, list)):
            raise ApplyError(f"{parent_ptr} non è un oggetto")
        before = doc.get(op.target)
        if before == op.value:
            return None
        jp.set_value(doc.data, op.target, copy.deepcopy(op.value))
        # sovrascrivere un oggetto non vuoto con un contenuto diverso cambia il contratto, anche sotto /info
        overwrite = isinstance(before, dict) and bool(before)
        return AppliedChange(type=op.type, rule_id=op.rule_id,
                             category=ChangeCategory.GOVERNANCE if _doc_only(op.target) and not overwrite
                             else ChangeCategory.SEMANTIC,
                             locations=[op.target], description=f"impostato {op.target}", before=before, after=op.value)

    def _apply_remove_field(self, doc: SpecDocument, op: ops.RemoveField) -> AppliedChange | None:
        _require(doc, op.target, "campo da rimuovere")
        before = jp.remove(doc.data, op.target)
        return AppliedChange(type=op.type, rule_id=op.rule_id,
                             category=ChangeCategory.GOVERNANCE if _doc_only(op.target) else ChangeCategory.SEMANTIC,
                             locations=[op.target], description=f"rimosso {op.target}", before=before)

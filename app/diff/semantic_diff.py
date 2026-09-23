"""Semantic diff nativo (original vs candidato) sull'Object Model: lista piatta di change tipizzati.

- `breaking` è classificato con la tabella in `compat.py`
- `expected` è True solo se il change è riconducibile a un AppliedChange (quindi a un ruleId):
  è il meccanismo che rende verificabili deterministicamente i claim del Critic.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.diff.compat import ChangeType, is_breaking
from app.model import pointer as jp
from app.model.document import HTTP_METHODS, SpecDocument
from app.model.issues import Severity, Violation, ViolationSource
from app.model.refs import RefIndex
from app.refactor.operations import AppliedChange

_SCHEMA_PREFIX = "/components/schemas/"


class DiffChange(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    type: ChangeType
    location: str
    breaking: bool
    expected: bool = False
    rule_id: str | None = Field(None, alias="ruleId")
    before: Any = None
    after: Any = None

    def dump(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


class RenameMap:
    """Rename applicati (schema e path): servono ad accoppiare gli elementi e a canonicalizzare i pointer."""

    def __init__(self, applied: list[AppliedChange]):
        self.schemas: dict[str, str] = {}
        self.paths: dict[str, str] = {}
        for change in applied:
            if change.type == "RENAME_SCHEMA":
                self.schemas[str(change.before)] = str(change.after)
            elif change.type == "RENAME_PATH":
                self.paths[str(change.before)] = str(change.after)
        self.schemas = self._close(self.schemas)
        self.paths = self._close(self.paths)

    @staticmethod
    def _close(mapping: dict[str, str]) -> dict[str, str]:
        """Rename a catena (A->B poi B->C) collassati in A->C."""
        out = {}
        for start in mapping:
            end, seen = mapping[start], {start}
            while end in mapping and end not in seen:
                seen.add(end)
                end = mapping[end]
            out[start] = end
        return out

    def canonical(self, pointer: str) -> str:
        tokens = jp.split(pointer)
        if len(tokens) >= 3 and tokens[:2] == ["components", "schemas"] and tokens[2] in self.schemas:
            tokens[2] = self.schemas[tokens[2]]
        elif len(tokens) >= 2 and tokens[0] == "paths" and tokens[1] in self.paths:
            tokens[1] = self.paths[tokens[1]]
        return jp.join(tokens)

    def canonical_ref(self, ref: str) -> str:
        return "#" + self.canonical(ref[1:]) if ref.startswith("#") else ref


class SemanticDiff:
    def __init__(self, before: SpecDocument, after: SpecDocument, applied: list[AppliedChange]):
        self.a, self.b = before.data, after.data
        self.refs_a, self.refs_b = RefIndex(self.a), RefIndex(self.b)
        self.applied = applied
        self.renames = RenameMap(applied)
        ctx_a, ctx_b = self.refs_a.usage_contexts(), self.refs_b.usage_contexts()
        self.contexts: dict[str, set[str]] = {}
        for ptr, ctxs in [*ctx_a.items(), *ctx_b.items()]:
            self.contexts.setdefault(self.renames.canonical(ptr), set()).update(ctxs)
        self.changes: list[DiffChange] = []

    # ── API ────────────────────────────────────────────────────────────
    def compute(self) -> list[DiffChange]:
        self.changes = []
        if self.a.get("openapi") != self.b.get("openapi"):
            self._add(ChangeType.OPENAPI_VERSION_CHANGED, "/openapi", before=self.a.get("openapi") or self.a.get("swagger"),
                      after=self.b.get("openapi"))
        self._diff_paths()
        self._diff_schemas()
        self._diff_security_schemes()
        self._mark_expected()
        return self.changes

    # ── infrastruttura ─────────────────────────────────────────────────
    def _add(self, ctype: ChangeType, location: str, before: Any = None, after: Any = None,
             contexts: set[str] | None = None, required: bool = False) -> None:
        self.changes.append(DiffChange(type=ctype, location=location, breaking=is_breaking(ctype, contexts, required),
                                       before=_summary(before), after=_summary(after)))

    def _mark_expected(self) -> None:
        traced = [(self.renames.canonical(loc), c.rule_id) for c in self.applied for loc in c.locations]
        for change in self.changes:
            loc = self.renames.canonical(change.location)
            for applied_loc, rule_id in traced:
                # solo "il change applicato contiene questo elemento": un change dentro un'operation
                # non giustifica la rimozione dell'intera operation
                if jp.is_prefix(applied_loc, loc):
                    change.expected, change.rule_id = True, rule_id
                    break

    # ── paths / operations ─────────────────────────────────────────────
    def _diff_paths(self) -> None:
        pa, pb = self.a.get("paths") or {}, self.b.get("paths") or {}
        paired: dict[str, str] = {}
        for old in pa:
            new = self.renames.paths.get(old, old)
            if new in pb:
                paired[old] = new
        for old in pa:
            if old not in paired:
                self._add(ChangeType.PATH_REMOVED, jp.join(["paths", old]), before=sorted(_methods(pa[old])))
        for new in pb:
            if new not in paired.values():
                self._add(ChangeType.PATH_ADDED, jp.join(["paths", new]), after=sorted(_methods(pb[new])))
        for old, new in paired.items():
            if old != new:
                self._add(ChangeType.PATH_RENAMED, jp.join(["paths", new]), before=old, after=new)
            self._diff_path_item(old, new, pa[old] or {}, pb[new] or {})

    def _diff_path_item(self, old: str, new: str, ia: dict, ib: dict) -> None:
        for m in HTTP_METHODS:
            oa, ob = ia.get(m), ib.get(m)
            loc = jp.join(["paths", new, m])
            if isinstance(oa, dict) and not isinstance(ob, dict):
                self._add(ChangeType.OPERATION_REMOVED, jp.join(["paths", old, m]), before=oa.get("operationId") or m)
            elif isinstance(ob, dict) and not isinstance(oa, dict):
                self._add(ChangeType.OPERATION_ADDED, loc, after=ob.get("operationId") or m)
            elif isinstance(oa, dict) and isinstance(ob, dict):
                self._diff_operation(loc, ia, ib, oa, ob)

    def _diff_operation(self, loc: str, ia: dict, ib: dict, oa: dict, ob: dict) -> None:
        if oa.get("operationId") != ob.get("operationId"):
            self._add(ChangeType.OPERATION_ID_CHANGED, jp.child(loc, "operationId"), oa.get("operationId"),
                      ob.get("operationId"))
        self._diff_parameters(loc, ia, ib, oa, ob)
        self._diff_request_body(loc, oa.get("requestBody"), ob.get("requestBody"))
        self._diff_responses(loc, oa.get("responses") or {}, ob.get("responses") or {})
        self._diff_security(loc, oa, ob)

    def _params(self, refs: RefIndex, item: dict, op: dict, loc: str) -> dict[tuple, tuple[str, dict]]:
        out: dict[tuple, tuple[str, dict]] = {}
        path_ptr = jp.parent(loc)[0]
        for base, params in ((path_ptr, item.get("parameters") or []), (loc, op.get("parameters") or [])):
            for idx, raw in enumerate(params):
                p = refs.resolve_ref(raw)
                if isinstance(p, dict) and "name" in p:
                    out[(p.get("in"), p.get("name"))] = (jp.child(base, "parameters", idx), p)
        return out

    def _diff_parameters(self, loc: str, ia: dict, ib: dict, oa: dict, ob: dict) -> None:
        before = self._params(self.refs_a, ia, oa, loc)
        after = self._params(self.refs_b, ib, ob, loc)
        for key, (ptr, p) in before.items():
            if key not in after:
                self._add(ChangeType.PARAMETER_REMOVED, ptr, before=f"{key[0]}:{key[1]}")
        for key, (ptr, p) in after.items():
            if key not in before:
                self._add(ChangeType.PARAMETER_ADDED, ptr, after=f"{key[0]}:{key[1]}", required=bool(p.get("required")))
                continue
            pa = before[key][1]
            if not pa.get("required") and p.get("required"):
                self._add(ChangeType.PARAMETER_BECAME_REQUIRED, ptr, False, True)
            elif pa.get("required") and not p.get("required"):
                self._add(ChangeType.PARAMETER_BECAME_OPTIONAL, ptr, True, False)
            if not self._equivalent(pa.get("schema"), p.get("schema")):
                self._add(ChangeType.PARAMETER_SCHEMA_CHANGED, jp.child(ptr, "schema"), pa.get("schema"), p.get("schema"))

    def _diff_request_body(self, loc: str, ra: Any, rb: Any) -> None:
        ra, rb = self.refs_a.resolve_ref(ra), self.refs_b.resolve_ref(rb)
        ptr = jp.child(loc, "requestBody")
        if ra and not rb:
            self._add(ChangeType.REQUEST_BODY_REMOVED, ptr, before=sorted((ra.get("content") or {}).keys()))
            return
        if rb and not ra:
            self._add(ChangeType.REQUEST_BODY_ADDED, ptr, after=sorted((rb.get("content") or {}).keys()),
                      required=bool(rb.get("required")))
            return
        if not ra:
            return
        if not ra.get("required") and rb.get("required"):
            self._add(ChangeType.REQUEST_BODY_BECAME_REQUIRED, ptr, False, True)
        self._diff_content(ptr, ra.get("content") or {}, rb.get("content") or {}, {"request"})

    def _diff_responses(self, loc: str, ra: dict, rb: dict) -> None:
        for status in ra:
            ptr = jp.child(loc, "responses", status)
            if status not in rb:
                self._add(ChangeType.RESPONSE_REMOVED, ptr, before=self.refs_a.resolve_ref(ra[status]))
                continue
            a, b = self.refs_a.resolve_ref(ra[status]) or {}, self.refs_b.resolve_ref(rb[status]) or {}
            self._diff_content(ptr, a.get("content") or {}, b.get("content") or {}, {"response"})
        for status in rb:
            if status not in ra:
                self._add(ChangeType.RESPONSE_ADDED, jp.child(loc, "responses", status),
                          after=self.refs_b.resolve_ref(rb[status]))

    def _diff_content(self, ptr: str, ca: dict, cb: dict, contexts: set[str]) -> None:
        for mt in ca:
            if mt not in cb:
                self._add(ChangeType.MEDIA_TYPE_REMOVED, jp.child(ptr, "content", mt), before=mt, contexts=contexts)
        for mt in cb:
            if mt not in ca:
                self._add(ChangeType.MEDIA_TYPE_ADDED, jp.child(ptr, "content", mt), after=mt, contexts=contexts)
            else:
                self._diff_schema(jp.child(ptr, "content", mt, "schema"), (ca[mt] or {}).get("schema"),
                                  (cb[mt] or {}).get("schema"), contexts, set())

    def _effective_security(self, doc: dict, op: dict) -> tuple[list, bool]:
        if "security" in op:
            return op.get("security") or [], True
        return doc.get("security") or [], False

    def _diff_security(self, loc: str, oa: dict, ob: dict) -> None:
        sa, _ = self._effective_security(self.a, oa)
        sb, local = self._effective_security(self.b, ob)
        names_a = sorted({n for r in sa for n in (r or {})})
        names_b = sorted({n for r in sb for n in (r or {})})
        if names_a == names_b:
            return
        ptr = jp.child(loc, "security") if local else "/security"
        if not names_a:
            self._add(ChangeType.SECURITY_REQUIREMENT_ADDED, ptr, None, names_b)
        elif not names_b:
            self._add(ChangeType.SECURITY_REQUIREMENT_REMOVED, ptr, names_a, None)
        else:
            self._add(ChangeType.SECURITY_REQUIREMENT_CHANGED, ptr, names_a, names_b)

    # ── componenti ─────────────────────────────────────────────────────
    def _diff_schemas(self) -> None:
        sa = (self.a.get("components") or {}).get("schemas") or {}
        sb = (self.b.get("components") or {}).get("schemas") or {}
        paired = {old: self.renames.schemas.get(old, old) for old in sa if self.renames.schemas.get(old, old) in sb}
        for old in sa:
            if old not in paired:
                self._add(ChangeType.SCHEMA_REMOVED, jp.join(["components", "schemas", old]), before=old)
        for new in sb:
            if new not in paired.values():
                self._add(ChangeType.SCHEMA_ADDED, jp.join(["components", "schemas", new]), after=new)
        for old, new in paired.items():
            ptr = jp.join(["components", "schemas", new])
            if old != new:
                self._add(ChangeType.SCHEMA_RENAMED, ptr, before=old, after=new)
            self._diff_schema(ptr, sa[old], sb[new], self.contexts.get(ptr), set(), follow_refs=False)

    def _diff_security_schemes(self) -> None:
        sa = (self.a.get("components") or {}).get("securitySchemes") or {}
        sb = (self.b.get("components") or {}).get("securitySchemes") or {}
        # Swagger 2.0 convertito: il confronto avviene sempre tra documenti già in OpenAPI 3.x
        for name in sa:
            ptr = jp.join(["components", "securitySchemes", name])
            if name not in sb:
                self._add(ChangeType.SECURITY_SCHEME_REMOVED, ptr, before=sa[name])
            elif sa[name] != sb[name]:
                self._add(ChangeType.SECURITY_SCHEME_CHANGED, ptr, sa[name], sb[name])
        for name in sb:
            if name not in sa:
                self._add(ChangeType.SECURITY_SCHEME_ADDED, jp.join(["components", "securitySchemes", name]),
                          after=sb[name])

    # ── schemi ─────────────────────────────────────────────────────────
    def _same_component(self, a: Any, b: Any) -> bool:
        return (isinstance(a, dict) and isinstance(b, dict) and isinstance(a.get("$ref"), str)
                and isinstance(b.get("$ref"), str) and self.renames.canonical_ref(a["$ref"]) == b["$ref"])

    def _equivalent(self, a: Any, b: Any) -> bool:
        if self._same_component(a, b):
            return True
        saved, self.changes = self.changes, []
        try:
            self._diff_schema("", a, b, None, set())
            return not self.changes
        finally:
            self.changes = saved

    def _diff_schema(self, loc: str, a: Any, b: Any, contexts: set[str] | None, visited: set,
                     follow_refs: bool = True) -> None:
        if a is None and b is None:
            return
        if self._same_component(a, b) and follow_refs:
            return  # stesso componente (eventualmente rinominato): lo confronta _diff_schemas una volta sola
        key = (id(a), id(b))
        if key in visited:
            return  # $ref circolari: interrompi
        visited = visited | {key}
        ra, rb = self.refs_a.resolve_ref(a), self.refs_b.resolve_ref(b)
        if not isinstance(ra, dict) or not isinstance(rb, dict):
            if ra != rb:
                self._add(ChangeType.SCHEMA_TYPE_CHANGED, loc, ra, rb, contexts=contexts)
            return
        if (ra.get("type"), ra.get("format")) != (rb.get("type"), rb.get("format")):
            self._add(ChangeType.SCHEMA_TYPE_CHANGED, loc, {"type": ra.get("type"), "format": ra.get("format")},
                      {"type": rb.get("type"), "format": rb.get("format")}, contexts=contexts)
        ea, eb = ra.get("enum"), rb.get("enum")
        if isinstance(ea, list) or isinstance(eb, list):
            for v in ea or []:
                if v not in (eb or []):
                    self._add(ChangeType.ENUM_VALUE_REMOVED, jp.child(loc, "enum"), before=v, contexts=contexts)
            for v in eb or []:
                if v not in (ea or []):
                    self._add(ChangeType.ENUM_VALUE_ADDED, jp.child(loc, "enum"), after=v, contexts=contexts)
        pa, pb = ra.get("properties") or {}, rb.get("properties") or {}
        req_a, req_b = set(ra.get("required") or []), set(rb.get("required") or [])
        for name in pa:
            if name not in pb:
                self._add(ChangeType.PROPERTY_REMOVED, jp.child(loc, "properties", name), before=name, contexts=contexts)
        for name in pb:
            ptr = jp.child(loc, "properties", name)
            if name not in pa:
                self._add(ChangeType.PROPERTY_ADDED, ptr, after=name, contexts=contexts, required=name in req_b)
                continue
            if name not in req_a and name in req_b:
                self._add(ChangeType.PROPERTY_BECAME_REQUIRED, ptr, False, True, contexts=contexts)
            elif name in req_a and name not in req_b:
                self._add(ChangeType.PROPERTY_BECAME_OPTIONAL, ptr, True, False, contexts=contexts)
            self._diff_schema(ptr, pa[name], pb[name], contexts, visited)
        if "items" in ra or "items" in rb:
            self._diff_schema(jp.child(loc, "items"), ra.get("items"), rb.get("items"), contexts, visited)
        for combiner in ("allOf", "oneOf", "anyOf"):
            la, lb = ra.get(combiner) or [], rb.get(combiner) or []
            for idx in range(max(len(la), len(lb))):
                self._diff_schema(jp.child(loc, combiner, idx), la[idx] if idx < len(la) else None,
                                  lb[idx] if idx < len(lb) else None, contexts, visited)


def _methods(item: Any) -> list[str]:
    return [m for m in HTTP_METHODS if isinstance(item, dict) and isinstance(item.get(m), dict)]


def _summary(value: Any) -> Any:
    """Before/after compatti nel report (le response mantengono la description)."""
    if isinstance(value, dict):
        if "description" in value and ("content" in value or len(value) <= 2):
            return {"description": value.get("description")}
        text = str(value)
        return value if len(text) <= 300 else {"summary": text[:300] + "..."}
    return value


def compute_diff(before: SpecDocument, after: SpecDocument, applied: list[AppliedChange]) -> list[DiffChange]:
    return SemanticDiff(before, after, applied).compute()


def untraced_violations(changes: list[DiffChange], file: str) -> list[Violation]:
    """Change non riconducibili ad alcuna operazione pianificata: contratto modificato silenziosamente."""
    out = []
    for c in changes:
        if c.expected:
            continue
        out.append(Violation(
            rule_id="DIFF-UNTRACED-CHANGE", severity=Severity.ERROR if c.breaking else Severity.WARNING, file=file,
            path=c.location, message=f"{c.type.value} non riconducibile ad alcuna operazione pianificata"
            + (" (breaking)" if c.breaking else ""),
            expected="nessuna modifica", actual={"before": c.before, "after": c.after},
            suggested_fix="Ripristina l'elemento originale (SET_FIELD con il valore originale)",
            source=ViolationSource.SEMANTIC_DIFF))
    return out

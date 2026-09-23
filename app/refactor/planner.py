"""RefactoringPlanner: produce un piano di RefactorOperation tipizzate, distinto dall'esecuzione, e lo valida.

Validazione del piano (prima di eseguirlo): ruleId sconosciuti, operazioni in conflitto tra loro
(due rename diversi dello stesso elemento, rename verso un nome già esistente, definizioni diverse dello
stesso componente, set/remove sullo stesso campo). In un conflitto tra un'operazione motivata da una regola
Spectral (deterministica) e una motivata da una regola LLM, vince la prima; negli altri casi entrambe sono scartate.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.errors import LlmCallError
from app.llm.base import AgentRole, LlmRequest
from app.llm.structured import StructuredLlm
from app.logging_setup import get_logger
from app.model.document import SpecDocument
from app.model.issues import Violation
from app.model.refs import RefIndex
from app.prompts import load_prompt
from app.refactor import operations as ops
from app.refactor.fragments import Fragment, build_slice, render
from app.rules.models import CompiledRule, SpectralRule
from app.rules.registry import RuleRegistry

log = get_logger("planner")

# ordine di applicazione: prima si creano i componenti, poi si rinominano/sostituiscono, i rename di path per ultimi
# (i rename vengono dopo le operazioni che usano i vecchi nomi come target, così i target restano validi)
_PHASE = {
    "UPDATE_OPENAPI_VERSION": 0, "ADD_COMPONENT": 1, "ADD_SECURITY_SCHEME": 1, "MOVE_COMPONENT": 2,
    "RENAME_PROPERTY": 4, "RENAME_SCHEMA": 7, "RENAME_PARAMETER": 8, "RENAME_PATH": 9,
}


class PlanIssue(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    kind: str  # UNKNOWN_RULE | CONFLICT | DUPLICATE_TARGET | LLM_FAILED
    message: str
    rule_ids: list[str] = Field(default_factory=list, alias="ruleIds")
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    kept: dict[str, Any] | None = None


class RefactoringPlan(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    iteration: int
    operations: list[ops.PlannedOperation] = Field(default_factory=list)
    issues: list[PlanIssue] = Field(default_factory=list)
    rationales: dict[str, str] = Field(default_factory=dict)
    failed_fragments: list[str] = Field(default_factory=list, alias="failedFragments")

    def dump(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "operations": [p.dump() for p in self.operations],
            "issues": [i.model_dump(by_alias=True, mode="json") for i in self.issues],
            "rationales": self.rationales,
            "failedFragments": self.failed_fragments,
        }


def describe_rule(rule) -> dict[str, Any]:
    """Rappresentazione compatta di una regola per il prompt (niente corpus completo)."""
    if isinstance(rule, SpectralRule):
        return {"ruleId": rule.id, "origin": "spectral", "severity": rule.severity.value, "description": rule.description}
    assert isinstance(rule, CompiledRule)
    return {
        "ruleId": rule.id, "origin": "natural-language", "severity": rule.severity.value, "text": rule.text,
        "requirements": [r.model_dump(by_alias=True, mode="json") for r in rule.requirements],
        **({"overriddenBy": rule.overridden_by} if rule.overridden_by else {}),
    }


def _op_key(op) -> tuple:
    """Chiave dell'elemento su cui l'operazione agisce (per rilevare conflitti)."""
    t = op.type
    if t == "RENAME_SCHEMA":
        return ("schema", op.from_)
    if t == "RENAME_PROPERTY":
        return ("property", op.target, op.from_)
    if t == "RENAME_PATH":
        return ("path", op.from_)
    if t == "RENAME_PARAMETER":
        return ("parameter", op.target, op.in_, op.from_)
    if t == "ADD_COMPONENT":
        return ("component", op.component_type, op.name)
    if t == "ADD_SECURITY_SCHEME":
        return ("component", "securitySchemes", op.name)
    if t in ("SET_FIELD", "REMOVE_FIELD"):
        return ("field", op.target)
    if t == "ADD_HEADER":
        return ("header", op.target, op.header.lower())
    if t == "ADD_OPERATION_ID":
        return ("field", op.target + "/operationId")
    if t == "SET_SECURITY_REQUIREMENT":
        return ("field", op.target + "/security")
    if t in ("CONVERT_ERROR_RESPONSE", "REPLACE_RESPONSE_SCHEMA"):
        return ("response-content", op.target)
    if t == "ADD_RESPONSE":
        return ("response", op.target, op.status)
    if t == "MOVE_COMPONENT":
        return ("move", op.source)
    return (t,)


def _payload(op) -> dict[str, Any]:
    data = op.model_dump(by_alias=True, mode="json")
    data.pop("ruleId", None)
    return data


class PlanValidator:
    def __init__(self, registry: RuleRegistry):
        self.registry = registry

    def _is_mechanical(self, rule_id: str) -> bool:
        return isinstance(self.registry.get(rule_id), SpectralRule) or rule_id.startswith(("OAS-", "DIFF-"))

    def validate(self, doc: SpecDocument, planned: list[ops.PlannedOperation],
                 known_rule_ids: set[str]) -> tuple[list[ops.PlannedOperation], list[PlanIssue]]:
        issues: list[PlanIssue] = []
        accepted: list[ops.PlannedOperation] = []

        # 1) tracciabilità: ogni operazione deve citare una regola/violazione esistente
        for p in planned:
            rid = p.operation.rule_id
            if self.registry.get(rid) is None and rid not in known_rule_ids:
                issues.append(PlanIssue(kind="UNKNOWN_RULE", message=f"{p.operation.type} cita un ruleId sconosciuto '{rid}'",
                                        rule_ids=[rid], rejected=[p.dump()]))
            else:
                accepted.append(p)

        # 2) duplicati identici -> uno solo; stesso elemento con payload diversi -> conflitto
        by_key: dict[tuple, list[ops.PlannedOperation]] = {}
        for p in accepted:
            by_key.setdefault(_op_key(p.operation), []).append(p)
        survivors: list[ops.PlannedOperation] = []
        for key, group in by_key.items():
            distinct: dict[str, ops.PlannedOperation] = {}
            for p in group:
                distinct.setdefault(repr(sorted(_payload(p.operation).items())), p)
            if len(distinct) == 1:
                survivors.append(next(iter(distinct.values())))
                continue
            candidates = list(distinct.values())
            mechanical = [p for p in candidates if self._is_mechanical(p.operation.rule_id)]
            mech_payloads = {repr(sorted(_payload(p.operation).items())) for p in mechanical}
            if len(mech_payloads) == 1:
                winner = mechanical[0]
                survivors.append(winner)
                issues.append(PlanIssue(
                    kind="CONFLICT", message=f"operazioni in conflitto su {key}: prevale quella motivata dalla regola "
                                             f"deterministica {winner.operation.rule_id}",
                    rule_ids=sorted({p.operation.rule_id for p in candidates}),
                    rejected=[p.dump() for p in candidates if p is not winner], kept=winner.dump()))
            else:
                issues.append(PlanIssue(
                    kind="CONFLICT", message=f"operazioni in conflitto sullo stesso elemento {key}: tutte scartate",
                    rule_ids=sorted({p.operation.rule_id for p in candidates}),
                    rejected=[p.dump() for p in candidates]))

        # 3) rename che produrrebbero duplicati (verso nomi esistenti o due rename verso lo stesso nome)
        survivors = self._check_rename_targets(doc, survivors, issues)
        survivors.sort(key=lambda p: _PHASE.get(p.operation.type, 5))
        return survivors, issues

    @staticmethod
    def _check_rename_targets(doc: SpecDocument, planned: list[ops.PlannedOperation],
                              issues: list[PlanIssue]) -> list[ops.PlannedOperation]:
        schemas = set(((doc.data.get("components") or {}).get("schemas") or {}).keys())
        paths = set(doc.paths().keys())
        renamed_away = {("schema", p.operation.from_) for p in planned if p.operation.type == "RENAME_SCHEMA"} | \
                       {("path", p.operation.from_) for p in planned if p.operation.type == "RENAME_PATH"}
        dest_count: dict[tuple, list[ops.PlannedOperation]] = {}
        for p in planned:
            op = p.operation
            if op.type == "RENAME_SCHEMA":
                dest_count.setdefault(("schema", op.to), []).append(p)
            elif op.type == "RENAME_PATH":
                dest_count.setdefault(("path", op.to), []).append(p)
            elif op.type == "RENAME_PROPERTY":
                dest_count.setdefault(("property", op.target, op.to), []).append(p)
        rejected: set[int] = set()
        for dest, group in dest_count.items():
            existing = (dest[0] == "schema" and dest[1] in schemas) or (dest[0] == "path" and dest[1] in paths)
            existing = existing and dest not in renamed_away
            if len(group) > 1 or existing:
                reason = "esiste già nel documento" if existing else "è la destinazione di più rename"
                issues.append(PlanIssue(kind="CONFLICT", message=f"rename verso {dest[1]!r}: {reason} (duplicato)",
                                        rule_ids=sorted({p.operation.rule_id for p in group}),
                                        rejected=[p.dump() for p in group]))
                rejected.update(id(p) for p in group)
        return [p for p in planned if id(p) not in rejected]


class RefactoringPlanner:
    def __init__(self, llm: StructuredLlm, registry: RuleRegistry, budget_tokens: int):
        self.llm = llm
        self.registry = registry
        self.budget_tokens = budget_tokens
        self.system = load_prompt("refactor")
        self.validator = PlanValidator(registry)

    async def propose_for_fragment(self, doc: SpecDocument, refs: RefIndex, fragment: Fragment,
                                   violations: list[Violation]) -> ops.OperationsProposal:
        rules = self.registry.for_fragment(fragment.kind, fragment.method, fragment.path)
        slice_ = build_slice(doc, fragment, refs, self.budget_tokens)
        user = (
            "## Fragment\n" + render(slice_)
            + "\n\n## Applicable rules\n" + render([describe_rule(r) for r in rules])
            + "\n\n## Current violations on this fragment\n"
            + render([v.dump() for v in violations])
            + "\n\nPropose the typed operations (JSON)."
        )
        request = LlmRequest(role=AgentRole.REFACTOR, task="plan-fragment", system=self.system, user=user,
                             context={"fragment": fragment.pointer, "kind": fragment.kind.value,
                                      "violations": [v.dump() for v in violations],
                                      "rules": [r.id for r in rules]})
        return await self.llm.generate(request, ops.OperationsProposal)

    async def plan(self, doc: SpecDocument, refs: RefIndex, targets: list[tuple[Fragment, list[Violation]]],
                   iteration: int, known_rule_ids: set[str]) -> RefactoringPlan:
        plan = RefactoringPlan(iteration=iteration)
        proposed: list[ops.PlannedOperation] = []
        for fragment, violations in targets:
            try:
                proposal = await self.propose_for_fragment(doc, refs, fragment, violations)
            except LlmCallError as exc:
                plan.failed_fragments.append(fragment.pointer or "/")
                plan.issues.append(PlanIssue(kind="LLM_FAILED", message=f"{fragment.label}: {exc}"))
                log.warning("[PLAN] %s: frammento marcato come FALLITO (%s)", fragment.label, exc)
                continue
            plan.rationales[fragment.pointer or "/"] = proposal.rationale
            proposed.extend(ops.PlannedOperation(operation=o, fragment=fragment.pointer or "/", proposed_by="refactor")
                            for o in proposal.operations)
        plan.operations, validation_issues = self.validator.validate(doc, proposed, known_rule_ids)
        plan.issues.extend(validation_issues)
        log.info("[PLAN] %d operazioni proposte, %d nel piano validato, %d problemi di piano",
                 len(proposed), len(plan.operations), len(plan.issues))
        return plan

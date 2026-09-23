import copy

from app.diff.compat import ChangeType, is_breaking
from app.diff.semantic_diff import compute_diff
from app.model.document import SpecDocument, SpecVersion
from app.refactor.engine import RefactoringEngine
from app.refactor.operations import OperationsProposal, PlannedOperation
from app.refactor.planner import PlanValidator
from app.rules.models import SpectralRule, RuleScope
from app.rules.registry import RuleRegistry

BASE = {
    "openapi": "3.0.3", "info": {"title": "x", "version": "1.0.0"},
    "paths": {"/orders": {"post": {
        "operationId": "createOrder",
        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/order"}}}},
        "responses": {"201": {"description": "ok"}, "404": {"description": "nf"}}}}},
    "components": {"schemas": {
        "order": {"type": "object", "properties": {"status": {"type": "string", "enum": ["A", "B"]}}},
        "Order": {"type": "object"}}},
}


def doc(data=None) -> SpecDocument:
    return SpecDocument(source_file="t.yaml", source_version=SpecVersion.OPENAPI_3_0,
                        target_version=SpecVersion.OPENAPI_3_0, data=copy.deepcopy(data or BASE))


def planned(*ops) -> list[PlannedOperation]:
    return [PlannedOperation(operation=o) for o in OperationsProposal.model_validate({"operations": list(ops)}).operations]


def registry() -> RuleRegistry:
    spectral = SpectralRule(id="SPEC-1", description="d", severity="ERROR", given="$", scope=RuleScope.ANY,
                            rulesetFile="r.yaml")
    return RuleRegistry([], [spectral])


def test_plan_rejects_conflicting_renames_and_rename_to_existing_name():
    ops, issues = PlanValidator(registry()).validate(doc(), planned(
        {"type": "RENAME_SCHEMA", "from": "order", "to": "PurchaseOrder", "ruleId": "LLM-1"},
        {"type": "RENAME_SCHEMA", "from": "order", "to": "SalesOrder", "ruleId": "LLM-2"},
    ), known_rule_ids={"LLM-1", "LLM-2"})
    assert ops == [] and issues[0].kind == "CONFLICT" and len(issues[0].rejected) == 2

    ops, issues = PlanValidator(registry()).validate(doc(), planned(
        {"type": "RENAME_SCHEMA", "from": "order", "to": "Order", "ruleId": "LLM-1"}), known_rule_ids={"LLM-1"})
    assert ops == [] and "duplicato" in issues[0].message


def test_plan_conflict_prefers_the_deterministic_rule_and_reports_it():
    ops, issues = PlanValidator(registry()).validate(doc(), planned(
        {"type": "RENAME_SCHEMA", "from": "order", "to": "PurchaseOrder", "ruleId": "LLM-1"},
        {"type": "RENAME_SCHEMA", "from": "order", "to": "SalesOrder", "ruleId": "SPEC-1"},
    ), known_rule_ids={"LLM-1"})
    assert [p.operation.to for p in ops] == ["SalesOrder"]
    assert issues[0].kept["ruleId"] == "SPEC-1"


def test_plan_rejects_unknown_rule_ids_and_dedupes_identical_ops():
    ops, issues = PlanValidator(registry()).validate(doc(), planned(
        {"type": "REMOVE_FIELD", "target": "/paths/~1orders/post/responses/404", "ruleId": "MADE-UP"},
        {"type": "ADD_HEADER", "target": "/paths/~1orders/post", "header": "Idempotency-Key", "ruleId": "SPEC-1"},
        {"type": "ADD_HEADER", "target": "/paths/~1orders/post", "header": "Idempotency-Key", "ruleId": "SPEC-1"},
    ), known_rule_ids=set())
    assert [i.kind for i in issues] == ["UNKNOWN_RULE"]
    assert len(ops) == 1


def test_engine_reports_missing_target_explicitly_and_keeps_going():
    new, applied, failures = RefactoringEngine().apply(doc(), planned(
        {"type": "RENAME_PATH", "from": "/orders", "to": "/purchase-orders", "ruleId": "R"},
        {"type": "ADD_HEADER", "target": "/paths/~1orders/post", "header": "Idempotency-Key", "ruleId": "R"},
        {"type": "ADD_OPERATION_ID", "target": "/paths/~1purchase-orders/post", "operationId": "create", "ruleId": "R"},
    ))
    assert [c.type for c in applied] == ["RENAME_PATH", "ADD_OPERATION_ID"]
    assert failures[0].type == "ADD_HEADER" and "non trovato" in failures[0].reason


def test_engine_rename_schema_updates_refs_and_classifies_changes():
    new, applied, failures = RefactoringEngine().apply(doc(), planned(
        {"type": "RENAME_SCHEMA", "from": "order", "to": "PurchaseOrder", "ruleId": "R"},
        {"type": "ADD_HEADER", "target": "/paths/~1orders/post", "header": "Idempotency-Key", "ruleId": "R"},
        {"type": "SET_FIELD", "target": "/info/description", "value": "desc", "ruleId": "R"},
    ))
    assert not failures
    ref = new.data["paths"]["/orders"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/PurchaseOrder"
    assert [c.category for c in applied] == ["STRUCTURAL", "SEMANTIC", "GOVERNANCE"]
    assert BASE["components"]["schemas"].get("order")  # input non mutato


def test_breaking_classification_table():
    assert is_breaking(ChangeType.PARAMETER_ADDED, required=True)
    assert not is_breaking(ChangeType.PARAMETER_ADDED, required=False)
    assert is_breaking(ChangeType.PROPERTY_ADDED, {"request"}, required=True)
    assert not is_breaking(ChangeType.PROPERTY_ADDED, {"request"}, required=False)
    assert not is_breaking(ChangeType.PROPERTY_ADDED, {"response"}, required=True)
    assert is_breaking(ChangeType.ENUM_VALUE_REMOVED, {"request"})
    assert not is_breaking(ChangeType.ENUM_VALUE_REMOVED, {"response"})
    assert is_breaking(ChangeType.SECURITY_REQUIREMENT_ADDED)
    assert not is_breaking(ChangeType.SECURITY_REQUIREMENT_REMOVED)
    assert is_breaking(ChangeType.RESPONSE_REMOVED)


def test_diff_flags_untraced_changes_as_unexpected():
    before = doc()
    after, applied, _ = RefactoringEngine().apply(before, planned(
        {"type": "ADD_HEADER", "target": "/paths/~1orders/post", "header": "Idempotency-Key", "ruleId": "R-1"}))
    del after.data["paths"]["/orders"]["post"]["responses"]["404"]  # regressione non pianificata
    after.data["components"]["schemas"]["order"]["properties"]["status"]["enum"] = ["A"]
    changes = {c.type: c for c in compute_diff(before, after, applied)}
    header = changes[ChangeType.PARAMETER_ADDED]
    assert header.expected and header.rule_id == "R-1" and header.breaking
    removed = changes[ChangeType.RESPONSE_REMOVED]
    assert not removed.expected and removed.breaking and removed.before == {"description": "nf"}
    enum = changes[ChangeType.ENUM_VALUE_REMOVED]
    assert enum.breaking and not enum.expected  # schema usato in request: rimuovere un valore accettato rompe


def test_diff_pairs_renamed_schemas_instead_of_remove_add():
    before = doc()
    after, applied, _ = RefactoringEngine().apply(before, planned(
        {"type": "RENAME_SCHEMA", "from": "order", "to": "PurchaseOrder", "ruleId": "R"}))
    changes = compute_diff(before, after, applied)
    assert [c.type for c in changes] == [ChangeType.SCHEMA_RENAMED]
    assert changes[0].expected and not changes[0].breaking

"""Naming deterministico con aggiornamento dei riferimenti, collisioni, frammenti a livello di operation,
ripresa da checkpoint, apiLifecycle draft."""

from __future__ import annotations

import copy
import json

import pytest

from app.diff.compat import ChangeType
from app.diff.semantic_diff import compute_diff
from app.errors import PipelineError
from app.llm.base import AgentRole
from app.model.document import SpecDocument, SpecVersion
from app.model.refs import RefIndex
from app.output import OutputWriter
from app.pipeline import RefactorPipeline, RunStatus
from app.refactor.deterministic import deterministic_fixes
from app.refactor.engine import RefactoringEngine
from app.refactor.fragments import build_fragments, build_slice
from app.refactor.planner import PlanValidator, rules_for
from app.rules.models import Casing, CompiledRule, NameCasing, NameTarget, RuleCondition, RuleScope
from app.rules.registry import RuleRegistry
from app.validators.rule_eval import RuleEvaluator
from tests.conftest import APIS

ORDER = {
    "type": "object",
    "required": ["order_total", "currency"],
    "properties": {"order_total": {"type": "number", "format": "double"}, "currency": {"type": "string"}},
    "example": {"order_total": 10.5, "currency": "EUR"},
}
PET = {
    "type": "object",
    "required": ["pet_type"],
    "properties": {"pet_type": {"type": "string"}},
    "discriminator": {"propertyName": "pet_type", "mapping": {"cat": "#/components/schemas/cat_pet", "dog": "cat_pet"}},
}
SPEC = {
    "openapi": "3.0.3", "info": {"title": "x", "version": "1.0.0"},
    "paths": {
        "/Order_Items/{orderId}": {
            "parameters": [{"name": "orderId", "in": "path", "required": True, "schema": {"type": "string"}},
                           {"name": "x_trace_id", "in": "header", "schema": {"type": "string"}}],
            "get": {
                "operationId": "get_order",
                "parameters": [{"name": "page_size", "in": "query", "schema": {"type": "integer", "format": "int32"}}],
                "responses": {"200": {
                    "description": "ok",
                    "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/Order"},
                        "example": {"order_total": 5, "currency": "EUR"},
                        "examples": {"big": {"value": {"order_total": 999, "currency": "USD"}}}}},
                    "links": {"self": {"operationId": "get_order",
                                       "parameters": {"orderId": "$request.path.orderId",
                                                      "query.page_size": "$request.query.page_size"}}}}}},
            "put": {"operationId": "put_order", "responses": {"200": {"description": "ok"}}},
        },
        "/pets": {"get": {"operationId": "listPets", "responses": {"200": {
            "description": "ok", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/cat_pet"}},
                "example": [{"pet_type": "cat"}]}},
            "links": {"order": {"operationRef": "#/paths/~1Order_Items~1{orderId}/get",
                                "parameters": {"orderId": "$response.body#/id"}}}}}}},
    },
    "components": {"schemas": {"Order": ORDER, "cat_pet": PET}},
}


def doc(data=None) -> SpecDocument:
    return SpecDocument(source_file="t.yaml", source_version=SpecVersion.OPENAPI_3_0,
                        target_version=SpecVersion.OPENAPI_3_0, data=copy.deepcopy(data or SPEC))


def naming_rule(*targets: tuple[NameTarget, Casing]) -> CompiledRule:
    return CompiledRule(id="NAMING-X", text="t", file="n.md", line=1, scope=RuleScope.ANY, condition=RuleCondition(),
                        requirements=[NameCasing(target=t, casing=c) for t, c in targets])


def run_deterministic(base: SpecDocument, rule: CompiledRule):
    violations = RuleEvaluator(base).evaluate([rule])
    registry = RuleRegistry([rule], [])
    det = deterministic_fixes(base, violations, registry, build_fragments(base))
    ordered, issues = PlanValidator(registry).validate(base, det.operations, {})  # come nella pipeline
    assert not [i for i in issues if i.kind != "TARGET_NORMALIZED"], issues
    new, applied, failures = RefactoringEngine().apply(base, ordered)
    return violations, det, new, applied, failures


def test_property_rename_updates_required_examples_and_referenced_schema_usages():
    base = doc()
    _, det, new, applied, failures = run_deterministic(base, naming_rule((NameTarget.PROPERTY_NAME, Casing.CAMEL)))
    assert not failures
    order = new.data["components"]["schemas"]["Order"]
    assert list(order["properties"]) == ["orderTotal", "currency"]
    assert order["required"] == ["orderTotal", "currency"]
    assert order["example"] == {"orderTotal": 10.5, "currency": "EUR"}
    media = new.data["paths"]["/Order_Items/{orderId}"]["get"]["responses"]["200"]["content"]["application/json"]
    assert media["example"] == {"orderTotal": 5, "currency": "EUR"}  # media type che usa lo schema via $ref
    assert media["examples"]["big"]["value"] == {"orderTotal": 999, "currency": "USD"}
    pet = new.data["components"]["schemas"]["cat_pet"]
    assert pet["discriminator"]["propertyName"] == "petType" and pet["required"] == ["petType"]
    pets = new.data["paths"]["/pets"]["get"]["responses"]["200"]["content"]["application/json"]
    assert pets["example"] == [{"petType": "cat"}]  # array di oggetti via items.$ref
    # tracciate col ruleId, SEMANTIC e breaking
    assert all(c.rule_id == "NAMING-X" and c.category == "SEMANTIC" and c.proposed_by == "deterministic"
               for c in applied)
    changes = compute_diff(base, new, applied)
    assert all(c.expected for c in changes)
    assert {c.type for c in changes if c.breaking} >= {ChangeType.PROPERTY_REMOVED}


def test_schema_rename_updates_refs_and_discriminator_mappings():
    _, det, new, applied, failures = run_deterministic(doc(), naming_rule((NameTarget.SCHEMA_NAME, Casing.PASCAL)))
    assert not failures and [c.type for c in applied] == ["RENAME_SCHEMA"]
    assert "CatPet" in new.data["components"]["schemas"] and "cat_pet" not in new.data["components"]["schemas"]
    items = new.data["paths"]["/pets"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["items"]
    assert items == {"$ref": "#/components/schemas/CatPet"}
    assert new.data["components"]["schemas"]["CatPet"]["discriminator"]["mapping"] == {
        "cat": "#/components/schemas/CatPet", "dog": "CatPet"}
    assert RefIndex(new.data).broken_refs() == []


def test_path_parameter_and_operation_id_renames_update_template_and_links():
    rule = naming_rule((NameTarget.PATH_SEGMENT, Casing.KEBAB), (NameTarget.QUERY_PARAMETER, Casing.CAMEL),
                       (NameTarget.HEADER, Casing.TRAIN), (NameTarget.OPERATION_ID, Casing.CAMEL))
    _, det, new, applied, failures = run_deterministic(doc(), rule)
    assert not failures, failures
    paths = new.data["paths"]
    assert "/order-items/{order-id}" in paths and "/Order_Items/{orderId}" not in paths
    item = paths["/order-items/{order-id}"]
    assert [p["name"] for p in item["parameters"]] == ["order-id", "X-Trace-Id"]
    op = item["get"]
    assert op["operationId"] == "getOrder" and op["parameters"][0]["name"] == "pageSize"
    link = op["responses"]["200"]["links"]["self"]
    assert link["operationId"] == "getOrder"
    assert link["parameters"] == {"order-id": "$request.path.order-id", "query.pageSize": "$request.query.pageSize"}
    other = paths["/pets"]["get"]["responses"]["200"]["links"]["order"]
    assert other["operationRef"] == "#/paths/~1order-items~1{order-id}/get"
    assert other["parameters"] == {"order-id": "$response.body#/id"}
    assert {p.operation.type for p in det.operations} == {"RENAME_PARAMETER", "RENAME_PATH", "ADD_OPERATION_ID"}


def test_collisions_are_not_renamed_and_go_to_the_llm():
    data = copy.deepcopy(SPEC)
    order = data["components"]["schemas"]["Order"]
    order["properties"]["orderTotal"] = {"type": "number", "format": "double"}  # esiste già
    order["properties"]["tax_amount"] = {"type": "number", "format": "double"}
    order["properties"]["tax__amount"] = {"type": "number", "format": "double"}  # diventerebbe uguale
    data["components"]["schemas"]["CatPet"] = {"type": "object"}  # cat_pet -> CatPet collide
    rule = naming_rule((NameTarget.PROPERTY_NAME, Casing.CAMEL), (NameTarget.SCHEMA_NAME, Casing.PASCAL))
    violations, det, new, applied, _ = run_deterministic(doc(data), rule)
    renamed = {(c.type, c.before) for c in applied}
    assert ("RENAME_PROPERTY", "order_total") not in renamed
    assert ("RENAME_PROPERTY", "tax_amount") not in renamed and ("RENAME_PROPERTY", "tax__amount") not in renamed
    assert ("RENAME_SCHEMA", "cat_pet") not in renamed
    assert ("RENAME_PROPERTY", "pet_type") in renamed  # nessuna collisione: rinominata
    visible = [v for v in violations if det.is_llm_visible(v)]
    assert {v.path.rsplit("/", 1)[-1] for v in visible} == {"order_total", "tax_amount", "tax__amount", "cat_pet"}


def test_operation_fragment_carries_only_the_operation_and_path_parameters():
    base = doc()
    [frag] = [f for f in build_fragments(base) if f.pointer == "/paths/~1Order_Items~1{orderId}/get"]
    slice_ = build_slice(base, frag, RefIndex(base.data), 12000)
    assert slice_["content"] == base.data["paths"]["/Order_Items/{orderId}"]["get"]  # non l'intero path item
    assert "put" not in json.dumps(slice_["content"])
    assert [p["name"] for p in slice_["pathParameters"]] == ["orderId", "x_trace_id"]


async def test_only_rules_cited_by_the_fragment_violations_are_sent(rules_dir, tmp_path):
    from app.llm.fake import FakeLlmProvider
    from app.llm.structured import StructuredLlm
    from app.rules.interpreter import RuleInterpreter
    from app.rules.registry import build_registry
    from app.model.document import ElementKind
    from tests.fake_agents import interpreter_answer

    registry, _ = await build_registry(rules_dir, RuleInterpreter(StructuredLlm(FakeLlmProvider(interpreter_answer),
                                                                                5, 0), tmp_path / "c"))
    applicable = registry.for_fragment(ElementKind.OPERATION, "get", "/x")
    cited = rules_for(registry, ["DE-JSON-001-camelcase-properties", "SEC-001", "DE-JSON-001-camelcase-properties",
                                 "UNKNOWN"])
    assert [r.id for r in cited] == ["DE-JSON-001-camelcase-properties", "SEC-001"]
    assert len(applicable) > 10 * len(cited) / 2


# ── pipeline ─────────────────────────────────────────────────────────────
@pytest.mark.needs_node
async def test_spectral_casing_violations_are_fixed_without_llm(config, agents, rules_dir):
    """Caso 002: DE-JSON-001 (Spectral) e NAMING-001 corrette in modo deterministico, niente Refactor Agent."""
    provider = agents.provider()
    result = await RefactorPipeline(config, provider, rules_dir).run(APIS / "case-002-naming.yaml")
    assert not [r for r in provider.requests if r.role == AgentRole.REFACTOR]
    props = result.final.data["components"]["schemas"]["PaymentOrder"]["properties"]
    assert set(props) == {"id", "amountValue", "currency", "payerIban", "createdAt"}
    renames = [p for p in result.plans[0].operations if p.operation.type.startswith("RENAME")]
    assert renames and all(p.proposed_by == "deterministic" for p in renames)
    assert not [v for v in result.final_governance if v.severity == "ERROR"]


@pytest.mark.needs_node
async def test_draft_api_lifecycle_lists_breaking_changes_but_is_success(config, agents, rules_dir, tmp_path):
    cfg = config.with_overrides(api_lifecycle="draft")
    result = await RefactorPipeline(cfg, agents.provider(), rules_dir).run(APIS / "case-002-naming.yaml")
    summary = json.loads((OutputWriter(tmp_path / "out").write(result) / "reports" / "summary.json").read_text())
    assert result.status == RunStatus.SUCCESS
    assert summary["status"] == "SUCCESS" and summary["apiLifecycle"] == "draft"
    assert summary["breakingChanges"]  # restano elencate


class Crash(Exception):
    pass


@pytest.mark.needs_node
async def test_resume_from_checkpoint_after_simulated_interruption(config, agents, rules_dir, tmp_path):
    spec = APIS / "case-001-swagger2-legacy.yaml"
    # riferimento: run senza interruzioni
    reference = await RefactorPipeline(config, agents.provider(), rules_dir).run(spec)

    # run interrotta alla terza chiamata del Refactor Agent (dopo 2 frammenti pianificati)
    checkpoint = tmp_path / "ckpt"
    calls = {"refactor": 0}
    original_refactor = agents.refactor

    def crashing_refactor(request):
        calls["refactor"] += 1
        if calls["refactor"] == 3:
            raise Crash("interruzione simulata")
        return original_refactor(request)

    agents.refactor = crashing_refactor
    with pytest.raises(Crash):
        await RefactorPipeline(config, agents.provider(), rules_dir).run(spec, checkpoint)
    journal = (checkpoint / "llm-journal.jsonl").read_text().splitlines()
    assert sum(1 for line in journal if json.loads(line)["role"] == "refactor") == 2

    # ripresa: le chiamate già fatte vengono dal checkpoint, il resto dal modello
    agents.refactor = original_refactor
    provider = agents.provider()
    resumed = await RefactorPipeline(config, provider, rules_dir).run(spec, checkpoint, resume=True)
    assert resumed.resumed and resumed.replayed_llm_calls >= 2
    live_refactor = [r for r in provider.requests if r.role == AgentRole.REFACTOR]
    assert len(live_refactor) == 2  # 4 frammenti: 2 dal checkpoint, 2 dal modello
    assert resumed.status == reference.status and resumed.final.data == reference.final.data
    assert [c.description for c in resumed.final_applied] == [c.description for c in reference.final_applied]


@pytest.mark.needs_node
async def test_resume_refuses_changed_inputs_and_missing_checkpoint(config, agents, rules_dir, tmp_path):
    spec = APIS / "case-003-no-problem-details.yaml"
    checkpoint = tmp_path / "ckpt"
    with pytest.raises(PipelineError, match="nessun checkpoint"):
        await RefactorPipeline(config, agents.provider(), rules_dir).run(spec, checkpoint, resume=True)
    await RefactorPipeline(config, agents.provider(), rules_dir).run(spec, checkpoint)
    (rules_dir / "general.md").write_text((rules_dir / "general.md").read_text() + "- [NEW-1] Nuova regola.\n")
    with pytest.raises(PipelineError, match="regole"):
        await RefactorPipeline(config, agents.provider(), rules_dir).run(spec, checkpoint, resume=True)
    with pytest.raises(PipelineError, match="configurazione"):
        cfg = config.with_overrides(max_iterations=5)
        (rules_dir / "general.md").write_text((rules_dir / "general.md").read_text().replace("- [NEW-1] Nuova regola.\n", ""))
        await RefactorPipeline(cfg, agents.provider(), rules_dir).run(spec, checkpoint, resume=True)

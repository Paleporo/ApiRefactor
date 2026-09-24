"""Correttezza del feedback loop: correzioni deterministiche, piano stretto, target robusti, Critic solo quando
serve, nessuna regressione tra iterazioni, output = miglior candidato, tempi per fase."""

from __future__ import annotations

import copy
import json

import pytest
import yaml

from app.llm.base import AgentRole
from app.model import pointer as jp
from app.model.document import SpecDocument, SpecVersion
from app.output import OutputWriter
from app.pipeline import PHASES, RefactorPipeline, RunStatus
from app.refactor.deterministic import deterministic_fixes, generate_operation_id
from app.refactor.engine import RefactoringEngine
from app.refactor.fragments import build_fragments
from app.refactor.operations import OperationsProposal, PlannedOperation
from app.refactor.planner import PlanValidator
from app.rules.models import (CompiledRule, ErrorFormat, RequireOperationId, RequireResponse, RequireSecurity,
                              RuleCondition, RuleScope)
from app.rules.registry import RuleRegistry
from app.validators.rule_eval import RuleEvaluator
from tests.conftest import APIS

R400 = "/paths/~1accounts/get/responses/400"


def load(name: str) -> dict:
    return yaml.safe_load((APIS / name).read_text())


def as_doc(data: dict) -> SpecDocument:
    return SpecDocument(source_file="t.yaml", source_version=SpecVersion.OPENAPI_3_0,
                        target_version=SpecVersion.OPENAPI_3_0, data=copy.deepcopy(data))


def planned(*ops, fragment: str = "", proposed_by: str = "refactor") -> list[PlannedOperation]:
    return [PlannedOperation(operation=o, fragment=fragment, proposed_by=proposed_by)
            for o in OperationsProposal.model_validate({"operations": list(ops)}).operations]


def compiled(rule_id: str, *requirements, methods=None) -> CompiledRule:
    return CompiledRule(id=rule_id, text="t", file="r.md", line=1, scope=RuleScope.OPERATION,
                        condition=RuleCondition(methods=methods), requirements=list(requirements))


def det_for(doc: SpecDocument, *rules: CompiledRule):
    registry = RuleRegistry(list(rules), [])
    violations = RuleEvaluator(doc).evaluate(list(rules))
    return violations, deterministic_fixes(doc, violations, registry, build_fragments(doc))


# ── 1. correzioni deterministiche ────────────────────────────────────────
def test_error_format_is_fixed_deterministically_reusing_or_creating_problem():
    doc = as_doc(load("case-003-no-problem-details.yaml"))
    rule = compiled("ERR-001", ErrorFormat(requiredProperties=["type", "title", "status", "code"]))
    violations, det = det_for(doc, rule)
    assert len(det.handled) == len(violations) == 2
    kinds = [p.operation.type for p in det.operations]
    assert kinds == ["ADD_COMPONENT", "CONVERT_ERROR_RESPONSE", "CONVERT_ERROR_RESPONSE"]  # Problem creato una volta
    problem = det.operations[0].operation
    assert problem.name == "Problem" and problem.definition["required"] == ["type", "title", "status", "code"]
    assert problem.definition["properties"]["status"] == {"type": "integer", "format": "int32"}
    assert all(p.proposed_by == "deterministic" and p.operation.rule_id == "ERR-001" for p in det.operations)
    assert det.covered == [R400, "/paths/~1accounts~1{account-id}/get/responses/404"]

    # uno schema conforme esistente viene riusato; un "Problem" non conforme non viene sovrascritto
    doc2 = as_doc(load("case-005-regression-guard.yaml"))
    doc2.data["paths"]["/cards"]["post"]["responses"]["400"]["content"] = {
        "application/json": {"schema": {"type": "object"}}}
    _, det2 = det_for(doc2, rule)
    assert [p.operation.type for p in det2.operations] == ["CONVERT_ERROR_RESPONSE"]
    assert det2.operations[0].operation.schema_ref == "#/components/schemas/Problem"
    doc2.data["components"]["schemas"]["Problem"] = {"type": "object", "properties": {"message": {"type": "string"}}}
    _, det3 = det_for(doc2, rule)
    assert det3.operations[0].operation.name == "ProblemDetails"


def test_other_deterministic_fixes_and_generated_values():
    doc = as_doc(load("case-004-incomplete-security.yaml"))
    del doc.data["paths"]["/merchants"]["get"]["operationId"]
    rules = [compiled("OPID-001", RequireOperationId()),
             compiled("RESP-001", RequireResponse(status="404"), methods=["get"]),
             compiled("SEC-001", RequireSecurity(schemeType="http", scheme="bearer", bearerFormat="JWT"))]
    _, det = det_for(doc, *rules)
    by_type: dict[str, list] = {}
    for p in det.operations:
        by_type.setdefault(p.operation.type, []).append(p.operation)
    assert by_type["ADD_OPERATION_ID"][0].operation_id == "getMerchants"
    assert by_type["ADD_RESPONSE"][0].description == "Not Found"  # reason phrase HTTP
    assert by_type["ADD_SECURITY_SCHEME"][0].scheme == {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    # GET /merchants/{merchant-id} ha già un requisito (bearerAuth, non definito): sostituirlo non è meccanico -> LLM
    assert [o.target for o in by_type["SET_SECURITY_REQUIREMENT"]] == ["/paths/~1merchants/get"]
    assert generate_operation_id("/accounts/{account-id}", "get", {"getAccountsByAccountId"}) == \
        "getAccountsByAccountId2"


# ── 2. piano più stretto ─────────────────────────────────────────────────
def test_llm_operation_without_violation_in_its_fragment_is_discarded():
    doc = as_doc(load("case-003-no-problem-details.yaml"))
    evidence = {"/paths/~1accounts/get": [("DE-STATUS-002", R400 + "/content")],
                "/components/schemas/Account": [("DE-JSON-001", "/components/schemas/Account/properties/x")]}
    ops, issues = PlanValidator(RuleRegistry([], [])).validate(doc, planned(
        {"type": "ADD_SECURITY_SCHEME", "name": "bearerAuth", "scheme": {"type": "http", "scheme": "bearer"},
         "ruleId": "SEC-001"},
        {"type": "ADD_OPERATION_ID", "target": "/paths/~1accounts/get", "operationId": "x", "ruleId": "DE-JSON-001"},
        fragment="/paths/~1accounts/get"), evidence)
    assert ops == []
    assert [i.kind for i in issues] == ["RULE_WITHOUT_VIOLATION"] * 2  # DE-JSON-001 è di un altro frammento


def test_set_field_overwriting_an_object_needs_a_violation_on_that_element():
    doc = as_doc(load("case-003-no-problem-details.yaml"))
    content = {"application/problem+json": {"schema": {"$ref": "#/components/schemas/ErrorMessage"}}}
    evidence = {"/paths/~1accounts/get": [("R-1", "/paths/~1accounts/get/responses/200")]}
    ops, issues = PlanValidator(RuleRegistry([], [])).validate(doc, planned(
        {"type": "SET_FIELD", "target": R400 + "/content", "value": content, "ruleId": "R-1"},
        fragment="/paths/~1accounts/get"), evidence)
    assert ops == [] and issues[0].kind == "SET_FIELD_UNJUSTIFIED"
    evidence["/paths/~1accounts/get"].append(("R-1", R400 + "/content"))
    ops, _ = PlanValidator(RuleRegistry([], [])).validate(doc, planned(
        {"type": "SET_FIELD", "target": R400 + "/content", "value": content, "ruleId": "R-1"},
        fragment="/paths/~1accounts/get"), evidence)
    assert len(ops) == 1
    _, applied, _ = RefactoringEngine().apply(doc, ops)
    assert applied[0].category == "SEMANTIC"
    # anche sotto /info: sovrascrivere un oggetto non vuoto è SEMANTIC; impostare un campo è GOVERNANCE
    _, applied, _ = RefactoringEngine().apply(doc, planned(
        {"type": "SET_FIELD", "target": "/info/contact", "value": {"name": "Altro"}, "ruleId": "R"},
        {"type": "SET_FIELD", "target": "/info/x-owner", "value": "team", "ruleId": "R"}))
    assert [c.category for c in applied] == ["SEMANTIC", "GOVERNANCE"]


# ── 3. target robusti ────────────────────────────────────────────────────
def test_unescaped_media_type_target_is_normalized_when_unique():
    doc = as_doc(load("case-003-no-problem-details.yaml"))
    new, applied, failures = RefactoringEngine().apply(doc, planned(
        {"type": "REPLACE_RESPONSE_SCHEMA", "target": "/paths//accounts/get/responses/400",
         "mediaType": "application/json", "schema": {"type": "object"}, "ruleId": "R"},
        {"type": "SET_FIELD", "target": R400 + "/content/application/json/schema/description", "value": "x",
         "ruleId": "R"}))
    assert not failures
    assert jp.resolve(new.data, R400 + "/content/application~1json/schema") == {"type": "object", "description": "x"}
    assert all("normalizzato" in c.description for c in applied)
    # la normalizzazione fa riconoscere come duplicate due operazioni scritte con escape diversi
    new_schema = {"$ref": "#/components/schemas/ErrorMessage"}
    ops, issues = PlanValidator(RuleRegistry([], [])).validate(doc, planned(
        {"type": "REPLACE_RESPONSE_SCHEMA", "target": R400, "schema": new_schema, "ruleId": "R"},
        {"type": "REPLACE_RESPONSE_SCHEMA", "target": "/paths//accounts/get/responses/400", "schema": new_schema,
         "ruleId": "R"}, fragment="/paths/~1accounts/get"), {"/paths/~1accounts/get": [("R", R400)]})
    assert len(ops) == 1 and any(i.kind == "TARGET_NORMALIZED" for i in issues)


def test_ambiguous_or_missing_target_is_not_guessed():
    data = {"x": {"application/json": {"k": 1}, "application": {"json": {"q": 2}}}}
    assert jp.normalize(data, "/x/application/json/q") == "/x/application/json/q"  # esiste così com'è
    assert jp.normalize(data, "/x/application/json/k") == "/x/application~1json/k"  # unica lettura esistente
    ambiguous = {"a/b": {"c": {"k": 1}}, "a": {"b/c": {"k": 2}}}
    assert jp.normalize(ambiguous, "/a/b/c/k") is None  # due letture: nessuna scelta
    assert jp.normalize(data, "/x/text/plain/k") is None  # nessuna lettura
    doc = as_doc({"openapi": "3.0.3", "info": {"title": "x", "version": "1"}, "paths": {}, **ambiguous})
    _, applied, failures = RefactoringEngine().apply(doc, planned(
        {"type": "REMOVE_FIELD", "target": "/a/b/c/k", "ruleId": "R"}))
    assert applied == [] and failures  # fallisce in modo esplicito, come prima


# ── 4-7. pipeline ────────────────────────────────────────────────────────
@pytest.mark.needs_node
async def test_case_003_error_format_fixed_deterministically_despite_a_misbehaving_llm(config, agents, rules_dir,
                                                                                      tmp_path):
    """Una violazione per l'LLM (snake_case) e un LLM che propone anche operazioni sbagliate."""
    spec = load("case-003-no-problem-details.yaml")
    spec["components"]["schemas"]["Account"]["properties"]["holder_name"] = \
        spec["components"]["schemas"]["Account"]["properties"].pop("holderName")
    spec_file = tmp_path / "accounts.yaml"
    spec_file.write_text(yaml.safe_dump(spec, sort_keys=False))
    content = {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorMessage"}}}

    def misbehaving_refactor(request):
        ops = [  # tutte con ruleId senza violazioni nel frammento, o target non escapati: da scartare
            {"type": "ADD_SECURITY_SCHEME", "name": "extraAuth", "scheme": {"type": "http", "scheme": "basic"},
             "ruleId": "SEC-001"},
            {"type": "REPLACE_RESPONSE_SCHEMA", "target": "/paths/~1accounts/get/responses/400",
             "mediaType": "application/json", "schema": {"type": "object"}, "ruleId": "DE-STATUS-002-problem-json-errors"},
            {"type": "SET_FIELD", "target": R400 + "/content", "value": content, "ruleId": "ERR-001"},
        ]
        for v in request.context["violations"]:
            if v["path"].endswith("/holder_name"):
                ops.append({"type": "RENAME_PROPERTY", "target": "/components/schemas/Account", "from": "holder_name",
                            "to": "holderName", "ruleId": v["ruleId"]})
        return {"operations": ops, "rationale": "misbehaving"}

    agents.refactor = misbehaving_refactor
    provider = agents.provider()
    result = await RefactorPipeline(config, provider, rules_dir).run(spec_file)

    # rename della proprietà e conversione degli errori: breaking attesi
    assert result.status == RunStatus.SUCCESS_WITH_BREAKING_CHANGES, result.reasons
    for ptr in (R400, "/paths/~1accounts~1{account-id}/get/responses/404"):
        assert jp.resolve(result.final.data, ptr)["content"] == {
            "application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}
    assert "extraAuth" not in result.final.data["components"]["securitySchemes"]
    plan = result.plans[0]
    assert {p.operation.type for p in plan.operations if p.proposed_by == "deterministic"} == {
        "ADD_COMPONENT", "CONVERT_ERROR_RESPONSE"}
    assert sum(1 for i in plan.issues if i.kind == "RULE_WITHOUT_VIOLATION") == 3
    # l'LLM non ha ricevuto le violazioni errorFormat né quelle Spectral sulle stesse response
    sent = [v["ruleId"] for r in provider.requests if r.role == AgentRole.REFACTOR for v in r.context["violations"]]
    assert sent and not {"ERR-001", "DE-STATUS-002-problem-json-errors"} & set(sent)
    assert not [v for v in result.final_governance if v.rule_id in ("ERR-001", "DE-STATUS-002-problem-json-errors")]


@pytest.mark.needs_node
async def test_critic_not_invoked_on_candidates_with_governance_errors(config, agents, rules_dir, tmp_path):
    agents.refactor = lambda r: {"operations": [], "rationale": "nothing"}
    agents.correction = lambda r: {"operations": [], "rationale": "nothing"}
    provider = agents.provider()
    result = await RefactorPipeline(config, provider, rules_dir).run(APIS / "case-002-naming.yaml")
    out = OutputWriter(tmp_path / "output").write(result)

    assert not [r for r in provider.requests if r.role == AgentRole.CRITIC]
    assert all(it.critic.skipped and "ERROR di governance" in it.critic.skip_reason for it in result.iterations)
    critic_report = json.loads((out / "reports" / "critic-report.json").read_text())
    assert all(i["skipped"] and i["skipReason"] for i in critic_report["iterations"])
    assert result.status == RunStatus.NEEDS_REVIEW
    assert result.output_is_baseline  # nessun candidato ha migliorato la baseline


@pytest.mark.needs_node
async def test_worsening_correction_is_rejected_and_output_is_best_candidate(config, agents, rules_dir, tmp_path):
    def partial_refactor(request):  # V1 migliora: corregge solo amount_value
        ops = [{"type": "RENAME_PROPERTY", "target": "/components/schemas/payment_order", "from": "amount_value",
                "to": "amountValue", "ruleId": v["ruleId"]}
               for v in request.context["violations"] if v["path"].endswith("/amount_value")]
        return {"operations": ops[:1], "rationale": "partial"}

    def worsening_correction(request):  # aggiunge una proprietà non conforme: +2 ERROR
        rule = next((p["rule_id"] for p in request.context["problems"] if p["rule_id"].startswith("DE-JSON-001")), None)
        ops = [] if rule is None else [{"type": "SET_FIELD", "ruleId": rule, "value": {"type": "string"},
                                        "target": "/components/schemas/payment_order/properties/bad_prop"}]
        return {"operations": ops, "rationale": "worse"}

    agents.refactor, agents.correction = partial_refactor, worsening_correction
    provider = agents.provider()
    result = await RefactorPipeline(config, provider, rules_dir).run(APIS / "case-002-naming.yaml")
    out = OutputWriter(tmp_path / "output").write(result)

    rejected = [it for it in result.iterations if it.rejected]
    assert [it.iteration for it in rejected] == [2, 3]
    assert any("ERROR da" in r for r in rejected[0].rejection_reasons)
    assert all(v.path.endswith("/bad_prop") for v in rejected[0].new_violations)
    # output = miglior candidato (V1), non l'ultimo scartato; mai peggiore della baseline
    assert result.final_iteration == 1 and not result.output_is_baseline
    props = result.final.data["components"]["schemas"]["payment_order"]["properties"]
    assert "amountValue" in props and "bad_prop" not in props
    assert result.final_counts["errors"] < result.baseline_counts["errors"]
    # la seconda correzione riceve il tentativo scartato, per non ripeterlo
    corrections = [r for r in provider.requests if r.role == AgentRole.CORRECTION]
    assert "previousAttemptRejected" not in corrections[0].user and "previousAttemptRejected" in corrections[1].user
    summary = json.loads((out / "reports" / "summary.json").read_text())
    assert [r["iteration"] for r in summary["rejectedIterations"]] == [2, 3]
    assert summary["output"] == {"iteration": 1, "isBaseline": False, "note": None}
    changes = json.loads((out / "reports" / "changes.json").read_text())
    assert changes["rejected"] and all("bad_prop" in c["description"] or "bad_prop" in json.dumps(c["locations"])
                                       for c in changes["rejected"])
    assert all(c["inFinalOutput"] for c in changes["applied"])


@pytest.mark.needs_node
async def test_no_candidate_better_than_baseline_outputs_the_original(config, agents, rules_dir, tmp_path):
    agents.refactor = lambda r: {"operations": [], "rationale": "nothing"}
    agents.correction = lambda r: {"operations": [
        {"type": "SET_FIELD", "ruleId": p["rule_id"], "value": {"type": "string"},
         "target": "/components/schemas/payment_order/properties/other_bad"}
        for p in r.context["problems"] if p["rule_id"].startswith("DE-JSON-001")][:1], "rationale": "worse"}
    result = await RefactorPipeline(config, agents.provider(), rules_dir).run(APIS / "case-002-naming.yaml")
    out = OutputWriter(tmp_path / "output").write(result)

    assert result.output_is_baseline and result.final.data == result.baseline.data
    assert result.status == RunStatus.NEEDS_REVIEW
    summary = json.loads((out / "reports" / "summary.json").read_text())
    assert summary["output"]["isBaseline"] is True
    assert "nessun candidato migliora la baseline" in summary["output"]["note"]
    assert (out / "refactored" / "case-002-naming.yaml").read_text() == yaml.safe_dump(
        result.baseline.data, sort_keys=False, allow_unicode=True, width=120)


@pytest.mark.needs_node
async def test_summary_reports_phase_timings_and_llm_calls_per_role(config, agents, rules_dir, tmp_path):
    result = await RefactorPipeline(config, agents.provider(), rules_dir).run(APIS / "case-002-naming.yaml")
    summary = json.loads((OutputWriter(tmp_path / "output").write(result) / "reports" / "summary.json").read_text())
    assert set(PHASES) | {"total"} <= set(summary["timings"])
    assert all(isinstance(v, float) and v >= 0 for v in summary["timings"].values())
    roles = summary["llmByRole"]
    assert roles["rule-interpreter"]["calls"] == 8 and roles["refactor"]["calls"] >= 1 and roles["critic"]["calls"] >= 1
    assert all({"calls", "seconds", "failures"} <= set(s) for s in roles.values())

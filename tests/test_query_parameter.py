"""Requisito DSL requireQueryParameter + operazione ADD_QUERY_PARAMETER."""

from __future__ import annotations

import copy

import pytest

from app.diff.compat import ChangeType
from app.diff.semantic_diff import compute_diff
from app.model.document import SpecDocument, SpecVersion
from app.pipeline import RefactorPipeline, RunStatus
from app.prompts import load_prompt
from app.refactor.engine import RefactoringEngine
from app.refactor.operations import OperationsProposal, PlannedOperation
from app.refactor.planner import PlanValidator
from app.rules.models import CompiledRule, RequireQueryParameter, RuleCondition, RuleScope
from app.rules.registry import RuleRegistry
from app.validators.rule_eval import RuleEvaluator
from tests.conftest import APIS
from tests.fake_agents import interpreter_answer

LIMIT = {"name": "limit", "in": "query", "schema": {"type": "integer", "format": "int32"}}
SPEC = {
    "openapi": "3.0.3", "info": {"title": "x", "version": "1.0.0"},
    "paths": {
        "/items": {
            "get": {"operationId": "listItems", "responses": {"200": {"description": "ok"}}},
            "post": {"operationId": "createItem", "responses": {"201": {"description": "ok"}}},
        },
        "/items/{item-id}": {
            "parameters": [{"name": "item-id", "in": "path", "required": True, "schema": {"type": "string"}}],
            "get": {"operationId": "getItem", "parameters": [{**LIMIT, "required": True}],
                    "responses": {"200": {"description": "ok"}}},
        },
        "/archived-items": {
            "parameters": [LIMIT],
            "get": {"operationId": "listArchived", "responses": {"200": {"description": "ok"}}},
        },
        "/deleted-items": {
            "get": {"operationId": "listDeleted", "parameters": [{"$ref": "#/components/parameters/Limit"}],
                    "responses": {"200": {"description": "ok"}}},
        },
    },
    "components": {"parameters": {"Limit": LIMIT}},
}


def doc() -> SpecDocument:
    return SpecDocument(source_file="t.yaml", source_version=SpecVersion.OPENAPI_3_0,
                        target_version=SpecVersion.OPENAPI_3_0, data=copy.deepcopy(SPEC))


def rule(required: bool = False, methods: list[str] | None = None) -> CompiledRule:
    return CompiledRule(id="Q-001", text="t", file="q.md", line=1, scope=RuleScope.OPERATION,
                        condition=RuleCondition(methods=methods if methods is not None else ["get"]),
                        requirements=[RequireQueryParameter(name="limit", required=required)])


def planned(*ops) -> list[PlannedOperation]:
    return [PlannedOperation(operation=o) for o in OperationsProposal.model_validate({"operations": list(ops)}).operations]


def test_evaluator_required_false_checks_only_presence_on_matching_methods():
    violations = {v.path: v for v in RuleEvaluator(doc()).evaluate([rule(required=False)])}
    # GET /items/{item-id} dichiara limit obbligatorio: con required=false basta che esista -> nessuna violazione.
    # POST escluso da condition.methods; path-level e $ref soddisfano il requisito
    assert set(violations) == {"/paths/~1items/get"}
    assert violations["/paths/~1items/get"].expected == {"name": "limit", "required": False}


def test_evaluator_required_true_and_all_methods():
    paths = {v.path for v in RuleEvaluator(doc()).evaluate([rule(required=True, methods=[])])}
    assert paths == {"/paths/~1items/get", "/paths/~1items/post", "/paths/~1archived-items/get",
                     "/paths/~1deleted-items/get"}


def test_engine_adds_only_missing_and_refuses_ambiguous_targets():
    base = doc()
    new, applied, failures = RefactoringEngine().apply(base, planned(
        {"type": "ADD_QUERY_PARAMETER", "target": "/paths/~1items/get", "name": "limit", "ruleId": "Q-001",
         "schema": {"type": "integer", "format": "int32"}},
        {"type": "ADD_QUERY_PARAMETER", "target": "/paths/~1items~1{item-id}/get", "name": "limit",
         "required": False, "ruleId": "Q-001"},
        {"type": "ADD_QUERY_PARAMETER", "target": "/paths/~1archived-items/get", "name": "limit", "ruleId": "Q-001"},
        {"type": "ADD_QUERY_PARAMETER", "target": "/paths/~1archived-items/get", "name": "limit", "required": True,
         "ruleId": "Q-001"},
        {"type": "ADD_QUERY_PARAMETER", "target": "/paths/~1deleted-items/get", "name": "limit", "required": True,
         "ruleId": "Q-001"},
    ))
    assert new.data["paths"]["/items"]["get"]["parameters"] == [
        {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "format": "int32"}}]
    # required=false su un parametro obbligatorio esistente: nessuna modifica dell'obbligatorietà
    assert new.data["paths"]["/items/{item-id}"]["get"]["parameters"][0]["required"] is True
    assert [(c.type, c.category) for c in applied] == [("ADD_QUERY_PARAMETER", "GOVERNANCE")]
    # path-level già presente -> nessun effetto; required=true su path-level/$ref opzionali -> errore esplicito
    assert [f.target for f in failures] == ["/paths/~1archived-items/get", "/paths/~1deleted-items/get"]
    assert "livello di path" in failures[0].reason and "$ref" in failures[1].reason
    assert "parameters" not in new.data["paths"]["/archived-items"]["get"]
    assert new.data["paths"]["/deleted-items"]["get"]["parameters"] == [{"$ref": "#/components/parameters/Limit"}]


def test_required_query_parameter_is_semantic_and_breaking_optional_is_not():
    base = doc()
    new, applied, _ = RefactoringEngine().apply(base, planned(
        {"type": "ADD_QUERY_PARAMETER", "target": "/paths/~1items/get", "name": "limit", "ruleId": "Q-001"},
        {"type": "ADD_QUERY_PARAMETER", "target": "/paths/~1items/post", "name": "dryRun", "required": True,
         "ruleId": "Q-002"},
    ))
    assert [c.category for c in applied] == ["GOVERNANCE", "SEMANTIC"]
    added = {c.location: c for c in compute_diff(base, new, applied) if c.type == ChangeType.PARAMETER_ADDED}
    assert not added["/paths/~1items/get/parameters/0"].breaking
    assert added["/paths/~1items/post/parameters/0"].breaking
    assert all(c.expected for c in added.values())


def test_plan_conflict_on_same_query_parameter():
    ops, issues = PlanValidator(RuleRegistry([], [])).validate(doc(), planned(
        {"type": "ADD_QUERY_PARAMETER", "target": "/paths/~1items/get", "name": "limit", "required": False,
         "ruleId": "A"},
        {"type": "ADD_QUERY_PARAMETER", "target": "/paths/~1items/get", "name": "limit", "required": True,
         "ruleId": "B"},
    ), {"": [("A", "/paths/~1items/get"), ("B", "/paths/~1items/get")]})
    assert ops == [] and issues[0].kind == "CONFLICT"


def test_interpreter_prompt_forbids_closest_match_fallback():
    prompt = load_prompt("rule_interpreter")
    assert "requireQueryParameter" in prompt
    assert "NEVER fall back to the most similar kind" in prompt


@pytest.mark.needs_node
async def test_pipeline_adds_missing_query_parameter_end_to_end(config, agents, rules_dir):
    (rules_dir / "query.md").write_text(
        "- [QUERY-LANG-001] Le operation GET devono accettare il query parameter opzionale `lang`.\n")

    def interpreter(request):
        if request.context["ruleId"] == "QUERY-LANG-001":
            return {"scope": "operation", "condition": {"methods": ["get"]}, "severity": "ERROR",
                    "requirements": [{"kind": "requireQueryParameter", "name": "lang", "required": False}]}
        return interpreter_answer(request)

    agents.interpreter = interpreter
    result = await RefactorPipeline(config, agents.provider(), rules_dir).run(APIS / "case-005-regression-guard.yaml")

    assert result.status == RunStatus.SUCCESS, result.reasons
    get_params = result.final.data["paths"]["/cards/{card-id}"]["get"]["parameters"]
    assert {"name": "lang", "in": "query", "required": False, "schema": {"type": "string"}} in get_params
    assert not any(p.get("name") == "lang" for p in result.final.data["paths"]["/cards"]["post"].get("parameters", []))
    added = [c for c in result.final_diff if c.type == ChangeType.PARAMETER_ADDED and c.rule_id == "QUERY-LANG-001"]
    assert len(added) == 1 and added[0].expected and not added[0].breaking
    assert not [v for v in result.final_governance if v.rule_id == "QUERY-LANG-001"]


# ── semantica di required: false = "deve esistere", required: true = "deve esistere ed essere obbligatorio" ──
REQUIRED_LIMIT = {**LIMIT, "required": True}
DECLARED = {
    "operation": {"/x": {"get": {"parameters": [REQUIRED_LIMIT], "responses": {"200": {"description": "ok"}}}}},
    "path": {"/x": {"parameters": [REQUIRED_LIMIT], "get": {"responses": {"200": {"description": "ok"}}}}},
    "ref": {"/x": {"get": {"parameters": [{"$ref": "#/components/parameters/Limit"}],
                          "responses": {"200": {"description": "ok"}}}}},
}


def spec_with(level: str, limit_required: bool = True) -> SpecDocument:
    limit = {**LIMIT, "required": limit_required}
    paths = copy.deepcopy(DECLARED[level])
    if level == "operation":
        paths["/x"]["get"]["parameters"] = [limit]
    elif level == "path":
        paths["/x"]["parameters"] = [limit]
    return SpecDocument(source_file="t.yaml", source_version=SpecVersion.OPENAPI_3_0,
                        target_version=SpecVersion.OPENAPI_3_0,
                        data={"openapi": "3.0.3", "info": {"title": "x", "version": "1.0.0"}, "paths": paths,
                              "components": {"parameters": {"Limit": limit}}})


@pytest.mark.parametrize("level", ["operation", "path", "ref"])
def test_required_false_accepts_a_mandatory_parameter_without_touching_it(level):
    base = spec_with(level, limit_required=True)
    assert RuleEvaluator(base).evaluate([rule(required=False)]) == []
    new, applied, failures = RefactoringEngine().apply(base, planned(
        {"type": "ADD_QUERY_PARAMETER", "target": "/paths/~1x/get", "name": "limit", "required": False,
         "ruleId": "Q-001"}))
    assert applied == [] and failures == []
    assert new.data == base.data  # nemmeno una lista `parameters` creata
    assert compute_diff(base, new, applied) == []


def test_required_false_missing_parameter_is_added_as_optional_non_breaking():
    base = spec_with("operation")
    del base.data["paths"]["/x"]["get"]["parameters"]
    [violation] = RuleEvaluator(base).evaluate([rule(required=False)])
    assert violation.expected == {"name": "limit", "required": False}
    new, applied, _ = RefactoringEngine().apply(base, planned(
        {"type": "ADD_QUERY_PARAMETER", "target": "/paths/~1x/get", "name": "limit", "required": False,
         "ruleId": "Q-001"}))
    assert new.data["paths"]["/x"]["get"]["parameters"][0]["required"] is False
    assert [c.category for c in applied] == ["GOVERNANCE"]
    [change] = compute_diff(base, new, applied)
    assert change.type == ChangeType.PARAMETER_ADDED and not change.breaking and change.expected
    assert RuleEvaluator(new).evaluate([rule(required=False)]) == []


@pytest.mark.parametrize("level", ["operation", "path", "ref"])
def test_required_true_flags_an_optional_parameter(level):
    base = spec_with(level, limit_required=False)
    [violation] = RuleEvaluator(base).evaluate([rule(required=True)])
    assert violation.expected == {"name": "limit", "required": True} and violation.actual == {"required": False}


def test_required_true_correction_is_semantic_and_breaking():
    base = spec_with("operation", limit_required=False)
    new, applied, failures = RefactoringEngine().apply(base, planned(
        {"type": "ADD_QUERY_PARAMETER", "target": "/paths/~1x/get", "name": "limit", "required": True,
         "ruleId": "Q-001"}))
    assert not failures and [c.category for c in applied] == ["SEMANTIC"]
    assert new.data["paths"]["/x"]["get"]["parameters"][0]["required"] is True
    [change] = compute_diff(base, new, applied)
    assert change.type == ChangeType.PARAMETER_BECAME_REQUIRED and change.breaking and change.expected
    assert RuleEvaluator(new).evaluate([rule(required=True)]) == []


@pytest.mark.needs_node
async def test_pipeline_does_not_relax_a_mandatory_parameter_end_to_end(config, agents, rules_dir, tmp_path):
    """Regola di paginazione con required=false su un'API che dichiara limit obbligatorio: nessun cambio."""
    import yaml

    spec = yaml.safe_load((APIS / "case-003-no-problem-details.yaml").read_text())
    spec["paths"]["/accounts"]["get"]["parameters"][0]["required"] = True
    spec_file = tmp_path / "accounts-required-limit.yaml"
    spec_file.write_text(yaml.safe_dump(spec, sort_keys=False))
    (rules_dir / "paging.md").write_text("- [PAGE-LIMIT-001] GET /accounts deve accettare il query parameter `limit`.\n")

    def interpreter(request):
        if request.context["ruleId"] == "PAGE-LIMIT-001":
            return {"scope": "operation", "condition": {"methods": ["get"], "pathPattern": "^/accounts$"},
                    "severity": "ERROR",
                    "requirements": [{"kind": "requireQueryParameter", "name": "limit", "required": False}]}
        return interpreter_answer(request)

    agents.interpreter = interpreter
    result = await RefactorPipeline(config, agents.provider(), rules_dir).run(spec_file)

    # il caso 003 converte le response di errore (breaking, atteso); nessuna modifica breaking su `limit`
    assert result.status == RunStatus.SUCCESS_WITH_BREAKING_CHANGES, result.reasons
    assert not [b for b in result.breaking_changes if "parameters" in b["location"]]
    assert result.final.data["paths"]["/accounts"]["get"]["parameters"][0]["required"] is True
    assert not [v for it in result.iterations for v in it.governance if v.rule_id == "PAGE-LIMIT-001"]
    assert not [c for c in result.applied if c.rule_id == "PAGE-LIMIT-001"]
    assert not [c for c in result.final_diff if "parameters" in c.location]

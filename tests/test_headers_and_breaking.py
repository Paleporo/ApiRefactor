"""requireHeader: required senza default ("supportare" = accettare); modifiche breaking in evidenza."""

from __future__ import annotations

import json

import pytest
import yaml

from app.cli import EXIT_CODES, main
from app.diff.compat import ChangeType
from app.diff.semantic_diff import compute_diff
from app.model.document import SpecDocument, SpecVersion
from app.output import OutputWriter
from app.pipeline import RefactorPipeline, RunStatus
from app.refactor.engine import RefactoringEngine
from app.refactor.operations import OperationsProposal, PlannedOperation
from app.rules.models import CompiledRule, RequireHeader, RuleCondition, RuleScope
from app.validators.rule_eval import RuleEvaluator
from tests.conftest import APIS
from tests.fake_agents import interpreter_answer

POST = "/paths/~1cards/post"
HEADER = {"name": "Idempotency-Key", "in": "header", "schema": {"type": "string"}}
MANDATORY_RULE = "- [HDR-REQ-001] Ogni POST deve inviare obbligatoriamente l'header `X-Request-Id`.\n"


def test_require_header_has_no_default():
    with pytest.raises(Exception, match="required"):
        RequireHeader(header="Idempotency-Key")


def spec_with_header(level: str, header_required: bool) -> SpecDocument:
    data = yaml.safe_load((APIS / "case-005-regression-guard.yaml").read_text())
    header = {**HEADER, "required": header_required}
    if level == "operation":
        data["paths"]["/cards"]["post"]["parameters"] = [header]
    elif level == "path":
        data["paths"]["/cards"]["parameters"] = [header]
    else:
        data["components"]["parameters"] = {"IdempotencyKey": header}
        data["paths"]["/cards"]["post"]["parameters"] = [{"$ref": "#/components/parameters/IdempotencyKey"}]
    return SpecDocument(source_file="t.yaml", source_version=SpecVersion.OPENAPI_3_0,
                        target_version=SpecVersion.OPENAPI_3_0, data=data)


def rule(required: bool) -> CompiledRule:
    return CompiledRule(id="HTTP-IDEMPOTENCY-001", text="t", file="g.md", line=1, scope=RuleScope.OPERATION,
                        condition=RuleCondition(methods=["post"]),
                        requirements=[RequireHeader(header="idempotency-key", required=required)])


def add_header(required: bool) -> list[PlannedOperation]:
    ops = OperationsProposal.model_validate({"operations": [
        {"type": "ADD_HEADER", "target": POST, "header": "Idempotency-Key", "required": required,
         "ruleId": "HTTP-IDEMPOTENCY-001"}]}).operations
    return [PlannedOperation(operation=o) for o in ops]


@pytest.mark.parametrize("level", ["operation", "path", "ref"])
def test_required_false_accepts_any_existing_header_and_never_touches_it(level):
    for header_required in (True, False):
        base = spec_with_header(level, header_required)
        assert RuleEvaluator(base).evaluate([rule(required=False)]) == []  # nome confrontato senza maiuscole
        new, applied, failures = RefactoringEngine().apply(base, add_header(required=False))
        assert applied == [] and failures == [] and new.data == base.data


@pytest.mark.parametrize("level", ["operation", "path", "ref"])
def test_required_true_flags_an_optional_header(level):
    base = spec_with_header(level, header_required=False)
    [violation] = RuleEvaluator(base).evaluate([rule(required=True)])
    assert violation.expected == {"header": "idempotency-key", "required": True}
    new, applied, failures = RefactoringEngine().apply(base, add_header(required=True))
    if level == "operation":
        assert [c.category for c in applied] == ["SEMANTIC"]
        [change] = compute_diff(base, new, applied)
        assert change.type == ChangeType.PARAMETER_BECAME_REQUIRED and change.breaking and change.expected
    else:  # non si duplica l'header sull'operation: errore esplicito
        assert applied == [] and failures and new.data == base.data


def test_missing_header_with_required_false_is_added_optional_and_non_breaking():
    base = spec_with_header("operation", header_required=False)
    del base.data["paths"]["/cards"]["post"]["parameters"]
    new, applied, _ = RefactoringEngine().apply(base, add_header(required=False))
    assert new.data["paths"]["/cards"]["post"]["parameters"] == [{**HEADER, "required": False}]
    assert [c.category for c in applied] == ["GOVERNANCE"]
    [change] = compute_diff(base, new, applied)
    assert change.type == ChangeType.PARAMETER_ADDED and not change.breaking


@pytest.mark.needs_node
async def test_idempotency_rule_adds_optional_header_and_run_is_success(config, agents, rules_dir, tmp_path):
    result = await RefactorPipeline(config, agents.provider(), rules_dir).run(APIS / "case-005-regression-guard.yaml")
    out = OutputWriter(tmp_path / "output").write(result)

    assert result.registry.get("HTTP-IDEMPOTENCY-001").requirements[0].required is False
    assert result.status == RunStatus.SUCCESS, result.reasons
    params = result.final.data["paths"]["/cards"]["post"]["parameters"]
    assert {**HEADER, "required": False} in params
    [added] = [c for c in result.final_diff if c.rule_id == "HTTP-IDEMPOTENCY-001"]
    assert added.type == ChangeType.PARAMETER_ADDED and not added.breaking
    summary = json.loads((out / "reports" / "summary.json").read_text())
    assert summary["status"] == "SUCCESS" and summary["breakingChanges"] == []


def mandatory_interpreter(request):
    if request.context["ruleId"] == "HDR-REQ-001":
        return {"scope": "operation", "condition": {"methods": ["post"]}, "severity": "ERROR",
                "requirements": [{"kind": "requireHeader", "header": "X-Request-Id", "required": True}]}
    return interpreter_answer(request)


@pytest.mark.needs_node
async def test_mandatory_header_rule_is_breaking_and_reported(config, agents, rules_dir, tmp_path):
    (rules_dir / "headers.md").write_text(MANDATORY_RULE)
    agents.interpreter = mandatory_interpreter
    result = await RefactorPipeline(config, agents.provider(), rules_dir).run(APIS / "case-005-regression-guard.yaml")
    out = OutputWriter(tmp_path / "output").write(result)

    assert result.registry.get("HDR-REQ-001").requirements[0].required is True
    assert result.status == RunStatus.SUCCESS_WITH_BREAKING_CHANGES, result.reasons
    assert result.reasons == []  # nulla da rivedere: solo modifiche breaking da comunicare
    assert any(p["name"] == "X-Request-Id" and p["required"] for p in
               result.final.data["paths"]["/cards"]["post"]["parameters"])
    summary = json.loads((out / "reports" / "summary.json").read_text())
    assert list(summary)[:2] == ["status", "breakingChanges"]
    assert summary["breakingChanges"] == [{"location": f"{POST}/parameters/1", "type": "PARAMETER_ADDED",
                                           "ruleId": "HDR-REQ-001", "expected": True}]
    semantic = json.loads((out / "reports" / "changes.json").read_text())["semanticChanges"]
    assert [c["ruleId"] for c in semantic] == ["HDR-REQ-001"]


@pytest.mark.needs_node
def test_cli_reports_breaking_changes_with_a_distinct_exit_code(agents, rules_dir, tmp_path, monkeypatch, capsys):
    import app.service

    (rules_dir / "headers.md").write_text(MANDATORY_RULE)
    agents.interpreter = mandatory_interpreter
    monkeypatch.setattr(app.service, "OllamaProvider", lambda config: agents.provider())
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"compiledRulesCacheDir": str(tmp_path / "cache")}))
    code = main(["refactor", "--input", str(APIS / "case-005-regression-guard.yaml"), "--output",
                 str(tmp_path / "out"), "--rules", str(rules_dir), "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == EXIT_CODES["SUCCESS_WITH_BREAKING_CHANGES"] == 4
    assert len(set(EXIT_CODES.values())) == len(EXIT_CODES)  # ogni stato ha il suo codice
    assert "Stato finale: SUCCESS_WITH_BREAKING_CHANGES" in out
    assert "1 modifiche BREAKING" in out and "PARAMETER_ADDED" in out and "HDR-REQ-001" in out


async def test_cache_compiled_with_previous_dsl_is_recompiled(tmp_path, rules_dir):
    """Le regole in cache del DSL 2 avevano requireHeader.required = true per default: vanno ricompilate."""
    from app.llm.fake import FakeLlmProvider
    from app.llm.structured import StructuredLlm
    from app.rules.interpreter import RuleInterpreter
    from app.rules.models import DSL_VERSION
    from app.rules.registry import build_registry

    calls: list = []

    def handler(request):
        calls.append(request.context["ruleId"])
        return interpreter_answer(request)

    interpreter = RuleInterpreter(StructuredLlm(FakeLlmProvider(handler), 5, 0), tmp_path / "cache")
    await build_registry(rules_dir, interpreter)
    [cache] = (tmp_path / "cache").glob("general.*.json")
    payload = json.loads(cache.read_text())
    payload["dslVersion"] = str(int(DSL_VERSION) - 1)
    for r in payload["rules"]:
        if r["id"] == "HTTP-IDEMPOTENCY-001":
            r["requirements"][0]["required"] = True  # il vecchio default
    cache.write_text(json.dumps(payload))

    before = len(calls)
    registry, _ = await build_registry(rules_dir, interpreter)
    assert len(calls) - before == 5  # general.md ricompilato
    assert registry.get("HTTP-IDEMPOTENCY-001").requirements[0].required is False
    assert json.loads(cache.read_text())["dslVersion"] == DSL_VERSION

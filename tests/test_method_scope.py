"""Regole che nominano metodi HTTP: la compilazione deve restringere condition.methods a quei metodi.

Regressione osservata con Ollama reale: HTTP-IDEMPOTENCY-001 ("Ogni endpoint POST ...") compilata con
condition.methods = null -> Idempotency-Key obbligatorio aggiunto anche alle GET (SEMANTIC, breaking e,
essendo motivato da un ruleId, "expected" nel diff: né diff né Critic potevano bloccarlo).
"""

from __future__ import annotations

import json

import pytest

from app.llm.fake import FakeLlmProvider
from app.llm.structured import StructuredLlm
from app.output import OutputWriter
from app.pipeline import RefactorPipeline, RunStatus
from app.rules.interpreter import RuleInterpreter, check_method_scope, methods_named_in
from app.rules.models import JudgmentRequirement, RequireHeader, RuleCondition
from app.rules.registry import build_registry
from tests.conftest import APIS
from tests.fake_agents import RULE_DRAFTS, interpreter_answer

IDEMPOTENCY_TEXT = "Ogni endpoint POST deve supportare Idempotency-Key"
HEADER = [RequireHeader(header="Idempotency-Key", required=True)]
UNSCOPED_IDEMPOTENCY = {**RULE_DRAFTS["HTTP-IDEMPOTENCY-001"], "condition": {"methods": None}}


def test_methods_named_in_text():
    assert methods_named_in("Ogni endpoint POST e put, poi `GET`/Delete") == ["post", "put", "get", "delete"]
    assert methods_named_in("target, budget, deleted, gotten, input: nessun metodo") == []


@pytest.mark.parametrize("methods, ok", [
    (None, False),              # la regola varrebbe per ogni metodo
    ([], False),
    (["get"], False),           # metodo sbagliato
    (["post", "get"], False),   # più ampia del testo
    (["post"], True),
    (["POST"], True),
])
def test_check_method_scope_on_mechanical_rules(methods, ok):
    call = lambda: check_method_scope(IDEMPOTENCY_TEXT, RuleCondition(methods=methods), HEADER)  # noqa: E731
    if ok:
        call()
    else:
        with pytest.raises(ValueError, match="condition.methods"):
            call()


def test_check_method_scope_ignores_judgment_rules_and_texts_without_methods():
    check_method_scope(IDEMPOTENCY_TEXT, RuleCondition(), [JudgmentRequirement(guidance="g")])
    check_method_scope("Tutte le operation devono avere un operationId", RuleCondition(), HEADER)


def interpreter_with(handler, tmp_path, retries: int = 1) -> tuple[RuleInterpreter, FakeLlmProvider]:
    provider = FakeLlmProvider(handler)
    return RuleInterpreter(StructuredLlm(provider, call_timeout=5, technical_retries=retries), tmp_path / "cache"), provider


async def test_unscoped_compilation_is_retried_with_feedback_and_normalized(tmp_path, rules_dir):
    answers = iter([UNSCOPED_IDEMPOTENCY, {**RULE_DRAFTS["HTTP-IDEMPOTENCY-001"], "condition": {"methods": ["POST"]}}])

    def handler(request):
        if request.context["ruleId"] == "HTTP-IDEMPOTENCY-001":
            return next(answers)
        return interpreter_answer(request)

    interpreter, provider = interpreter_with(handler, tmp_path)
    registry, report = await build_registry(rules_dir, interpreter)
    rule = registry.get("HTTP-IDEMPOTENCY-001")
    assert rule.condition.methods == ["post"] and not rule.compile_failed
    assert not report.failed_rules
    retried = [r for r in provider.requests if r.context.get("ruleId") == "HTTP-IDEMPOTENCY-001"]
    assert len(retried) == 2 and "condition.methods is null" in retried[1].user


async def test_persistently_unscoped_rule_becomes_judgment_with_warning_and_is_not_cached(tmp_path, rules_dir):
    def handler(request):
        if request.context["ruleId"] == "HTTP-IDEMPOTENCY-001":
            return UNSCOPED_IDEMPOTENCY
        return interpreter_answer(request)

    interpreter, _ = interpreter_with(handler, tmp_path, retries=2)
    registry, report = await build_registry(rules_dir, interpreter)
    rule = registry.get("HTTP-IDEMPOTENCY-001")
    assert rule.compile_failed and rule.judgment_only
    assert report.failed_rules == ["HTTP-IDEMPOTENCY-001"]
    assert any("HTTP-IDEMPOTENCY-001" in w and "giudizio" in w for w in registry.info.warnings)
    assert not any(p.name.startswith("general.") for p in (tmp_path / "cache").glob("*.json"))


async def test_non_conforming_rule_in_cache_is_recompiled(tmp_path, rules_dir):
    calls: list = []

    def handler(request):
        calls.append(request.context["ruleId"])
        return interpreter_answer(request)

    interpreter, _ = interpreter_with(handler, tmp_path)
    await build_registry(rules_dir, interpreter)
    [cache] = [p for p in (tmp_path / "cache").glob("general.*.json")]
    payload = json.loads(cache.read_text())
    for rule in payload["rules"]:  # simula una cache scritta prima del controllo, con la regola troppo ampia
        if rule["id"] == "HTTP-IDEMPOTENCY-001":
            rule["condition"]["methods"] = None
    cache.write_text(json.dumps(payload))

    before = len(calls)
    registry, report = await build_registry(rules_dir, interpreter)
    assert len(calls) - before == 5  # solo general.md (5 regole) ricompilato
    assert registry.get("HTTP-IDEMPOTENCY-001").condition.methods == ["post"]


@pytest.mark.needs_node
async def test_case_003_get_operations_never_receive_idempotency_key(config, agents, rules_dir, tmp_path):
    """Il modello continua a compilare HTTP-IDEMPOTENCY-001 senza methods: nessun header sulle GET."""
    def interpreter(request):
        if request.context["ruleId"] == "HTTP-IDEMPOTENCY-001":
            return UNSCOPED_IDEMPOTENCY
        return interpreter_answer(request)

    agents.interpreter = interpreter
    result = await RefactorPipeline(config, agents.provider(), rules_dir).run(APIS / "case-003-no-problem-details.yaml")
    out = OutputWriter(tmp_path / "output").write(result)

    # la regola non è verificata meccanicamente: la run non può chiudere in SUCCESS
    assert result.status == RunStatus.NEEDS_REVIEW
    [reason] = result.reasons
    assert "HTTP-IDEMPOTENCY-001" in reason and "condition.methods is null" in reason
    summary = json.loads((out / "reports" / "summary.json").read_text())
    assert summary["status"] == "NEEDS_REVIEW" and summary["reasons"] == result.reasons
    [failed] = summary["compileFailedRules"]
    assert failed["ruleId"] == "HTTP-IDEMPOTENCY-001" and failed["file"].endswith("general.md")
    assert "condition.methods is null" in failed["reason"]
    # tutto il resto è pulito: l'unico motivo di revisione è la regola non compilata
    assert all(it.exit_conditions.all_met for it in result.iterations[-1:])
    for item in result.final.data["paths"].values():
        for op in item.values():
            assert not any(p.get("name") == "Idempotency-Key" for p in op.get("parameters", []))
    assert not [c for c in result.applied if c.rule_id == "HTTP-IDEMPOTENCY-001"]
    assert not [v for it in result.iterations for v in it.governance if v.rule_id == "HTTP-IDEMPOTENCY-001"]
    assert not [c for c in result.final_diff if c.rule_id == "HTTP-IDEMPOTENCY-001"]
    report = json.loads((out / "reports" / "governance-report.json").read_text())
    assert "HTTP-IDEMPOTENCY-001" in report["rules"]["compileCache"]["failed"]
    assert any("HTTP-IDEMPOTENCY-001" in w for w in report["rules"]["warnings"])


@pytest.mark.needs_node
async def test_any_compile_failed_rule_forces_needs_review(config, agents, rules_dir, tmp_path):
    """Qualunque causa di compileFailed (qui: output non conforme allo schema) porta a NEEDS_REVIEW."""
    def interpreter(request):
        if request.context["ruleId"] == "SEC-001":
            return "non è JSON"
        return interpreter_answer(request)

    agents.interpreter = interpreter
    result = await RefactorPipeline(config, agents.provider(), rules_dir).run(APIS / "case-005-regression-guard.yaml")
    out = OutputWriter(tmp_path / "output").write(result)

    assert result.status == RunStatus.NEEDS_REVIEW
    summary = json.loads((out / "reports" / "summary.json").read_text())
    assert [f["ruleId"] for f in summary["compileFailedRules"]] == ["SEC-001"]
    assert "schema" in summary["compileFailedRules"][0]["reason"]
    assert any("SEC-001" in r for r in summary["reasons"])


@pytest.mark.needs_node
async def test_no_compile_failed_rules_keeps_success_and_empty_list(config, agents, rules_dir, tmp_path):
    result = await RefactorPipeline(config, agents.provider(), rules_dir).run(APIS / "case-005-regression-guard.yaml")
    out = OutputWriter(tmp_path / "output").write(result)
    assert result.status == RunStatus.SUCCESS, result.reasons
    assert json.loads((out / "reports" / "summary.json").read_text())["compileFailedRules"] == []

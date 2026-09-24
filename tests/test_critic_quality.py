"""Qualità del Critic reale (Ollama, modello da config.yaml) su candidati difettosi costruiti a mano.

I candidati sono ottenuti in modo deterministico dai casi esistenti, senza Refactor Agent; le regole vengono
dagli agenti finti (deterministiche). Per ogni difetto il Critic deve produrre almeno una issue bloccante
pertinente; sul caso di controllo deve accettare. Esito e durata di ogni caso vengono riportati a fine run
(tabella nel terminale e output/critic-quality.json).

Esecuzione: uv run pytest -m requires_ollama tests/test_critic_quality.py
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import pytest
import yaml

from app.config import load_config
from app.critic import CriticEngine, Verification
from app.diff.semantic_diff import compute_diff, untraced_violations
from app.errors import PreflightError
from app.llm.fake import FakeLlmProvider
from app.llm.ollama_provider import OllamaProvider
from app.llm.structured import StructuredLlm
from app.model import pointer as jp
from app.model.document import SpecDocument, SpecVersion
from app.refactor.engine import RefactoringEngine
from app.refactor.fragments import build_fragments
from app.refactor.operations import OperationsProposal, PlannedOperation
from app.rules.interpreter import RuleInterpreter
from app.rules.registry import build_registry
from app.validators.governance import GovernanceValidator
from app.validators.openapi_validator import OpenAPIValidator
from tests.conftest import APIS, CRITIC_QUALITY_RESULTS, ROOT
from tests.fake_agents import interpreter_answer

pytestmark = [pytest.mark.requires_ollama, pytest.mark.needs_node]


@dataclass
class Scenario:
    name: str
    spec: str
    fragment: str
    defect: str | None  # None = caso di controllo
    mutate: Callable[[SpecDocument], list]  # modifica il candidato; ritorna gli AppliedChange tracciati


def remove_404(doc: SpecDocument) -> list:
    jp.remove(doc.data, "/paths/~1cards~1{card-id}/get/responses/404")
    return []


def remove_required_currency(doc: SpecDocument) -> list:
    schema = doc.data["components"]["schemas"]["payment_order"]
    del schema["properties"]["currency"]
    schema["required"] = [r for r in schema["required"] if r != "currency"]
    return []


def holder_name_to_integer(doc: SpecDocument) -> list:
    doc.data["components"]["schemas"]["Card"]["properties"]["holderName"] = {"type": "integer", "format": "int32"}
    return []


def legit_optional_header(doc: SpecDocument) -> list:
    ops = OperationsProposal.model_validate({"operations": [
        {"type": "ADD_HEADER", "target": "/paths/~1cards/post", "header": "Idempotency-Key", "required": False,
         "ruleId": "HTTP-IDEMPOTENCY-001"}]}).operations
    new, applied, _ = RefactoringEngine().apply(doc, [PlannedOperation(operation=o) for o in ops])
    doc.data = new.data
    return applied


SCENARIOS = [
    Scenario("response 404 rimossa", "case-005-regression-guard.yaml", "/paths/~1cards~1{card-id}/get",
             "/paths/~1cards~1{card-id}/get/responses/404", remove_404),
    Scenario("campo required rimosso da uno schema di request", "case-002-naming.yaml",
             "/components/schemas/payment_order", "/components/schemas/payment_order/properties/currency",
             remove_required_currency),
    Scenario("tipo proprietà string -> integer", "case-005-regression-guard.yaml", "/components/schemas/Card",
             "/components/schemas/Card/properties/holderName", holder_name_to_integer),
    Scenario("controllo: header opzionale motivato da regola", "case-005-regression-guard.yaml",
             "/paths/~1cards/post", None, legit_optional_header),
]


@pytest.fixture
async def env(tmp_path, rules_dir):
    config = load_config(ROOT / "config.yaml").with_overrides(compiled_rules_cache_dir=str(tmp_path / "cache"))
    provider = OllamaProvider(config)
    try:
        await provider.preflight()
    except PreflightError as exc:
        pytest.skip(f"Ollama non disponibile: {exc}")
    rules_llm = StructuredLlm(FakeLlmProvider(interpreter_answer), 5, 0)
    registry, _ = await build_registry(rules_dir, RuleInterpreter(rules_llm, tmp_path / "cache"))
    llm = StructuredLlm(provider, config.llm_call_timeout_seconds, config.llm_technical_retries)
    return {"config": config, "registry": registry, "llm": llm,
            "critic": CriticEngine(llm, registry, config.llm_context_token_budget)}


def load(name: str) -> SpecDocument:
    return SpecDocument(source_file=name, source_version=SpecVersion.OPENAPI_3_0,
                        target_version=SpecVersion.OPENAPI_3_0, data=yaml.safe_load((APIS / name).read_text()))


def pertinent(issue, defect: str, fragment: str) -> bool:
    """Bloccante e sul difetto: confermata dal diff (l'unico change non tracciato è il difetto) o localizzata lì."""
    if not issue.blocking:
        return False
    if issue.verification == Verification.VERIFIED:
        return True
    locations = [issue.issue.location] + ([issue.issue.claim.location] if issue.issue.claim else [])
    return any(loc and loc != fragment and (jp.is_prefix(defect, loc) or jp.is_prefix(loc, defect))
               for loc in locations)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_real_critic_on_handcrafted_candidates(env, scenario: Scenario):
    baseline = load(scenario.spec)
    candidate = baseline.clone()
    applied = scenario.mutate(candidate)
    diff = compute_diff(baseline, candidate, applied)
    validation = OpenAPIValidator().validate(candidate)
    governance = GovernanceValidator(env["config"], env["registry"]).validate(candidate)
    [fragment] = [f for f in build_fragments(candidate) if f.pointer == scenario.fragment]

    started = time.monotonic()
    report = await env["critic"].review(iteration=1, baseline=baseline, candidate=candidate, fragments=[fragment],
                                        applied=applied, diff=diff, validation=validation,
                                        governance=governance + untraced_violations(diff, candidate.source_file))
    seconds = round(time.monotonic() - started, 1)

    relevant = [i for i in report.issues if scenario.defect and pertinent(i, scenario.defect, scenario.fragment)]
    ok = report.accepted if scenario.defect is None else bool(relevant)
    CRITIC_QUALITY_RESULTS.append({
        "case": scenario.name, "model": env["config"].critic_model, "criticThink": env["config"].critic_think,
        "ok": ok, "accepted": report.accepted, "seconds": seconds,
        "blocking": [f"{i.issue.type.value}@{i.issue.location} [{i.verification.value}]" for i in report.blocking],
        "pertinent": len(relevant), "failedFragments": report.failed_fragments,
    })
    assert not report.failed_fragments, "chiamata al Critic fallita"
    if scenario.defect is None:
        assert report.accepted, f"il Critic ha rifiutato un candidato corretto: {report.dump()['issues']}"
    else:
        assert relevant, f"nessuna issue bloccante pertinente su {scenario.defect}: {report.dump()['issues']}"

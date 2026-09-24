"""CASE 001-005: pipeline end-to-end con LlmProvider finto (deterministico), Spectral e swagger2openapi reali."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.diff.compat import ChangeType
from app.model import pointer as jp
from app.pipeline import RefactorPipeline, RunStatus
from app.refactor.engine import RefactoringEngine
from app.output import OutputWriter
from tests.conftest import APIS

pytestmark = pytest.mark.needs_node


async def run_case(config, agents, rules_dir, spec: str, tmp_path: Path):
    pipeline = RefactorPipeline(config, agents.provider(), rules_dir)
    result = await pipeline.run(APIS / spec)
    out = OutputWriter(tmp_path / "output").write(result)
    return result, out


def operations_of(data: dict) -> set[tuple[str, str]]:
    return {(p, m) for p, item in data["paths"].items() for m in item
            if m in ("get", "put", "post", "delete", "patch", "head", "options", "trace")}


# ── CASE 001 ─────────────────────────────────────────────────────────────
async def test_case_001_swagger2_upgrade(config, agents, rules_dir, tmp_path):
    source_text = (APIS / "case-001-swagger2-legacy.yaml").read_text()
    result, out = await run_case(config, agents, rules_dir, "case-001-swagger2-legacy.yaml", tmp_path)

    assert result.status == RunStatus.SUCCESS, result.reasons
    final = result.final.data
    assert final["openapi"].startswith("3.0")
    # nessun endpoint perso
    source = yaml.safe_load(source_text)
    assert operations_of(source) == operations_of(final)
    # schemi equivalenti (definitions -> components/schemas, $ref riscritti)
    legacy = json.loads(json.dumps(source["definitions"]).replace("#/definitions/", "#/components/schemas/"))
    assert {k: final["components"]["schemas"][k] for k in legacy} == legacy
    # validazione finale OK, report di conversione presente
    assert not [v for v in result.final_validation if v.severity == "ERROR"]
    report = json.loads((out / "reports" / "validation-report.json").read_text())
    assert report["conversion"]["converted"] and report["conversion"]["tool"] == "swagger2openapi"
    assert any(c.type == "UPDATE_OPENAPI_VERSION" for c in result.applied)
    # nessun change non tracciato nel diff finale; il file originale è intatto e copiato
    assert all(c.expected for c in result.final_diff)
    assert (APIS / "case-001-swagger2-legacy.yaml").read_text() == source_text
    assert (out / "original" / "case-001-swagger2-legacy.yaml").read_text() == source_text


# ── CASE 002 ─────────────────────────────────────────────────────────────
async def test_case_002_naming(config, agents, rules_dir, tmp_path):
    result, out = await run_case(config, agents, rules_dir, "case-002-naming.yaml", tmp_path)

    assert result.status == RunStatus.SUCCESS, result.reasons
    schemas = result.final.data["components"]["schemas"]
    assert "PaymentOrder" in schemas and "payment_order" not in schemas
    props = schemas["PaymentOrder"]["properties"]
    assert set(props) == {"id", "amountValue", "currency", "payerIban", "createdAt"}
    assert schemas["PaymentOrder"]["required"] == ["amountValue", "currency"]
    # i $ref seguono il rename
    body = result.final.data["paths"]["/payment-orders"]["post"]["requestBody"]["content"]["application/json"]
    assert body["schema"]["$ref"] == "#/components/schemas/PaymentOrder"
    # il rename delle proprietà cambia il wire: SEMANTIC, evidenziato, breaking ma tracciato a una regola
    semantic = [c for c in result.applied if c.category == "SEMANTIC"]
    assert {c.type for c in semantic} == {"RENAME_PROPERTY"}
    renamed = [c for c in result.final_diff if c.type == ChangeType.PROPERTY_REMOVED]
    assert renamed and all(c.breaking and c.expected and c.rule_id for c in renamed)
    assert any(c.type == ChangeType.SCHEMA_RENAMED and not c.breaking for c in result.final_diff)
    # due regole (Spectral DE-JSON-001 e NAMING-001) chiedono lo stesso rename: deduplicato, non un conflitto
    assert not [i for p in result.plans for i in p.issues if i.kind == "CONFLICT"]
    assert not [v for v in result.final_governance if v.severity == "ERROR"]


# ── CASE 003 ─────────────────────────────────────────────────────────────
async def test_case_003_problem_details(config, agents, rules_dir, tmp_path):
    result, out = await run_case(config, agents, rules_dir, "case-003-no-problem-details.yaml", tmp_path)

    assert result.status == RunStatus.SUCCESS, result.reasons
    data = result.final.data
    for ptr in ("/paths/~1accounts/get/responses/400", "/paths/~1accounts~1{account-id}/get/responses/404"):
        content = jp.resolve(data, ptr)["content"]
        assert content == {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}
        assert jp.resolve(data, ptr)["description"]  # la description originale è preservata
    problem = data["components"]["schemas"]["Problem"]
    assert {"type", "title", "status", "code"} <= set(problem["properties"])
    # Problem aggiunto una sola volta anche se proposto da due frammenti (dedup nel piano)
    assert sum(1 for c in result.applied if c.type == "ADD_COMPONENT") == 1
    assert all(c.category == "SEMANTIC" for c in result.applied if c.type == "CONVERT_ERROR_RESPONSE")
    assert not [v for v in result.final_governance
                if v.rule_id in ("DE-STATUS-002-problem-json-errors", "ERR-001")]
    removed_media = [c for c in result.final_diff if c.type == ChangeType.MEDIA_TYPE_REMOVED]
    assert removed_media and all(c.expected and c.breaking for c in removed_media)


# ── CASE 004 ─────────────────────────────────────────────────────────────
async def test_case_004_security(config, agents, rules_dir, tmp_path):
    result, out = await run_case(config, agents, rules_dir, "case-004-incomplete-security.yaml", tmp_path)

    # la baseline non è nemmeno valida (security verso uno schema non definito)
    first_plan_rules = {p.operation.rule_id for p in result.plans[0].operations}
    assert "OAS-SECURITY-UNDEFINED" in first_plan_rules or "SEC-001" in first_plan_rules
    assert result.status == RunStatus.SUCCESS, result.reasons
    data = result.final.data
    assert data["components"]["securitySchemes"]["bearerAuth"] == {
        "type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    for _, item in data["paths"].items():
        for op in item.values():
            assert op.get("security", data.get("security")) == [{"bearerAuth": []}]
    diff_types = {c.type for c in result.final_diff}
    assert ChangeType.SECURITY_SCHEME_ADDED in diff_types
    restriction = [c for c in result.final_diff if c.type == ChangeType.SECURITY_REQUIREMENT_ADDED]
    assert restriction and all(c.breaking and c.expected for c in restriction)
    assert not [v for v in result.final_validation if v.severity == "ERROR"]


# ── CASE 005 ─────────────────────────────────────────────────────────────
LOST = "/paths/~1cards~1{card-id}/get/responses/404"


def inject_regression(monkeypatch, persistent: bool) -> None:
    """Simula un refactoring difettoso: la response 404 sparisce senza alcuna operazione pianificata."""
    original_apply = RefactoringEngine.apply
    state = {"calls": 0}

    def buggy_apply(self, doc, planned, iteration=0):
        result, applied, failures = original_apply(self, doc, planned, iteration)
        state["calls"] += 1
        if (persistent or state["calls"] == 1) and result.exists(LOST):
            jp.remove(result.data, LOST)
        return result, applied, failures

    monkeypatch.setattr(RefactoringEngine, "apply", buggy_apply)


def critic_that_spots_regression(request):
    """Risposta predefinita del Critic: segnala la 404 persa quando il diff del frammento la mostra."""
    if any(c["type"] == "RESPONSE_REMOVED" and c["location"] == LOST for c in request.context["diff"]):
        return {"accepted": False, "issues": [{
            "type": "ELEMENT_LOST", "severity": "ERROR", "location": LOST,
            "message": "La response 404 di GET /cards/{card-id} è stata rimossa senza che alcuna regola lo richieda",
            "claim": {"kind": "REMOVED", "location": LOST}}]}
    return {"accepted": True, "issues": []}


async def test_case_005_critic_blocks_regression(config, agents, rules_dir, tmp_path, monkeypatch):
    inject_regression(monkeypatch, persistent=False)
    agents.critic = critic_that_spots_regression
    original_404 = yaml.safe_load((APIS / "case-005-regression-guard.yaml").read_text())[
        "paths"]["/cards/{card-id}"]["get"]["responses"]["404"]

    def correction(request):  # ripristina l'elemento perso con il valore originale mostrato dal Correction input
        ops = []
        for p in request.context["problems"]:
            if p["location"] == LOST or p["location"].startswith(LOST):
                ops.append({"type": "SET_FIELD", "target": LOST, "value": original_404, "ruleId": p["rule_id"]})
        return {"operations": ops[:1], "rationale": "restore lost response"}

    agents.correction = correction
    result, out = await run_case(config, agents, rules_dir, "case-005-regression-guard.yaml", tmp_path)

    first = result.iterations[0]
    # il diff deterministico vede la rimozione come NON tracciata e breaking
    assert any(v.path == LOST and v.severity == "ERROR" for v in first.untraced)
    # il claim del Critic è verificato dal semantic diff e blocca l'accettazione
    assert first.critic.accepted is False
    blocking = first.critic.blocking
    assert blocking and blocking[0].verification == "VERIFIED" and blocking[0].issue.location == LOST
    assert not first.exit_conditions.all_met
    # la correzione mirata ripristina la response: iterazione 2 accettata
    assert result.status == RunStatus.SUCCESS, result.reasons
    assert jp.resolve(result.final.data, LOST) == original_404
    critic_report = json.loads((out / "reports" / "critic-report.json").read_text())
    assert critic_report["iterations"][0]["accepted"] is False


async def test_case_005_persistent_regression_needs_review(config, agents, rules_dir, tmp_path, monkeypatch):
    inject_regression(monkeypatch, persistent=True)
    agents.critic = critic_that_spots_regression
    result, _ = await run_case(config, agents, rules_dir, "case-005-regression-guard.yaml", tmp_path)

    assert len(result.iterations) == config.max_iterations
    assert result.status == RunStatus.NEEDS_REVIEW  # mai dichiarare successo dopo maxIterations
    assert all(not it.exit_conditions.critic_accepted for it in result.iterations)
    # ogni candidato perde la 404 (1 ERROR non tracciato) quanto la baseline ne aveva uno (Idempotency-Key):
    # nessuno migliora la baseline, quindi l'output è l'originale, mai un candidato con la regressione
    assert result.output_is_baseline and result.final.data == result.baseline.data
    assert any("nessun candidato migliora la baseline" in r for r in result.reasons)


async def test_case_005_false_critic_claim_is_refuted(config, agents, rules_dir, tmp_path):
    """Un claim fattuale falso del Critic viene smentito dal diff e non blocca."""
    agents.critic = lambda r: {"accepted": False, "issues": [{
        "type": "ELEMENT_LOST", "severity": "ERROR", "location": "/paths/~1cards/post/responses/400",
        "message": "La response 400 è stata rimossa", "claim": {"kind": "REMOVED",
                                                                 "location": "/paths/~1cards/post/responses/400"}}]}
    result, _ = await run_case(config, agents, rules_dir, "case-005-regression-guard.yaml", tmp_path)

    issues = result.iterations[0].critic.issues
    assert issues and issues[0].verification == "REFUTED" and not issues[0].blocking
    assert result.status == RunStatus.SUCCESS, result.reasons

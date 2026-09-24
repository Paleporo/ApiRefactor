"""Agenti LLM finti e deterministici per i test: risposte predefinite, nessun Ollama richiesto.

- interpreter: bozze compilate predefinite per le regole di rules/*.md (le regole sconosciute diventano "judgment")
- refactor/correction: fixer scriptato che, dalle violazioni ricevute nel contesto, produce operazioni tipizzate
- critic: accetta sempre, salvo override per test specifici (es. CASE 005)
"""

from __future__ import annotations

import re
from typing import Any, Callable

from app.llm.base import AgentRole, LlmRequest
from app.llm.fake import FakeLlmProvider
from app.model import pointer as jp
from app.validators.casing import convert
from app.rules.models import Casing

PROBLEM_SCHEMA = {
    "type": "object",
    "required": ["type", "title", "status", "code"],
    "properties": {
        "type": {"type": "string", "format": "uri"},
        "title": {"type": "string"},
        "status": {"type": "integer", "format": "int32"},
        "detail": {"type": "string"},
        "instance": {"type": "string"},
        "code": {"type": "string"},
    },
}
BEARER = {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}

RULE_DRAFTS: dict[str, dict[str, Any]] = {
    "HTTP-IDEMPOTENCY-001": {"scope": "operation", "condition": {"methods": ["post"]},
                             "requirements": [{"kind": "requireHeader", "header": "Idempotency-Key", "required": True}],
                             "severity": "ERROR"},
    "ERR-001": {"scope": "response", "condition": {"statusPattern": "^[45]\\d\\d$"},
                "requirements": [{"kind": "errorFormat", "mediaType": "application/problem+json",
                                  "requiredProperties": ["type", "title", "status", "code"]}], "severity": "ERROR"},
    "OPID-001": {"scope": "operation", "requirements": [{"kind": "requireOperationId", "unique": True}],
                 "severity": "ERROR"},
    "NAMING-001": {"scope": "schema", "requirements": [
        {"kind": "nameCasing", "target": "schemaName", "casing": "pascal"},
        {"kind": "nameCasing", "target": "propertyName", "casing": "camel"}], "severity": "ERROR"},
    "SEC-001": {"scope": "operation", "requirements": [
        {"kind": "requireSecurity", "schemeType": "http", "scheme": "bearer", "bearerFormat": "JWT"}],
        "severity": "ERROR"},
}


def judgment(text: str, severity: str = "WARNING") -> dict[str, Any]:
    return {"scope": "any", "requirements": [{"kind": "judgment", "guidance": text}], "severity": severity}


def interpreter_answer(request: LlmRequest) -> dict[str, Any]:
    rule_id = request.context["ruleId"]
    return RULE_DRAFTS.get(rule_id) or judgment(request.context["text"])


def _response_pointer(path: str) -> str:
    tokens = jp.split(path)
    idx = tokens.index("responses")
    return jp.join(tokens[: idx + 2])


def scripted_fixes(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Operazioni tipizzate per le violazioni note (stesso formato di output richiesto all'LLM)."""
    ops: list[dict[str, Any]] = []
    for p in problems:
        rid = p.get("ruleId") or p.get("rule_id")
        path = p.get("path") if "path" in p else p.get("location", "")
        tokens = jp.split(path)
        if rid == "HTTP-IDEMPOTENCY-001":
            ops.append({"type": "ADD_HEADER", "target": path, "header": "Idempotency-Key", "required": True,
                        "ruleId": rid})
        elif rid == "SEC-001":
            ops += [{"type": "ADD_SECURITY_SCHEME", "name": "bearerAuth", "scheme": BEARER, "ruleId": rid},
                    {"type": "SET_SECURITY_REQUIREMENT", "target": path, "requirements": [{"bearerAuth": []}],
                     "ruleId": rid}]
        elif rid == "OAS-SECURITY-UNDEFINED":
            name = str(p.get("expected") or (p.get("details") or {}).get("expected") or "").rsplit("/", 1)[-1]
            ops.append({"type": "ADD_SECURITY_SCHEME", "name": name or "bearerAuth", "scheme": BEARER, "ruleId": rid})
        elif rid in ("ERR-001", "DE-STATUS-002-problem-json-errors"):
            ops += [{"type": "ADD_COMPONENT", "componentType": "schemas", "name": "Problem",
                     "definition": PROBLEM_SCHEMA, "ruleId": rid},
                    {"type": "CONVERT_ERROR_RESPONSE", "target": _response_pointer(path),
                     "schemaRef": "#/components/schemas/Problem", "ruleId": rid}]
        elif rid in ("NAMING-001", "DE-JSON-001-camelcase-properties"):
            if len(tokens) == 3 and tokens[:2] == ["components", "schemas"]:
                ops.append({"type": "RENAME_SCHEMA", "from": tokens[2], "to": convert(tokens[2], Casing.PASCAL),
                            "ruleId": rid})
            elif len(tokens) >= 2 and tokens[-2] == "properties":
                ops.append({"type": "RENAME_PROPERTY", "target": jp.join(tokens[:-2]), "from": tokens[-1],
                            "to": convert(tokens[-1], Casing.CAMEL), "ruleId": rid})
        elif str(p.get("suggestedFix") or (p.get("details") or {}).get("suggestedFix") or "").startswith(
                "ADD_QUERY_PARAMETER"):
            expected = p.get("expected") or (p.get("details") or {}).get("expected")
            ops.append({"type": "ADD_QUERY_PARAMETER", "target": jp.join(tokens[:3]), "name": expected["name"],
                        "required": expected["required"], "schema": {"type": "string"}, "ruleId": rid})
        elif rid == "OPID-001":
            op_ptr = jp.join(tokens[:3])
            ops.append({"type": "ADD_OPERATION_ID", "target": op_ptr,
                        "operationId": re.sub(r"\W", "", tokens[2] + "_" + tokens[1]), "ruleId": rid})
    return ops


class FakeAgents:
    """Router delle risposte per ruolo, con hook sovrascrivibili dai singoli test."""

    def __init__(self) -> None:
        self.critic: Callable[[LlmRequest], dict[str, Any]] = lambda r: {"accepted": True, "issues": []}
        self.correction: Callable[[LlmRequest], dict[str, Any]] = lambda r: {
            "operations": scripted_fixes(r.context["problems"]), "rationale": "scripted correction"}
        self.refactor: Callable[[LlmRequest], dict[str, Any]] = lambda r: {
            "operations": scripted_fixes(r.context["violations"]), "rationale": "scripted refactor"}
        self.interpreter = interpreter_answer

    def __call__(self, request: LlmRequest) -> Any:
        if request.role == AgentRole.RULE_INTERPRETER:
            return self.interpreter(request)
        if request.role == AgentRole.REFACTOR:
            return self.refactor(request)
        if request.role == AgentRole.CORRECTION:
            return self.correction(request)
        return self.critic(request)

    def provider(self) -> FakeLlmProvider:
        return FakeLlmProvider(self)

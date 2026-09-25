"""Formato unificato delle violazioni: planner e correction engine non distinguono la fonte (Spectral / regola LLM / validator)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class ViolationSource(StrEnum):
    OPENAPI = "openapi-validator"
    CONVERSION = "conversion"
    SPECTRAL = "spectral"
    COMPILED_RULE = "compiled-rule"
    SEMANTIC_DIFF = "semantic-diff"
    STRUCTURAL = "structural"


class Violation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    rule_id: str = Field(alias="ruleId")
    severity: Severity
    file: str = ""
    path: str = ""  # JSON pointer nel documento candidato
    operation_id: str | None = Field(None, alias="operationId")
    message: str
    expected: Any = None
    actual: Any = None
    suggested_fix: str | None = Field(None, alias="suggestedFix")
    source: ViolationSource
    # uso interno: indice del requisito della regola compilata che ha prodotto la violazione (non nei report)
    requirement_index: int | None = Field(None, exclude=True)

    def dump(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


def count_by_severity(violations: list[Violation]) -> dict[str, int]:
    counts = {s.value: 0 for s in Severity}
    for v in violations:
        counts[v.severity.value] += 1
    return counts


def summary(violations: list[Violation]) -> str:
    c = count_by_severity(violations)
    return f"{c['ERROR']} ERROR, {c['WARNING']} WARNING, {c['INFO']} INFO"

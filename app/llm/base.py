"""Interfaccia LLM disaccoppiata dal provider: la business logic dipende solo da `LlmProvider`."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AgentRole(StrEnum):
    RULE_INTERPRETER = "rule-interpreter"
    REFACTOR = "refactor"
    CORRECTION = "correction"
    CRITIC = "critic"


class LlmRequest(BaseModel):
    role: AgentRole
    task: str
    system: str
    user: str
    # metadati strutturati (fragment pointer, issue, ...): NON inviati al modello, servono a log e provider finti
    context: dict[str, Any] = Field(default_factory=dict)


class LlmProvider(ABC):
    """Un provider esegue UN tentativo di chiamata e ritorna il testo grezzo (JSON atteso).

    Retry tecnici, timeout, validazione pydantic e logging sono responsabilità di `StructuredLlm`,
    così ogni provider (Ollama oggi, altri domani) eredita lo stesso comportamento.
    """

    @abstractmethod
    def model_for(self, role: AgentRole) -> str: ...

    @abstractmethod
    async def complete(self, request: LlmRequest, json_schema: dict[str, Any], timeout: float) -> str: ...

    @abstractmethod
    async def preflight(self) -> None:
        """Verifica raggiungibilità e presenza dei modelli configurati; solleva PreflightError."""

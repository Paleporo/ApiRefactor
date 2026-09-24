"""LlmProvider finto e deterministico: risposte predefinite per test riproducibili senza Ollama."""

from __future__ import annotations

import json
from typing import Any, Callable

from pydantic import BaseModel

from app.errors import LlmCallError
from app.llm.base import AgentRole, LlmProvider, LlmRequest

Handler = Callable[[LlmRequest], "BaseModel | dict | str | Exception"]


class FakeLlmProvider(LlmProvider):
    """`handler(request)` riceve la richiesta (incluso `context` strutturato) e ritorna la risposta.

    La risposta passa comunque dal parsing/validazione reale di StructuredLlm, come per Ollama.
    """

    def __init__(self, handler: Handler):
        self.handler = handler
        self.requests: list[LlmRequest] = []
        self.schemas: list[dict[str, Any]] = []  # JSON Schema ricevuti, come li riceverebbe Ollama

    def model_for(self, role: AgentRole) -> str:
        return f"fake-{role.value}"

    async def complete(self, request: LlmRequest, json_schema: dict[str, Any], timeout: float) -> str:
        self.requests.append(request)
        self.schemas.append(json_schema)
        result = self.handler(request)
        if isinstance(result, Exception):
            if isinstance(result, LlmCallError):
                raise result
            raise LlmCallError(str(result))
        if isinstance(result, BaseModel):
            return result.model_dump_json(by_alias=True)
        if isinstance(result, dict):
            return json.dumps(result)
        return str(result)

    async def preflight(self) -> None:
        return None

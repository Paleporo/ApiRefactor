"""Implementazione `LlmProvider` per Ollama locale (client ufficiale ollama-python, structured outputs via JSON schema)."""

from __future__ import annotations

from typing import Any

import httpx
import ollama

from app.config import AppConfig
from app.errors import LlmCallError, PreflightError
from app.llm.base import AgentRole, LlmProvider, LlmRequest


class OllamaProvider(LlmProvider):
    def __init__(self, config: AppConfig):
        self.config = config
        self.models = {
            AgentRole.RULE_INTERPRETER: config.refactor_model,
            AgentRole.REFACTOR: config.refactor_model,
            AgentRole.CORRECTION: config.refactor_model,
            AgentRole.CRITIC: config.critic_model,
        }
        self.client = ollama.AsyncClient(host=config.ollama_host, timeout=config.llm_call_timeout_seconds)
        self._thinking: dict[str, bool] = {}  # modello -> supporta il parametro `think` (capability "thinking")

    async def supports_thinking(self, model: str) -> bool:
        if model not in self._thinking:
            try:
                info = await self.client.show(model)
                self._thinking[model] = "thinking" in (info.capabilities or [])
            except Exception:  # noqa: BLE001 - sonda opzionale: qualunque errore = capacità ignota, mai bloccante
                self._thinking[model] = False  # capacità ignota: non si passa `think`
        return self._thinking[model]

    def model_for(self, role: AgentRole) -> str:
        return self.models[role]

    async def complete(self, request: LlmRequest, json_schema: dict[str, Any], timeout: float) -> str:
        model = self.model_for(request.role)
        extra: dict[str, Any] = {}
        if request.role == AgentRole.CRITIC and await self.supports_thinking(model):
            # esplicito anche quando è false: i modelli reasoning (es. deepseek-r1) ragionano di default
            extra["think"] = self.config.critic_think
        try:
            response = await self.client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
                format=json_schema,
                options={
                    "temperature": self.config.llm_temperature,
                    # il default di Ollama (2-4K) troncherebbe i frammenti: budget + margine per la risposta
                    "num_ctx": self.config.llm_context_token_budget + 4096,
                },
                **extra,
            )
        except (ollama.ResponseError, httpx.HTTPError, ConnectionError) as exc:
            raise LlmCallError(f"errore Ollama: {exc}") from exc
        return response.message.content or ""

    async def preflight(self) -> None:
        try:
            listing = await self.client.list()
        except (httpx.HTTPError, ConnectionError, ollama.ResponseError) as exc:
            raise PreflightError(
                f"Ollama non raggiungibile su {self.config.ollama_host} ({exc}). Avvialo con `ollama serve`."
            ) from None
        available = set()
        for m in listing.models:
            name = m.model or ""
            available.add(name)
            if name.endswith(":latest"):
                available.add(name.removesuffix(":latest"))
        missing = sorted({m for m in self.models.values() if m not in available})
        if missing:
            pulls = " && ".join(f"ollama pull {m}" for m in missing)
            raise PreflightError(f"Modelli Ollama non presenti localmente: {', '.join(missing)}. Esegui: {pulls}")
        await self.supports_thinking(self.config.critic_model)

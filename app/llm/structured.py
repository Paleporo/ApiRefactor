"""Chiamate LLM con output strutturato validato da pydantic + retry tecnici (distinti dai retry di qualità)."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.errors import LlmCallError
from app.llm.base import LlmProvider, LlmRequest
from app.logging_setup import get_logger
from app.runtime import Deadline

log = get_logger("llm")
T = TypeVar("T", bound=BaseModel)

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> str:
    """Rimuove blocchi <think> (modelli reasoning) e fence markdown; isola il primo oggetto JSON."""
    text = _THINK.sub("", text).strip()
    text = _FENCE.sub("", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


class StructuredLlm:
    def __init__(self, provider: LlmProvider, call_timeout: float, technical_retries: int, deadline: Deadline | None = None):
        self.provider = provider
        self.call_timeout = call_timeout
        self.technical_retries = technical_retries
        self.deadline = deadline
        self.calls = 0

    async def generate(self, request: LlmRequest, response_model: type[T]) -> T:
        schema = response_model.model_json_schema(by_alias=True)
        attempts = self.technical_retries + 1
        last_error = ""
        current = request
        for attempt in range(1, attempts + 1):
            timeout = self.call_timeout
            if self.deadline is not None:
                self.deadline.check(f"LLM {request.task}")
                timeout = max(1.0, min(timeout, self.deadline.remaining))
            model = self.provider.model_for(request.role)
            log.debug(
                "[LLM] → %s/%s (model=%s, tentativo %d/%d, ~%d token)\n--- system ---\n%s\n--- user ---\n%s",
                request.role.value, request.task, model, attempt, attempts,
                (len(current.system) + len(current.user)) // 4, current.system, current.user,
            )
            self.calls += 1
            try:
                raw = await asyncio.wait_for(self.provider.complete(current, schema, timeout), timeout=timeout)
            except asyncio.TimeoutError:
                last_error = f"timeout dopo {timeout:.0f}s"
                log.warning("[LLM] %s: %s (tentativo %d/%d)", request.task, last_error, attempt, attempts)
                continue
            except LlmCallError as exc:
                last_error = str(exc)
                log.warning("[LLM] %s: %s (tentativo %d/%d)", request.task, last_error, attempt, attempts)
                continue
            log.debug("[LLM] ← %s/%s risposta:\n%s", request.role.value, request.task, raw)
            try:
                return response_model.model_validate_json(extract_json(raw))
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                last_error = f"output non conforme allo schema: {str(exc)[:800]}"
                log.warning("[LLM] %s: output non valido (tentativo %d/%d)", request.task, attempt, attempts)
                # il tentativo successivo riceve l'errore di validazione come feedback
                current = request.model_copy(
                    update={
                        "user": request.user
                        + "\n\nLa risposta precedente non rispettava lo schema JSON richiesto. Errore:\n"
                        + last_error
                        + "\nRispondi SOLO con un oggetto JSON valido secondo lo schema."
                    }
                )
        raise LlmCallError(f"Chiamata LLM '{request.task}' fallita dopo {attempts} tentativi: {last_error}")

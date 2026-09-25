"""Chiamate LLM con output strutturato validato da pydantic + retry tecnici (distinti dai retry di qualità)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from app.errors import LlmCallError
from app.llm.base import LlmProvider, LlmRequest
from app.logging_setup import get_logger
from app.runtime import Deadline

log = get_logger("llm")
T = TypeVar("T", bound=BaseModel)

# proprietà discriminatore delle union pydantic (Literal con default -> "const" non in required)
DISCRIMINATORS = ("kind", "type")

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> str:
    """Rimuove blocchi <think> (modelli reasoning) e fence markdown; isola il primo oggetto JSON."""
    text = _THINK.sub("", text).strip()
    text = _FENCE.sub("", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def llm_json_schema(response_model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema da inviare al modello, con i discriminatori obbligatori e in testa.

    Pydantic genera `kind`/`type` con un default, quindi fuori da `required`. La grammatica di Ollama emette
    prima i campi obbligatori: un modello che parte da "kind" potrebbe scegliere solo le varianti senza campi
    obbligatori (es. tutte le regole -> requireOperationId, tutte le operazioni -> ADD_OPERATION_ID).
    """
    schema = response_model.model_json_schema(by_alias=True)
    _require_discriminators(schema)
    return schema


def _require_discriminators(node: Any) -> None:
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            for name in DISCRIMINATORS:
                prop = props.get(name)
                if isinstance(prop, dict) and "const" in prop:
                    node["required"] = [name, *(r for r in node.get("required", []) if r != name)]
                    node["properties"] = {name: prop, **{k: v for k, v in props.items() if k != name}}
                    break
        for value in node.values():
            _require_discriminators(value)
    elif isinstance(node, list):
        for value in node:
            _require_discriminators(value)


class StructuredLlm:
    def __init__(self, provider: LlmProvider, call_timeout: float, technical_retries: int, deadline: Deadline | None = None,
                 journal: Any = None):
        self.provider = provider
        self.journal = journal  # app.checkpoint.Checkpoint: risposte già ottenute in una run interrotta
        self.call_timeout = call_timeout
        self.technical_retries = technical_retries
        self.deadline = deadline
        self.calls = 0
        # per ruolo: chiamate (tentativi), secondi totali, tentativi falliti
        self.stats: dict[str, dict[str, float]] = {}

    def _journal(self, key: str | None, raw: str, request: LlmRequest) -> None:
        if self.journal is not None and key is not None:
            self.journal.put(key, raw, {"role": request.role.value, "task": request.task,
                                        "fragment": request.context.get("fragment") or request.context.get("ruleId")})

    def _record(self, role: str, seconds: float, failed: bool, prompt_tokens: int = 0) -> None:
        s = self.stats.setdefault(role, {"calls": 0, "seconds": 0.0, "failures": 0, "promptTokens": 0})
        s["calls"] += 1
        s["promptTokens"] += prompt_tokens  # stima: caratteri / 4 (system + user)
        s["seconds"] = round(s["seconds"] + seconds, 3)
        s["failures"] += int(failed)

    async def generate(self, request: LlmRequest, response_model: type[T],
                       check: Callable[[T], None] | None = None) -> T:
        """`check` (opzionale) verifica vincoli deterministici oltre allo schema: se solleva ValueError il
        tentativo conta come non conforme e il messaggio torna al modello, come per gli errori di schema."""
        schema = llm_json_schema(response_model)
        journal_key = None
        if self.journal is not None:
            journal_key = self.journal.key({"role": request.role.value, "task": request.task, "system": request.system,
                                            "user": request.user, "model": self.provider.model_for(request.role),
                                            "schema": response_model.__name__})
            cached = self.journal.get(journal_key)
            if cached is not None:
                try:
                    result = response_model.model_validate_json(extract_json(cached))
                    if check is not None:
                        check(result)
                    self.journal.replayed += 1
                    log.debug("[CHECKPOINT] %s/%s servita dal checkpoint", request.role.value, request.task)
                    return result
                except (ValidationError, json.JSONDecodeError, ValueError):
                    pass  # voce non più valida: si richiama il modello
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
                "[LLM] -> %s/%s (model=%s, tentativo %d/%d, ~%d token)\n--- system ---\n%s\n--- user ---\n%s",
                request.role.value, request.task, model, attempt, attempts,
                (len(current.system) + len(current.user)) // 4, current.system, current.user,
            )
            self.calls += 1
            started = time.monotonic()
            try:
                raw = await asyncio.wait_for(self.provider.complete(current, schema, timeout), timeout=timeout)
            except asyncio.TimeoutError:
                self._record(request.role.value, time.monotonic() - started, failed=True)
                last_error = f"timeout dopo {timeout:.0f}s"
                log.warning("[LLM] %s: %s (tentativo %d/%d)", request.task, last_error, attempt, attempts)
                continue
            except LlmCallError as exc:
                self._record(request.role.value, time.monotonic() - started, failed=True)
                last_error = str(exc)
                log.warning("[LLM] %s: %s (tentativo %d/%d)", request.task, last_error, attempt, attempts)
                continue
            elapsed = time.monotonic() - started
            self._record(request.role.value, elapsed, failed=False,
                         prompt_tokens=(len(current.system) + len(current.user)) // 4)
            log.info("[LLM] %s/%s %s: %.1fs", request.role.value, request.task,
                     request.context.get("fragment") or request.context.get("ruleId") or "", elapsed)
            log.debug("[LLM] <- %s/%s risposta:\n%s", request.role.value, request.task, raw)
            try:
                result = response_model.model_validate_json(extract_json(raw))
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
                continue
            if check is None:
                self._journal(journal_key, raw, request)
                return result
            try:
                check(result)
                self._journal(journal_key, raw, request)
                return result
            except ValueError as exc:
                last_error = f"output non conforme ai vincoli: {exc}"
                log.warning("[LLM] %s: %s (tentativo %d/%d)", request.task, last_error, attempt, attempts)
                current = request.model_copy(
                    update={
                        "user": request.user
                        + "\n\nLa risposta precedente era JSON valido ma violava un vincolo obbligatorio:\n"
                        + str(exc)
                        + "\nCorreggi la risposta rispettando il vincolo. Rispondi SOLO con un oggetto JSON valido."
                    }
                )
        raise LlmCallError(f"Chiamata LLM '{request.task}' fallita dopo {attempts} tentativi: {last_error}")

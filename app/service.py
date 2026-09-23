"""Servizio applicativo: unico punto d'ingresso per CLI ed eventuali endpoint REST (nessuna logica nel trasporto)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.config import AppConfig
from app.llm.base import LlmProvider
from app.llm.ollama_provider import OllamaProvider
from app.output import OutputWriter
from app.pipeline import RefactorPipeline, RunResult


class RefactorOutcome(BaseModel):
    status: str
    reasons: list[str]
    output_dir: str
    iterations: int


async def refactor_file(config: AppConfig, input_path: str | Path, output_dir: str | Path | None = None,
                        rules_dir: str | Path | None = None,
                        provider: LlmProvider | None = None) -> tuple[RefactorOutcome, RunResult]:
    provider = provider or OllamaProvider(config)
    pipeline = RefactorPipeline(config, provider, rules_dir or config.rules_dir)
    result = await pipeline.run(input_path)
    out = OutputWriter(output_dir or config.output_dir).write(result)
    outcome = RefactorOutcome(status=result.status.value, reasons=result.reasons, output_dir=str(out),
                              iterations=len(result.iterations))
    return outcome, result

"""Configurazione unica della pipeline: config.yaml è la fonte dei default, i flag CLI la sovrascrivono per la singola run."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.errors import ConfigurationError

DEFAULT_CONFIG_FILE = "config.yaml"


class AppConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    target_openapi_version: Literal["3.0", "3.1"] = Field("3.0", alias="targetOpenApiVersion")
    max_iterations: int = Field(3, alias="maxIterations", ge=1)
    refactor_model: str = Field("qwen3-coder:30b", alias="refactorModel")
    critic_model: str = Field("deepseek-r1:14b", alias="criticModel")
    critic_think: bool = Field(False, alias="criticThink")
    ollama_host: str = Field("http://localhost:11434", alias="ollamaHost")
    llm_call_timeout_seconds: float = Field(600, alias="llmCallTimeoutSeconds", gt=0)
    llm_technical_retries: int = Field(2, alias="llmTechnicalRetries", ge=0)
    run_timeout_seconds: float = Field(7200, alias="runTimeoutSeconds", gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field("INFO", alias="logLevel")
    llm_temperature: float = Field(0.0, alias="llmTemperature", ge=0)
    llm_context_token_budget: int = Field(12000, alias="llmContextTokenBudget", gt=1000)

    rules_dir: str = Field("rules", alias="rulesDir")
    output_dir: str = Field("output", alias="outputDir")
    compiled_rules_cache_dir: str = Field(".cache/compiled-rules", alias="compiledRulesCacheDir")
    spectral_command: str = Field("spectral", alias="spectralCommand")
    swagger2openapi_command: str = Field("swagger2openapi", alias="swagger2openapiCommand")
    external_tool_timeout_seconds: float = Field(120, alias="externalToolTimeoutSeconds", gt=0)

    def with_overrides(self, **overrides: Any) -> "AppConfig":
        """Ritorna una copia con i valori non-None sovrascritti (usato dai flag CLI)."""
        data = self.model_dump()
        data.update({k: v for k, v in overrides.items() if v is not None})
        try:
            return AppConfig.model_validate(data)
        except ValidationError as exc:
            raise ConfigurationError(f"Override di configurazione non valido: {exc}") from exc


def load_config(path: str | Path | None = None) -> AppConfig:
    """Carica config.yaml. Se il path non è indicato e il file di default non esiste, usa i default del modello."""
    explicit = path is not None
    cfg_path = Path(path) if explicit else Path(DEFAULT_CONFIG_FILE)
    if not cfg_path.exists():
        if explicit:
            raise ConfigurationError(f"File di configurazione non trovato: {cfg_path}")
        return AppConfig()
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"config.yaml non è YAML valido ({cfg_path}): {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"config.yaml deve contenere una mappa chiave/valore ({cfg_path})")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"config.yaml non valido ({cfg_path}):\n{exc}") from exc

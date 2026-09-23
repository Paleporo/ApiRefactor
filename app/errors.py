"""Errori di dominio: ogni fallimento previsto ha un messaggio leggibile, mai uno stack trace grezzo."""


class PipelineError(Exception):
    """Base per tutti gli errori attesi della pipeline (la CLI li stampa senza traceback)."""

    exit_code = 3


class ConfigurationError(PipelineError):
    """Configurazione non valida (config.yaml, regole, ruleId in collisione...)."""


class SpecParseError(PipelineError):
    """File di specifica non leggibile o sintatticamente rotto."""

    def __init__(self, file: str, message: str, line: int | None = None, column: int | None = None):
        self.file = file
        self.line = line
        self.column = column
        where = f"{file}"
        if line is not None:
            where += f", riga {line}" + (f", colonna {column}" if column is not None else "")
        super().__init__(f"Impossibile parsare la specifica ({where}): {message}")


class EmptySpecError(PipelineError):
    """Specifica senza path: nulla da rifattorizzare (uscita pulita, non un errore della pipeline)."""

    exit_code = 0


class UnsupportedSpecError(PipelineError):
    """Versione sorgente/target non supportata."""


class ExternalToolError(PipelineError):
    """Un processo esterno (Spectral, swagger2openapi) manca o ha prodotto output inutilizzabile."""


class PreflightError(PipelineError):
    """Ollama non raggiungibile o modelli configurati assenti."""


class LlmCallError(PipelineError):
    """Una chiamata LLM è fallita anche dopo i retry tecnici."""


class RunTimeoutError(PipelineError):
    """Budget di tempo complessivo della run esaurito."""

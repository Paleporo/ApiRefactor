"""Esposizione REST opzionale (anteprima per una futura Web UI): stessa logica del CLI via app.service.

Avvio: uv run --extra server uvicorn app.api:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.config import load_config
from app.errors import EmptySpecError, PipelineError
from app.service import RefactorOutcome, refactor_file

app = FastAPI(title="AI OpenAPI Refactoring Engine", version="0.1.0")


class RefactorRequest(BaseModel):
    input: str
    output: str | None = None
    rules: str | None = None
    max_iterations: int | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/refactor", response_model=RefactorOutcome)
async def refactor(request: RefactorRequest) -> RefactorOutcome:
    config = load_config().with_overrides(max_iterations=request.max_iterations)
    try:
        outcome, _ = await refactor_file(config, request.input, request.output, request.rules)
    except EmptySpecError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return outcome

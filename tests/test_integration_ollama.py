"""Integrazione end-to-end con il vero Ollama: eseguire con `uv run pytest -m requires_ollama`.

Richiede `ollama serve` attivo e i modelli di config.yaml scaricati; altrimenti i test vengono saltati.
Sono lenti su CPU (minuti): per questo sono esclusi dalla suite di default.
"""

import pytest

from app.config import load_config
from app.errors import PreflightError
from app.llm.ollama_provider import OllamaProvider
from app.pipeline import RefactorPipeline, RunStatus
from tests.conftest import APIS, ROOT

pytestmark = [pytest.mark.requires_ollama, pytest.mark.needs_node]


@pytest.fixture
async def ollama_config(tmp_path):
    config = load_config(ROOT / "config.yaml").with_overrides(
        compiled_rules_cache_dir=str(tmp_path / "cache"), max_iterations=2)
    try:
        await OllamaProvider(config).preflight()
    except PreflightError as exc:
        pytest.skip(f"Ollama non disponibile: {exc}")
    return config


async def test_ollama_compiles_rules_and_refactors_case_003(ollama_config, rules_dir):
    result = await RefactorPipeline(ollama_config, OllamaProvider(ollama_config), rules_dir).run(
        APIS / "case-003-no-problem-details.yaml")
    # con un LLM reale non si pretende SUCCESS, ma la pipeline deve concludersi con uno stato esplicito,
    # un documento valido e nessun change non tracciato
    assert result.status in (RunStatus.SUCCESS, RunStatus.NEEDS_REVIEW)
    assert not [v for v in result.final_validation if v.severity == "ERROR"]
    assert all(c.expected for c in result.final_diff if c.breaking)
    assert all(r.requirements for r in result.registry.compiled)


async def test_ollama_upgrades_swagger2(ollama_config, rules_dir):
    result = await RefactorPipeline(ollama_config, OllamaProvider(ollama_config), rules_dir).run(
        APIS / "case-001-swagger2-legacy.yaml")
    assert result.final.data["openapi"].startswith("3.0")
    assert result.status != RunStatus.FAILED

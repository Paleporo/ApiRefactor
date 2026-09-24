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


async def test_ollama_compiles_security_and_naming_rules_into_the_right_requirements(ollama_config, rules_dir,
                                                                                    tmp_path):
    """Regressione: senza discriminatori obbligatori ogni regola collassava su requireOperationId."""
    from app.llm.structured import StructuredLlm
    from app.rules.interpreter import RuleInterpreter
    from app.rules.loader import parse_markdown_rules

    llm = StructuredLlm(OllamaProvider(ollama_config), ollama_config.llm_call_timeout_seconds,
                        ollama_config.llm_technical_retries)
    interpreter = RuleInterpreter(llm, tmp_path / "cache")
    sources = {r.id: r for f in ("security.md", "general.md") for r in parse_markdown_rules(rules_dir / f)}

    sec = await interpreter.compile_rule(sources["SEC-001"])
    assert [r.kind for r in sec.requirements] == ["requireSecurity"]
    assert sec.requirements[0].scheme_type == "http"

    naming = await interpreter.compile_rule(sources["NAMING-001"])
    assert [r.kind for r in naming.requirements] == ["nameCasing", "nameCasing"]
    assert {(r.target.value, r.casing.value) for r in naming.requirements} == {
        ("schemaName", "pascal"), ("propertyName", "camel")}


async def test_ollama_compiles_query_parameter_rule_and_keeps_inexpressible_ones_as_judgment(
        ollama_config, rules_dir, tmp_path):
    from app.llm.structured import StructuredLlm
    from app.rules.interpreter import RuleInterpreter
    from app.rules.loader import parse_markdown_rules

    (rules_dir / "query.md").write_text(
        "- [QUERY-LANG-001] Le operation GET devono accettare il query parameter opzionale `lang`.\n")
    llm = StructuredLlm(OllamaProvider(ollama_config), ollama_config.llm_call_timeout_seconds,
                        ollama_config.llm_technical_retries)
    interpreter = RuleInterpreter(llm, tmp_path / "cache")

    lang = await interpreter.compile_rule(parse_markdown_rules(rules_dir / "query.md")[0])
    assert [r.kind for r in lang.requirements] == ["requireQueryParameter"]
    assert (lang.requirements[0].name, lang.requirements[0].required) == ("lang", False)
    assert [m.lower() for m in lang.condition.methods or []] == ["get"]

    # "che restituiscono collezioni" non è esprimibile con condition: deve restare judgment, non il requisito più simile
    pagination = await interpreter.compile_rule(parse_markdown_rules(rules_dir / "pagination.md")[0])
    assert [r.kind for r in pagination.requirements] == ["judgment"]

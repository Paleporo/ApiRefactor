import pytest

from app.errors import ConfigurationError
from app.llm.base import AgentRole
from app.llm.fake import FakeLlmProvider
from app.llm.structured import StructuredLlm
from app.rules.interpreter import RuleInterpreter
from app.rules.loader import parse_markdown_rules
from app.rules.models import RuleScope
from app.rules.registry import build_registry
from app.model.document import ElementKind
from tests.fake_agents import interpreter_answer


def interpreter(tmp_path, calls: list):
    def handler(request):
        calls.append(request.context["ruleId"])
        return interpreter_answer(request)
    llm = StructuredLlm(FakeLlmProvider(handler), call_timeout=5, technical_retries=0)
    return RuleInterpreter(llm, tmp_path / "cache")


def test_markdown_rule_parsing_ids_and_continuations(tmp_path):
    md = tmp_path / "headers.md"
    md.write_text("# Headers\n\nIntro text.\n\n- [HDR-001] Primo\n  continua qui.\n- Secondo senza id\n"
                  "* **HDR-XYZ**: Terzo\n")
    rules = parse_markdown_rules(md)
    assert [(r.id, r.text) for r in rules] == [("HDR-001", "Primo continua qui."), ("HEADERS-002", "Secondo senza id"),
                                               ("HDR-XYZ", "Terzo")]
    assert rules[0].section == "Headers" and rules[0].line == 5


async def test_compiled_rules_are_cached_and_invalidated_on_change(tmp_path, rules_dir):
    calls: list = []
    await build_registry(rules_dir, interpreter(tmp_path, calls))
    first = len(calls)
    assert first == 8
    await build_registry(rules_dir, interpreter(tmp_path, calls))
    assert len(calls) == first  # nessuna ricompilazione: cache valida
    general = rules_dir / "general.md"
    general.write_text(general.read_text() + "- [NEW-001] Nuova regola.\n")
    registry, report = await build_registry(rules_dir, interpreter(tmp_path, calls))
    assert len(calls) == first + 6  # solo general.md (5+1 regole) ricompilato
    assert registry.get("NEW-001") is not None
    assert any("general.md" in f for f in report.compiled_files)


async def test_rule_id_collision_between_sources_is_a_configuration_error(tmp_path, rules_dir):
    (rules_dir / "extra.md").write_text("- [DE-INFO-001-required-fields] Regola che collide con Spectral\n")
    with pytest.raises(ConfigurationError, match="Collisione di ruleId"):
        await build_registry(rules_dir, interpreter(tmp_path, []))


async def test_rule_id_collision_between_markdown_files(tmp_path, rules_dir):
    (rules_dir / "extra.md").write_text("- [SEC-001] Duplicato\n")
    with pytest.raises(ConfigurationError, match="SEC-001"):
        await build_registry(rules_dir, interpreter(tmp_path, []))


async def test_no_rules_gives_explicit_warning(tmp_path):
    empty = tmp_path / "norules"
    empty.mkdir()
    registry, _ = await build_registry(empty, interpreter(tmp_path, []))
    assert registry.is_empty
    assert any("NESSUNA REGOLA" in w for w in registry.info.warnings)


async def test_spectral_wins_conflict_with_llm_rule_and_it_is_reported(tmp_path, rules_dir):
    (rules_dir / "props.md").write_text("- [PROPS-001] Le properties devono essere snake_case.\n")

    def handler(request):
        if request.context["ruleId"] == "PROPS-001":
            return {"scope": "property", "requirements": [{"kind": "nameCasing", "target": "propertyName",
                                                           "casing": "snake"}], "severity": "ERROR"}
        return interpreter_answer(request)
    llm = StructuredLlm(FakeLlmProvider(handler), call_timeout=5, technical_retries=0)
    registry, _ = await build_registry(rules_dir, RuleInterpreter(llm, tmp_path / "c"))
    conflict = next(c for c in registry.conflicts if c.loser == "PROPS-001")
    assert conflict.winner == "DE-JSON-001-camelcase-properties"
    assert registry.get("PROPS-001").overridden_by == ["DE-JSON-001-camelcase-properties"]


async def test_scope_query_returns_only_applicable_rules(tmp_path, rules_dir):
    registry, _ = await build_registry(rules_dir, interpreter(tmp_path, []))
    post_rules = {r.id for r in registry.for_fragment(ElementKind.OPERATION, method="post", path="/a")}
    get_rules = {r.id for r in registry.for_fragment(ElementKind.OPERATION, method="get", path="/a")}
    assert "HTTP-IDEMPOTENCY-001" in post_rules and "HTTP-IDEMPOTENCY-001" not in get_rules
    assert "DE-HTTP-001-get-no-body" in get_rules and "DE-HTTP-001-get-no-body" not in post_rules
    schema_rules = {r.id for r in registry.for_fragment(ElementKind.SCHEMA)}
    assert "DE-JSON-001-camelcase-properties" in schema_rules and "DE-INFO-001-required-fields" not in schema_rules
    assert {r.id for r in registry.applicable(RuleScope.DOCUMENT)} >= {"DE-INFO-001-required-fields"}
    assert len(post_rules) < len(registry.all)


async def test_compile_failure_keeps_rule_as_judgment_and_is_not_cached(tmp_path, rules_dir):
    def handler(request):
        if request.role == AgentRole.RULE_INTERPRETER and request.context["ruleId"] == "SEC-001":
            return "this is not json"
        return interpreter_answer(request)
    llm = StructuredLlm(FakeLlmProvider(handler), call_timeout=5, technical_retries=1)
    registry, report = await build_registry(rules_dir, RuleInterpreter(llm, tmp_path / "c"))
    rule = registry.get("SEC-001")
    assert rule.compile_failed and rule.judgment_only
    assert report.failed_rules == ["SEC-001"]
    assert not any("security" in p.name for p in (tmp_path / "c").glob("*.json"))


async def test_cache_from_older_dsl_version_is_recompiled(tmp_path, rules_dir):
    import json

    calls: list = []
    await build_registry(rules_dir, interpreter(tmp_path, calls))
    for cache in (tmp_path / "cache").glob("*.json"):
        payload = json.loads(cache.read_text())
        payload["dslVersion"] = "1"
        cache.write_text(json.dumps(payload))
    before = len(calls)
    await build_registry(rules_dir, interpreter(tmp_path, calls))
    assert len(calls) == before * 2  # DSL cambiato: tutte le regole ricompilate, nessuna letta dalla cache vecchia


def test_markdown_lazy_continuation_and_paragraph_boundaries(tmp_path):
    md = tmp_path / "paging.md"
    md.write_text(
        "# Paging\n\n"
        "- [P-001] Prima riga\n"
        "continuazione non indentata subito dopo la voce.\n"        # lazy continuation (CommonMark)
        "  continuazione indentata.\n"
        "\n"
        "  continuazione indentata dopo una riga vuota.\n"          # resta nella voce
        "- [P-002] Seconda regola.\n"
        "\n"
        "Paragrafo di contesto dopo una riga vuota: non è una regola.\n"
        "  Né questa riga, che segue il paragrafo.\n"
        "- [P-003] Terza regola.\n"
        "## Altra sezione\n"
        "testo sotto il titolo, senza voce: ignorato.\n")
    rules = {r.id: r for r in parse_markdown_rules(md)}
    assert rules["P-001"].text == ("Prima riga continuazione non indentata subito dopo la voce. "
                                   "continuazione indentata. continuazione indentata dopo una riga vuota.")
    assert rules["P-002"].text == "Seconda regola."
    assert rules["P-003"].text == "Terza regola."
    assert list(rules) == ["P-001", "P-002", "P-003"]


def test_project_rule_files_parse_to_complete_non_duplicated_texts():
    import re

    from tests.conftest import ROOT

    rules = {r.id: r for f in sorted((ROOT / "rules").glob("*.md")) for r in parse_markdown_rules(f)}
    for rule in rules.values():
        sentences = [s.strip() for s in re.split(r"[.;:]\s+", rule.text) if s.strip()]
        assert len(sentences) == len(set(sentences)), f"{rule.id}: testo duplicato: {rule.text}"
    for rid in ("PAGINATION-001", "NAMING-DOMAIN-001"):
        assert rules[rid].text.count("Regola di giudizio") == 1, rules[rid].text
    assert rules["PAGINATION-001"].text.endswith("non riguarda gli status code.")

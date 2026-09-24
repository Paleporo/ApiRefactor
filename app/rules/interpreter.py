"""RuleInterpreter: compila le regole in linguaggio naturale nel DSL strutturato, con cache per file invalidata via hash."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from app.errors import LlmCallError
from app.llm.base import AgentRole, LlmRequest
from app.llm.structured import StructuredLlm
from app.logging_setup import get_logger
from app.prompts import load_prompt
from app.rules.loader import parse_markdown_rules
from app.rules.models import (DSL_VERSION, CompiledRule, CompiledRuleDraft, JudgmentRequirement, RuleCondition,
                              RuleScope, RuleSource)

log = get_logger("rules")


HTTP_METHOD_WORDS = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\b", re.IGNORECASE)


def methods_named_in(text: str) -> list[str]:
    """Metodi HTTP nominati nel testo della regola (anche minuscoli), in minuscolo e senza ripetizioni."""
    return list(dict.fromkeys(m.lower() for m in HTTP_METHOD_WORDS.findall(text)))


def check_method_scope(text: str, condition: RuleCondition, requirements: list) -> None:
    """Una regola meccanica non deve mai essere più ampia del testo sui metodi HTTP.

    Se il testo nomina dei metodi, `condition.methods` deve contenere esattamente quei metodi: null o un
    sottoinsieme la applicherebbero a metodi non previsti (es. Idempotency-Key obbligatorio sulle GET),
    un metodo in più la allargherebbe. Le regole solo-giudizio non vengono applicate meccanicamente: non si
    controllano. Solleva ValueError con un messaggio destinato al modello.
    """
    named = methods_named_in(text)
    if not named or all(getattr(r, "kind", None) == "judgment" for r in requirements):
        return
    declared = [m.lower() for m in condition.methods] if condition.methods else None
    if declared is None:
        raise ValueError(f"the rule text names the HTTP method(s) {named} but condition.methods is null: set "
                         f"condition.methods to {named} (otherwise the rule would apply to every method)")
    if set(declared) != set(named):
        raise ValueError(f"the rule text names the HTTP method(s) {named} but condition.methods is {declared}: "
                         f"it must be exactly {named}. If the text restricts methods in a way this cannot express "
                         "(e.g. 'all methods except GET'), use a judgment requirement instead")


class CompileReport:
    def __init__(self) -> None:
        self.cache_hits: list[str] = []
        self.compiled_files: list[str] = []
        self.failed_rules: list[str] = []
        self.failure_reasons: dict[str, str] = {}


def _hash(file: Path) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()


class RuleInterpreter:
    def __init__(self, llm: StructuredLlm | None, cache_dir: Path):
        self.llm = llm
        self.cache_dir = cache_dir
        self.system = load_prompt("rule_interpreter")

    def _cache_file(self, source: Path) -> Path:
        # il nome include un hash del path assoluto: due rules/ diverse non condividono la cache
        tag = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:8]
        return self.cache_dir / f"{source.stem}.{tag}.json"

    def _read_cache(self, source: Path, digest: str, model: str) -> list[CompiledRule] | None:
        cache = self._cache_file(source)
        if not cache.exists():
            return None
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("sha256") != digest or payload.get("dslVersion") != DSL_VERSION or payload.get("model") != model:
            log.info("[RULES] %s modificato (hash/modello/DSL diverso): ricompilo", source.name)
            return None
        rules = [CompiledRule.model_validate(r) for r in payload.get("rules", [])]
        for rule in rules:  # cache scritta prima del controllo sui metodi: non si riusa una regola troppo ampia
            try:
                check_method_scope(rule.text, rule.condition, rule.requirements)
            except ValueError as exc:
                log.warning("[RULES] %s: la regola in cache %s non è conforme (%s): ricompilo", source.name, rule.id, exc)
                return None
        return rules

    def _write_cache(self, source: Path, digest: str, model: str, rules: list[CompiledRule]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "sourceFile": str(source),
            "sha256": digest,
            "model": model,
            "dslVersion": DSL_VERSION,
            "rules": [r.model_dump(by_alias=True, mode="json") for r in rules],
        }
        self._cache_file(source).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    async def compile_rule(self, source: RuleSource) -> CompiledRule:
        assert self.llm is not None
        request = LlmRequest(
            role=AgentRole.RULE_INTERPRETER,
            task="compile-rule",
            system=self.system,
            user=(
                f"Rule id: {source.id}\n"
                + (f"Section: {source.section}\n" if source.section else "")
                + f"Rule text: {source.text}\n"
            ),
            context={"ruleId": source.id, "text": source.text, "file": source.file},
        )
        draft = await self.llm.generate(
            request, CompiledRuleDraft, check=lambda d: check_method_scope(source.text, d.condition, d.requirements))
        condition = draft.condition
        if condition.methods:
            condition = condition.model_copy(update={"methods": list(dict.fromkeys(m.lower() for m in condition.methods))})
        return CompiledRule(
            id=source.id, text=source.text, file=source.file, line=source.line,
            scope=draft.scope, condition=condition, requirements=draft.requirements, severity=draft.severity,
        )

    async def compile_file(self, source: Path, report: CompileReport) -> list[CompiledRule]:
        digest = _hash(source)
        model = self.llm.provider.model_for(AgentRole.RULE_INTERPRETER) if self.llm else "none"
        cached = self._read_cache(source, digest, model)
        if cached is not None:
            report.cache_hits.append(str(source))
            log.info("[RULES] %s: %d regole dalla cache", source.name, len(cached))
            return cached
        sources = parse_markdown_rules(source)
        compiled: list[CompiledRule] = []
        any_failed = False
        for rule in sources:
            if self.llm is None:
                raise LlmCallError("Nessun LLM disponibile per compilare le regole in linguaggio naturale")
            try:
                compiled.append(await self.compile_rule(rule))
            except LlmCallError as exc:
                # la regola non viene persa: resta come regola di giudizio per il Critic, marcata come fallita
                any_failed = True
                report.failed_rules.append(rule.id)
                report.failure_reasons[rule.id] = str(exc)
                log.warning("[RULES] compilazione di %s fallita (%s): resta solo come regola di giudizio", rule.id, exc)
                compiled.append(
                    CompiledRule(
                        id=rule.id, text=rule.text, file=rule.file, line=rule.line, scope=RuleScope.ANY,
                        requirements=[JudgmentRequirement(guidance=rule.text)], compile_failed=True,
                    )
                )
        if not any_failed:  # le compilazioni fallite non vanno in cache: si ritenta alla prossima run
            self._write_cache(source, digest, model, compiled)
        report.compiled_files.append(str(source))
        log.info("[RULES] %s: %d regole compilate", source.name, len(compiled))
        return compiled

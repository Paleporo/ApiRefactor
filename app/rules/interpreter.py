"""RuleInterpreter: compila le regole in linguaggio naturale nel DSL strutturato, con cache per file invalidata via hash."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.errors import LlmCallError
from app.llm.base import AgentRole, LlmRequest
from app.llm.structured import StructuredLlm
from app.logging_setup import get_logger
from app.prompts import load_prompt
from app.rules.loader import parse_markdown_rules
from app.rules.models import DSL_VERSION, CompiledRule, CompiledRuleDraft, JudgmentRequirement, RuleScope, RuleSource

log = get_logger("rules")


class CompileReport:
    def __init__(self) -> None:
        self.cache_hits: list[str] = []
        self.compiled_files: list[str] = []
        self.failed_rules: list[str] = []


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
        return [CompiledRule.model_validate(r) for r in payload.get("rules", [])]

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
        draft = await self.llm.generate(request, CompiledRuleDraft)
        return CompiledRule(
            id=source.id, text=source.text, file=source.file, line=source.line,
            scope=draft.scope, condition=draft.condition, requirements=draft.requirements, severity=draft.severity,
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

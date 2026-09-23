"""RuleRegistry: corpus unificato delle regole (Spectral + linguaggio naturale compilato) con query per scope.

All'LLM non si passa mai l'intero corpus: `for_fragment` ritorna solo le regole applicabili a un frammento.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Union

from pydantic import BaseModel

from app.errors import ConfigurationError
from app.logging_setup import get_logger
from app.model.document import ElementKind
from app.rules.loader import load_spectral_rules, natural_language_rule_files, spectral_ruleset_files
from app.rules.models import Casing, CompiledRule, NameCasing, NameTarget, RuleConflict, RuleScope, SpectralRule

log = get_logger("rules")

AnyRule = Union[CompiledRule, SpectralRule]

# quali scope di regola sono pertinenti a ciascun tipo di frammento
_FRAGMENT_SCOPES: dict[ElementKind, set[RuleScope]] = {
    ElementKind.DOCUMENT: {RuleScope.DOCUMENT, RuleScope.ANY},
    ElementKind.PATH: {RuleScope.PATH, RuleScope.ANY},
    ElementKind.OPERATION: {
        RuleScope.OPERATION, RuleScope.PARAMETER, RuleScope.RESPONSE, RuleScope.PROPERTY, RuleScope.ANY,
    },
    ElementKind.SCHEMA: {RuleScope.SCHEMA, RuleScope.PROPERTY, RuleScope.ANY},
}


class RuleSetInfo(BaseModel):
    natural_language_files: list[str] = []
    spectral_rulesets: list[str] = []
    warnings: list[str] = []


class RuleRegistry:
    def __init__(self, compiled: list[CompiledRule], spectral: list[SpectralRule], info: RuleSetInfo | None = None):
        self.compiled = compiled
        self.spectral = spectral
        self.info = info or RuleSetInfo()
        self._check_collisions()
        self.conflicts: list[RuleConflict] = detect_conflicts(compiled, spectral)

    # ── integrità ──────────────────────────────────────────────────────
    def _check_collisions(self) -> None:
        seen: dict[str, str] = {}
        collisions = []
        for rule in [*self.compiled, *self.spectral]:
            where = rule.file if isinstance(rule, CompiledRule) else rule.ruleset_file
            if rule.id in seen:
                collisions.append(f"'{rule.id}' definito sia in {seen[rule.id]} sia in {where}")
            else:
                seen[rule.id] = where
        if collisions:
            raise ConfigurationError("Collisione di ruleId tra regole di fonti diverse:\n  - " + "\n  - ".join(collisions))

    # ── query ──────────────────────────────────────────────────────────
    @property
    def all(self) -> list[AnyRule]:
        return [*self.spectral, *self.compiled]

    @property
    def is_empty(self) -> bool:
        return not self.compiled and not self.spectral

    def get(self, rule_id: str) -> AnyRule | None:
        return next((r for r in self.all if r.id == rule_id), None)

    def applicable(
        self, scope: RuleScope, method: str | None = None, status: str | None = None, path: str | None = None
    ) -> list[AnyRule]:
        """Regole applicabili a un singolo elemento (scope + contesto method/status/path)."""
        out: list[AnyRule] = []
        for rule in self.all:
            if rule.scope not in (scope, RuleScope.ANY):
                continue
            if isinstance(rule, SpectralRule):
                if method and rule.methods and method.lower() not in rule.methods:
                    continue
            else:
                cond = rule.condition
                if method and cond.methods and method.lower() not in [m.lower() for m in cond.methods]:
                    continue
                if status and cond.status_pattern and not re.search(cond.status_pattern, status):
                    continue
                if path and cond.path_pattern and not re.search(cond.path_pattern, path):
                    continue
            out.append(rule)
        return out

    def for_fragment(self, kind: ElementKind, method: str | None = None, path: str | None = None) -> list[AnyRule]:
        scopes = _FRAGMENT_SCOPES.get(kind, {RuleScope.ANY})
        out: list[AnyRule] = []
        for rule in self.all:
            if rule.scope not in scopes:
                continue
            if isinstance(rule, SpectralRule):
                if method and rule.methods and method not in rule.methods:
                    continue
                if not method and rule.methods:
                    continue
            else:
                cond = rule.condition
                if cond.methods:
                    if not method or method.lower() not in [m.lower() for m in cond.methods]:
                        continue
                if path and cond.path_pattern and not re.search(cond.path_pattern, path):
                    continue
            out.append(rule)
        return out

    def judgment_rules(self) -> list[CompiledRule]:
        return [r for r in self.compiled if not r.deterministic]


# ── conflitti tra regole ──────────────────────────────────────────────
def _spectral_casing(rule: SpectralRule) -> tuple[NameTarget, Casing] | None:
    """Vincolo di naming espresso da una regola Spectral, se riconoscibile."""
    given = rule.given.replace(" ", "")
    if rule.function == "casing" and rule.function_options:
        casing = {"camel": Casing.CAMEL, "pascal": Casing.PASCAL, "kebab": Casing.KEBAB, "snake": Casing.SNAKE,
                  "macro": Casing.MACRO, "cobol": Casing.TRAIN}.get(str(rule.function_options.get("type")))
        if casing is None:
            return None
        if "properties.*~" in given:
            return NameTarget.PROPERTY_NAME, casing
        if "in=='query'" in given:
            return NameTarget.QUERY_PARAMETER, casing
        if "in=='header'" in given:
            return NameTarget.HEADER, casing
        return None
    if rule.function == "kebabCasePath":
        return NameTarget.PATH_SEGMENT, Casing.KEBAB
    if rule.function == "pattern" and "in=='header'" in given:
        return NameTarget.HEADER, Casing.TRAIN
    return None


def detect_conflicts(compiled: list[CompiledRule], spectral: list[SpectralRule]) -> list[RuleConflict]:
    """Stesso elemento, indicazioni diverse: vince la regola deterministica (Spectral). Il conflitto è sempre riportato."""
    conflicts: list[RuleConflict] = []
    mechanical = {c[0]: (r, c[1]) for r in spectral if (c := _spectral_casing(r))}
    casing_rules: dict[NameTarget, list[tuple[CompiledRule, Casing]]] = {}
    for rule in compiled:
        for req in rule.requirements:
            if isinstance(req, NameCasing):
                casing_rules.setdefault(req.target, []).append((rule, req.casing))
    for target, entries in casing_rules.items():
        if target in mechanical:
            spec_rule, spec_casing = mechanical[target]
            for rule, casing in entries:
                if casing != spec_casing:
                    rule.overridden_by.append(spec_rule.id)
                    conflicts.append(RuleConflict(
                        element=target.value, winner=spec_rule.id, loser=rule.id,
                        reason=f"{spec_rule.id} (Spectral) richiede {spec_casing.value}, {rule.id} richiede {casing.value}: "
                               "prevale la regola deterministica",
                    ))
        distinct = {c for _, c in entries}
        if len(distinct) > 1 and target not in mechanical:
            first_rule, first_casing = entries[0]
            for rule, casing in entries[1:]:
                if casing != first_casing:
                    rule.overridden_by.append(first_rule.id)
                    conflicts.append(RuleConflict(
                        element=target.value, winner=first_rule.id, loser=rule.id,
                        reason=f"regole in linguaggio naturale in conflitto ({first_casing.value} vs {casing.value}): "
                               f"prevale {first_rule.id} (ordine di caricamento); da risolvere nei file .md",
                    ))
    for c in conflicts:
        log.warning("[RULES] Conflitto: %s", c.reason)
    return conflicts


async def build_registry(rules_dir: Path, interpreter) -> tuple[RuleRegistry, object]:
    """Carica e compila tutto il corpus regole. `interpreter` è un RuleInterpreter (può essere senza LLM se non ci sono .md)."""
    from app.rules.interpreter import CompileReport

    info = RuleSetInfo()
    report = CompileReport()
    md_files = natural_language_rule_files(rules_dir)
    rulesets = spectral_ruleset_files(rules_dir)
    info.natural_language_files = [str(f) for f in md_files]
    info.spectral_rulesets = [str(f) for f in rulesets]

    spectral: list[SpectralRule] = []
    for rs in rulesets:
        spectral.extend(load_spectral_rules(rs))
    compiled: list[CompiledRule] = []
    for md in md_files:
        compiled.extend(await interpreter.compile_file(md, report))

    if not md_files and not rulesets:
        msg = (f"NESSUNA REGOLA trovata in '{rules_dir}' (né file .md né ruleset .spectral.yaml): "
               "verrà eseguita SOLO la validazione OpenAPI di base, nessun controllo di governance.")
        info.warnings.append(msg)
        log.warning("!" * 80)
        log.warning("[RULES] %s", msg)
        log.warning("!" * 80)
    elif not rulesets:
        info.warnings.append(f"Nessun ruleset Spectral in '{rules_dir}/spectral': governance meccanica non eseguita.")
        log.warning("[RULES] %s", info.warnings[-1])
    elif not md_files:
        info.warnings.append(f"Nessun file .md in '{rules_dir}': nessuna regola in linguaggio naturale.")
        log.warning("[RULES] %s", info.warnings[-1])

    registry = RuleRegistry(compiled, spectral, info)
    log.info("[RULES] %d regole Spectral, %d regole compilate da linguaggio naturale (%d solo-giudizio), %d conflitti",
             len(spectral), len(compiled), len(registry.judgment_rules()), len(registry.conflicts))
    return registry, report

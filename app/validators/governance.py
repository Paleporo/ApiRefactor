"""GovernanceValidator deterministico: Spectral (regole meccaniche) + regole compilate dal linguaggio naturale
+ controlli strutturali (required orfani).

Entrambe le fonti producono lo stesso formato `Violation`: chi consuma non distingue la provenienza.
"""

from __future__ import annotations

from app.config import AppConfig
from app.logging_setup import get_logger
from app.model.document import SpecDocument
from app.model.issues import Violation, summary
from app.rules.registry import RuleRegistry
from app.validators.rule_eval import RuleEvaluator
from app.validators.structural import required_orphans
from app.validators.spectral import SpectralRunner

log = get_logger("governance")


class GovernanceValidator:
    def __init__(self, config: AppConfig, registry: RuleRegistry):
        self.registry = registry
        self.spectral = SpectralRunner(config, registry.spectral)

    def validate(self, doc: SpecDocument) -> list[Violation]:
        violations = self.spectral.lint(doc)
        violations += RuleEvaluator(doc).evaluate(self.registry.compiled)
        violations += required_orphans(doc)  # controllo strutturale deterministico, sempre attivo
        log.info("[GOVERNANCE] %s", summary(violations))
        return violations

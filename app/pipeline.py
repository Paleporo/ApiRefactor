"""Orchestrazione della pipeline, indipendente dal trasporto (CLI oggi, endpoint FastAPI domani).

  InputLoader -> SpecificationParser -> RuleLoader -> RuleInterpreter -> RefactoringPlanner -> RefactoringEngine
  -> [OpenAPIValidator -> GovernanceValidator -> CriticEngine -> CorrectionEngine]* -> FinalValidator -> OutputWriter

AI transforms. Validators verify. AI critic reviews. Validators decide.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import AppConfig
from app.converter import ConversionResult, upgrade_to_target
from app.correction import CorrectionEngine, Problem
from app.critic import CriticEngine, CriticReport, ReviewedIssue
from app.diff.semantic_diff import DiffChange, RenameMap, compute_diff, untraced_violations
from app.errors import ExternalToolError, RunTimeoutError
from app.llm.base import LlmProvider
from app.llm.structured import StructuredLlm
from app.loader import load_spec
from app.logging_setup import get_logger
from app.model import pointer as jp
from app.model.document import ElementKind, SpecDocument
from app.model.issues import Severity, Violation, ViolationSource, summary
from app.model.refs import RefIndex
from app.refactor.engine import RefactoringEngine
from app.refactor.fragments import Fragment, build_fragments, fragment_content, fragment_for, group_by_fragment
from app.refactor.operations import AppliedChange, ApplyFailure, ChangeCategory
from app.refactor.planner import RefactoringPlan, RefactoringPlanner
from app.rules.interpreter import RuleInterpreter
from app.rules.loader import spectral_ruleset_files
from app.rules.registry import RuleRegistry, build_registry
from app.runtime import Deadline
from app.validators.governance import GovernanceValidator
from app.validators.openapi_validator import OpenAPIValidator
from app.validators.spectral import INSTALL_HINT, spectral_available

log = get_logger("pipeline")

PIPELINE_VERSION_RULE = "PIPELINE-TARGET-VERSION"
ENGINE_FAILURE_RULE = "ENGINE-APPLY-FAILED"


class RunStatus(StrEnum):
    SUCCESS = "SUCCESS"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class ExitConditions(BaseModel):
    openapi_valid: bool = Field(alias="openapiValid")
    no_governance_errors: bool = Field(alias="noGovernanceErrors")
    critic_accepted: bool = Field(alias="criticAccepted")
    no_engine_failures: bool = Field(alias="noEngineFailures")
    model_config = ConfigDict(populate_by_name=True)

    @property
    def all_met(self) -> bool:
        return self.openapi_valid and self.no_governance_errors and self.critic_accepted and self.no_engine_failures


class IterationRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)
    iteration: int
    validation: list[Violation]
    governance: list[Violation]
    untraced: list[Violation]
    critic: CriticReport | None
    engine_failures: list[ApplyFailure] = Field(default_factory=list)
    exit_conditions: ExitConditions


class RunResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    status: RunStatus
    reasons: list[str] = Field(default_factory=list)
    source: SpecDocument
    baseline: SpecDocument
    final: SpecDocument
    final_iteration: int = 0
    conversion: ConversionResult | None = None
    registry: RuleRegistry | None = None
    compile_report: Any = None
    plans: list[RefactoringPlan] = Field(default_factory=list)
    applied: list[AppliedChange] = Field(default_factory=list)
    failures: list[ApplyFailure] = Field(default_factory=list)
    iterations: list[IterationRecord] = Field(default_factory=list)
    final_validation: list[Violation] = Field(default_factory=list)
    final_governance: list[Violation] = Field(default_factory=list)
    final_diff: list[DiffChange] = Field(default_factory=list)
    fragment_states: dict[str, dict[str, Any]] = Field(default_factory=dict)
    llm_calls: int = 0
    elapsed_seconds: float = 0.0


def _errors(violations: list[Violation]) -> list[Violation]:
    return [v for v in violations if v.severity == Severity.ERROR]


def _content_fp(doc: SpecDocument, fragment: Fragment) -> str:
    blob = json.dumps(fragment_content(doc, fragment), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class RefactorPipeline:
    def __init__(self, config: AppConfig, provider: LlmProvider, rules_dir: str | Path | None = None):
        self.config = config
        self.provider = provider
        self.rules_dir = Path(rules_dir or config.rules_dir)

    # ── preflight ──────────────────────────────────────────────────────
    async def preflight(self, needs_spectral: bool) -> None:
        log.info("[PREFLIGHT] verifica LLM provider e tool esterni")
        await self.provider.preflight()
        if needs_spectral and not spectral_available(self.config):
            raise ExternalToolError(f"Ruleset Spectral presenti ma CLI Spectral non disponibile. {INSTALL_HINT}")

    # ── run ────────────────────────────────────────────────────────────
    async def run(self, input_path: str | Path) -> RunResult:
        deadline = Deadline(self.config.run_timeout_seconds)
        llm = StructuredLlm(self.provider, self.config.llm_call_timeout_seconds, self.config.llm_technical_retries,
                            deadline)

        log.info("[LOAD] %s", input_path)
        source = load_spec(input_path, self.config.target_openapi_version)  # EmptySpecError/SpecParseError: uscita pulita
        await self.preflight(needs_spectral=bool(spectral_ruleset_files(self.rules_dir)))

        conversion = upgrade_to_target(source, self.config)
        baseline = conversion.document
        applied: list[AppliedChange] = []
        if conversion.converted:
            applied.append(AppliedChange(
                type="UPDATE_OPENAPI_VERSION", rule_id=PIPELINE_VERSION_RULE, category=ChangeCategory.STRUCTURAL,
                locations=["/openapi"], description=f"{source.openapi_version} -> {baseline.openapi_version} "
                                                    f"({conversion.tool})",
                before=source.openapi_version, after=baseline.openapi_version))

        log.info("[RULES] caricamento regole da %s", self.rules_dir)
        interpreter = RuleInterpreter(llm, Path(self.config.compiled_rules_cache_dir))
        registry, compile_report = await build_registry(self.rules_dir, interpreter)

        result = RunResult(status=RunStatus.FAILED, source=source, baseline=baseline, final=baseline,
                           conversion=conversion, registry=registry, compile_report=compile_report, applied=applied)
        oas = OpenAPIValidator()
        governance = GovernanceValidator(self.config, registry)
        planner = RefactoringPlanner(llm, registry, self.config.llm_context_token_budget)
        critic = CriticEngine(llm, registry, self.config.llm_context_token_budget)
        corrector = CorrectionEngine(llm, registry, self.config.llm_context_token_budget)
        engine = RefactoringEngine()
        conversion_issues = [Violation(rule_id="CONVERSION-WARNING", severity=Severity.WARNING,
                                       file=source.source_file, path=i.location, message=i.message,
                                       source=ViolationSource.CONVERSION) for i in conversion.issues]

        candidate, candidate_iter = baseline, 0
        best_valid: tuple[int, SpecDocument] | None = None
        states: dict[str, dict[str, Any]] = {}
        carried_blocking: dict[str, list[ReviewedIssue]] = {}
        pending_failures: list[ApplyFailure] = []
        llm_failures: list[str] = []
        try:
            # ── piano iniziale ─────────────────────────────────────────
            deadline.check("PLAN")
            log.info("[VALIDATE] baseline")
            base_violations = oas.validate(baseline) + governance.validate(baseline)
            fragments = build_fragments(baseline)
            grouped = group_by_fragment([v for v in base_violations if v.severity != Severity.INFO], fragments)
            targets = []
            for frag in fragments:
                judgment = [r for r in registry.for_fragment(frag.kind, frag.method, frag.path)
                            if getattr(r, "judgment_only", False)]
                if grouped.get(frag.pointer) or (judgment and frag.kind in (ElementKind.OPERATION, ElementKind.SCHEMA)):
                    targets.append((frag, grouped.get(frag.pointer, [])))
            log.info("[PLAN] %d frammenti da elaborare su %d (%d violazioni iniziali: %s)",
                     len(targets), len(fragments), len(base_violations), summary(base_violations))
            known = {r.id for r in registry.all} | {v.rule_id for v in base_violations}
            plan = await planner.plan(baseline, RefIndex(baseline.data), targets, iteration=1, known_rule_ids=known)
            result.plans.append(plan)
            llm_failures += plan.failed_fragments

            log.info("[ENGINE] applicazione di %d operazioni", len(plan.operations))
            candidate, changes, pending_failures = engine.apply(baseline, plan.operations, iteration=1)
            candidate_iter = 1
            applied.extend(changes)
            result.failures.extend(pending_failures)
            self._log_changes(changes)

            # ── feedback loop ──────────────────────────────────────────
            for iteration in range(1, self.config.max_iterations + 1):
                log.info("──── Iterazione %d/%d ────", iteration, self.config.max_iterations)
                deadline.check("VALIDATE")
                validation = oas.validate(candidate) + (conversion_issues if iteration == 1 else [])
                log.info("[VALIDATE] OpenAPI: %s", summary(validation))
                deadline.check("GOVERNANCE")
                gov = governance.validate(candidate)
                diff = compute_diff(baseline, candidate, applied)
                untraced = untraced_violations(diff, source.source_file)
                if untraced:
                    log.info("[DIFF] %d change non tracciati (%s)", len(untraced), summary(untraced))

                deadline.check("CRITIC")
                cand_fragments = build_fragments(candidate)
                to_review = self._fragments_to_review(baseline, candidate, cand_fragments, diff, applied, states)
                log.info("[CRITIC] %d frammenti da rivedere", len(to_review))
                report = await critic.review(iteration=iteration, baseline=baseline, candidate=candidate,
                                             fragments=to_review, applied=applied, diff=diff,
                                             validation=validation, governance=gov + untraced)
                llm_failures += report.failed_fragments
                reviewed = set(report.reviewed_fragments)
                for frag in cand_fragments:
                    key = frag.pointer or "/"
                    if key in reviewed:
                        carried_blocking[key] = [i for i in report.issues if i.fragment == key and i.blocking]
                        states[key] = {"fingerprint": _content_fp(candidate, frag), "reviewedAt": iteration,
                                       "status": "REJECTED" if carried_blocking[key] else "ACCEPTED"}
                for key, issues in carried_blocking.items():
                    if key not in reviewed and issues:  # frammento invariato: il rifiuto precedente resta valido
                        report.issues.extend(issues)
                        report.accepted = False

                conditions = ExitConditions(
                    openapi_valid=not _errors(validation),
                    no_governance_errors=not _errors(gov) and not _errors(untraced),
                    critic_accepted=report.accepted,
                    no_engine_failures=not pending_failures,
                )
                result.iterations.append(IterationRecord(iteration=iteration, validation=validation, governance=gov,
                                                         untraced=untraced, critic=report,
                                                         engine_failures=pending_failures, exit_conditions=conditions))
                if conditions.openapi_valid:
                    best_valid = (iteration, candidate)
                log.info("[LOOP] condizioni di uscita: %s", conditions.model_dump(by_alias=True))
                if conditions.all_met:
                    log.info("[LOOP] tutte le condizioni soddisfatte all'iterazione %d", iteration)
                    break
                if iteration == self.config.max_iterations:
                    log.warning("[LOOP] maxIterations (%d) raggiunto senza soddisfare le condizioni di uscita",
                                self.config.max_iterations)
                    break

                # ── correzione mirata ──────────────────────────────────
                deadline.check("CORRECTION")
                targets_c = self._correction_targets(candidate, cand_fragments, validation, gov, untraced, report,
                                                     pending_failures, applied)
                known = ({r.id for r in registry.all} | {v.rule_id for v in validation + gov + untraced}
                         | {i.effective_rule_id for i in report.issues} | {ENGINE_FAILURE_RULE})
                plan = await corrector.correct(iteration=iteration + 1, baseline=baseline, candidate=candidate,
                                               targets=targets_c, applied=applied, known_rule_ids=known)
                result.plans.append(plan)
                llm_failures += plan.failed_fragments
                candidate, changes, pending_failures = engine.apply(candidate, plan.operations, iteration=iteration + 1)
                candidate_iter = iteration + 1
                applied.extend(changes)
                result.failures.extend(pending_failures)
                self._log_changes(changes)
        except RunTimeoutError as exc:
            log.error("[TIMEOUT] %s", exc)
            result.reasons.append(str(exc))

        # ── final validator ────────────────────────────────────────────
        final_iteration, final = candidate_iter, candidate
        if _errors(oas.validate(candidate)) and best_valid is not None and best_valid[0] != candidate_iter:
            final_iteration, final = best_valid
            result.reasons.append(f"l'ultimo candidato non è OpenAPI valido: output = ultimo candidato valido "
                                  f"(iterazione {final_iteration})")
        # il verdetto di Critic/engine vale solo se riferito proprio al candidato finale
        last = next((r for r in result.iterations if r.iteration == final_iteration), None)
        log.info("[FINAL] validazione finale del candidato (iterazione %d)", final_iteration)
        result.final, result.final_iteration = final, final_iteration
        result.final_validation = oas.validate(final)
        result.final_governance = governance.validate(final)
        final_applied = [c for c in applied if c.iteration <= final_iteration]
        result.applied = applied  # audit completo; changes.json indica cosa è incluso nel finale
        result.final_diff = compute_diff(baseline, final, final_applied)
        final_untraced = untraced_violations(result.final_diff, source.source_file)
        result.fragment_states = states
        result.llm_calls = llm.calls
        result.elapsed_seconds = round(deadline.elapsed, 2)
        result.status = self._decide(result, last, final_untraced, llm_failures)
        log.info("[FINAL] stato: %s%s", result.status.value, f" — {'; '.join(result.reasons)}" if result.reasons else "")
        return result

    # ── helper ─────────────────────────────────────────────────────────
    def _decide(self, result: RunResult, last: IterationRecord | None, final_untraced: list[Violation],
                llm_failures: list[str]) -> RunStatus:
        if _errors(result.final_validation):
            result.reasons.append(f"validazione OpenAPI finale fallita: {summary(result.final_validation)}")
            return RunStatus.FAILED
        ok = True
        if _errors(result.final_governance) or _errors(final_untraced):
            ok = False
            result.reasons.append(f"violazioni di governance residue: {summary(result.final_governance)}"
                                  + (f"; change non tracciati: {len(_errors(final_untraced))}" if final_untraced else ""))
        if last is None or not last.exit_conditions.critic_accepted:
            ok = False
            result.reasons.append("Critic: candidato non accettato" if last else
                                  "il candidato finale non è stato rivisto dal Critic")
        if last is not None and not last.exit_conditions.no_engine_failures:
            ok = False
            result.reasons.append("operazioni di refactoring non applicabili (vedi changes.json)")
        if llm_failures:
            ok = False
            result.reasons.append(f"chiamate LLM fallite dopo i retry tecnici sui frammenti: {sorted(set(llm_failures))}")
        if any("Budget complessivo" in r for r in result.reasons):
            ok = False
        return RunStatus.SUCCESS if ok else RunStatus.NEEDS_REVIEW

    @staticmethod
    def _log_changes(changes: list[AppliedChange]) -> None:
        semantic = [c for c in changes if c.category == ChangeCategory.SEMANTIC]
        log.info("[ENGINE] %d modifiche applicate (%d SEMANTIC)", len(changes), len(semantic))
        for c in semantic:
            log.warning("[ENGINE] SEMANTIC [%s] %s", c.rule_id, c.description)

    @staticmethod
    def _fragments_to_review(baseline: SpecDocument, candidate: SpecDocument, fragments: list[Fragment],
                             diff: list[DiffChange], applied: list[AppliedChange],
                             states: dict[str, dict[str, Any]]) -> list[Fragment]:
        """Solo frammenti cambiati rispetto all'originale e non già accettati con lo stesso contenuto."""
        renames = RenameMap(applied)
        touched = {renames.canonical(loc) for c in applied for loc in c.locations if c.rule_id != PIPELINE_VERSION_RULE}
        touched |= {renames.canonical(c.location) for c in diff}
        out = []
        for frag in fragments:
            key = frag.pointer or "/"
            fp = _content_fp(candidate, frag)
            if states.get(key, {}).get("fingerprint") == fp:
                continue  # già rivisto con questo contenuto: non si ripete il lavoro
            if frag.kind == ElementKind.PATH and not any(t == frag.pointer for t in touched):
                continue
            if frag.pointer == "":
                changed = any(jp.split(t)[:1] not in (["paths"], ["components"]) or
                              t.startswith("/components/securitySchemes") for t in touched)
            else:
                changed = any(jp.is_prefix(frag.pointer, t) for t in touched)
            if changed:
                out.append(frag)
        return out

    @staticmethod
    def _correction_targets(candidate: SpecDocument, fragments: list[Fragment], validation: list[Violation],
                            gov: list[Violation], untraced: list[Violation], report: CriticReport,
                            failures: list[ApplyFailure], applied: list[AppliedChange]):
        problems: dict[str, list[Problem]] = {}

        def add(location: str, problem: Problem) -> None:
            problems.setdefault(fragment_for(location, fragments).pointer, []).append(problem)

        for v in _errors(validation) + _errors(gov) + _errors(untraced):
            add(v.path, Problem(source=v.source.value, rule_id=v.rule_id, severity=v.severity.value, location=v.path,
                                message=v.message, details={"expected": v.expected, "actual": v.actual,
                                                            "suggestedFix": v.suggested_fix}))
        for issue in report.blocking:
            add(issue.issue.location or issue.fragment, Problem(
                source="critic", rule_id=issue.effective_rule_id, severity=issue.issue.severity.value,
                location=issue.issue.location, message=issue.issue.message,
                details={"verification": issue.verification.value, "evidence": issue.evidence}))
        for f in failures:
            add(f.target if f.target.startswith("/") else "", Problem(
                source="engine", rule_id=ENGINE_FAILURE_RULE, severity="ERROR", location=f.target,
                message=f"{f.type} ({f.rule_id}) non applicata: {f.reason}"))

        renames = RenameMap(applied)
        reverse_paths = {v: k for k, v in renames.paths.items()}
        reverse_schemas = {v: k for k, v in renames.schemas.items()}
        by_ptr = {f.pointer: f for f in fragments}
        return [(by_ptr[ptr], items, CriticEngine._original_pointer(ptr, reverse_paths, reverse_schemas))
                for ptr, items in problems.items() if ptr in by_ptr]

"""Orchestrazione della pipeline, indipendente dal trasporto (CLI oggi, endpoint FastAPI domani).

  InputLoader -> SpecificationParser -> RuleLoader -> RuleInterpreter -> RefactoringPlanner -> RefactoringEngine
  -> [OpenAPIValidator -> GovernanceValidator -> CriticEngine -> CorrectionEngine]* -> FinalValidator -> OutputWriter

AI transforms. Validators verify. AI critic reviews. Validators decide.

Garanzie del feedback loop:
- le violazioni con una correzione nota sono corrette in modo deterministico; l'LLM riceve solo le altre;
- il Critic è invocato solo su candidati OpenAPI validi e senza ERROR del GovernanceValidator
  (gli ERROR del semantic diff non lo escludono: individuare le regressioni è proprio il suo compito);
- una correzione che aumenta gli ERROR o introduce violazioni nuove viene scartata: si resta sul candidato corrente;
- l'output è il miglior candidato visto (valido, poi meno ERROR, poi meno WARNING), mai peggiore della baseline.
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator

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
from app.refactor.deterministic import DeterministicPlan, deterministic_fixes
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
PHASES = ("compile", "plan", "engine", "validation", "critic", "correction")


class RunStatus(StrEnum):
    SUCCESS = "SUCCESS"
    # tutte le condizioni soddisfatte, ma l'output contiene modifiche breaking (attese, motivate da regole):
    # i client esistenti vanno informati/aggiornati, quindi non è un successo "pieno"
    SUCCESS_WITH_BREAKING_CHANGES = "SUCCESS_WITH_BREAKING_CHANGES"
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


class Evaluation(BaseModel):
    """Esito deterministico di un candidato: validazione OpenAPI, governance, semantic diff."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    validation: list[Violation]
    governance: list[Violation]
    diff: list[DiffChange]
    untraced: list[Violation]

    @property
    def findings(self) -> list[Violation]:
        """ERROR e WARNING confrontabili tra candidati (i warning di conversione riguardano solo la baseline)."""
        return [v for v in self.validation + self.governance + self.untraced
                if v.severity != Severity.INFO and v.source != ViolationSource.CONVERSION]

    @property
    def valid(self) -> bool:
        return not _errors(self.validation)

    @property
    def errors(self) -> int:
        return len(_errors(self.findings))

    @property
    def warnings(self) -> int:
        return sum(1 for v in self.findings if v.severity == Severity.WARNING)

    @property
    def rank(self) -> tuple[bool, int, int]:
        """Ordine di qualità: OpenAPI valido, poi meno ERROR, poi meno WARNING."""
        return self.valid, -self.errors, -self.warnings

    def counts(self) -> dict[str, Any]:
        return {"openapiValid": self.valid, "errors": self.errors, "warnings": self.warnings}


class Candidate(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    iteration: int
    doc: SpecDocument
    applied: list[AppliedChange]
    evaluation: Evaluation
    failures: list[ApplyFailure] = Field(default_factory=list)
    critic: CriticReport | None = None


class IterationRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)
    iteration: int
    validation: list[Violation]
    governance: list[Violation]
    untraced: list[Violation]
    critic: CriticReport | None
    engine_failures: list[ApplyFailure] = Field(default_factory=list)
    exit_conditions: ExitConditions
    rejected: bool = False
    rejection_reasons: list[str] = Field(default_factory=list)
    new_violations: list[Violation] = Field(default_factory=list)


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
    applied: list[AppliedChange] = Field(default_factory=list)  # tutte le modifiche dei candidati accettati
    final_applied: list[AppliedChange] = Field(default_factory=list)  # quelle incluse nell'output
    rejected_changes: list[AppliedChange] = Field(default_factory=list)  # quelle dei candidati scartati
    failures: list[ApplyFailure] = Field(default_factory=list)
    iterations: list[IterationRecord] = Field(default_factory=list)
    baseline_validation: list[Violation] = Field(default_factory=list)
    baseline_governance: list[Violation] = Field(default_factory=list)
    baseline_counts: dict[str, Any] = Field(default_factory=dict)
    final_counts: dict[str, Any] = Field(default_factory=dict)
    final_validation: list[Violation] = Field(default_factory=list)
    final_governance: list[Violation] = Field(default_factory=list)
    final_diff: list[DiffChange] = Field(default_factory=list)
    breaking_changes: list[dict[str, Any]] = Field(default_factory=list)
    output_is_baseline: bool = False
    output_note: str | None = None
    fragment_states: dict[str, dict[str, Any]] = Field(default_factory=dict)
    compile_failed_rules: list[dict[str, Any]] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
    llm_stats: dict[str, dict[str, float]] = Field(default_factory=dict)
    llm_calls: int = 0
    elapsed_seconds: float = 0.0


def _errors(violations: list[Violation]) -> list[Violation]:
    return [v for v in violations if v.severity == Severity.ERROR]


def _content_fp(doc: SpecDocument, fragment: Fragment) -> str:
    blob = json.dumps(fragment_content(doc, fragment), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class PhaseTimer:
    """Tempo cumulato per fase (una fase può ripetersi a ogni iterazione)."""

    def __init__(self) -> None:
        self.seconds: dict[str, float] = {p: 0.0 for p in PHASES}

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.seconds[name] = round(self.seconds.get(name, 0.0) + time.monotonic() - started, 3)


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
        timer = PhaseTimer()
        llm = StructuredLlm(self.provider, self.config.llm_call_timeout_seconds, self.config.llm_technical_retries,
                            deadline)

        log.info("[LOAD] %s", input_path)
        source = load_spec(input_path, self.config.target_openapi_version)  # EmptySpecError/SpecParseError: uscita pulita
        await self.preflight(needs_spectral=bool(spectral_ruleset_files(self.rules_dir)))

        conversion = upgrade_to_target(source, self.config)
        baseline = conversion.document
        lineage: list[AppliedChange] = []
        if conversion.converted:
            lineage.append(AppliedChange(
                type="UPDATE_OPENAPI_VERSION", rule_id=PIPELINE_VERSION_RULE, category=ChangeCategory.STRUCTURAL,
                locations=["/openapi"], description=f"{source.openapi_version} -> {baseline.openapi_version} "
                                                    f"({conversion.tool})",
                before=source.openapi_version, after=baseline.openapi_version))

        log.info("[RULES] caricamento regole da %s", self.rules_dir)
        with timer.phase("compile"):
            interpreter = RuleInterpreter(llm, Path(self.config.compiled_rules_cache_dir))
            registry, compile_report = await build_registry(self.rules_dir, interpreter)

        result = RunResult(status=RunStatus.FAILED, source=source, baseline=baseline, final=baseline,
                           conversion=conversion, registry=registry, compile_report=compile_report)
        oas = OpenAPIValidator()
        governance = GovernanceValidator(self.config, registry)
        planner = RefactoringPlanner(llm, registry, self.config.llm_context_token_budget)
        critic = CriticEngine(llm, registry, self.config.llm_context_token_budget)
        corrector = CorrectionEngine(llm, registry, self.config.llm_context_token_budget)
        engine = RefactoringEngine()
        conversion_issues = [Violation(rule_id="CONVERSION-WARNING", severity=Severity.WARNING,
                                       file=source.source_file, path=i.location, message=i.message,
                                       source=ViolationSource.CONVERSION) for i in conversion.issues]

        log.info("[VALIDATE] baseline")
        with timer.phase("validation"):
            base_eval = self._evaluate(oas, governance, baseline, baseline, lineage, [])
        log.info("[VALIDATE] baseline: %s", summary(base_eval.validation + base_eval.governance))
        result.baseline_validation, result.baseline_governance = base_eval.validation, base_eval.governance
        baseline_candidate = Candidate(iteration=0, doc=baseline, applied=list(lineage), evaluation=base_eval)
        current = baseline_candidate
        accepted: list[Candidate] = [baseline_candidate]
        states: dict[str, dict[str, Any]] = {}
        carried_blocking: dict[str, list[ReviewedIssue]] = {}
        llm_failures: list[str] = []
        try:
            # ── piano iniziale: correzioni deterministiche + LLM sul resto ──
            deadline.check("PLAN")
            with timer.phase("plan"):
                plan = await self._initial_plan(planner, registry, baseline, base_eval)
            result.plans.append(plan)
            llm_failures += plan.failed_fragments
            with timer.phase("engine"):
                pending = engine.apply(baseline, plan.operations, iteration=1)
            self._log_changes(pending[1])
            rejection_note: dict[str, Any] | None = None

            # ── feedback loop ──────────────────────────────────────────
            for iteration in range(1, self.config.max_iterations + 1):
                log.info("---- Iterazione %d/%d ----", iteration, self.config.max_iterations)
                doc, changes, failures = pending
                result.failures.extend(failures)
                applied = current.applied + changes
                deadline.check("VALIDATE")
                with timer.phase("validation"):
                    ev = self._evaluate(oas, governance, baseline, doc, applied,
                                        conversion_issues if iteration == 1 else [])
                log.info("[VALIDATE] OpenAPI: %s | governance: %s | change non tracciati: %d", summary(ev.validation),
                         summary(ev.governance), len(ev.untraced))

                # gate: una correzione non deve mai peggiorare il candidato corrente
                reasons, fresh = self._regression(current.evaluation, ev, applied) if iteration > 1 else ([], [])
                if reasons:
                    log.warning("[LOOP] correzione dell'iterazione %d SCARTATA: %s", iteration, "; ".join(reasons))
                    report = CriticReport.skipped_for(iteration, "candidato scartato (regressione): "
                                                      + "; ".join(reasons))
                    result.iterations.append(self._record(iteration, ev, report, failures, rejected=True,
                                                          reasons=reasons, fresh=fresh))
                    result.rejected_changes.extend(changes)
                    rejection_note = {"reasons": reasons,
                                      "rejectedOperations": [f"{c.type} [{c.rule_id}] {c.description}" for c in changes],
                                      "newViolations": [f"{v.rule_id} @ {v.path}" for v in fresh[:20]]}
                else:
                    rejection_note = None
                    deadline.check("CRITIC")
                    with timer.phase("critic"):
                        report = await self._critic_stage(critic, iteration, baseline, doc, applied, ev, states,
                                                          carried_blocking)
                    llm_failures += report.failed_fragments
                    current = Candidate(iteration=iteration, doc=doc, applied=applied, evaluation=ev,
                                        failures=failures, critic=report)
                    accepted.append(current)
                    record = self._record(iteration, ev, report, failures)
                    result.iterations.append(record)
                    log.info("[LOOP] condizioni di uscita: %s", record.exit_conditions.model_dump(by_alias=True))
                    if record.exit_conditions.all_met:
                        log.info("[LOOP] tutte le condizioni soddisfatte all'iterazione %d", iteration)
                        break
                if iteration == self.config.max_iterations:
                    log.warning("[LOOP] maxIterations (%d) raggiunto senza soddisfare le condizioni di uscita",
                                self.config.max_iterations)
                    break

                # ── correzione mirata del candidato corrente ───────────
                deadline.check("CORRECTION")
                with timer.phase("correction"):
                    plan = await self._correction_plan(corrector, registry, baseline, current, iteration + 1,
                                                       rejection_note)
                result.plans.append(plan)
                llm_failures += plan.failed_fragments
                with timer.phase("engine"):
                    pending = engine.apply(current.doc, plan.operations, iteration=iteration + 1)
                self._log_changes(pending[1])
        except RunTimeoutError as exc:
            log.error("[TIMEOUT] %s", exc)
            result.reasons.append(str(exc))

        # ── final validator: il miglior candidato, mai peggiore della baseline ──
        final = self._select_output(accepted, baseline_candidate, result)
        log.info("[FINAL] validazione finale del candidato (iterazione %d)", final.iteration)
        with timer.phase("validation"):
            result.final, result.final_iteration = final.doc, final.iteration
            result.final_validation = oas.validate(final.doc)
            result.final_governance = governance.validate(final.doc)
            result.final_diff = compute_diff(baseline, final.doc, final.applied)
            final_untraced = untraced_violations(result.final_diff, source.source_file)
        result.breaking_changes = [{"location": c.location, "type": c.type.value, "ruleId": c.rule_id,
                                    "expected": c.expected} for c in result.final_diff if c.breaking]
        result.final_applied = final.applied
        result.applied = accepted[-1].applied if len(accepted) > 1 else list(lineage)
        result.baseline_counts = base_eval.counts()
        result.final_counts = Evaluation(validation=result.final_validation, governance=result.final_governance,
                                         diff=result.final_diff, untraced=final_untraced).counts()
        result.fragment_states = states
        result.llm_calls = llm.calls
        result.llm_stats = llm.stats
        result.timings = {**timer.seconds, "total": round(deadline.elapsed, 3)}
        result.elapsed_seconds = round(deadline.elapsed, 2)
        record = next((r for r in result.iterations if r.iteration == final.iteration and not r.rejected), None)
        result.status = self._decide(result, final, record, final_untraced, llm_failures)
        log.info("[FINAL] stato: %s%s", result.status.value, f" - {'; '.join(result.reasons)}" if result.reasons else "")
        for b in result.breaking_changes:
            log.warning("[FINAL] BREAKING %s @ %s (ruleId %s)", b["type"], b["location"], b["ruleId"])
        log.info("[FINAL] tempi per fase (s): %s", result.timings)
        return result

    # ── fasi ───────────────────────────────────────────────────────────
    @staticmethod
    def _evaluate(oas: OpenAPIValidator, governance: GovernanceValidator, baseline: SpecDocument,
                  doc: SpecDocument, applied: list[AppliedChange], extra: list[Violation]) -> Evaluation:
        diff = compute_diff(baseline, doc, applied)
        return Evaluation(validation=oas.validate(doc) + extra, governance=governance.validate(doc), diff=diff,
                          untraced=untraced_violations(diff, doc.source_file))

    @staticmethod
    def _split(doc: SpecDocument, violations: list[Violation], registry: RuleRegistry,
               fragments: list[Fragment]) -> tuple[DeterministicPlan, list[Violation]]:
        """Correzioni deterministiche + violazioni che restano per l'LLM (né corrette né sugli elementi riscritti)."""
        det = deterministic_fixes(doc, violations, registry, fragments)
        visible = [v for v in violations if det.is_llm_visible(v)]
        log.info("[PLAN] %d violazioni con correzione deterministica (%d operazioni), %d per l'LLM",
                 len(det.handled), len(det.operations), len(visible))
        return det, visible

    async def _initial_plan(self, planner: RefactoringPlanner, registry: RuleRegistry, baseline: SpecDocument,
                            base_eval: Evaluation) -> RefactoringPlan:
        fragments = build_fragments(baseline)
        findings = [v for v in base_eval.validation + base_eval.governance if v.severity != Severity.INFO]
        det, visible = self._split(baseline, findings, registry, fragments)
        grouped = group_by_fragment(visible, fragments)
        # solo frammenti con violazioni per l'LLM: le regole di giudizio restano al Critic
        targets = [(f, grouped[f.pointer]) for f in fragments if grouped.get(f.pointer)]
        log.info("[PLAN] %d frammenti all'LLM su %d", len(targets), len(fragments))
        return await planner.plan(baseline, RefIndex(baseline.data), targets, iteration=1,
                                  deterministic=det.operations)

    async def _critic_stage(self, critic: CriticEngine, iteration: int, baseline: SpecDocument, doc: SpecDocument,
                            applied: list[AppliedChange], ev: Evaluation, states: dict[str, dict[str, Any]],
                            carried_blocking: dict[str, list[ReviewedIssue]]) -> CriticReport:
        gov_errors = _errors(ev.governance)
        if not ev.valid or gov_errors:
            reason = ("candidato non valido OpenAPI" if not ev.valid else
                      f"{len(gov_errors)} ERROR di governance")
            log.info("[CRITIC] non invocato (%s): si passa direttamente alla correzione", reason)
            return CriticReport.skipped_for(iteration, f"{reason}: il candidato va prima corretto")
        fragments = build_fragments(doc)
        to_review = self._fragments_to_review(baseline, doc, fragments, ev.diff, applied, states)
        log.info("[CRITIC] %d frammenti da rivedere", len(to_review))
        report = await critic.review(iteration=iteration, baseline=baseline, candidate=doc, fragments=to_review,
                                     applied=applied, diff=ev.diff, validation=ev.validation,
                                     governance=ev.governance + ev.untraced)
        reviewed = set(report.reviewed_fragments)
        for frag in fragments:
            key = frag.pointer or "/"
            if key in reviewed:
                carried_blocking[key] = [i for i in report.issues if i.fragment == key and i.blocking]
                states[key] = {"fingerprint": _content_fp(doc, frag), "reviewedAt": iteration,
                               "status": "REJECTED" if carried_blocking[key] else "ACCEPTED"}
        for key, issues in carried_blocking.items():
            if key not in reviewed and issues:  # frammento invariato: il rifiuto precedente resta valido
                report.issues.extend(issues)
                report.accepted = False
        return report

    async def _correction_plan(self, corrector: CorrectionEngine, registry: RuleRegistry, baseline: SpecDocument,
                               current: Candidate, iteration: int,
                               rejection_note: dict[str, Any] | None) -> RefactoringPlan:
        ev = current.evaluation
        fragments = build_fragments(current.doc)
        findings = [v for v in ev.validation + ev.governance + ev.untraced
                    if v.severity != Severity.INFO and v.source != ViolationSource.CONVERSION]
        det, visible = self._split(current.doc, findings, registry, fragments)
        report = current.critic or CriticReport(iteration=current.iteration, accepted=True)
        targets = self._correction_targets(fragments, _errors(visible), report, current.failures, current.applied)
        return await corrector.correct(iteration=iteration, baseline=baseline, candidate=current.doc,
                                       targets=targets, applied=current.applied, deterministic=det.operations,
                                       previous_rejection=rejection_note)

    # ── decisioni ──────────────────────────────────────────────────────
    @staticmethod
    def _regression(previous: Evaluation, new: Evaluation,
                    applied: list[AppliedChange]) -> tuple[list[str], list[Violation]]:
        """Motivi per scartare una correzione: più ERROR, o violazioni nuove su elementi prima conformi."""
        renames = RenameMap(applied)
        reasons: list[str] = []
        if new.errors > previous.errors:
            reasons.append(f"ERROR da {previous.errors} a {new.errors}")
        before = {(v.rule_id, renames.canonical(v.path)) for v in previous.findings}
        fresh = [v for v in new.findings if (v.rule_id, renames.canonical(v.path)) not in before]
        if fresh:
            sample = ", ".join(f"{v.rule_id}@{v.path or '/'}" for v in fresh[:5])
            reasons.append(f"{len(fresh)} violazioni nuove ({sample}{', ...' if len(fresh) > 5 else ''})")
        return reasons, fresh

    @staticmethod
    def _record(iteration: int, ev: Evaluation, report: CriticReport, failures: list[ApplyFailure],
                rejected: bool = False, reasons: list[str] | None = None,
                fresh: list[Violation] | None = None) -> IterationRecord:
        conditions = ExitConditions(openapi_valid=ev.valid,
                                    no_governance_errors=not _errors(ev.governance) and not _errors(ev.untraced),
                                    critic_accepted=report.accepted, no_engine_failures=not failures)
        return IterationRecord(iteration=iteration, validation=ev.validation, governance=ev.governance,
                               untraced=ev.untraced, critic=report, engine_failures=failures,
                               exit_conditions=conditions, rejected=rejected, rejection_reasons=reasons or [],
                               new_violations=fresh or [])

    @staticmethod
    def _select_output(accepted: list[Candidate], baseline: Candidate, result: RunResult) -> Candidate:
        """Miglior candidato visto; se nessuno migliora la baseline l'output coincide con la baseline."""
        candidates = accepted[1:]
        best = max(candidates, key=lambda c: (c.evaluation.rank, bool(c.critic and c.critic.accepted), c.iteration),
                   default=None)
        if best is not None and best.evaluation.rank > baseline.evaluation.rank:
            if best is not accepted[-1]:
                result.output_note = (f"output = candidato dell'iterazione {best.iteration}, il migliore visto "
                                      f"(quelli successivi non lo hanno migliorato)")
            return best
        result.output_is_baseline = True
        b = baseline.evaluation
        if b.errors or b.warnings or not b.valid:
            result.output_note = ("nessun candidato migliora la baseline: l'output coincide con l'originale "
                                  + ("(convertito alla versione target) " if baseline.applied else "")
                                  + f"({b.errors} ERROR, {b.warnings} WARNING)")
        else:
            result.output_note = "la baseline non ha violazioni: nessuna modifica necessaria"
        return baseline

    def _decide(self, result: RunResult, final: Candidate, record: IterationRecord | None,
                final_untraced: list[Violation], llm_failures: list[str]) -> RunStatus:
        if _errors(result.final_validation):
            result.reasons.append(f"validazione OpenAPI finale fallita: {summary(result.final_validation)}")
            return RunStatus.FAILED
        ok = True
        if result.output_is_baseline and (result.baseline_counts.get("errors") or result.baseline_counts.get("warnings")):
            ok = False
            result.reasons.append(result.output_note or "nessun candidato migliora la baseline")
        if _errors(result.final_governance) or _errors(final_untraced):
            ok = False
            result.reasons.append(f"violazioni di governance residue: {summary(result.final_governance)}"
                                  + (f"; change non tracciati: {len(_errors(final_untraced))}" if final_untraced else ""))
        changed = bool([c for c in final.applied if c.rule_id != PIPELINE_VERSION_RULE])
        if record is None:
            if changed:
                ok = False
                result.reasons.append("il candidato finale non è stato rivisto dal Critic")
        elif not record.exit_conditions.critic_accepted:
            ok = False
            critic = record.critic
            result.reasons.append(f"Critic non invocato: {critic.skip_reason}" if critic and critic.skipped
                                  else "Critic: candidato non accettato")
        if final.failures:
            ok = False
            result.reasons.append("operazioni di refactoring non applicabili (vedi changes.json)")
        if llm_failures:
            ok = False
            result.reasons.append(f"chiamate LLM fallite dopo i retry tecnici sui frammenti: {sorted(set(llm_failures))}")
        if any("Budget complessivo" in r for r in result.reasons):
            ok = False
        # una regola non compilata in modo conforme non è verificata meccanicamente: il risultato va rivisto
        reasons = getattr(result.compile_report, "failure_reasons", {}) or {}
        result.compile_failed_rules = [
            {"ruleId": r.id, "file": r.file, "line": r.line, "reason": reasons.get(r.id, "motivo non registrato")}
            for r in (result.registry.compiled if result.registry else []) if r.compile_failed]
        if result.compile_failed_rules:
            ok = False
            result.reasons.append(
                "regole non compilate in modo conforme, attive solo come giudizio: "
                + "; ".join(f"{f['ruleId']} ({Path(f['file']).name}:{f['line']}): {f['reason']}"
                            for f in result.compile_failed_rules))
        if not ok:
            return RunStatus.NEEDS_REVIEW
        return RunStatus.SUCCESS_WITH_BREAKING_CHANGES if result.breaking_changes else RunStatus.SUCCESS

    # ── helper ─────────────────────────────────────────────────────────
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
    def _correction_targets(fragments: list[Fragment], violations: list[Violation], report: CriticReport,
                            failures: list[ApplyFailure], applied: list[AppliedChange]):
        problems: dict[str, list[Problem]] = {}

        def add(location: str, problem: Problem) -> None:
            problems.setdefault(fragment_for(location, fragments).pointer, []).append(problem)

        for v in violations:
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


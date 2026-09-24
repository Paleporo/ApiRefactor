"""CriticEngine: revisore separato dal Refactor Agent (prompt e modello distinti).

"Validators decide" vale anche per il Critic: ogni claim fattuale (elemento rimosso/cambiato/aggiunto,
$ref rotto, regola non applicata) viene incrociato con il semantic diff / i validatori prima di poter
bloccare un'iterazione. Solo i giudizi realmente semantici (qualità del naming, coerenza) restano al Critic.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.diff.compat import REMOVAL_TYPES, ChangeType
from app.diff.semantic_diff import DiffChange, RenameMap
from app.errors import LlmCallError
from app.llm.base import AgentRole, LlmRequest
from app.llm.structured import StructuredLlm
from app.logging_setup import get_logger
from app.model import pointer as jp
from app.model.document import SpecDocument
from app.model.issues import Severity, Violation
from app.model.refs import RefIndex
from app.prompts import load_prompt
from app.refactor.fragments import Fragment, build_slice, fragment_content, render
from app.refactor.operations import AppliedChange
from app.refactor.planner import describe_rule
from app.rules.models import SpectralRule
from app.rules.registry import RuleRegistry

log = get_logger("critic")


class CriticIssueType(StrEnum):
    RULE_NOT_APPLIED = "RULE_NOT_APPLIED"
    ELEMENT_LOST = "ELEMENT_LOST"
    INVENTED_ELEMENT = "INVENTED_ELEMENT"
    UNNECESSARY_CHANGE = "UNNECESSARY_CHANGE"
    SEMANTIC_ALTERATION = "SEMANTIC_ALTERATION"
    BROKEN_REFERENCE = "BROKEN_REFERENCE"
    SCHEMA_INCONSISTENCY = "SCHEMA_INCONSISTENCY"
    REGRESSION = "REGRESSION"
    MIGRATION_ERROR = "MIGRATION_ERROR"
    NAMING_QUALITY = "NAMING_QUALITY"
    OTHER = "OTHER"


class CriticClaim(BaseModel):
    kind: Literal["REMOVED", "CHANGED", "ADDED"]
    location: str


class CriticIssue(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    type: CriticIssueType
    severity: Severity = Severity.ERROR
    rule_id: str | None = Field(None, alias="ruleId")
    location: str = ""
    message: str
    claim: CriticClaim | None = None


class CriticVerdict(BaseModel):
    """Output strutturato del modello Critic per un frammento."""

    accepted: bool
    issues: list[CriticIssue] = Field(default_factory=list)


class Verification(StrEnum):
    VERIFIED = "VERIFIED"  # fatto confermato e non tracciato da alcuna regola: blocca (se ERROR)
    TRACED = "TRACED"  # fatto vero ma riconducibile a un'operazione pianificata con ruleId: non blocca
    REFUTED = "REFUTED"  # smentito dai validatori/diff: scartato
    JUDGMENT = "JUDGMENT"  # giudizio semantico non verificabile: affidato al Critic


# tipi di issue che esprimono un fatto verificabile sul contratto
_FACTUAL = {
    CriticIssueType.ELEMENT_LOST: "REMOVED",
    CriticIssueType.INVENTED_ELEMENT: "ADDED",
    CriticIssueType.UNNECESSARY_CHANGE: "CHANGED",
    CriticIssueType.SEMANTIC_ALTERATION: "CHANGED",
    CriticIssueType.REGRESSION: "CHANGED",
}


class ReviewedIssue(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    fragment: str
    issue: CriticIssue
    verification: Verification
    evidence: str
    blocking: bool

    @property
    def effective_rule_id(self) -> str:
        return self.issue.rule_id or f"CRITIC-{self.issue.type.value}"

    def dump(self) -> dict[str, Any]:
        return {"fragment": self.fragment, **self.issue.model_dump(by_alias=True, mode="json"),
                "verification": self.verification.value, "evidence": self.evidence, "blocking": self.blocking}


class CriticReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    iteration: int
    accepted: bool
    reviewed_fragments: list[str] = Field(default_factory=list, alias="reviewedFragments")
    failed_fragments: list[str] = Field(default_factory=list, alias="failedFragments")
    model_accepted: dict[str, bool] = Field(default_factory=dict, alias="modelAccepted")
    issues: list[ReviewedIssue] = Field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = Field(None, alias="skipReason")

    @classmethod
    def skipped_for(cls, iteration: int, reason: str) -> "CriticReport":
        """Critic non invocato: il candidato non è accettabile per motivi deterministici (non è 'accettato')."""
        return cls(iteration=iteration, accepted=False, skipped=True, skip_reason=reason)

    @property
    def blocking(self) -> list[ReviewedIssue]:
        return [i for i in self.issues if i.blocking]

    def dump(self) -> dict[str, Any]:
        return {"iteration": self.iteration, "accepted": self.accepted, "skipped": self.skipped,
                "skipReason": self.skip_reason, "reviewedFragments": self.reviewed_fragments,
                "failedFragments": self.failed_fragments, "modelAccepted": self.model_accepted,
                "issues": [i.dump() for i in self.issues]}


class ClaimVerifier:
    """Verifica deterministica delle affermazioni del Critic."""

    def __init__(self, candidate: SpecDocument, diff: list[DiffChange], governance: list[Violation],
                 registry: RuleRegistry, applied: list[AppliedChange]):
        self.candidate = candidate
        self.diff = diff
        self.governance = governance
        self.registry = registry
        self.renames = RenameMap(applied)

    def verify(self, issue: CriticIssue) -> tuple[Verification, str]:
        if issue.type == CriticIssueType.BROKEN_REFERENCE:
            broken = [u for u in RefIndex(self.candidate.data).broken_refs()
                      if not issue.location or jp.is_prefix(issue.location, u.at) or jp.is_prefix(u.at, issue.location)]
            if broken:
                return Verification.VERIFIED, f"$ref rotti confermati: {[u.ref for u in broken]}"
            return Verification.REFUTED, "nessun $ref rotto nel candidato in quella posizione"
        if issue.type == CriticIssueType.RULE_NOT_APPLIED:
            return self._verify_rule_not_applied(issue)
        if issue.type in _FACTUAL or issue.claim is not None:
            claim = issue.claim or CriticClaim(kind=_FACTUAL[issue.type], location=issue.location)  # type: ignore[arg-type]
            return self._verify_claim(claim)
        return Verification.JUDGMENT, "giudizio semantico: non verificabile deterministicamente"

    def _verify_rule_not_applied(self, issue: CriticIssue) -> tuple[Verification, str]:
        if not issue.rule_id:
            return Verification.JUDGMENT, "regola non specificata: giudizio del Critic"
        rule = self.registry.get(issue.rule_id)
        if rule is None:
            return Verification.REFUTED, f"ruleId '{issue.rule_id}' inesistente nel corpus regole"
        if isinstance(rule, SpectralRule) or rule.deterministic:
            hits = [v for v in self.governance if v.rule_id == issue.rule_id]
            if hits:
                return Verification.VERIFIED, f"{len(hits)} violazioni deterministiche di {issue.rule_id} confermate"
            if isinstance(rule, SpectralRule) or not rule.judgment_only:
                return Verification.REFUTED, f"il validatore deterministico non rileva violazioni di {issue.rule_id}"
        return Verification.JUDGMENT, f"{issue.rule_id} è una regola di giudizio: valutazione del Critic"

    def _verify_claim(self, claim: CriticClaim) -> tuple[Verification, str]:
        loc = self.renames.canonical(claim.location)
        related = []
        for change in self.diff:
            cloc = self.renames.canonical(change.location)
            if not (jp.is_prefix(loc, cloc) or jp.is_prefix(cloc, loc)):
                continue
            if claim.kind == "REMOVED" and change.type not in REMOVAL_TYPES | {ChangeType.PATH_RENAMED}:
                continue
            if claim.kind == "ADDED" and not change.type.value.endswith("_ADDED"):
                continue
            related.append(change)
        if not related:
            return Verification.REFUTED, f"il semantic diff non contiene alcun change {claim.kind} in {claim.location}"
        untraced = [c for c in related if not c.expected]
        if untraced:
            return Verification.VERIFIED, "confermato dal semantic diff, NON tracciato da alcuna regola: " + \
                ", ".join(f"{c.type.value}@{c.location}" for c in untraced)
        rules = sorted({c.rule_id for c in related if c.rule_id})
        return Verification.TRACED, f"change reale ma pianificato (ruleId {', '.join(rules)})"


class CriticEngine:
    def __init__(self, llm: StructuredLlm, registry: RuleRegistry, budget_tokens: int):
        self.llm = llm
        self.registry = registry
        self.budget_tokens = budget_tokens
        self.system = load_prompt("critic")

    async def review(self, *, iteration: int, baseline: SpecDocument, candidate: SpecDocument,
                     fragments: list[Fragment], applied: list[AppliedChange], diff: list[DiffChange],
                     validation: list[Violation], governance: list[Violation]) -> CriticReport:
        report = CriticReport(iteration=iteration, accepted=True)
        verifier = ClaimVerifier(candidate, diff, governance + validation, self.registry, applied)
        refs = RefIndex(candidate.data)
        renames = RenameMap(applied)
        reverse_paths = {v: k for k, v in renames.paths.items()}
        reverse_schemas = {v: k for k, v in renames.schemas.items()}
        for fragment in fragments:
            ptr = fragment.pointer
            original_ptr = self._original_pointer(ptr, reverse_paths, reverse_schemas)
            inside = lambda loc: bool(ptr) and jp.is_prefix(ptr, renames.canonical(loc))  # noqa: E731
            payload = {
                "original": fragment_content(baseline, fragment.model_copy(update={"pointer": original_ptr}))
                if baseline.exists(original_ptr) or not original_ptr else None,
                "candidate": build_slice(candidate, fragment, refs, self.budget_tokens),
                "appliedOperations": [c.model_dump(by_alias=True, mode="json", include={"type", "rule_id", "category",
                                                                                          "description", "locations"})
                                      for c in applied if any(inside(loc) for loc in c.locations)],
                "semanticDiff": [c.dump() for c in diff if inside(c.location)],
                "validation": [v.dump() for v in validation + governance if inside(v.path)],
                "rules": [describe_rule(r) for r in self.registry.for_fragment(fragment.kind, fragment.method,
                                                                               fragment.path)],
            }
            request = LlmRequest(role=AgentRole.CRITIC, task="critic-fragment", system=self.system,
                                 user="## Review input\n" + render(payload) + "\n\nReturn your verdict (JSON).",
                                 context={"fragment": ptr, "iteration": iteration,
                                          "diff": payload["semanticDiff"], "applied": payload["appliedOperations"]})
            try:
                verdict = await self.llm.generate(request, CriticVerdict)
            except LlmCallError as exc:
                log.warning("[CRITIC] %s: revisione FALLITA (%s)", fragment.label, exc)
                report.failed_fragments.append(ptr or "/")
                report.accepted = False
                continue
            report.reviewed_fragments.append(ptr or "/")
            report.model_accepted[ptr or "/"] = verdict.accepted
            for issue in verdict.issues:
                status, evidence = verifier.verify(issue)
                blocking = issue.severity == Severity.ERROR and status in (Verification.VERIFIED, Verification.JUDGMENT)
                report.issues.append(ReviewedIssue(fragment=ptr or "/", issue=issue, verification=status,
                                                   evidence=evidence, blocking=blocking))
                log.debug("[CRITIC] %s %s @%s -> %s (%s)", issue.type.value, issue.severity.value, issue.location,
                          status.value, evidence)
        if report.blocking:
            report.accepted = False
        refuted = sum(1 for i in report.issues if i.verification == Verification.REFUTED)
        log.info("[CRITIC] accepted=%s, %d issue (%d bloccanti, %d claim smentiti dai validatori), %d frammenti rivisti",
                 report.accepted, len(report.issues), len(report.blocking), refuted, len(report.reviewed_fragments))
        return report

    @staticmethod
    def _original_pointer(ptr: str, reverse_paths: dict[str, str], reverse_schemas: dict[str, str]) -> str:
        tokens = jp.split(ptr)
        if len(tokens) >= 2 and tokens[0] == "paths" and tokens[1] in reverse_paths:
            tokens[1] = reverse_paths[tokens[1]]
        if len(tokens) >= 3 and tokens[:2] == ["components", "schemas"] and tokens[2] in reverse_schemas:
            tokens[2] = reverse_schemas[tokens[2]]
        return jp.join(tokens)

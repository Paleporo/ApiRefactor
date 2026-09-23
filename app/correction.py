"""CorrectionEngine: patch mirate sui soli problemi localizzati (mai rigenerazione del documento).

Stesso formato di output del RefactoringPlanner (operazioni tipizzate), stessa validazione del piano.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.errors import LlmCallError
from app.llm.base import AgentRole, LlmRequest
from app.llm.structured import StructuredLlm
from app.logging_setup import get_logger
from app.model import pointer as jp
from app.model.document import SpecDocument
from app.model.refs import RefIndex
from app.prompts import load_prompt
from app.refactor import operations as ops
from app.refactor.fragments import Fragment, build_slice, fragment_content, render
from app.refactor.planner import PlanIssue, PlanValidator, RefactoringPlan, describe_rule
from app.rules.registry import RuleRegistry

log = get_logger("correction")


class Problem(BaseModel):
    """Un problema residuo da correggere, già normalizzato (validator, governance, critic, engine)."""

    source: str
    rule_id: str
    severity: str
    location: str
    message: str
    details: Any = None


class CorrectionEngine:
    def __init__(self, llm: StructuredLlm, registry: RuleRegistry, budget_tokens: int):
        self.llm = llm
        self.registry = registry
        self.budget_tokens = budget_tokens
        self.system = load_prompt("correction")
        self.validator = PlanValidator(registry)

    async def correct(self, *, iteration: int, baseline: SpecDocument, candidate: SpecDocument,
                      targets: list[tuple[Fragment, list[Problem], str]], applied: list[ops.AppliedChange],
                      known_rule_ids: set[str]) -> RefactoringPlan:
        """`targets`: (frammento nel candidato, problemi, pointer del frammento nel documento originale)."""
        plan = RefactoringPlan(iteration=iteration)
        refs = RefIndex(candidate.data)
        proposed: list[ops.PlannedOperation] = []
        for fragment, problems, original_ptr in targets:
            ptr = fragment.pointer
            original = None
            if not original_ptr or baseline.exists(original_ptr):
                original = fragment_content(baseline, fragment.model_copy(update={"pointer": original_ptr}))
            payload = {
                "originalFragment": original,
                "candidate": build_slice(candidate, fragment, refs, self.budget_tokens),
                "alreadyApplied": [c.model_dump(by_alias=True, mode="json", include={"type", "rule_id", "description"})
                                   for c in applied if any(ptr and jp.is_prefix(ptr, loc) for loc in c.locations)],
                "problems": [p.model_dump(mode="json") for p in problems],
                "rules": [describe_rule(r) for r in self.registry.for_fragment(fragment.kind, fragment.method,
                                                                               fragment.path)],
            }
            request = LlmRequest(role=AgentRole.CORRECTION, task="correct-fragment", system=self.system,
                                 user="## Correction input\n" + render(payload) + "\n\nPropose targeted operations (JSON).",
                                 context={"fragment": ptr, "iteration": iteration,
                                          "problems": [p.model_dump(mode="json") for p in problems],
                                          "original": original})
            try:
                proposal = await self.llm.generate(request, ops.OperationsProposal)
            except LlmCallError as exc:
                plan.failed_fragments.append(ptr or "/")
                plan.issues.append(PlanIssue(kind="LLM_FAILED", message=f"{fragment.label}: {exc}"))
                log.warning("[CORRECTION] %s: frammento marcato come FALLITO (%s)", fragment.label, exc)
                continue
            plan.rationales[ptr or "/"] = proposal.rationale
            proposed.extend(ops.PlannedOperation(operation=o, fragment=ptr or "/", proposed_by="correction")
                            for o in proposal.operations)
        plan.operations, issues = self.validator.validate(candidate, proposed, known_rule_ids)
        plan.issues.extend(issues)
        log.info("[CORRECTION] %d frammenti, %d operazioni proposte, %d nel piano validato",
                 len(targets), len(proposed), len(plan.operations))
        return plan

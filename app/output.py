"""OutputWriter: scrive original/, refactored/, reports/ — senza mai sovrascrivere il file sorgente."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from app.errors import ConfigurationError
from app.loader import dump_spec
from app.model.issues import count_by_severity
from app.pipeline import RunResult


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


class OutputWriter:
    def __init__(self, output_root: str | Path):
        self.output_root = Path(output_root)

    def write(self, result: RunResult) -> Path:
        source = Path(result.source.source_file).resolve()
        base = (self.output_root / result.source.api_name).resolve()
        original_dir, refactored_dir, reports_dir = base / "original", base / "refactored", base / "reports"
        targets = [original_dir / source.name, refactored_dir / source.name]
        if any(t == source for t in targets):
            raise ConfigurationError(f"La cartella di output {base} sovrascriverebbe il file sorgente: scegline un'altra")
        for d in (original_dir, refactored_dir, reports_dir):
            d.mkdir(parents=True, exist_ok=True)

        shutil.copy2(source, original_dir / source.name)
        as_json = source.suffix.lower() == ".json"
        refactored_name = source.stem + (".json" if as_json else ".yaml")
        (refactored_dir / refactored_name).write_text(dump_spec(result.final.data, as_json), encoding="utf-8")

        self._reports(result, reports_dir)
        return base

    def _reports(self, r: RunResult, reports: Path) -> None:
        registry = r.registry
        _write_json(reports / "refactoring-plan.json", {"plans": [p.dump() for p in r.plans]})
        _write_json(reports / "validation-report.json", {
            "conversion": {
                "converted": bool(r.conversion and r.conversion.converted),
                "tool": r.conversion.tool if r.conversion else None,
                "sourceVersion": r.source.source_version.value, "targetVersion": r.source.target_version.value,
                "issues": [i.model_dump() for i in (r.conversion.issues if r.conversion else [])],
            },
            "iterations": [{"iteration": it.iteration, "rejected": it.rejected,
                            "summary": count_by_severity(it.validation),
                            "violations": [v.dump() for v in it.validation]} for it in r.iterations],
            "final": {"iteration": r.final_iteration, "summary": count_by_severity(r.final_validation),
                      "violations": [v.dump() for v in r.final_validation]},
        })
        _write_json(reports / "governance-report.json", {
            "rules": {
                "naturalLanguageFiles": registry.info.natural_language_files if registry else [],
                "spectralRulesets": registry.info.spectral_rulesets if registry else [],
                "warnings": registry.info.warnings if registry else [],
                "conflicts": [c.model_dump() for c in (registry.conflicts if registry else [])],
                "compiledRules": [c.model_dump(by_alias=True, mode="json") for c in (registry.compiled if registry else [])],
                "compileCache": {"hits": getattr(r.compile_report, "cache_hits", []),
                                 "compiled": getattr(r.compile_report, "compiled_files", []),
                                 "failed": getattr(r.compile_report, "failed_rules", [])},
            },
            "baseline": {"summary": count_by_severity(r.baseline_governance),
                         "violations": [v.dump() for v in r.baseline_governance]},
            "iterations": [{"iteration": it.iteration, "rejected": it.rejected,
                            "rejectionReasons": it.rejection_reasons,
                            "newViolations": [v.dump() for v in it.new_violations],
                            "summary": count_by_severity(it.governance + it.untraced),
                            "violations": [v.dump() for v in it.governance + it.untraced]} for it in r.iterations],
            "final": {"iteration": r.final_iteration, "summary": count_by_severity(r.final_governance),
                      "violations": [v.dump() for v in r.final_governance]},
        })
        _write_json(reports / "critic-report.json", {
            "iterations": [it.critic.dump() for it in r.iterations if it.critic],
            "fragmentStates": r.fragment_states,
        })
        in_final = {id(c) for c in r.final_applied}
        _write_json(reports / "changes.json", {
            "applied": [{**c.model_dump(by_alias=True, mode="json"), "inFinalOutput": id(c) in in_final}
                        for c in r.applied],
            "semanticChanges": [c.model_dump(by_alias=True, mode="json") for c in r.final_applied
                                if c.category.value == "SEMANTIC"],
            "rejected": [c.model_dump(by_alias=True, mode="json") for c in r.rejected_changes],
            "failures": [f.model_dump(by_alias=True, mode="json") for f in r.failures],
        })
        _write_json(reports / "semantic-diff.json", {
            "summary": {"total": len(r.final_diff), "breaking": sum(c.breaking for c in r.final_diff),
                        "unexpected": sum(not c.expected for c in r.final_diff)},
            "changes": [c.dump() for c in r.final_diff],
        })
        _write_json(reports / "summary.json", {
            "status": r.status.value,
            "breakingChanges": r.breaking_changes,
            "reasons": r.reasons, "source": r.source.source_file,
            "sourceVersion": r.source.source_version.value, "targetVersion": r.source.target_version.value,
            "iterations": len(r.iterations), "finalIteration": r.final_iteration,
            "output": {"iteration": r.final_iteration, "isBaseline": r.output_is_baseline, "note": r.output_note},
            "baselineCounts": r.baseline_counts, "finalCounts": r.final_counts,
            "exitConditions": [{"iteration": it.iteration, "rejected": it.rejected,
                                **it.exit_conditions.model_dump(by_alias=True)} for it in r.iterations],
            "rejectedIterations": [{"iteration": it.iteration, "reasons": it.rejection_reasons}
                                   for it in r.iterations if it.rejected],
            "finalValidation": count_by_severity(r.final_validation),
            "finalGovernance": count_by_severity(r.final_governance),
            "compileFailedRules": r.compile_failed_rules,
            "timings": r.timings,
            "llmByRole": r.llm_stats,
            "llmCalls": r.llm_calls, "elapsedSeconds": r.elapsed_seconds,
        })

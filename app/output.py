"""OutputWriter: scrive original/, refactored/, reports/ — senza mai sovrascrivere il file sorgente."""

from __future__ import annotations

import csv
import json
import re
import shutil
from pathlib import Path
from typing import Any

from app.errors import ConfigurationError
from app.loader import dump_spec
from app.model import pointer as jp
from app.model.issues import count_by_severity
from app.pipeline import RunResult


ACRONYM = re.compile(r"[A-Z]{2,}")
RENAME_COLUMNS = ["element", "location", "from", "to", "ruleId", "proposedBy", "iteration", "needsReview",
                  "reviewReason"]


def _container_renames(change) -> list[tuple[str, str, str]]:
    """Rinomine che spostano altri elementi: (tipo, vecchio, nuovo) per schema e chiavi di path."""
    r = change.rename or {}
    if r.get("element") == "schema":
        return [("schemas", r["from"], r["to"])]
    if r.get("element") == "path":
        return [("paths", r["from"], r["to"])]
    if r.get("pathFrom"):
        return [("paths", r["pathFrom"], r["pathTo"])]
    return []


def _to_final(location: str, later: list[tuple[str, str, str]]) -> str:
    """Porta una posizione alle coordinate del documento finale applicando le rinomine successive."""
    tokens = jp.split(location)
    for kind, old, new in later:
        if kind == "schemas" and tokens[:3] == ["components", "schemas", old]:
            tokens[2] = new
        elif kind == "paths" and tokens[:2] == ["paths", old]:
            tokens[1] = new
    return jp.join(tokens)


def rename_map(result: RunResult) -> list[dict[str, Any]]:
    """Rinomine incluse nell'output, con la posizione nel documento finale. Un nome originale con un acronimo
    (2+ maiuscole consecutive) è marcato per revisione: la conversione lo ricompone per parole
    (es. payer_IBAN -> payerIban), e va verificato che il risultato sia quello voluto (IBAN, HTTP, ID, ...)."""
    rows = []
    applied = result.final_applied
    for i, c in enumerate(applied):
        if not c.rename:
            continue
        later = [m for other in applied[i + 1:] for m in _container_renames(other)]
        info = {k: v for k, v in c.rename.items() if k not in ("pathFrom", "pathTo")}
        info["location"] = _to_final(info["location"], later)
        acronyms = sorted(set(ACRONYM.findall(c.rename["from"])))
        rows.append({**info, "ruleId": c.rule_id, "proposedBy": c.proposed_by, "iteration": c.iteration,
                     "needsReview": bool(acronyms),
                     "reviewReason": f"acronimo nel nome originale: {', '.join(acronyms)}" if acronyms else ""})
    return rows


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
        renames = rename_map(r)
        _write_json(reports / "renames.json", {
            "summary": {"total": len(renames), "needsReview": sum(x["needsReview"] for x in renames)},
            "renames": renames,
        })
        with open(reports / "renames.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=RENAME_COLUMNS)
            writer.writeheader()
            writer.writerows(renames)
        _write_json(reports / "semantic-diff.json", {
            "summary": {"total": len(r.final_diff), "breaking": sum(c.breaking for c in r.final_diff),
                        "unexpected": sum(not c.expected for c in r.final_diff)},
            "changes": [c.dump() for c in r.final_diff],
        })
        _write_json(reports / "summary.json", {
            "status": r.status.value,
            "breakingChanges": r.breaking_changes,
            "apiLifecycle": r.api_lifecycle,
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
            "renames": {"total": len(renames), "needsReview": sum(x["needsReview"] for x in renames)},
            "timings": r.timings,
            "llmByRole": r.llm_stats,
            "llmCalls": r.llm_calls, "elapsedSeconds": r.elapsed_seconds,
            "resume": {"resumed": r.resumed, "replayedLlmCalls": r.replayed_llm_calls},
        })

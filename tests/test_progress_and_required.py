"""required ereditati via allOf, required orfani, esempi annidati, riepilogo baseline per regola, avanzamento."""

from __future__ import annotations

import copy
import json
import re
import time

import pytest

from app.cli import main
from app.loader import load_spec
from app.model.document import SpecDocument, SpecVersion
from app.output import OutputWriter, rename_map
from app.pipeline import RefactorPipeline
from app.progress import ProgressTracker, render_status
from app.refactor.engine import RefactoringEngine
from app.refactor.operations import OperationsProposal, PlannedOperation
from app.validators.structural import RULE_ID, required_orphans
from tests.conftest import APIS

A = "#/components/schemas/Order"


def doc(schemas: dict, paths: dict | None = None) -> SpecDocument:
    return SpecDocument(source_file="t.yaml", source_version=SpecVersion.OPENAPI_3_0,
                        target_version=SpecVersion.OPENAPI_3_0,
                        data={"openapi": "3.0.3", "info": {"title": "x", "version": "1.0.0"},
                              "paths": copy.deepcopy(paths or {}), "components": {"schemas": copy.deepcopy(schemas)}})


def rename(base: SpecDocument, schema: str, old: str, new: str):
    ops = OperationsProposal.model_validate({"operations": [
        {"type": "RENAME_PROPERTY", "target": f"/components/schemas/{schema}", "from": old, "to": new,
         "ruleId": "R"}]}).operations
    return RefactoringEngine().apply(base, [PlannedOperation(operation=o) for o in ops])


# ── 1. required ereditati via allOf + required orfani ────────────────────
INHERIT = {
    "Order": {"type": "object", "properties": {"order_total": {"type": "number"}, "note": {"type": "string"}}},
    "PaidOrder": {"allOf": [{"$ref": A}], "required": ["order_total"]},  # diretto
    "Refund": {"allOf": [{"$ref": "#/components/schemas/PaidOrder"},  # transitivo + membro inline
                         {"type": "object", "required": ["order_total", "note"]}]},
    "Legacy": {"allOf": [{"$ref": A}, {"properties": {"order_total": {"type": "string"}}}],
               "required": ["order_total"]},  # dichiarato anche da un altro membro: non va toccato
}


def test_rename_updates_required_inherited_via_allof_directly_and_transitively():
    new, applied, failures = rename(doc(INHERIT), "Order", "order_total", "orderTotal")
    assert not failures
    schemas = new.data["components"]["schemas"]
    assert schemas["PaidOrder"]["required"] == ["orderTotal"]
    assert schemas["Refund"]["allOf"][1]["required"] == ["orderTotal", "note"]
    assert schemas["Legacy"]["required"] == ["order_total"]
    assert required_orphans(new) == []
    assert "required" in applied[0].description


def test_orphan_required_is_an_error():
    base = doc(INHERIT)
    new, _, _ = rename(base, "Order", "order_total", "orderTotal")
    new.data["components"]["schemas"]["PaidOrder"]["required"] = ["order_total"]  # rinomina non propagata
    [orphan] = required_orphans(new)
    assert orphan.rule_id == RULE_ID and orphan.severity == "ERROR"
    assert orphan.path == "/components/schemas/PaidOrder/required/0" and orphan.actual == "order_total"
    assert required_orphans(base) == []  # prima della rinomina il required era valido


def test_required_orphan_check_has_no_false_positives_on_the_cases():
    for spec in sorted(APIS.glob("case-00[2-5]*.yaml")):
        assert required_orphans(load_spec(spec)) == [], spec.name


# ── 2. esempi annidati ──────────────────────────────────────────────────
NESTED = {
    "LineItem": {"type": "object", "properties": {"unit_price": {"type": "number"}, "sku": {"type": "string"}}},
    "Order": {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"$ref": "#/components/schemas/LineItem"}},
        "extra": {"type": "object", "additionalProperties": {"$ref": "#/components/schemas/LineItem"}}},
        "example": {"lines": [{"unit_price": 1, "sku": "a"}], "extra": {"k": {"unit_price": 2}}}},
    "Payment": {"oneOf": [{"$ref": "#/components/schemas/LineItem"}, {"type": "object"}],
                "example": {"unit_price": 3}},
}
PATHS = {"/orders": {"get": {"responses": {"200": {"description": "ok", "content": {"application/json": {
    "schema": {"$ref": A},
    "examples": {"one": {"value": {"lines": [{"unit_price": 4}]}},
                 "shared": {"$ref": "#/components/examples/SharedOrder"},
                 "remote": {"externalValue": "https://example.com/order.json"}}}}}}}}}


def test_nested_examples_follow_the_schema_and_undeterminable_ones_are_flagged():
    base = doc(NESTED, PATHS)
    base.data["components"]["examples"] = {"SharedOrder": {"value": {"lines": [{"unit_price": 5}]}}}
    new, applied, failures = rename(base, "LineItem", "unit_price", "unitPrice")
    assert not failures
    order = new.data["components"]["schemas"]["Order"]["example"]
    assert order == {"lines": [{"unitPrice": 1, "sku": "a"}], "extra": {"k": {"unitPrice": 2}}}
    media = new.data["paths"]["/orders"]["get"]["responses"]["200"]["content"]["application/json"]
    assert media["examples"]["one"]["value"] == {"lines": [{"unitPrice": 4}]}
    assert new.data["components"]["examples"]["SharedOrder"]["value"] == {"lines": [{"unitPrice": 5}]}
    # oneOf: il ramo non è determinabile -> non toccato, segnalato; externalValue: non aggiornabile, segnalato
    assert new.data["components"]["schemas"]["Payment"]["example"] == {"unit_price": 3}
    review = applied[0].rename["examplesToReview"]
    assert "/components/schemas/Payment/example" in review
    assert any(p.endswith("/examples/remote/externalValue") for p in review)


def test_rename_map_marks_stale_examples_for_review():
    class R:  # RunResult minimale per rename_map
        pass
    base = doc(NESTED, PATHS)
    new, applied, _ = rename(base, "LineItem", "unit_price", "unitPrice")
    result = R()
    result.final_applied = applied
    [row] = rename_map(result)
    assert row["needsReview"] and "esempi non aggiornabili" in row["reviewReason"]
    assert "/components/schemas/Payment/example" in row["examplesToReview"]


# ── 4. riepilogo della baseline per regola ───────────────────────────────
@pytest.mark.needs_node
async def test_baseline_summary_by_rule_in_log_and_summary(config, agents, rules_dir, tmp_path, caplog):
    caplog.set_level("INFO", logger="app")
    result = await RefactorPipeline(config, agents.provider(), rules_dir).run(APIS / "case-003-no-problem-details.yaml")
    summary = json.loads((OutputWriter(tmp_path / "o").write(result) / "reports" / "summary.json").read_text())
    by_rule = {r["ruleId"]: r for r in summary["baselineByRule"]["rules"]}
    assert by_rule["ERR-001"] == {"ruleId": "ERR-001", "severity": "ERROR", "total": 2, "deterministic": 2,
                                  "llm": 0, "reevaluated": 0}
    assert by_rule["DE-STATUS-002-problem-json-errors"]["reevaluated"] == 2  # response riscritte da ERR-001
    assert summary["baselineByRule"]["totals"] == {"total": 4, "deterministic": 2, "llm": 0, "reevaluated": 2}
    assert summary["baselineByRule"]["llmFragments"] == 0
    totals = [r["total"] for r in summary["baselineByRule"]["rules"]]
    assert totals == sorted(totals, reverse=True)
    lines = [r.getMessage() for r in caplog.records if "[BASELINE]" in r.getMessage()]
    assert lines and "TOTALE" in lines[-2] and "frammenti all'LLM: 0" in lines[-1]
    # la tabella è stampata prima della pianificazione, cioè prima di ogni chiamata LLM di refactoring
    first_engine = next(i for i, r in enumerate(caplog.records) if r.getMessage().startswith("[ENGINE]"))
    assert max(i for i, r in enumerate(caplog.records) if "[BASELINE]" in r.getMessage()) < first_engine


# ── 5. avanzamento ──────────────────────────────────────────────────────
class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_progress_eta_excludes_checkpoint_replays_and_file_is_atomic(tmp_path, caplog):
    caplog.set_level("INFO", logger="app")
    clock = Clock()
    path = tmp_path / "reports" / "progress.json"
    tracker = ProgressTracker(path, source="api.yaml", max_iterations=3, stale_after_seconds=600, clock=clock)
    tracker.set_iteration(1)
    tracker.set_phase("plan", total=5, announce=True)
    tracker.fragment_done("/paths/~1a/get", 0.01, replayed=True)  # da checkpoint: esclusa dalla media
    clock.now += 60
    tracker.fragment_done("/paths/~1b/get", 60.0)
    clock.now += 120
    tracker.fragment_done("/paths/~1c/get", 120.0)
    data = json.loads(path.read_text())
    assert not list(path.parent.glob("*.tmp"))  # scrittura atomica: nessun temporaneo residuo
    assert data["phase"] == "plan" and data["iteration"] == 1
    assert data["fragments"] == {"done": 3, "total": 5, "fromCheckpoint": 1, "failed": 0, "skipped": 0}
    assert data["phaseEtaSeconds"] == 180.0  # 2 rimanenti x media 90s (la replay non abbassa la media)
    assert data["phaseElapsedSeconds"] == 180.0
    messages = [r.getMessage() for r in caplog.records]
    assert "[PLAN] 5 frammenti da elaborare" in messages
    assert re.search(r"^\[PLAN\] 1/5 /paths/~1a/get: da checkpoint \| trascorso 0s \| stima fine fase n/d$",
                     "\n".join(messages), re.M)
    assert re.search(r"^\[PLAN\] 3/5 /paths/~1c/get: 120\.0s \| trascorso 3m00s \| stima fine fase \d\d:\d\d$",
                     "\n".join(messages), re.M)


@pytest.mark.needs_node
async def test_progress_file_during_and_after_a_run(config, agents, rules_dir, tmp_path, caplog):
    caplog.set_level("INFO", logger="app")
    path = tmp_path / "reports" / "progress.json"
    seen: list[dict] = []
    original = agents.critic

    def critic_peeking(request):  # legge progress.json a run in corso, come farebbe `status`
        seen.append(json.loads(path.read_text()))
        return original(request)

    agents.critic = critic_peeking
    result = await RefactorPipeline(config, agents.provider(), rules_dir).run(
        APIS / "case-001-swagger2-legacy.yaml", progress_path=path)
    assert seen and all(s["status"] == "running" and s["phase"] == "critic" for s in seen)
    assert [s["fragments"]["done"] for s in seen] == list(range(len(seen)))
    final = json.loads(path.read_text())
    assert final["status"] == result.status.value and final["phase"] == "done"
    assert final["llm"]["calls"] == result.llm_calls
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "[PLAN] 4 frammenti da elaborare" in messages
    assert re.search(r"\[PLAN\] 4/4 \S+: [\d.]+s \| trascorso \S+ \| stima fine fase completata", messages)
    assert "---- Iterazione 1/3 ----" in messages


@pytest.mark.needs_node
async def test_progress_counts_checkpoint_replays_on_resume(config, agents, rules_dir, tmp_path, caplog):
    spec = APIS / "case-001-swagger2-legacy.yaml"
    checkpoint, path = tmp_path / "ckpt", tmp_path / "reports" / "progress.json"
    calls = {"n": 0}
    original = agents.refactor

    def crashing(request):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("interruzione simulata")
        return original(request)

    agents.refactor = crashing
    with pytest.raises(RuntimeError):
        await RefactorPipeline(config, agents.provider(), rules_dir).run(spec, checkpoint, progress_path=path)
    interrupted = json.loads(path.read_text())
    assert interrupted["status"] == "running" and interrupted["fragments"]["done"] == 2

    agents.refactor = original
    caplog.clear()
    caplog.set_level("INFO", logger="app")
    await RefactorPipeline(config, agents.provider(), rules_dir).run(spec, checkpoint, resume=True,
                                                                     progress_path=path)
    plan_lines = [r.getMessage() for r in caplog.records if re.match(r"\[PLAN\] \d/4 ", r.getMessage())]
    assert [("da checkpoint" in m) for m in plan_lines] == [True, True, False, False]
    final = json.loads(path.read_text())
    # le 2 pianificazioni già fatte; le regole compilate vengono dalla loro cache, non dal modello né dal checkpoint
    assert final["llm"]["fromCheckpoint"] == 2


def write_progress(tmp_path, **overrides) -> str:
    base = {"source": "apis/psp.yaml", "status": "running", "phase": "plan", "phaseName": "pianificazione",
            "iteration": 1, "maxIterations": 3,
            "fragments": {"done": 12, "total": 73, "fromCheckpoint": 2, "failed": 1, "skipped": 0},
            "llm": {"calls": 30, "failed": 1, "fromCheckpoint": 2}, "runElapsedSeconds": 3725.0,
            "phaseElapsedSeconds": 580.0, "phaseEta": "2026-09-25T16:52:00", "phaseEtaSeconds": 2940.0,
            "lastUpdate": "2026-09-25T16:03:10", "lastUpdateEpoch": time.time(), "staleAfterSeconds": 600}
    base.update(overrides)
    folder = tmp_path / "output" / "psp"
    (folder / "reports").mkdir(parents=True)
    (folder / "reports" / "progress.json").write_text(json.dumps(base))
    return str(folder)


def test_status_command_prints_a_readable_summary(tmp_path, capsys):
    assert main(["status", "--output", write_progress(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Run: apis/psp.yaml — in corso" in out
    assert "Iterazione: 1/3   Fase: pianificazione" in out
    assert "Frammenti: 12/73 (2 da checkpoint, 1 falliti) | trascorso fase 9m40s | stima fine fase 16:52" in out
    assert "Chiamate LLM: 30 (1 fallite, 2 da checkpoint)" in out and "Tempo totale: 1h02m" in out
    assert "nessun avanzamento" not in out


def test_status_command_flags_a_stale_run_and_handles_missing_files(tmp_path, capsys):
    folder = write_progress(tmp_path, lastUpdateEpoch=time.time() - 1500)
    assert main(["status", "--output", folder]) == 0
    assert "nessun avanzamento da 25m" in capsys.readouterr().out
    assert main(["status", "--output", str(tmp_path / "nope")]) == 1
    assert "Nessun file di stato" in capsys.readouterr().out


def test_status_watch_stops_when_the_run_is_over(tmp_path, capsys, monkeypatch):
    folder = write_progress(tmp_path)
    path = f"{folder}/reports/progress.json"

    def finish_run(seconds):  # durante l'attesa la run termina
        data = json.loads(open(path).read())
        data.update(status="SUCCESS", phase="done")
        open(path, "w").write(json.dumps(data))

    monkeypatch.setattr(time, "sleep", finish_run)
    assert main(["status", "--output", folder, "--watch"]) == 0
    out = capsys.readouterr().out
    assert "in corso" in out and "terminata: SUCCESS" in out


def test_render_status_for_a_finished_run():
    text = render_status({"source": "a.yaml", "status": "NEEDS_REVIEW", "phase": "done", "phaseName": "terminata",
                          "iteration": 3, "maxIterations": 3,
                          "fragments": {"done": 0, "total": 0, "fromCheckpoint": 0, "failed": 0, "skipped": 0},
                          "llm": {"calls": 5, "failed": 0, "fromCheckpoint": 0}, "runElapsedSeconds": 12,
                          "phaseElapsedSeconds": 0, "lastUpdate": "2026-09-25T10:00:00", "lastUpdateEpoch": 0,
                          "staleAfterSeconds": 1})
    assert "terminata: NEEDS_REVIEW" in text and "nessun avanzamento" not in text

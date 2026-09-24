"""Schema JSON inviato all'LLM: i discriminatori delle union devono essere obbligatori e in testa.

La grammatica di Ollama emette prima i campi `required`: se `kind`/`type` non lo sono, il modello può
scegliere solo le varianti senza altri campi obbligatori (regressione osservata con Ollama reale).
"""

from __future__ import annotations

import ast
import io
import logging
import sys
from pathlib import Path
from typing import Any, Iterator

import jsonschema
import pytest

from app.critic import CriticVerdict
from app.llm.structured import DISCRIMINATORS, llm_json_schema
from app.logging_setup import configure_logging, get_logger
from app.pipeline import RefactorPipeline
from app.refactor.operations import OperationsProposal
from app.rules.models import CompiledRuleDraft
from tests.conftest import APIS, ROOT


def discriminated_objects(node: Any) -> Iterator[tuple[str, dict]]:
    """(nome del discriminatore, oggetto) per ogni oggetto dello schema con una proprietà kind/type costante."""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            for name in DISCRIMINATORS:
                if isinstance(props.get(name), dict) and "const" in props[name]:
                    yield name, node
        for value in node.values():
            yield from discriminated_objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from discriminated_objects(value)


def assert_discriminators_first(schema: dict) -> int:
    found = list(discriminated_objects(schema))
    for name, obj in found:
        assert obj.get("required", [])[:1] == [name], f"{obj.get('title')}: {name} non è il primo di required"
        assert next(iter(obj["properties"])) == name, f"{obj.get('title')}: {name} non è la prima proprietà"
    return len(found)


@pytest.mark.parametrize("model, variants", [(CompiledRuleDraft, 8), (OperationsProposal, 17), (CriticVerdict, 0)])
def test_every_variant_requires_its_discriminator_first(model, variants):
    assert assert_discriminators_first(llm_json_schema(model)) == variants


def test_fixed_schema_still_accepts_valid_payloads_and_rejects_missing_discriminator():
    schema = llm_json_schema(CompiledRuleDraft)
    good = {"scope": "operation", "requirements": [{"kind": "requireSecurity", "schemeType": "http",
                                                    "scheme": "bearer"}]}
    jsonschema.validate(good, schema)
    CompiledRuleDraft.model_validate(good)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"scope": "operation", "requirements": [{"schemeType": "http"}]}, schema)

    ops = llm_json_schema(OperationsProposal)
    jsonschema.validate({"operations": [{"type": "ADD_HEADER", "ruleId": "R", "target": "/paths/~1a/post",
                                         "header": "Idempotency-Key"}]}, ops)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"operations": [{"ruleId": "R", "target": "/paths/~1a/post", "operationId": "x"}]}, ops)


@pytest.mark.needs_node
async def test_every_schema_sent_during_a_run_has_discriminators_first(config, agents, rules_dir):
    provider = agents.provider()
    # caso 002: il nameCasing non ha correzione deterministica, quindi Refactor Agent e Critic vengono chiamati
    await RefactorPipeline(config, provider, rules_dir).run(APIS / "case-002-naming.yaml")
    titles = {s.get("title") for s in provider.schemas}
    assert {"CompiledRuleDraft", "OperationsProposal", "CriticVerdict"} <= titles
    assert sum(assert_discriminators_first(s) for s in provider.schemas) > 0


def test_logging_survives_a_cp1252_console(monkeypatch):
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)
    errors: list = []
    monkeypatch.setattr(logging.Handler, "handleError", lambda self, record: errors.append(record))
    logger = logging.getLogger("app")
    saved_handlers, saved_level = logger.handlers[:], logger.level
    try:
        configure_logging("DEBUG")
        get_logger("test").debug("[LLM] <- risposta con caratteri fuori cp1252: → ✓ 漢")
        console.flush()
    finally:
        # ripristina gli handler originali: riconfigurare qui legherebbe il logger a uno stream di test poi chiuso
        logger.handlers[:] = saved_handlers
        logger.setLevel(saved_level)
        monkeypatch.undo()
    assert not errors
    text = raw.getvalue().decode("cp1252")
    assert "[LLM] <- risposta" in text and "?" in text


def test_log_and_print_literals_are_cp1252_encodable():
    offenders = []
    for file in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in ("debug", "info", "warning", "error", "exception", "print"):
                continue
            for arg in ast.walk(node.args[0]):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    try:
                        arg.value.encode("cp1252")
                    except UnicodeEncodeError:
                        offenders.append(f"{Path(file).relative_to(ROOT)}:{node.lineno}: {arg.value[:60]!r}")
    assert not offenders, "\n".join(offenders)

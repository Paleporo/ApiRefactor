"""Upgrade di versione verso il target OpenAPI.

Decisione esplicita: Swagger 2.0 -> OpenAPI 3.0 usa il CLI Node `swagger2openapi` (processo esterno),
perché in Python non esiste un convertitore maturo quanto quello (Node è già richiesto per Spectral).
OpenAPI 3.0 -> 3.1 è un passo deterministico in Python (nullable -> type array, versione).
Warning/errori della conversione non vengono mai ignorati: finiscono nel validation report.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.config import AppConfig
from app.errors import ExternalToolError
from app.external import require_tool, run_process
from app.logging_setup import get_logger
from app.model import pointer as jp
from app.model.document import SpecDocument, SpecVersion

log = get_logger("converter")

WARNING_EXTENSION = "x-s2o-warning"


class ConversionIssue(BaseModel):
    severity: str  # WARNING | ERROR
    location: str
    message: str


class ConversionResult(BaseModel):
    document: SpecDocument
    converted: bool
    tool: str | None = None
    issues: list[ConversionIssue] = []


def _collect_s2o_warnings(node: Any, pointer: str, out: list[ConversionIssue]) -> None:
    """swagger2openapi --warnOnly marca i problemi non patchabili con `x-s2o-warning`: li raccoglie e li rimuove."""
    if isinstance(node, dict):
        if WARNING_EXTENSION in node:
            out.append(ConversionIssue(severity="WARNING", location=pointer, message=str(node.pop(WARNING_EXTENSION))))
        for key, value in node.items():
            _collect_s2o_warnings(value, jp.child(pointer, key), out)
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            _collect_s2o_warnings(value, jp.child(pointer, idx), out)


def _swagger2_to_openapi3(doc: SpecDocument, config: AppConfig) -> ConversionResult:
    tool = require_tool(
        config.swagger2openapi_command,
        "Installa con: npm install -g swagger2openapi (oppure `npm install` nella root del progetto).",
    )
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "source.json"
        out = Path(tmp) / "converted.json"
        src.write_text(json.dumps(doc.data), encoding="utf-8")
        result = run_process(
            [tool, "--patch", "--warnOnly", "--outfile", str(out), str(src)], config.external_tool_timeout_seconds
        )
        issues: list[ConversionIssue] = []
        stderr = "\n".join(line for line in result.stderr.splitlines() if line.strip())
        if result.returncode != 0 or not out.exists():
            raise ExternalToolError(
                f"swagger2openapi ha fallito la conversione di {doc.source_file} (exit {result.returncode}): "
                f"{stderr or result.stdout or 'nessun output'}"
            )
        if stderr:
            issues.append(ConversionIssue(severity="WARNING", location="", message=f"swagger2openapi stderr: {stderr}"))
        try:
            converted = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ExternalToolError(f"swagger2openapi ha prodotto JSON non valido: {exc}") from None
    _collect_s2o_warnings(converted, "", issues)
    new_doc = SpecDocument(
        source_file=doc.source_file,
        source_version=doc.source_version,
        target_version=doc.target_version,
        data=converted,
    )
    return ConversionResult(document=new_doc, converted=True, tool="swagger2openapi", issues=issues)


def _nullable_to_type_array(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("nullable") is True and isinstance(node.get("type"), str):
            node["type"] = [node["type"], "null"]
            del node["nullable"]
        elif "nullable" in node and node.get("nullable") is False:
            del node["nullable"]
        for value in node.values():
            _nullable_to_type_array(value)
    elif isinstance(node, list):
        for value in node:
            _nullable_to_type_array(value)


def _openapi30_to_31(doc: SpecDocument) -> ConversionResult:
    new_doc = doc.clone()
    new_doc.data["openapi"] = "3.1.0"
    _nullable_to_type_array(new_doc.data)
    return ConversionResult(document=new_doc, converted=True, tool="builtin-3.0-to-3.1")


def upgrade_to_target(doc: SpecDocument, config: AppConfig) -> ConversionResult:
    """Porta il documento alla versione target. Ritorna il documento (eventualmente) convertito + issue di conversione."""
    target = doc.target_version
    if doc.source_version == SpecVersion.SWAGGER_2_0:
        log.info("[LOAD] Upgrade Swagger 2.0 -> OpenAPI 3.0 via swagger2openapi")
        result = _swagger2_to_openapi3(doc, config)
        if target == SpecVersion.OPENAPI_3_1:
            upgraded = _openapi30_to_31(result.document)
            upgraded.issues = result.issues
            upgraded.tool = "swagger2openapi+builtin-3.0-to-3.1"
            result = upgraded
        log.info("[LOAD] Conversione completata: %d warning", len(result.issues))
        return result
    if doc.source_version == SpecVersion.OPENAPI_3_0 and target == SpecVersion.OPENAPI_3_1:
        log.info("[LOAD] Upgrade OpenAPI 3.0 -> 3.1 (builtin)")
        return _openapi30_to_31(doc)
    return ConversionResult(document=doc.clone(), converted=False)

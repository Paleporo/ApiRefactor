"""Wrapper del CLI Spectral: `spectral lint --ruleset <file> <spec> --format json`, output convertito in Violation."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.config import AppConfig
from app.errors import ExternalToolError
from app.external import find_tool, require_tool, run_process
from app.logging_setup import get_logger
from app.model import pointer as jp
from app.model.document import HTTP_METHODS, SpecDocument
from app.model.issues import Violation, ViolationSource
from app.rules.loader import spectral_severity
from app.rules.models import SpectralRule

log = get_logger("governance")

INSTALL_HINT = "Installa con: npm install -g @stoplight/spectral-cli (oppure `npm install` nella root del progetto)."


def spectral_available(config: AppConfig) -> bool:
    return find_tool(config.spectral_command) is not None


def _operation_id(doc: SpecDocument, pointer: str) -> str | None:
    tokens = jp.split(pointer)
    if len(tokens) >= 3 and tokens[0] == "paths" and tokens[2] in HTTP_METHODS:
        op = doc.get(jp.join(tokens[:3]))
        if isinstance(op, dict):
            return op.get("operationId")
    return None


class SpectralRunner:
    def __init__(self, config: AppConfig, rules: list[SpectralRule]):
        self.config = config
        self.rules = {r.id: r for r in rules}
        self.rulesets = sorted({r.ruleset_file for r in rules})

    def lint(self, doc: SpecDocument) -> list[Violation]:
        if not self.rulesets:
            return []
        tool = require_tool(self.config.spectral_command, INSTALL_HINT)
        violations: list[Violation] = []
        with tempfile.TemporaryDirectory() as tmp:
            spec_file = Path(tmp) / "candidate.json"
            spec_file.write_text(json.dumps(doc.data, ensure_ascii=False), encoding="utf-8")
            for ruleset in self.rulesets:
                result = run_process(
                    [tool, "lint", "--ruleset", str(Path(ruleset).resolve()), "--format", "json", "--quiet",
                     str(spec_file)],
                    self.config.external_tool_timeout_seconds,
                )
                violations.extend(self._parse(result.stdout, result.stderr, result.returncode, doc, ruleset))
        return violations

    def _parse(self, stdout: str, stderr: str, code: int, doc: SpecDocument, ruleset: str) -> list[Violation]:
        text = stdout.strip()
        start = text.find("[")
        try:
            results = json.loads(text[start:]) if start != -1 else None
        except json.JSONDecodeError:
            results = None
        if results is None:
            if code == 0 and not text:
                return []
            raise ExternalToolError(f"Output di Spectral non parsabile (ruleset {ruleset}, exit {code}): "
                                    f"{(stderr or stdout).strip()[:1000]}")
        out = []
        for item in results:
            rule_id = str(item.get("code", "spectral-unknown"))
            path = jp.from_spectral_path(item.get("path") or [])
            rule = self.rules.get(rule_id)
            tokens = jp.split(path)
            out.append(Violation(
                rule_id=rule_id,
                severity=spectral_severity(item.get("severity", 1)),
                file=doc.source_file,
                path=path,
                operation_id=_operation_id(doc, path),
                message=str(item.get("message", "")),
                expected=rule.description if rule else None,
                actual=tokens[-1] if tokens else None,
                suggested_fix=(rule.description if rule else None),
                source=ViolationSource.SPECTRAL,
            ))
        return out

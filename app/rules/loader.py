"""RuleLoader: legge le regole in linguaggio naturale (rules/*.md) e i ruleset Spectral (rules/spectral/*.spectral.yaml)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.errors import ConfigurationError
from app.model.issues import Severity
from app.rules.models import RuleScope, RuleSource, SpectralRule

SPECTRAL_DIR = "spectral"
SPECTRAL_SUFFIX = ".spectral.yaml"
_EXCLUDED_MD = {"readme.md"}

_ITEM = re.compile(r"^(?P<indent>\s*)(?:[-*+]|\d+[.)])\s+(?P<body>.+)$")
_EXPLICIT_ID = re.compile(r"^(?:\[(?P<a>[A-Za-z0-9][A-Za-z0-9_.-]*)\]|\*\*(?P<b>[A-Za-z0-9][A-Za-z0-9_.-]*)\*\*:?)\s*")
_HEADING = re.compile(r"^#{1,6}\s+(?P<title>.+)$")


def natural_language_rule_files(rules_dir: Path) -> list[Path]:
    """File .md di primo livello in rules/ (le sottocartelle, es. spectral/, contengono solo documentazione)."""
    if not rules_dir.is_dir():
        return []
    return sorted(p for p in rules_dir.glob("*.md") if p.name.lower() not in _EXCLUDED_MD)


def parse_markdown_rules(file: Path) -> list[RuleSource]:
    """Ogni voce di elenco di primo livello è una regola; le righe indentate successive la continuano.

    ID: esplicito con `[ID]` o `**ID**:` in testa alla voce, altrimenti `<NOMEFILE>-<NNN>` stabile per posizione.
    """
    stem = re.sub(r"[^A-Za-z0-9]+", "-", file.stem).strip("-").upper()
    rules: list[RuleSource] = []
    section: str | None = None
    current: dict | None = None

    def flush() -> None:
        if current:
            text = " ".join(current["lines"]).strip()
            rid = current["id"] or f"{stem}-{len(rules) + 1:03d}"
            rules.append(RuleSource(id=rid, text=text, file=str(file), line=current["line"], section=section))

    for lineno, raw in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
        if heading := _HEADING.match(raw.strip()):
            flush()
            current = None
            section = heading.group("title").strip()
            continue
        item = _ITEM.match(raw)
        if item and len(item.group("indent")) == 0:
            flush()
            body = item.group("body").strip()
            explicit = _EXPLICIT_ID.match(body)
            rid = None
            if explicit:
                rid = explicit.group("a") or explicit.group("b")
                body = body[explicit.end():]
            current = {"id": rid, "lines": [body], "line": lineno}
        elif current and raw.strip() and raw.startswith((" ", "\t")):
            current["lines"].append(raw.strip())
        elif not raw.strip():
            continue
        else:
            flush()
            current = None
    flush()
    return rules


_SEVERITY = {"error": Severity.ERROR, "warn": Severity.WARNING, "info": Severity.INFO, "hint": Severity.INFO,
             0: Severity.ERROR, 1: Severity.WARNING, 2: Severity.INFO, 3: Severity.INFO}


def infer_spectral_scope(given: str) -> tuple[RuleScope, list[str] | None]:
    """Scope di una regola Spectral dedotto dalla sua espressione `given` (JSONPath)."""
    g = given.replace(" ", "")
    if m := re.fullmatch(r"\$\.paths\[\*\]\.(get|put|post|delete|patch|head|options|trace)", g):
        return RuleScope.OPERATION, [m.group(1)]
    if g.startswith("$.info") or g.startswith("$.servers") or g in ("$", "$.security"):
        return RuleScope.DOCUMENT, None
    if g.startswith("$.paths[*]~"):
        return RuleScope.PATH, None
    if "responses" in g:
        return RuleScope.RESPONSE, None
    if "parameters" in g:
        return RuleScope.PARAMETER, None
    if "properties" in g:
        return RuleScope.PROPERTY, None
    return RuleScope.ANY, None


def spectral_ruleset_files(rules_dir: Path) -> list[Path]:
    folder = rules_dir / SPECTRAL_DIR
    return sorted(folder.glob(f"*{SPECTRAL_SUFFIX}")) if folder.is_dir() else []


def load_spectral_rules(ruleset: Path) -> list[SpectralRule]:
    try:
        raw = yaml.safe_load(ruleset.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Ruleset Spectral non valido ({ruleset}): {exc}") from exc
    rules = []
    for rule_id, spec in (raw.get("rules") or {}).items():
        if not isinstance(spec, dict):
            continue  # es. `rule-name: off` per disattivare regole ereditate
        given = spec.get("given", "$")
        given = given[0] if isinstance(given, list) else given
        then = spec.get("then") or {}
        then = then[0] if isinstance(then, list) else then
        scope, methods = infer_spectral_scope(str(given))
        rules.append(
            SpectralRule(
                id=str(rule_id),
                description=spec.get("description", ""),
                message=spec.get("message"),
                severity=_SEVERITY.get(spec.get("severity", "warn"), Severity.WARNING),
                given=str(given),
                scope=scope,
                methods=methods,
                ruleset_file=str(ruleset),
                function=then.get("function"),
                function_options=then.get("functionOptions"),
            )
        )
    return rules


def spectral_severity(value: int | str) -> Severity:
    return _SEVERITY.get(value, Severity.WARNING)

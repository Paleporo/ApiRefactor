"""InputLoader + SpecificationParser: file JSON/YAML -> SpecDocument (Object Model)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from app.errors import EmptySpecError, SpecParseError, UnsupportedSpecError
from app.logging_setup import get_logger
from app.model.document import SpecDocument, SpecVersion, detect_version

log = get_logger("loader")


class _SpecYamlLoader(yaml.SafeLoader):
    """SafeLoader che non converte date/timestamp: in una spec `2024-01-01` resta una stringa."""


_SpecYamlLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _stringify_keys(node: Any) -> Any:
    """Le chiavi YAML come `200:` diventano int: nell'Object Model tutte le chiavi sono stringhe."""
    if isinstance(node, dict):
        return {str(k): _stringify_keys(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_stringify_keys(v) for v in node]
    return node


def parse_text(text: str, file: str) -> dict[str, Any]:
    suffix = Path(file).suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SpecParseError(file, exc.msg, exc.lineno, exc.colno) from None
    else:
        try:
            data = yaml.load(text, Loader=_SpecYamlLoader)  # noqa: S506 - SafeLoader subclass
        except yaml.MarkedYAMLError as exc:
            mark = exc.problem_mark or exc.context_mark
            message = " ".join(filter(None, [exc.context, exc.problem])) or str(exc)
            line = mark.line + 1 if mark else None
            col = mark.column + 1 if mark else None
            raise SpecParseError(file, message, line, col) from None
        except yaml.YAMLError as exc:
            raise SpecParseError(file, str(exc)) from None
    if data is None:
        raise EmptySpecError(f"{file}: il file è vuoto — nessun path da rifattorizzare")
    if not isinstance(data, dict):
        raise SpecParseError(file, "la radice del documento deve essere un oggetto (mappa)")
    return _stringify_keys(data)


def _has_external_refs(node: Any) -> bool:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            return True
        return any(_has_external_refs(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_external_refs(v) for v in node)
    return False


def _bundle_external_refs(data: dict[str, Any], file: Path) -> dict[str, Any]:
    """Risolve (via prance) solo i $ref verso altri file; i $ref interni restano tali (nessun grafo espanso)."""
    from prance.util.resolver import RESOLVE_FILES, RefResolver

    try:
        resolver = RefResolver(data, url=file.resolve().as_uri(), resolve_types=RESOLVE_FILES)
        resolver.resolve_references()
    except Exception as exc:  # prance solleva eccezioni eterogenee
        raise SpecParseError(str(file), f"$ref esterno non risolvibile: {exc}") from None
    return _stringify_keys(resolver.specs)


def load_spec(path: str | Path, target_version: str = "3.0") -> SpecDocument:
    file = Path(path)
    if not file.is_file():
        raise SpecParseError(str(file), "file non trovato")
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SpecParseError(str(file), f"file non leggibile: {exc}") from None

    data = parse_text(text, str(file))
    version = detect_version(data)
    if version is None:
        raise UnsupportedSpecError(
            f"{file}: versione non riconosciuta (serve 'swagger: \"2.0\"' oppure 'openapi: 3.0.x/3.1.x')"
        )
    paths = data.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise EmptySpecError(f"{file}: la specifica non contiene path — nessun path da rifattorizzare")

    if _has_external_refs(data):
        log.info("[LOAD] $ref esterni trovati: bundling con prance")
        data = _bundle_external_refs(data, file)

    target = SpecVersion(target_version)
    if version == SpecVersion.OPENAPI_3_1 and target == SpecVersion.OPENAPI_3_0:
        raise UnsupportedSpecError(f"{file}: downgrade da OpenAPI 3.1 a 3.0 non supportato (targetOpenApiVersion=3.0)")
    log.info("[LOAD] %s: versione rilevata %s, target %s, %d path", file.name, version.value, target.value, len(paths))
    return SpecDocument(source_file=str(file), source_version=version, target_version=target, data=data)


def dump_spec(data: dict[str, Any], as_json: bool) -> str:
    if as_json:
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120)

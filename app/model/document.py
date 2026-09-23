"""Object Model interno della specifica.

Dopo il parsing la pipeline non lavora mai su stringhe YAML: il documento è un albero
dict/list tipizzato come `SpecDocument` (pydantic) e i suoi elementi (operation, schema,
path) sono esposti come viste pydantic indirizzate da JSON Pointer. Le trasformazioni
(RefactoringEngine) mutano l'albero solo tramite operazioni tipizzate.
"""

from __future__ import annotations

import copy
import hashlib
import json
from enum import StrEnum
from typing import Any, Iterator

from pydantic import BaseModel, Field

from app.model import pointer as jp

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


class SpecVersion(StrEnum):
    SWAGGER_2_0 = "2.0"
    OPENAPI_3_0 = "3.0"
    OPENAPI_3_1 = "3.1"


class ElementKind(StrEnum):
    DOCUMENT = "document"
    PATH = "path"
    OPERATION = "operation"
    PARAMETER = "parameter"
    REQUEST_BODY = "requestBody"
    RESPONSE = "response"
    SCHEMA = "schema"
    PROPERTY = "property"
    SECURITY = "security"


class OperationView(BaseModel):
    path: str
    method: str
    pointer: str
    operation_id: str | None = None

    @property
    def label(self) -> str:
        return f"{self.method.upper()} {self.path}"


class SchemaView(BaseModel):
    name: str
    pointer: str


class SpecDocument(BaseModel):
    """Il documento (AST) + metadati di provenienza/versione."""

    source_file: str
    source_version: SpecVersion
    target_version: SpecVersion
    data: dict[str, Any] = Field(default_factory=dict)

    # ── accesso ────────────────────────────────────────────────────────
    def get(self, pointer: str, default: Any = None) -> Any:
        return jp.resolve(self.data, pointer, default=default)

    def exists(self, pointer: str) -> bool:
        return jp.exists(self.data, pointer)

    def clone(self) -> "SpecDocument":
        return SpecDocument(
            source_file=self.source_file,
            source_version=self.source_version,
            target_version=self.target_version,
            data=copy.deepcopy(self.data),
        )

    @property
    def openapi_version(self) -> str:
        return str(self.data.get("openapi") or self.data.get("swagger") or "")

    @property
    def api_name(self) -> str:
        from pathlib import Path

        return Path(self.source_file).stem

    def paths(self) -> dict[str, Any]:
        paths = self.data.get("paths")
        return paths if isinstance(paths, dict) else {}

    def operations(self) -> Iterator[OperationView]:
        for path, item in self.paths().items():
            if not isinstance(item, dict):
                continue
            for method in HTTP_METHODS:
                op = item.get(method)
                if isinstance(op, dict):
                    yield OperationView(
                        path=path,
                        method=method,
                        pointer=jp.join(["paths", path, method]),
                        operation_id=op.get("operationId"),
                    )

    def schemas(self) -> Iterator[SchemaView]:
        schemas = (self.data.get("components") or {}).get("schemas") or {}
        for name in schemas:
            yield SchemaView(name=name, pointer=jp.join(["components", "schemas", name]))

    def fingerprint(self, pointer: str = "") -> str:
        """Hash stabile del contenuto di un nodo: usato per tracciare lo stato per frammento."""
        node = self.get(pointer)
        blob = json.dumps(node, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def detect_version(data: dict[str, Any]) -> SpecVersion | None:
    if str(data.get("swagger", "")).startswith("2."):
        return SpecVersion.SWAGGER_2_0
    version = str(data.get("openapi", ""))
    if version.startswith("3.0"):
        return SpecVersion.OPENAPI_3_0
    if version.startswith("3.1"):
        return SpecVersion.OPENAPI_3_1
    return None

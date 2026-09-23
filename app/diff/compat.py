"""Tabella di classificazione breaking/non-breaking dei change (regole di compatibilità REST).

Struttura dati unica e riusabile: il semantic diff non contiene logica di compatibilità sparsa.
Ogni voce è: bool fisso, oppure dict per contesto ('request' / 'response'), oppure 'if-required'.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ChangeType(StrEnum):
    OPENAPI_VERSION_CHANGED = "OPENAPI_VERSION_CHANGED"
    PATH_ADDED = "PATH_ADDED"
    PATH_REMOVED = "PATH_REMOVED"
    PATH_RENAMED = "PATH_RENAMED"
    OPERATION_ADDED = "OPERATION_ADDED"
    OPERATION_REMOVED = "OPERATION_REMOVED"
    OPERATION_ID_CHANGED = "OPERATION_ID_CHANGED"
    PARAMETER_ADDED = "PARAMETER_ADDED"
    PARAMETER_REMOVED = "PARAMETER_REMOVED"
    PARAMETER_BECAME_REQUIRED = "PARAMETER_BECAME_REQUIRED"
    PARAMETER_BECAME_OPTIONAL = "PARAMETER_BECAME_OPTIONAL"
    PARAMETER_SCHEMA_CHANGED = "PARAMETER_SCHEMA_CHANGED"
    REQUEST_BODY_ADDED = "REQUEST_BODY_ADDED"
    REQUEST_BODY_REMOVED = "REQUEST_BODY_REMOVED"
    REQUEST_BODY_BECAME_REQUIRED = "REQUEST_BODY_BECAME_REQUIRED"
    MEDIA_TYPE_ADDED = "MEDIA_TYPE_ADDED"
    MEDIA_TYPE_REMOVED = "MEDIA_TYPE_REMOVED"
    RESPONSE_ADDED = "RESPONSE_ADDED"
    RESPONSE_REMOVED = "RESPONSE_REMOVED"
    SCHEMA_ADDED = "SCHEMA_ADDED"
    SCHEMA_REMOVED = "SCHEMA_REMOVED"
    SCHEMA_RENAMED = "SCHEMA_RENAMED"
    SCHEMA_TYPE_CHANGED = "SCHEMA_TYPE_CHANGED"
    PROPERTY_ADDED = "PROPERTY_ADDED"
    PROPERTY_REMOVED = "PROPERTY_REMOVED"
    PROPERTY_BECAME_REQUIRED = "PROPERTY_BECAME_REQUIRED"
    PROPERTY_BECAME_OPTIONAL = "PROPERTY_BECAME_OPTIONAL"
    ENUM_VALUE_ADDED = "ENUM_VALUE_ADDED"
    ENUM_VALUE_REMOVED = "ENUM_VALUE_REMOVED"
    SECURITY_REQUIREMENT_ADDED = "SECURITY_REQUIREMENT_ADDED"  # restrizione
    SECURITY_REQUIREMENT_REMOVED = "SECURITY_REQUIREMENT_REMOVED"  # rilassamento
    SECURITY_REQUIREMENT_CHANGED = "SECURITY_REQUIREMENT_CHANGED"
    SECURITY_SCHEME_ADDED = "SECURITY_SCHEME_ADDED"
    SECURITY_SCHEME_REMOVED = "SECURITY_SCHEME_REMOVED"
    SECURITY_SCHEME_CHANGED = "SECURITY_SCHEME_CHANGED"


IF_REQUIRED = "if-required"

BREAKING_RULES: dict[ChangeType, Any] = {
    ChangeType.OPENAPI_VERSION_CHANGED: False,
    ChangeType.PATH_ADDED: False,
    ChangeType.PATH_REMOVED: True,
    ChangeType.PATH_RENAMED: True,  # il vecchio URL sparisce
    ChangeType.OPERATION_ADDED: False,
    ChangeType.OPERATION_REMOVED: True,
    ChangeType.OPERATION_ID_CHANGED: False,  # non è sul wire (impatta solo la code generation)
    ChangeType.PARAMETER_ADDED: IF_REQUIRED,
    ChangeType.PARAMETER_REMOVED: True,
    ChangeType.PARAMETER_BECAME_REQUIRED: True,
    ChangeType.PARAMETER_BECAME_OPTIONAL: False,
    ChangeType.PARAMETER_SCHEMA_CHANGED: True,
    ChangeType.REQUEST_BODY_ADDED: IF_REQUIRED,
    ChangeType.REQUEST_BODY_REMOVED: True,
    ChangeType.REQUEST_BODY_BECAME_REQUIRED: True,
    ChangeType.MEDIA_TYPE_ADDED: False,
    ChangeType.MEDIA_TYPE_REMOVED: True,
    ChangeType.RESPONSE_ADDED: False,
    ChangeType.RESPONSE_REMOVED: True,
    ChangeType.SCHEMA_ADDED: False,
    ChangeType.SCHEMA_REMOVED: True,
    ChangeType.SCHEMA_RENAMED: False,  # i nomi dei componenti non sono sul wire
    ChangeType.SCHEMA_TYPE_CHANGED: True,
    ChangeType.PROPERTY_ADDED: {"request": IF_REQUIRED, "response": False},
    ChangeType.PROPERTY_REMOVED: {"request": True, "response": True},
    ChangeType.PROPERTY_BECAME_REQUIRED: {"request": True, "response": False},
    ChangeType.PROPERTY_BECAME_OPTIONAL: {"request": False, "response": True},
    ChangeType.ENUM_VALUE_ADDED: {"request": False, "response": True},
    ChangeType.ENUM_VALUE_REMOVED: {"request": True, "response": False},
    ChangeType.SECURITY_REQUIREMENT_ADDED: True,
    ChangeType.SECURITY_REQUIREMENT_REMOVED: False,
    ChangeType.SECURITY_REQUIREMENT_CHANGED: True,
    ChangeType.SECURITY_SCHEME_ADDED: False,
    ChangeType.SECURITY_SCHEME_REMOVED: True,
    ChangeType.SECURITY_SCHEME_CHANGED: True,
}

REMOVAL_TYPES = {t for t in ChangeType if t.value.endswith("_REMOVED")}


def is_breaking(change_type: ChangeType, contexts: set[str] | None = None, required: bool = False) -> bool:
    rule = BREAKING_RULES[change_type]
    if isinstance(rule, dict):
        ctxs = contexts or {"request", "response"}  # contesto ignoto: conservativo
        values = [rule[c] for c in ctxs if c in rule]
        return any(required if v == IF_REQUIRED else bool(v) for v in values)
    if rule == IF_REQUIRED:
        return required
    return bool(rule)

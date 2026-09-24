"""Rappresentazione strutturata e persistibile delle regole.

Le regole in linguaggio naturale (rules/*.md) vengono compilate dall'LLM in `CompiledRule`:
un DSL chiuso di requisiti che il GovernanceValidator sa valutare deterministicamente.
Ciò che non è esprimibile nel DSL diventa `JudgmentRequirement`: resta affidato al Critic.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from app.model.issues import Severity

# 3: requireHeader.required senza default. Le regole in cache compilate con il vecchio default (true)
# non si distinguono da quelle in cui il modello ha scelto true: vanno ricompilate tutte.
DSL_VERSION = "3"


class RuleScope(StrEnum):
    DOCUMENT = "document"
    PATH = "path"
    OPERATION = "operation"
    PARAMETER = "parameter"
    RESPONSE = "response"
    SCHEMA = "schema"
    PROPERTY = "property"
    ANY = "any"


class RuleOrigin(StrEnum):
    NATURAL_LANGUAGE = "natural-language"  # rules/*.md, compilata dall'LLM
    SPECTRAL = "spectral"  # rules/spectral/*.spectral.yaml, eseguita da Spectral


class Casing(StrEnum):
    PASCAL = "pascal"
    CAMEL = "camel"
    KEBAB = "kebab"
    SNAKE = "snake"
    MACRO = "macro"  # UPPER_SNAKE_CASE
    TRAIN = "train"  # Train-Case (header HTTP)


class NameTarget(StrEnum):
    SCHEMA_NAME = "schemaName"
    PROPERTY_NAME = "propertyName"
    QUERY_PARAMETER = "queryParameter"
    PATH_SEGMENT = "pathSegment"
    HEADER = "header"
    OPERATION_ID = "operationId"


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class RuleCondition(_Base):
    methods: list[str] | None = Field(None, description="Metodi HTTP a cui si applica (es. ['post']); null = tutti")
    status_pattern: str | None = Field(
        None, alias="statusPattern", description="Regex sugli status code (es. '^[45]\\d\\d$'); null = tutti"
    )
    path_pattern: str | None = Field(None, alias="pathPattern", description="Regex sul path; null = tutti")


class RequireHeader(_Base):
    kind: Literal["requireHeader"] = "requireHeader"
    header: str
    required: bool = Field(description="true: deve esistere ed essere dichiarato required; "
                                       "false: deve solo esistere, la sua obbligatorietà non viene verificata")


class RequireQueryParameter(_Base):
    kind: Literal["requireQueryParameter"] = "requireQueryParameter"
    name: str = Field(description="Nome esatto del query parameter (case-sensitive)")
    required: bool = Field(description="true: deve esistere ed essere dichiarato required; "
                                       "false: deve solo esistere, la sua obbligatorietà non viene verificata")


class RequireOperationId(_Base):
    kind: Literal["requireOperationId"] = "requireOperationId"
    unique: bool = True


class NameCasing(_Base):
    kind: Literal["nameCasing"] = "nameCasing"
    target: NameTarget
    casing: Casing


class ErrorFormat(_Base):
    kind: Literal["errorFormat"] = "errorFormat"
    media_type: str = Field("application/problem+json", alias="mediaType")
    required_properties: list[str] = Field(default_factory=list, alias="requiredProperties")


class RequireSecurity(_Base):
    kind: Literal["requireSecurity"] = "requireSecurity"
    scheme_type: Literal["http", "apiKey", "oauth2", "openIdConnect"] = Field(alias="schemeType")
    scheme: str | None = Field(None, description="Per schemeType=http, es. 'bearer'")
    bearer_format: str | None = Field(None, alias="bearerFormat")


class RequireResponse(_Base):
    kind: Literal["requireResponse"] = "requireResponse"
    status: str


class JudgmentRequirement(_Base):
    kind: Literal["judgment"] = "judgment"
    guidance: str = Field(description="Cosa deve valutare il Critic: la regola non è verificabile deterministicamente")


Requirement = Annotated[
    Union[
        RequireHeader, RequireQueryParameter, RequireOperationId, NameCasing, ErrorFormat, RequireSecurity,
        RequireResponse, JudgmentRequirement,
    ],
    Field(discriminator="kind"),
]


class CompiledRuleDraft(_Base):
    """Ciò che l'LLM produce per una singola regola (l'id e la provenienza sono assegnati deterministicamente)."""

    scope: RuleScope
    condition: RuleCondition = Field(default_factory=RuleCondition)
    requirements: list[Requirement] = Field(min_length=1)
    severity: Severity = Severity.ERROR


class RuleSource(_Base):
    """Una regola in linguaggio naturale estratta da un file .md."""

    id: str
    text: str
    file: str
    line: int
    section: str | None = None


class CompiledRule(_Base):
    id: str
    origin: RuleOrigin = RuleOrigin.NATURAL_LANGUAGE
    text: str
    file: str
    line: int
    scope: RuleScope
    condition: RuleCondition = Field(default_factory=RuleCondition)
    requirements: list[Requirement] = Field(min_length=1)
    severity: Severity = Severity.ERROR
    compile_failed: bool = Field(False, alias="compileFailed")
    overridden_by: list[str] = Field(default_factory=list, alias="overriddenBy")

    @property
    def deterministic(self) -> bool:
        return any(r.kind != "judgment" for r in self.requirements)

    @property
    def judgment_only(self) -> bool:
        return all(r.kind == "judgment" for r in self.requirements)


class SpectralRule(_Base):
    id: str
    origin: RuleOrigin = RuleOrigin.SPECTRAL
    description: str
    message: str | None = None
    severity: Severity
    given: str
    scope: RuleScope
    methods: list[str] | None = None
    ruleset_file: str = Field(alias="rulesetFile")
    function: str | None = None
    function_options: dict | None = Field(None, alias="functionOptions")


class RuleConflict(_Base):
    element: str
    winner: str
    loser: str
    reason: str

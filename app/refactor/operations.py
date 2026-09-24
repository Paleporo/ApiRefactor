"""Operazioni di refactoring tipizzate: l'unico output ammesso da Refactor Agent e Correction Engine (mai YAML libero)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class ChangeCategory(StrEnum):
    STRUCTURAL = "STRUCTURAL"  # forma diversa, comportamento invariato
    GOVERNANCE = "GOVERNANCE"  # richiesta dagli standard, senza effetti sul contratto
    SEMANTIC = "SEMANTIC"  # altera il comportamento/contratto: sempre evidenziata


class _Op(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    rule_id: str = Field(alias="ruleId", min_length=1, description="Regola/violazione che richiede l'operazione")


class UpdateOpenApiVersion(_Op):
    type: Literal["UPDATE_OPENAPI_VERSION"] = "UPDATE_OPENAPI_VERSION"
    version: str


class AddHeader(_Op):
    type: Literal["ADD_HEADER"] = "ADD_HEADER"
    target: str = Field(description="JSON pointer dell'operation, es. /paths/~1payments/post")
    header: str
    required: bool = True
    schema_: dict[str, Any] = Field(default_factory=lambda: {"type": "string"}, alias="schema")
    description: str | None = None


class AddQueryParameter(_Op):
    type: Literal["ADD_QUERY_PARAMETER"] = "ADD_QUERY_PARAMETER"
    target: str = Field(description="JSON pointer dell'operation, es. /paths/~1payments/get")
    name: str
    required: bool = False
    schema_: dict[str, Any] = Field(default_factory=lambda: {"type": "string"}, alias="schema")
    description: str | None = None


class AddOperationId(_Op):
    type: Literal["ADD_OPERATION_ID"] = "ADD_OPERATION_ID"
    target: str
    operation_id: str = Field(alias="operationId")


class RenameSchema(_Op):
    type: Literal["RENAME_SCHEMA"] = "RENAME_SCHEMA"
    from_: str = Field(alias="from", description="Nome attuale in components/schemas")
    to: str


class RenameProperty(_Op):
    type: Literal["RENAME_PROPERTY"] = "RENAME_PROPERTY"
    target: str = Field(description="JSON pointer dello schema che contiene `properties`")
    from_: str = Field(alias="from")
    to: str


class RenamePath(_Op):
    type: Literal["RENAME_PATH"] = "RENAME_PATH"
    from_: str = Field(alias="from", description="Path template attuale, es. /Payment_Orders")
    to: str


class RenameParameter(_Op):
    type: Literal["RENAME_PARAMETER"] = "RENAME_PARAMETER"
    target: str = Field(description="JSON pointer dell'operation (o del path item) che dichiara il parametro")
    in_: Literal["query", "header", "path", "cookie"] = Field(alias="in")
    from_: str = Field(alias="from")
    to: str


class MoveComponent(_Op):
    type: Literal["MOVE_COMPONENT"] = "MOVE_COMPONENT"
    source: str = Field(description="JSON pointer del nodo inline da spostare")
    component_type: Literal["schemas", "responses", "parameters", "requestBodies", "headers", "examples"] = Field(
        alias="componentType"
    )
    name: str


class AddComponent(_Op):
    type: Literal["ADD_COMPONENT"] = "ADD_COMPONENT"
    component_type: Literal["schemas", "responses", "parameters", "requestBodies", "headers", "examples"] = Field(
        alias="componentType"
    )
    name: str
    definition: dict[str, Any]


class ReplaceResponseSchema(_Op):
    type: Literal["REPLACE_RESPONSE_SCHEMA"] = "REPLACE_RESPONSE_SCHEMA"
    target: str = Field(description="JSON pointer della response, es. /paths/~1a/get/responses/200")
    media_type: str = Field("application/json", alias="mediaType")
    schema_: dict[str, Any] = Field(alias="schema")


class ConvertErrorResponse(_Op):
    type: Literal["CONVERT_ERROR_RESPONSE"] = "CONVERT_ERROR_RESPONSE"
    target: str = Field(description="JSON pointer della response di errore")
    media_type: str = Field("application/problem+json", alias="mediaType")
    schema_ref: str = Field(alias="schemaRef", description="es. #/components/schemas/Problem")


class AddSecurityScheme(_Op):
    type: Literal["ADD_SECURITY_SCHEME"] = "ADD_SECURITY_SCHEME"
    name: str
    scheme: dict[str, Any]


class SetSecurityRequirement(_Op):
    type: Literal["SET_SECURITY_REQUIREMENT"] = "SET_SECURITY_REQUIREMENT"
    target: str = Field("", description="'' per la security globale, oppure JSON pointer dell'operation")
    requirements: list[dict[str, list[str]]]


class AddResponse(_Op):
    type: Literal["ADD_RESPONSE"] = "ADD_RESPONSE"
    target: str = Field(description="JSON pointer dell'operation")
    status: str
    description: str
    media_type: str | None = Field(None, alias="mediaType")
    schema_: dict[str, Any] | None = Field(None, alias="schema")


class SetField(_Op):
    type: Literal["SET_FIELD"] = "SET_FIELD"
    target: str = Field(description="JSON pointer del campo (il parent deve esistere)")
    value: Any


class RemoveField(_Op):
    type: Literal["REMOVE_FIELD"] = "REMOVE_FIELD"
    target: str


RefactorOperation = Annotated[
    Union[
        UpdateOpenApiVersion, AddHeader, AddQueryParameter, AddOperationId, RenameSchema, RenameProperty, RenamePath, RenameParameter,
        MoveComponent, AddComponent, ReplaceResponseSchema, ConvertErrorResponse, AddSecurityScheme,
        SetSecurityRequirement, AddResponse, SetField, RemoveField,
    ],
    Field(discriminator="type"),
]


class OperationsProposal(BaseModel):
    """Output strutturato del Refactor Agent / Correction Engine per un frammento."""

    model_config = ConfigDict(populate_by_name=True)
    operations: list[RefactorOperation] = Field(default_factory=list)
    rationale: str = ""


class PlannedOperation(BaseModel):
    """Operazione nel piano, con provenienza (frammento e agente che l'ha proposta)."""

    model_config = ConfigDict(populate_by_name=True)
    operation: RefactorOperation
    fragment: str = ""
    proposed_by: str = Field("refactor", alias="proposedBy")

    def dump(self) -> dict[str, Any]:
        return {"fragment": self.fragment, "proposedBy": self.proposed_by,
                **self.operation.model_dump(by_alias=True, mode="json")}


class AppliedChange(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    type: str
    rule_id: str = Field(alias="ruleId")
    category: ChangeCategory
    locations: list[str] = Field(description="JSON pointer toccati (documento prima e dopo)")
    description: str
    before: Any = None
    after: Any = None
    iteration: int = 0


class ApplyFailure(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    type: str
    rule_id: str = Field(alias="ruleId")
    target: str
    reason: str
    iteration: int = 0

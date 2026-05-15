from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from .agent_task_models import AgentTaskEventType, AgentTaskRequestedAction, AgentTaskStatus
from .connector_models import AgentConnector
from .models import AgentAction, RiskLevel

SCHEMA_MAPPER_CONFIG_KEY = "schema_mapper"
MAX_MAPPER_FIELDS = 32
MAX_PATH_LENGTH = 120
ALLOWED_TARGET_FIELDS = {
    "action",
    "project_id",
    "title",
    "goal",
    "context",
    "source_url",
    "source_session_id",
    "source_task_id",
    "requested_action",
    "repository",
    "branch_preference",
    "risk_hint",
    "requires_approval",
    "metadata",
    "task_id",
    "event_type",
    "message",
    "status",
}


class ConnectorSchemaMapperConfig(BaseModel):
    enabled: bool = True
    default_action: AgentAction = AgentAction.CREATE_AGENT_TASK
    field_paths: dict[str, str] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)

    @field_validator("field_paths")
    @classmethod
    def validate_field_paths(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_MAPPER_FIELDS:
            raise ValueError("Connector schema mapper has too many mapped fields")
        for target, path in value.items():
            if target not in ALLOWED_TARGET_FIELDS:
                raise ValueError(f"Unsupported connector mapper target field: {target}")
            _validate_path(path)
        return value

    @field_validator("defaults")
    @classmethod
    def validate_defaults(cls, value: dict[str, Any]) -> dict[str, Any]:
        for target in value:
            if target not in ALLOWED_TARGET_FIELDS:
                raise ValueError(f"Unsupported connector mapper default field: {target}")
        return value


class ConnectorSchemaMapperPreviewRequest(BaseModel):
    sample_payload: dict[str, Any] = Field(default_factory=dict)
    mapper: ConnectorSchemaMapperConfig | None = None


class ConnectorSchemaMapperPreviewResponse(BaseModel):
    normalized_payload: dict[str, Any]
    mapper_enabled: bool
    warnings: list[str] = Field(default_factory=list)


class ConnectorSchemaMapperStatus(BaseModel):
    connector_id: str
    mapper: ConnectorSchemaMapperConfig | None = None
    mapper_enabled: bool = False


def _validate_path(path: str) -> None:
    clean = path.strip()
    if not clean:
        raise ValueError("Connector mapper path cannot be empty")
    if len(clean) > MAX_PATH_LENGTH:
        raise ValueError("Connector mapper path is too long")
    if any(part in {"", "__", "constructor", "prototype"} for part in clean.split(".")):
        raise ValueError(f"Connector mapper path is unsafe: {path}")


def _get_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return None
    return current


def _coerce_value(field_name: str, value: Any) -> Any:
    if value is None:
        return None
    if field_name == "action":
        return AgentAction(str(value)).value
    if field_name == "requested_action":
        return AgentTaskRequestedAction(str(value)).value
    if field_name == "event_type":
        return AgentTaskEventType(str(value)).value
    if field_name == "status":
        return AgentTaskStatus(str(value)).value
    if field_name == "risk_hint":
        return RiskLevel(str(value)).value
    if field_name == "requires_approval":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "y"}
        return bool(value)
    if field_name == "metadata":
        if not isinstance(value, dict):
            raise HTTPException(status_code=400, detail="Connector mapper metadata must resolve to an object")
        return value
    return str(value) if field_name in {"project_id", "title", "goal", "context", "source_url", "source_session_id", "source_task_id", "repository", "branch_preference", "task_id", "message"} else value


def _mapper_from_connector(connector: AgentConnector) -> ConnectorSchemaMapperConfig | None:
    raw = connector.config.get(SCHEMA_MAPPER_CONFIG_KEY)
    if not raw:
        return None
    return ConnectorSchemaMapperConfig.model_validate(raw)


def get_connector_schema_mapper_status(connector: AgentConnector) -> ConnectorSchemaMapperStatus:
    mapper = _mapper_from_connector(connector)
    return ConnectorSchemaMapperStatus(
        connector_id=connector.id,
        mapper=mapper,
        mapper_enabled=bool(mapper and mapper.enabled),
    )


def set_connector_schema_mapper(connector: AgentConnector, mapper: ConnectorSchemaMapperConfig) -> ConnectorSchemaMapperStatus:
    connector.config = {**connector.config, SCHEMA_MAPPER_CONFIG_KEY: mapper.model_dump(mode="json")}
    return get_connector_schema_mapper_status(connector)


def normalize_connector_payload(
    connector: AgentConnector,
    payload: dict[str, Any],
    mapper: ConnectorSchemaMapperConfig | None = None,
) -> dict[str, Any]:
    active_mapper = mapper if mapper is not None else _mapper_from_connector(connector)
    if not active_mapper or not active_mapper.enabled:
        return payload
    normalized: dict[str, Any] = {"action": active_mapper.default_action.value}
    for target, default in active_mapper.defaults.items():
        normalized[target] = _coerce_value(target, default)
    for target, path in active_mapper.field_paths.items():
        value = _get_path(payload, path)
        if value is not None:
            normalized[target] = _coerce_value(target, value)
    return normalized


def preview_connector_schema_mapping(
    connector: AgentConnector,
    request: ConnectorSchemaMapperPreviewRequest,
) -> ConnectorSchemaMapperPreviewResponse:
    mapper = request.mapper or _mapper_from_connector(connector)
    normalized = normalize_connector_payload(connector, request.sample_payload, mapper=mapper)
    warnings: list[str] = []
    if not mapper or not mapper.enabled:
        warnings.append("No enabled schema mapper is configured; preview returned original payload.")
    if "action" not in normalized:
        warnings.append("Normalized payload has no action field.")
    if normalized.get("action") == AgentAction.CREATE_AGENT_TASK and (not normalized.get("title") or not normalized.get("goal")):
        warnings.append("CREATE_AGENT_TASK payload should include title and goal.")
    return ConnectorSchemaMapperPreviewResponse(
        normalized_payload=normalized,
        mapper_enabled=bool(mapper and mapper.enabled),
        warnings=warnings,
    )

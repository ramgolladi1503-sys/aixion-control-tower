from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .agent_task_models import AgentTaskEventType, AgentTaskStatus
from .connector_models import AgentConnector, ConnectorStatus
from .connector_schema_mapper import ConnectorSchemaMapperConfig, normalize_connector_payload
from .connector_webhook import ConnectorWebhookPayload
from .models import AgentAction
from .store import store


class ConnectorSimulationRequest(BaseModel):
    sample_payload: dict[str, Any] = Field(default_factory=dict)
    mapper: ConnectorSchemaMapperConfig | None = None
    validate_secret_configured: bool = True


class ConnectorSimulationResponse(BaseModel):
    accepted: bool
    connector_id: str
    action: AgentAction | None = None
    normalized_payload: dict[str, Any] = Field(default_factory=dict)
    task_preview: dict[str, Any] | None = None
    event_preview: dict[str, Any] | None = None
    auth_ready: bool = False
    scope_ready: bool = False
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _action_value(action: AgentAction | str) -> str:
    return action.value if isinstance(action, AgentAction) else str(action)


def _allowed_action(connector: AgentConnector, action: AgentAction) -> bool:
    allowed = {_action_value(value) for value in connector.allowed_actions}
    return action.value in allowed


def _connector_secret_configured(connector: AgentConnector) -> bool:
    return bool(connector.secret_ref and connector.config.get("secret_hash"))


def _auth_warnings(connector: AgentConnector, *, validate_secret_configured: bool) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    if connector.status != ConnectorStatus.ENABLED:
        warnings.append("Connector is disabled; live webhook calls will be refused.")
    if validate_secret_configured and not _connector_secret_configured(connector):
        warnings.append("Connector secret is not configured; live webhook calls will be refused.")
    return connector.status == ConnectorStatus.ENABLED and (not validate_secret_configured or _connector_secret_configured(connector)), warnings


def _scope_errors(connector: AgentConnector, payload: ConnectorWebhookPayload) -> list[str]:
    errors: list[str] = []
    if payload.project_id and connector.allowed_project_ids and payload.project_id not in connector.allowed_project_ids:
        errors.append("Connector is not allowed for this project.")
    if payload.repository and connector.allowed_repositories and payload.repository not in connector.allowed_repositories:
        errors.append("Connector is not allowed for this repository.")
    return errors


def _simulate_create_task(connector: AgentConnector, payload: ConnectorWebhookPayload) -> tuple[dict[str, Any] | None, list[str], bool]:
    errors: list[str] = []
    if not _allowed_action(connector, AgentAction.CREATE_AGENT_TASK):
        errors.append("Connector is not allowed to perform CREATE_AGENT_TASK.")
    if not payload.project_id:
        errors.append("project_id is required for CREATE_AGENT_TASK.")
    elif payload.project_id not in store.projects:
        errors.append("Project not found.")
    if not payload.title or not payload.goal:
        errors.append("title and goal are required for CREATE_AGENT_TASK.")
    errors.extend(_scope_errors(connector, payload))
    if errors:
        return None, errors, False
    preview = {
        "provider": connector.provider_label,
        "project_id": payload.project_id,
        "title": payload.title,
        "goal": payload.goal,
        "context": payload.context,
        "requested_action": payload.requested_action,
        "repository": payload.repository,
        "branch_preference": payload.branch_preference,
        "risk_hint": payload.risk_hint,
        "requires_approval": payload.requires_approval,
        "metadata": {
            **payload.metadata,
            "connector_id": connector.id,
            "connector_name": connector.name,
            "connector_webhook": True,
        },
        "external_agent_id": connector.id,
        "external_agent_name": connector.name,
    }
    return preview, [], True


def _simulate_append_event(connector: AgentConnector, payload: ConnectorWebhookPayload) -> tuple[dict[str, Any] | None, list[str], bool]:
    errors: list[str] = []
    if not _allowed_action(connector, AgentAction.APPEND_AGENT_TASK_EVENT):
        errors.append("Connector is not allowed to perform APPEND_AGENT_TASK_EVENT.")
    if not payload.task_id:
        errors.append("task_id is required for APPEND_AGENT_TASK_EVENT.")
        return None, errors, False
    task = store.agent_tasks.get(payload.task_id)
    if not task:
        errors.append("Agent task not found.")
        return None, errors, False
    if task.external_agent_id != connector.id and task.metadata.get("connector_id") != connector.id:
        errors.append("Connector is not allowed for this task.")
    if task.project_id and connector.allowed_project_ids and task.project_id not in connector.allowed_project_ids:
        errors.append("Connector is not allowed for this project.")
    if task.repository and connector.allowed_repositories and task.repository not in connector.allowed_repositories:
        errors.append("Connector is not allowed for this repository.")
    if errors:
        return None, errors, False
    preview = {
        "task_id": task.id,
        "event_type": payload.event_type,
        "message": payload.message,
        "status": payload.status,
        "actor": f"connector:{connector.id}",
        "metadata": {
            **payload.metadata,
            "connector_id": connector.id,
            "connector_name": connector.name,
            "connector_webhook": True,
        },
    }
    return preview, [], True


def simulate_connector_webhook(connector: AgentConnector, request: ConnectorSimulationRequest) -> ConnectorSimulationResponse:
    errors: list[str] = []
    warnings: list[str] = []
    auth_ready, auth_warnings = _auth_warnings(connector, validate_secret_configured=request.validate_secret_configured)
    warnings.extend(auth_warnings)

    try:
        normalized = normalize_connector_payload(connector, request.sample_payload, mapper=request.mapper)
        payload = ConnectorWebhookPayload.model_validate(normalized)
    except Exception as error:  # noqa: BLE001 - simulator should return validation feedback instead of raising.
        return ConnectorSimulationResponse(
            accepted=False,
            connector_id=connector.id,
            normalized_payload={},
            auth_ready=auth_ready,
            scope_ready=False,
            errors=[f"Invalid connector webhook payload: {error}"],
            warnings=warnings,
        )

    task_preview: dict[str, Any] | None = None
    event_preview: dict[str, Any] | None = None
    scope_ready = False
    if payload.action == AgentAction.CREATE_AGENT_TASK:
        task_preview, action_errors, scope_ready = _simulate_create_task(connector, payload)
        errors.extend(action_errors)
    elif payload.action == AgentAction.APPEND_AGENT_TASK_EVENT:
        event_preview, action_errors, scope_ready = _simulate_append_event(connector, payload)
        errors.extend(action_errors)
    else:
        errors.append(f"Connector simulator does not support action: {payload.action.value}")

    return ConnectorSimulationResponse(
        accepted=auth_ready and scope_ready and not errors,
        connector_id=connector.id,
        action=payload.action,
        normalized_payload=payload.model_dump(mode="json"),
        task_preview=task_preview,
        event_preview=event_preview,
        auth_ready=auth_ready,
        scope_ready=scope_ready,
        errors=errors,
        warnings=warnings,
    )

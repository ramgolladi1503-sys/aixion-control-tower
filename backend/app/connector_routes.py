from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .auth import require_owner
from .connector_models import (
    AgentConnector,
    ConnectorCreate,
    ConnectorHealthStatus,
    ConnectorPublic,
    ConnectorStatus,
    ConnectorUpdate,
)
from .models import AuditEvent, AuthUser, now_utc
from .store import store

router = APIRouter(prefix="/connectors", tags=["connectors"])
OwnerDependency = Depends(require_owner)


def _audit(event_type: str, entity_id: str, details: dict, actor: str = "system") -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def _to_public(connector: AgentConnector) -> ConnectorPublic:
    return ConnectorPublic(
        id=connector.id,
        name=connector.name,
        connector_type=connector.connector_type,
        provider_label=connector.provider_label,
        endpoint_url=connector.endpoint_url,
        callback_url=connector.callback_url,
        auth_type=connector.auth_type,
        status=connector.status,
        health_status=connector.health_status,
        allowed_project_ids=connector.allowed_project_ids,
        allowed_repositories=connector.allowed_repositories,
        allowed_actions=connector.allowed_actions,
        rate_limit_per_minute=connector.rate_limit_per_minute,
        secret_configured=bool(connector.secret_ref),
        last_used_at=connector.last_used_at,
        last_health_check_at=connector.last_health_check_at,
        failed_auth_count=connector.failed_auth_count,
        last_error=connector.last_error,
        config=connector.config,
        created_at=connector.created_at,
        updated_at=connector.updated_at,
    )


def _get_connector_or_404(connector_id: str) -> AgentConnector:
    connector = store.agent_connectors.get(connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    return connector


def _validate_scopes(payload: ConnectorCreate | ConnectorUpdate) -> None:
    project_ids = payload.allowed_project_ids
    if project_ids:
        missing = [project_id for project_id in project_ids if project_id not in store.projects]
        if missing:
            raise HTTPException(status_code=404, detail={"message": "Project scope not found", "project_ids": missing})


def _connector_details(connector: AgentConnector) -> dict:
    return {
        "name": connector.name,
        "connector_type": connector.connector_type,
        "provider_label": connector.provider_label,
        "auth_type": connector.auth_type,
        "status": connector.status,
        "health_status": connector.health_status,
        "allowed_project_ids": connector.allowed_project_ids,
        "allowed_repositories": connector.allowed_repositories,
        "allowed_actions": connector.allowed_actions,
        "rate_limit_per_minute": connector.rate_limit_per_minute,
        "secret_configured": bool(connector.secret_ref),
    }


@router.post("", response_model=ConnectorPublic)
def create_connector(payload: ConnectorCreate, user: AuthUser = OwnerDependency) -> ConnectorPublic:
    _validate_scopes(payload)
    connector = AgentConnector(
        name=payload.name,
        connector_type=payload.connector_type,
        provider_label=payload.provider_label,
        endpoint_url=payload.endpoint_url,
        callback_url=payload.callback_url,
        auth_type=payload.auth_type,
        status=ConnectorStatus.ENABLED if payload.enabled else ConnectorStatus.DISABLED,
        health_status=ConnectorHealthStatus.UNKNOWN if payload.enabled else ConnectorHealthStatus.DISABLED,
        allowed_project_ids=payload.allowed_project_ids,
        allowed_repositories=payload.allowed_repositories,
        allowed_actions=payload.allowed_actions,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        created_by_user_id=user.id,
        config=payload.config,
    )
    store.agent_connectors[connector.id] = connector
    _audit("connector.created", connector.id, _connector_details(connector), actor=user.email)
    store.persist()
    return _to_public(connector)


@router.get("", response_model=list[ConnectorPublic])
def list_connectors(_: AuthUser = OwnerDependency) -> list[ConnectorPublic]:
    return [_to_public(connector) for connector in store.agent_connectors.values()]


@router.get("/{connector_id}", response_model=ConnectorPublic)
def get_connector(connector_id: str, _: AuthUser = OwnerDependency) -> ConnectorPublic:
    return _to_public(_get_connector_or_404(connector_id))


@router.patch("/{connector_id}", response_model=ConnectorPublic)
def update_connector(connector_id: str, payload: ConnectorUpdate, user: AuthUser = OwnerDependency) -> ConnectorPublic:
    _validate_scopes(payload)
    connector = _get_connector_or_404(connector_id)
    previous_status = connector.status
    update = payload.model_dump(exclude_unset=True)
    for field_name, value in update.items():
        if field_name == "enabled":
            connector.status = ConnectorStatus.ENABLED if value else ConnectorStatus.DISABLED
            connector.health_status = ConnectorHealthStatus.UNKNOWN if value else ConnectorHealthStatus.DISABLED
        elif hasattr(connector, field_name):
            setattr(connector, field_name, value)
    connector.updated_at = now_utc()
    _audit(
        "connector.updated",
        connector.id,
        {"previous_status": previous_status, "new": _connector_details(connector)},
        actor=user.email,
    )
    store.persist()
    return _to_public(connector)


@router.post("/{connector_id}/disable", response_model=ConnectorPublic)
def disable_connector(connector_id: str, user: AuthUser = OwnerDependency) -> ConnectorPublic:
    connector = _get_connector_or_404(connector_id)
    previous_status = connector.status
    connector.status = ConnectorStatus.DISABLED
    connector.health_status = ConnectorHealthStatus.DISABLED
    connector.updated_at = now_utc()
    _audit(
        "connector.disabled",
        connector.id,
        {"previous_status": previous_status, "new_status": connector.status},
        actor=user.email,
    )
    store.persist()
    return _to_public(connector)


@router.post("/{connector_id}/enable", response_model=ConnectorPublic)
def enable_connector(connector_id: str, user: AuthUser = OwnerDependency) -> ConnectorPublic:
    connector = _get_connector_or_404(connector_id)
    previous_status = connector.status
    connector.status = ConnectorStatus.ENABLED
    connector.health_status = ConnectorHealthStatus.UNKNOWN
    connector.updated_at = now_utc()
    _audit(
        "connector.enabled",
        connector.id,
        {"previous_status": previous_status, "new_status": connector.status},
        actor=user.email,
    )
    store.persist()
    return _to_public(connector)

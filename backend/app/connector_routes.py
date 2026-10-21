from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from .auth import require_owner
from .connector_credentials import (
    ConnectorCredentialStatus,
    ConnectorSecretIssueRequest,
    ConnectorSecretIssueResponse,
    connector_credential_status,
    issue_connector_secret,
    mark_connector_health_check,
    record_connector_auth_failure,
    record_connector_auth_success,
    revoke_connector_secret,
)
from .connector_models import (
    AgentConnector,
    ConnectorCreate,
    ConnectorHealthStatus,
    ConnectorPublic,
    ConnectorStatus,
    ConnectorUpdate,
)
from .connector_schema_mapper import (
    ConnectorSchemaMapperConfig,
    ConnectorSchemaMapperPreviewRequest,
    ConnectorSchemaMapperPreviewResponse,
    ConnectorSchemaMapperStatus,
    get_connector_schema_mapper_status,
    preview_connector_schema_mapping,
    set_connector_schema_mapper,
)
from .connector_simulator import ConnectorSimulationRequest, ConnectorSimulationResponse, simulate_connector_webhook
from .connector_templates import ConnectorTemplate, ConnectorTemplateList, connector_templates, get_connector_template
from .connector_webhook import ConnectorWebhookResponse, handle_connector_webhook
from .application_repository_access import (
    connector_repository_for_request,
    project_repository_for_request,
    workspace_id_for_request,
    workspace_membership_repository_for_request,
)
from .models import AuditEvent, AuthUser, now_utc
from .connector_repository import ConnectorRepositoryConflict, ConnectorRepositoryScopeError
from .project_repository import ProjectRepository
from .store import store
from .workspace_access import (
    connector_scope_is_consistent,
    project_workspace_id,
    workspace_id_for_user,
)

router = APIRouter(prefix="/connectors", tags=["connectors"])
OwnerDependency = Depends(require_owner)


def _audit(event_type: str, entity_id: str, details: dict, actor: str = "system") -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def _safe_config(config: dict) -> dict:
    return {
        key: value
        for key, value in config.items()
        if key not in {"secret_hash", "secret_hint", "secret_revoked_at", "secret_rotated_at"}
    }


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
        secret_configured=bool(connector.secret_ref and connector.config.get("secret_hash")),
        last_used_at=connector.last_used_at,
        last_health_check_at=connector.last_health_check_at,
        failed_auth_count=connector.failed_auth_count,
        last_error=connector.last_error,
        config=_safe_config(connector.config),
        created_at=connector.created_at,
        updated_at=connector.updated_at,
    )


def _get_connector_or_404(connector_id: str, user: AuthUser | None = None) -> AgentConnector:
    connector = store.agent_connectors.get(connector_id)
    if not connector or not connector_scope_is_consistent(connector):
        raise HTTPException(status_code=404, detail="Connector not found")
    if user is not None:
        workspace_id = workspace_id_for_user(user)
        if workspace_id is not None and connector.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="Connector not found")
    return connector


def _get_bound_connector_or_404(
    connector_id: str, user: AuthUser, request: Request
) -> AgentConnector | None:
    """Read from the explicitly bound repository, or signal local adapter use."""
    if getattr(request.app.state, "connector_repository", None) is None:
        return None
    workspace_id = workspace_id_for_request(user, request) or user.workspace_id
    if not workspace_id:
        raise HTTPException(status_code=404, detail="Connector not found")
    connector = connector_repository_for_request(request).get_for_workspace(
        connector_id, workspace_id=workspace_id
    )
    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    return connector


def _connector_audit(
    connector: AgentConnector, event_type: str, details: dict, actor: str
) -> AuditEvent:
    return AuditEvent(
        event_type=event_type,
        entity_id=connector.id,
        workspace_id=connector.workspace_id,
        details=details,
        actor=actor,
    )


def _validate_scopes(
    payload: ConnectorCreate | ConnectorUpdate,
    workspace_id: str | None,
    project_repository: ProjectRepository | None = None,
) -> None:
    project_ids = payload.allowed_project_ids
    if project_ids:
        if project_repository is None:
            missing = [
                project_id for project_id in project_ids
                if project_id not in store.projects
                or (workspace_id is not None and project_workspace_id(project_id) != workspace_id)
            ]
        else:
            missing = [
                project_id
                for project_id in project_ids
                if (
                    (project := project_repository.get(project_id)) is None
                    or (workspace_id is not None and project.workspace_id != workspace_id)
                )
            ]
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
        "secret_configured": bool(connector.secret_ref and connector.config.get("secret_hash")),
    }


@router.post("", response_model=ConnectorPublic)
def create_connector(
    payload: ConnectorCreate,
    user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorPublic:
    workspace_id = (
        workspace_id_for_user(user)
        if http_request is None
        else workspace_id_for_request(user, http_request)
    )
    repository = (
        None
        if http_request is None or getattr(http_request.app.state, "connector_repository", None) is None
        else connector_repository_for_request(http_request)
    )
    if repository is not None and not workspace_id:
        workspace_id = user.workspace_id
    if repository is not None and not workspace_id:
        raise HTTPException(status_code=404, detail="Workspace not found")
    project_repository = (
        None if http_request is None else project_repository_for_request(http_request)
    )
    _validate_scopes(payload, workspace_id, project_repository)
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
        workspace_id=workspace_id,
        config=payload.config,
    )
    if repository is None:
        store.agent_connectors[connector.id] = connector
        _audit("connector.created", connector.id, _connector_details(connector), actor=user.email)
        store.persist()
    else:
        try:
            repository.create(
                connector=connector,
                audit_event=_connector_audit(
                    connector, "connector.created", _connector_details(connector), user.email
                ),
            )
        except ConnectorRepositoryScopeError as error:
            raise HTTPException(status_code=404, detail="Connector scope not found") from error
    return _to_public(connector)


@router.get("", response_model=list[ConnectorPublic])
def list_connectors(
    user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> list[ConnectorPublic]:
    workspace_id = (
        workspace_id_for_user(user)
        if http_request is None
        else workspace_id_for_request(user, http_request)
    )
    project_repository = (
        None if http_request is None else project_repository_for_request(http_request)
    )
    membership_repository = (
        None
        if http_request is None
        else workspace_membership_repository_for_request(http_request)
    )
    if http_request is not None and getattr(http_request.app.state, "connector_repository", None) is not None:
        if not workspace_id:
            workspace_id = user.workspace_id
        if not workspace_id:
            raise HTTPException(status_code=404, detail="Workspace not found")
        return [
            _to_public(connector)
            for connector in connector_repository_for_request(http_request).list_for_workspace(workspace_id)
        ]
    return [_to_public(connector) for connector in store.agent_connectors.values()
            if connector_scope_is_consistent(
                connector,
                projects=project_repository,
                memberships=membership_repository,
            )
            and (workspace_id is None or connector.workspace_id == workspace_id)]


@router.get("/templates", response_model=ConnectorTemplateList)
def list_connector_templates(_: AuthUser = OwnerDependency) -> ConnectorTemplateList:
    return ConnectorTemplateList(templates=connector_templates())


@router.get("/templates/{template_id}", response_model=ConnectorTemplate)
def get_connector_template_route(template_id: str, _: AuthUser = OwnerDependency) -> ConnectorTemplate:
    return get_connector_template(template_id)


@router.post("/{connector_id}/webhook", response_model=ConnectorWebhookResponse)
async def connector_webhook(
    connector_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_aixion_connector_signature: str | None = Header(default=None),
    x_aixion_signature_version: str | None = Header(default=None),
    x_aixion_timestamp: str | None = Header(default=None),
    x_aixion_nonce: str | None = Header(default=None),
) -> ConnectorWebhookResponse:
    connector = _get_connector_or_404(connector_id)
    return await handle_connector_webhook(
        connector,
        request,
        authorization,
        x_aixion_connector_signature,
        x_aixion_signature_version,
        x_aixion_timestamp,
        x_aixion_nonce,
    )


@router.post("/{connector_id}/simulate", response_model=ConnectorSimulationResponse)
def simulate_connector_webhook_route(connector_id: str, payload: ConnectorSimulationRequest, user: AuthUser = OwnerDependency) -> ConnectorSimulationResponse:
    return simulate_connector_webhook(_get_connector_or_404(connector_id, user), payload)


@router.get("/{connector_id}/schema-mapper", response_model=ConnectorSchemaMapperStatus)
def get_connector_mapper(connector_id: str, user: AuthUser = OwnerDependency) -> ConnectorSchemaMapperStatus:
    return get_connector_schema_mapper_status(_get_connector_or_404(connector_id, user))


@router.put("/{connector_id}/schema-mapper", response_model=ConnectorSchemaMapperStatus)
def update_connector_mapper(connector_id: str, payload: ConnectorSchemaMapperConfig, user: AuthUser = OwnerDependency) -> ConnectorSchemaMapperStatus:
    connector = _get_connector_or_404(connector_id, user)
    status = set_connector_schema_mapper(connector, payload)
    connector.updated_at = now_utc()
    _audit("connector.schema_mapper_updated", connector.id, {"mapper_enabled": status.mapper_enabled, "mapped_fields": list(payload.field_paths.keys())}, actor=user.email)
    store.persist()
    return status


@router.post("/{connector_id}/schema-mapper/preview", response_model=ConnectorSchemaMapperPreviewResponse)
def preview_connector_mapper(connector_id: str, payload: ConnectorSchemaMapperPreviewRequest, user: AuthUser = OwnerDependency) -> ConnectorSchemaMapperPreviewResponse:
    return preview_connector_schema_mapping(_get_connector_or_404(connector_id, user), payload)


@router.get("/{connector_id}", response_model=ConnectorPublic)
def get_connector(
    connector_id: str, user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorPublic:
    bound = None if http_request is None else _get_bound_connector_or_404(connector_id, user, http_request)
    return _to_public(bound if bound is not None else _get_connector_or_404(connector_id, user))


@router.patch("/{connector_id}", response_model=ConnectorPublic)
def update_connector(
    connector_id: str, payload: ConnectorUpdate, user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorPublic:
    bound = None if http_request is None else _get_bound_connector_or_404(connector_id, user, http_request)
    workspace_id = (
        workspace_id_for_request(user, http_request) or user.workspace_id
        if http_request is not None else workspace_id_for_user(user)
    )
    project_repository = (
        project_repository_for_request(http_request)
        if http_request is not None and getattr(http_request.app.state, "connector_repository", None) is not None
        else None
    )
    _validate_scopes(payload, workspace_id, project_repository)
    connector = bound if bound is not None else _get_connector_or_404(connector_id, user)
    expected_updated_at = connector.updated_at
    previous_status = connector.status
    update = payload.model_dump(exclude_unset=True)
    for field_name, value in update.items():
        if field_name == "enabled":
            connector.status = ConnectorStatus.ENABLED if value else ConnectorStatus.DISABLED
            connector.health_status = ConnectorHealthStatus.UNKNOWN if value else ConnectorHealthStatus.DISABLED
        elif field_name == "config":
            connector.config = {**connector.config, **(value or {})}
        elif hasattr(connector, field_name):
            setattr(connector, field_name, value)
    connector.updated_at = now_utc()
    details = {"previous_status": previous_status, "new": _connector_details(connector)}
    if bound is None:
        _audit("connector.updated", connector.id, details, actor=user.email)
        store.persist()
    else:
        try:
            connector_repository_for_request(http_request).update(
                connector=connector,
                expected_updated_at=expected_updated_at,
                audit_event=_connector_audit(connector, "connector.updated", details, user.email),
            )
        except ConnectorRepositoryConflict as error:
            raise HTTPException(status_code=409, detail="Connector changed; reload and retry") from error
        except ConnectorRepositoryScopeError as error:
            raise HTTPException(status_code=404, detail="Connector scope not found") from error
    return _to_public(connector)


@router.post("/{connector_id}/disable", response_model=ConnectorPublic)
def disable_connector(connector_id: str, user: AuthUser = OwnerDependency) -> ConnectorPublic:
    connector = _get_connector_or_404(connector_id, user)
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
    connector = _get_connector_or_404(connector_id, user)
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


@router.get("/{connector_id}/credentials", response_model=ConnectorCredentialStatus)
def get_connector_credentials(connector_id: str, user: AuthUser = OwnerDependency) -> ConnectorCredentialStatus:
    return connector_credential_status(_get_connector_or_404(connector_id, user))


@router.post("/{connector_id}/secret/issue", response_model=ConnectorSecretIssueResponse)
def issue_connector_secret_route(
    connector_id: str,
    payload: ConnectorSecretIssueRequest | None = None,
    user: AuthUser = OwnerDependency,
) -> ConnectorSecretIssueResponse:
    connector = _get_connector_or_404(connector_id, user)
    secret, secret_ref, secret_hint = issue_connector_secret(connector)
    _audit(
        "connector.secret_issued",
        connector.id,
        {"secret_ref": secret_ref, "secret_hint": secret_hint, "note": payload.note if payload else ""},
        actor=user.email,
    )
    store.persist()
    return ConnectorSecretIssueResponse(connector=_to_public(connector), secret=secret, secret_hint=secret_hint)


@router.post("/{connector_id}/secret/rotate", response_model=ConnectorSecretIssueResponse)
def rotate_connector_secret_route(
    connector_id: str,
    payload: ConnectorSecretIssueRequest | None = None,
    user: AuthUser = OwnerDependency,
) -> ConnectorSecretIssueResponse:
    connector = _get_connector_or_404(connector_id, user)
    secret, secret_ref, secret_hint = issue_connector_secret(connector)
    _audit(
        "connector.secret_rotated",
        connector.id,
        {"secret_ref": secret_ref, "secret_hint": secret_hint, "note": payload.note if payload else ""},
        actor=user.email,
    )
    store.persist()
    return ConnectorSecretIssueResponse(connector=_to_public(connector), secret=secret, secret_hint=secret_hint)


@router.post("/{connector_id}/secret/revoke", response_model=ConnectorPublic)
def revoke_connector_secret_route(connector_id: str, user: AuthUser = OwnerDependency) -> ConnectorPublic:
    connector = _get_connector_or_404(connector_id, user)
    revoke_connector_secret(connector)
    _audit("connector.secret_revoked", connector.id, {"secret_configured": False}, actor=user.email)
    store.persist()
    return _to_public(connector)


@router.post("/{connector_id}/health/success", response_model=ConnectorPublic)
def mark_connector_success(connector_id: str, user: AuthUser = OwnerDependency) -> ConnectorPublic:
    connector = _get_connector_or_404(connector_id, user)
    record_connector_auth_success(connector)
    mark_connector_health_check(connector, healthy=True)
    _audit("connector.health_success", connector.id, _connector_details(connector), actor=user.email)
    store.persist()
    return _to_public(connector)


@router.post("/{connector_id}/health/failure", response_model=ConnectorPublic)
def mark_connector_failure(
    connector_id: str,
    payload: ConnectorSecretIssueRequest | None = None,
    user: AuthUser = OwnerDependency,
) -> ConnectorPublic:
    connector = _get_connector_or_404(connector_id, user)
    reason = payload.note if payload and payload.note else "manual health failure"
    record_connector_auth_failure(connector, reason)
    mark_connector_health_check(connector, healthy=False, reason=reason)
    _audit("connector.health_failure", connector.id, {**_connector_details(connector), "reason": reason}, actor=user.email)
    store.persist()
    return _to_public(connector)

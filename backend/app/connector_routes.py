from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from .agent_task_repository import PostgresAgentTaskRepository
from .auth import require_owner
from .connector_credentials import (
    ConnectorCredentialStatus,
    ConnectorSecretIssueRequest,
    ConnectorSecretIssueResponse,
    connector_credential_status,
    issue_connector_secret,
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
    agent_task_repository_for_request,
    application_repositories_for_request,
    approval_decision_repository_for_request,
    connector_repository_for_request,
    project_repository_for_request,
    workspace_id_for_request,
    workspace_membership_repository_for_request,
)
from .settings import get_settings
from .models import AuditEvent, AuthUser, now_utc
from .approval_decisions import approval_decision_repository
from .approval_repository import ApprovalInvocationLeaseRejected
from .connector_repository import (
    ConnectorRepository,
    ConnectorRepositoryConflict,
    ConnectorRepositoryScopeError,
    PostgresConnectorRepository,
)
from .project_repository import PostgresProjectRepository, ProjectRepository
from .workspace_membership_repository import PostgresWorkspaceMembershipRepository
from .store import store
from .workspace_access import (
    connector_scope_is_consistent,
    project_workspace_id,
    workspace_id_for_user,
)

router = APIRouter(prefix="/connectors", tags=["connectors"])
OwnerDependency = Depends(require_owner)


def _audit(event_type: str, entity_id: str, details: dict, actor: str = "system") -> AuditEvent:
    connector = store.agent_connectors.get(entity_id)
    event = AuditEvent(
        event_type=event_type,
        entity_id=entity_id,
        workspace_id=connector.workspace_id if connector is not None else None,
        details=details,
        actor=actor,
    )
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
    if _requires_bound_connector_repository():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PostgreSQL connector repository requires an app-bound request",
        )
    connector = store.agent_connectors.get(connector_id)
    if not connector or not connector_scope_is_consistent(connector):
        raise HTTPException(status_code=404, detail="Connector not found")
    if user is not None:
        workspace_id = workspace_id_for_user(user)
        if workspace_id is not None and connector.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="Connector not found")
    return connector


def _get_bound_connector_or_404(
    connector_id: str, user: AuthUser, request: Request | None
) -> AgentConnector | None:
    """Read from the explicitly bound repository, or signal local adapter use."""
    repository = _connector_repository_or_none(request)
    if repository is None:
        return None
    workspace_id = workspace_id_for_request(user, request) or user.workspace_id
    if not workspace_id:
        raise HTTPException(status_code=404, detail="Connector not found")
    connector = repository.get_for_workspace(
        connector_id, workspace_id=workspace_id
    )
    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    return connector


def _connector_repository_or_none(request: Request | None) -> ConnectorRepository | None:
    """Choose the configured connector authority, with no PostgreSQL fallback."""
    if request is None:
        if _requires_bound_connector_repository():
            raise HTTPException(
                status_code=503,
                detail="PostgreSQL connector repository runtime is unavailable",
            )
        return None
    if _requires_bound_connector_repository():
        try:
            return connector_repository_for_request(request)
        except RuntimeError as error:
            raise HTTPException(
                status_code=503,
                detail="PostgreSQL connector repository runtime is unavailable or mismatched",
            ) from error
    if getattr(request.app.state, "connector_repository", None) is None:
        return None
    return connector_repository_for_request(request)


def _requires_bound_connector_repository() -> bool:
    """Disallow process-global SQLite connector access in authoritative modes."""
    settings = get_settings()
    return bool(
        getattr(settings, "is_production", False)
        or getattr(settings, "postgres_auth_enabled", False)
    )


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


def _commit_bound_connector_update(
    request: Request,
    connector: AgentConnector,
    *,
    expected_updated_at,
    event_type: str,
    details: dict,
    actor: str,
) -> None:
    try:
        connector_repository_for_request(request).update(
            connector=connector,
            expected_updated_at=expected_updated_at,
            audit_event=_connector_audit(connector, event_type, details, actor),
        )
    except ConnectorRepositoryConflict as error:
        raise HTTPException(status_code=409, detail="Connector changed; reload and retry") from error
    except ConnectorRepositoryScopeError as error:
        raise HTTPException(status_code=404, detail="Connector scope not found") from error


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
    repository = _connector_repository_or_none(http_request)
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
    repository = _connector_repository_or_none(http_request)
    if repository is not None:
        if not workspace_id:
            workspace_id = user.workspace_id
        if not workspace_id:
            raise HTTPException(status_code=404, detail="Workspace not found")
        return [
            _to_public(connector)
            for connector in repository.list_for_workspace(workspace_id)
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
    selected_repository = _connector_repository_or_none(request)
    if selected_repository is None:
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

    if get_settings().postgres_auth_enabled:
        repositories = request.app.state.postgres_repositories
        selected_task_repository = repositories.agent_tasks
        selected_project_repository = repositories.projects
        selected_memberships = repositories.workspace_membership
    else:
        selected_task_repository = getattr(request.app.state, "agent_task_repository", None)
        selected_project_repository = getattr(request.app.state, "project_repository", None)
        selected_memberships = getattr(request.app.state, "workspace_membership_repository", None)
    if not all((
        isinstance(selected_repository, PostgresConnectorRepository),
        isinstance(selected_task_repository, PostgresAgentTaskRepository),
        isinstance(selected_project_repository, PostgresProjectRepository),
        isinstance(selected_memberships, PostgresWorkspaceMembershipRepository),
    )):
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL connector webhook requires one PostgreSQL connector, task, project, and workspace runtime",
        )
    connector = selected_repository.get_for_authentication(connector_id)
    if connector is None or not connector_scope_is_consistent(
        connector, projects=selected_project_repository, memberships=selected_memberships
    ):
        raise HTTPException(status_code=404, detail="Connector not found")
    return await handle_connector_webhook(
        connector,
        request,
        authorization,
        x_aixion_connector_signature,
        x_aixion_signature_version,
        x_aixion_timestamp,
        x_aixion_nonce,
        connector_repository=selected_repository,
        task_repository=selected_task_repository,
        project_repository=selected_project_repository,
        workspace_memberships=selected_memberships,
    )


@router.post("/{connector_id}/simulate", response_model=ConnectorSimulationResponse)
def simulate_connector_webhook_route(
    connector_id: str,
    payload: ConnectorSimulationRequest,
    user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorSimulationResponse:
    bound = _get_bound_connector_or_404(connector_id, user, http_request)
    if bound is None:
        # Keep the existing explicitly local/demo compatibility path. When a
        # connector repository is app-bound, simulation must use the same
        # request-bound project/task/membership authorities as live webhooks.
        return simulate_connector_webhook(
            _get_connector_or_404(connector_id, user), payload
        )

    try:
        task_repository = agent_task_repository_for_request(http_request)
        project_repository = project_repository_for_request(http_request)
        membership_repository = workspace_membership_repository_for_request(http_request)
        if get_settings().postgres_auth_enabled:
            repositories = application_repositories_for_request(http_request)
            if (
                repositories.agent_tasks is not task_repository
                or repositories.projects is not project_repository
                or repositories.workspace_membership is not membership_repository
            ):
                raise RuntimeError("Connector simulation repositories do not share one runtime")
    except RuntimeError as error:
        raise HTTPException(
            status_code=503,
            detail="Connector simulation requires the request-bound connector, task, project, "
            "and workspace repositories",
        ) from error

    return simulate_connector_webhook(
        bound,
        payload,
        project_repository=project_repository,
        task_repository=task_repository,
        workspace_memberships=membership_repository,
    )


@router.get("/{connector_id}/schema-mapper", response_model=ConnectorSchemaMapperStatus)
def get_connector_mapper(
    connector_id: str, user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorSchemaMapperStatus:
    bound = _get_bound_connector_or_404(connector_id, user, http_request)
    return get_connector_schema_mapper_status(bound if bound is not None else _get_connector_or_404(connector_id, user))


@router.put("/{connector_id}/schema-mapper", response_model=ConnectorSchemaMapperStatus)
def update_connector_mapper(
    connector_id: str, payload: ConnectorSchemaMapperConfig, user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorSchemaMapperStatus:
    bound = _get_bound_connector_or_404(connector_id, user, http_request)
    connector = bound if bound is not None else _get_connector_or_404(connector_id, user)
    expected_updated_at = connector.updated_at
    status = set_connector_schema_mapper(connector, payload)
    connector.updated_at = now_utc()
    details = {"mapper_enabled": status.mapper_enabled, "mapped_fields": list(payload.field_paths.keys())}
    if bound is None:
        _audit("connector.schema_mapper_updated", connector.id, details, actor=user.email)
        store.persist()
    else:
        _commit_bound_connector_update(http_request, connector, expected_updated_at=expected_updated_at,
                                       event_type="connector.schema_mapper_updated", details=details, actor=user.email)
    return status


@router.post("/{connector_id}/schema-mapper/preview", response_model=ConnectorSchemaMapperPreviewResponse)
def preview_connector_mapper(
    connector_id: str, payload: ConnectorSchemaMapperPreviewRequest, user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorSchemaMapperPreviewResponse:
    bound = _get_bound_connector_or_404(connector_id, user, http_request)
    connector = bound if bound is not None else _get_connector_or_404(connector_id, user)
    return preview_connector_schema_mapping(connector, payload)


@router.get("/{connector_id}", response_model=ConnectorPublic)
def get_connector(
    connector_id: str, user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorPublic:
    bound = _get_bound_connector_or_404(connector_id, user, http_request)
    return _to_public(bound if bound is not None else _get_connector_or_404(connector_id, user))


@router.patch("/{connector_id}", response_model=ConnectorPublic)
def update_connector(
    connector_id: str, payload: ConnectorUpdate, user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorPublic:
    bound = _get_bound_connector_or_404(connector_id, user, http_request)
    workspace_id = (
        workspace_id_for_request(user, http_request) or user.workspace_id
        if http_request is not None else workspace_id_for_user(user)
    )
    project_repository = (
        project_repository_for_request(http_request)
        if http_request is not None and _connector_repository_or_none(http_request) is not None
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
        _commit_bound_connector_update(
            http_request,
            connector,
            expected_updated_at=expected_updated_at,
            event_type="connector.updated",
            details=details,
            actor=user.email,
        )
    return _to_public(connector)


@router.post("/{connector_id}/disable", response_model=ConnectorPublic)
def disable_connector(
    connector_id: str, user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorPublic:
    bound = _get_bound_connector_or_404(connector_id, user, http_request)
    connector = bound if bound is not None else _get_connector_or_404(connector_id, user)
    expected_updated_at = connector.updated_at
    previous_status = connector.status
    connector.status = ConnectorStatus.DISABLED
    connector.health_status = ConnectorHealthStatus.DISABLED
    connector.updated_at = now_utc()
    details = {"previous_status": previous_status, "new_status": connector.status}
    if bound is None:
        _audit("connector.disabled", connector.id, details, actor=user.email)
        store.persist()
    else:
        _commit_bound_connector_update(http_request, connector, expected_updated_at=expected_updated_at,
                                       event_type="connector.disabled", details=details, actor=user.email)
    return _to_public(connector)


@router.post("/{connector_id}/enable", response_model=ConnectorPublic)
def enable_connector(
    connector_id: str, user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorPublic:
    bound = _get_bound_connector_or_404(connector_id, user, http_request)
    connector = bound if bound is not None else _get_connector_or_404(connector_id, user)
    expected_updated_at = connector.updated_at
    previous_status = connector.status
    connector.status = ConnectorStatus.ENABLED
    connector.health_status = ConnectorHealthStatus.UNKNOWN
    connector.updated_at = now_utc()
    details = {"previous_status": previous_status, "new_status": connector.status}
    if bound is None:
        _audit("connector.enabled", connector.id, details, actor=user.email)
        store.persist()
    else:
        _commit_bound_connector_update(http_request, connector, expected_updated_at=expected_updated_at,
                                       event_type="connector.enabled", details=details, actor=user.email)
    return _to_public(connector)


@router.get("/{connector_id}/credentials", response_model=ConnectorCredentialStatus)
def get_connector_credentials(
    connector_id: str, user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
    source_invocation_id: str | None = Query(default=None, min_length=1, max_length=256),
) -> ConnectorCredentialStatus:
    bound = _get_bound_connector_or_404(connector_id, user, http_request)
    connector = bound if bound is not None else _get_connector_or_404(connector_id, user)
    if source_invocation_id is None:
        return connector_credential_status(connector)
    workspace_id = (
        workspace_id_for_request(user, http_request) or user.workspace_id
        if http_request is not None else workspace_id_for_user(user)
    )
    if not workspace_id:
        return connector_credential_status(
            connector, source_invocation_id=source_invocation_id
        )
    try:
        repository = (
            approval_decision_repository_for_request(http_request)
            if http_request is not None
            else approval_decision_repository
        )
        lease = repository.get_live_invocation_lease(
            workspace_id=workspace_id,
            source_agent_id=connector.id,
            source_invocation_id=source_invocation_id,
        )
    except (RuntimeError, ApprovalInvocationLeaseRejected) as error:
        raise HTTPException(
            status_code=503,
            detail="Connector invocation liveness is unavailable",
        ) from error
    except Exception as error:
        # A failed status read must not fall back to last_used_at or cached health.
        raise HTTPException(
            status_code=503,
            detail="Connector invocation liveness is unavailable",
        ) from error
    return connector_credential_status(
        connector,
        invocation_lease=lease,
        source_invocation_id=source_invocation_id,
    )


@router.post("/{connector_id}/secret/issue", response_model=ConnectorSecretIssueResponse)
def issue_connector_secret_route(
    connector_id: str,
    payload: ConnectorSecretIssueRequest | None = None,
    user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorSecretIssueResponse:
    bound = _get_bound_connector_or_404(connector_id, user, http_request)
    connector = bound if bound is not None else _get_connector_or_404(connector_id, user)
    expected_updated_at = connector.updated_at
    secret, secret_ref, secret_hint = issue_connector_secret(connector)
    details = {"secret_ref": secret_ref, "secret_hint": secret_hint, "note": payload.note if payload else ""}
    if bound is None:
        _audit("connector.secret_issued", connector.id, details, actor=user.email)
        store.persist()
    else:
        _commit_bound_connector_update(http_request, connector, expected_updated_at=expected_updated_at,
                                       event_type="connector.secret_issued", details=details, actor=user.email)
    return ConnectorSecretIssueResponse(connector=_to_public(connector), secret=secret, secret_hint=secret_hint)


@router.post("/{connector_id}/secret/rotate", response_model=ConnectorSecretIssueResponse)
def rotate_connector_secret_route(
    connector_id: str,
    payload: ConnectorSecretIssueRequest | None = None,
    user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorSecretIssueResponse:
    bound = _get_bound_connector_or_404(connector_id, user, http_request)
    connector = bound if bound is not None else _get_connector_or_404(connector_id, user)
    expected_updated_at = connector.updated_at
    secret, secret_ref, secret_hint = issue_connector_secret(connector)
    details = {"secret_ref": secret_ref, "secret_hint": secret_hint, "note": payload.note if payload else ""}
    if bound is None:
        _audit("connector.secret_rotated", connector.id, details, actor=user.email)
        store.persist()
    else:
        _commit_bound_connector_update(http_request, connector, expected_updated_at=expected_updated_at,
                                       event_type="connector.secret_rotated", details=details, actor=user.email)
    return ConnectorSecretIssueResponse(connector=_to_public(connector), secret=secret, secret_hint=secret_hint)


@router.post("/{connector_id}/secret/revoke", response_model=ConnectorPublic)
def revoke_connector_secret_route(
    connector_id: str, user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorPublic:
    bound = _get_bound_connector_or_404(connector_id, user, http_request)
    connector = bound if bound is not None else _get_connector_or_404(connector_id, user)
    expected_updated_at = connector.updated_at
    revoke_connector_secret(connector)
    details = {"secret_configured": False}
    if bound is None:
        _audit("connector.secret_revoked", connector.id, details, actor=user.email)
        store.persist()
    else:
        _commit_bound_connector_update(http_request, connector, expected_updated_at=expected_updated_at,
                                       event_type="connector.secret_revoked", details=details, actor=user.email)
    return _to_public(connector)


@router.post("/{connector_id}/health/success", response_model=ConnectorPublic)
def mark_connector_success(
    connector_id: str, user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorPublic:
    # A management request cannot prove that the connector authenticated or is
    # reachable. Only authenticated connector traffic may update last_used_at
    # and runtime health, so retain this endpoint as a fail-closed compatibility
    # response instead of accepting a manual liveness assertion.
    if http_request is None:
        _get_connector_or_404(connector_id, user)
    else:
        _get_bound_connector_or_404(connector_id, user, http_request)
    raise HTTPException(
        status_code=409,
        detail="Connector health is established by authenticated connector traffic; manual success is not accepted",
    )


@router.post("/{connector_id}/health/failure", response_model=ConnectorPublic)
def mark_connector_failure(
    connector_id: str,
    payload: ConnectorSecretIssueRequest | None = None,
    user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ConnectorPublic:
    # A management request is not evidence of a connector authentication
    # failure. Runtime health and failed-auth counters are updated only by
    # observed connector traffic.
    if http_request is None:
        _get_connector_or_404(connector_id, user)
    else:
        _get_bound_connector_or_404(connector_id, user, http_request)
    raise HTTPException(
        status_code=409,
        detail="Connector health is derived from authenticated connector traffic; manual failure reports are not accepted",
    )

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from psycopg import Connection

from .agent_task_models import (
    AgentTask,
    AgentTaskEvent,
    AgentTaskEventCreate,
    AgentTaskEventType,
    AgentTaskRequestedAction,
    AgentTaskStatus,
)
from .agent_task_repository import AgentTaskRepositoryConflict
from .agent_task_repository import AgentTaskRepository
from .connector_credentials import (
    hash_connector_secret,
    record_connector_auth_failure,
    record_connector_auth_success,
    safe_equal,
)
from .connector_models import AgentConnector, ConnectorAuthType, ConnectorStatus
from .connector_schema_mapper import normalize_connector_payload
from .connector_repository import (
    ConnectorRepository,
    ConnectorRepositoryConflict,
    ConnectorRepositoryScopeError,
    ConnectorWebhookReplay,
)
from .project_repository import ProjectRepository
from .settings import get_settings
from .workspace_membership_repository import WorkspaceMembershipRepository
from .approval_decisions import agent_task_repository
from .models import AgentAction, AgentProvider, AuditEvent, RiskLevel, now_utc
from .store import store
from .workspace_access import (
    agent_task_scope_is_consistent,
    agent_task_workspace_id,
    connector_scope_is_consistent,
    connector_workspace_id,
)

CONNECTOR_RATE_LIMIT_WINDOW = timedelta(minutes=1)
MAX_WEBHOOK_METADATA_KEYS = 50
HMAC_SIGNATURE_VERSION = "v1"
HMAC_TIMESTAMP_TOLERANCE_SECONDS = 300
HMAC_NONCE_TTL_SECONDS = 600


@dataclass(frozen=True)
class HmacWebhookIdentity:
    nonce: str
    timestamp: int
    body_sha256: str
    received_at: int
    receipt_expires_at: int
    replay_response: ConnectorWebhookResponse | None = None


class ConnectorWebhookPayload(BaseModel):
    action: AgentAction = AgentAction.CREATE_AGENT_TASK
    project_id: str | None = None
    title: str | None = None
    goal: str | None = None
    context: str = ""
    source_url: str | None = None
    source_session_id: str | None = None
    source_task_id: str | None = None
    requested_action: AgentTaskRequestedAction = AgentTaskRequestedAction.CREATE_WORK_ORDER
    repository: str | None = None
    branch_preference: str | None = None
    risk_hint: RiskLevel | None = None
    requires_approval: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None
    event_type: AgentTaskEventType = AgentTaskEventType.NOTE
    message: str = ""
    status: AgentTaskStatus | None = None


class ConnectorWebhookResponse(BaseModel):
    accepted: bool
    action: AgentAction
    connector_id: str
    task_id: str | None = None
    event_id: str | None = None
    message: str


def _audit(event_type: str, entity_id: str, details: dict[str, Any], actor: str) -> AuditEvent:
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


def _provider_for_connector(connector: AgentConnector) -> AgentProvider:
    mapping = {
        "ANTIGRAVITY": AgentProvider.ANTIGRAVITY,
        "CHATGPT": AgentProvider.CHATGPT,
        "CLAUDE": AgentProvider.CLAUDE,
        "CURSOR": AgentProvider.CURSOR,
        "CODEX": AgentProvider.CODEX,
    }
    return mapping.get(str(connector.provider_label), AgentProvider.OTHER)


def _action_value(action: AgentAction | str) -> str:
    return action.value if isinstance(action, AgentAction) else str(action)


def _assert_connector_action(connector: AgentConnector, action: AgentAction) -> None:
    allowed = {_action_value(value) for value in connector.allowed_actions}
    if _action_value(action) not in allowed:
        raise HTTPException(status_code=403, detail=f"Connector is not allowed to perform {action.value}")


def _assert_connector_scope(connector: AgentConnector, *, project_id: str | None = None, repository: str | None = None) -> None:
    if project_id and connector.allowed_project_ids and project_id not in connector.allowed_project_ids:
        raise HTTPException(status_code=403, detail="Connector is not allowed for this project")
    if repository and connector.allowed_repositories and repository not in connector.allowed_repositories:
        raise HTTPException(status_code=403, detail="Connector is not allowed for this repository")


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _enforce_connector_rate_limit(connector: AgentConnector, *, persist_local: bool = True) -> None:
    now = now_utc()
    window_started = _parse_iso_datetime(connector.config.get("rate_limit_window_started_at"))
    count = int(connector.config.get("rate_limit_request_count") or 0)
    if window_started is None or window_started + CONNECTOR_RATE_LIMIT_WINDOW <= now:
        connector.config = {**connector.config, "rate_limit_window_started_at": now.isoformat(), "rate_limit_request_count": 1}
        connector.updated_at = now
        return
    if count >= connector.rate_limit_per_minute:
        connector.config = {**connector.config, "rate_limit_request_count": count + 1}
        connector.updated_at = now
        if persist_local:
            _audit("connector.webhook_rate_limited", connector.id, {"connector_id": connector.id, "limit": connector.rate_limit_per_minute, "request_count": count + 1}, actor=f"connector:{connector.id}")
            store.persist()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Connector webhook rate limit exceeded")
    connector.config = {**connector.config, "rate_limit_request_count": count + 1}
    connector.updated_at = now


def _body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _hmac_v1_signing_payload(*, timestamp: str, nonce: str, body: bytes) -> str:
    return f"{HMAC_SIGNATURE_VERSION}.{timestamp}.{nonce}.{_body_hash(body)}"


def expected_hmac_v1_signature(secret_hash: str, *, timestamp: str, nonce: str, body: bytes) -> str:
    signing_payload = _hmac_v1_signing_payload(timestamp=timestamp, nonce=nonce, body=body)
    return hmac.new(secret_hash.encode("utf-8"), signing_payload.encode("utf-8"), "sha256").hexdigest()


def _extract_hmac_signature(signature_header: str) -> str:
    signature = signature_header.strip()
    prefix = f"{HMAC_SIGNATURE_VERSION}="
    if signature.startswith(prefix):
        return signature.removeprefix(prefix).strip()
    return signature


def _parse_unix_timestamp(timestamp: str | None) -> int:
    if not timestamp:
        raise HTTPException(status_code=401, detail="Missing connector HMAC timestamp")
    try:
        return int(timestamp)
    except ValueError as error:
        raise HTTPException(status_code=401, detail="Invalid connector HMAC timestamp") from error


def _clean_hmac_nonce_history(connector: AgentConnector, now_epoch: int) -> dict[str, int]:
    raw_history = connector.config.get("hmac_nonce_history")
    history = raw_history if isinstance(raw_history, dict) else {}
    cleaned: dict[str, int] = {}
    for nonce, seen_at in history.items():
        try:
            seen_at_int = int(seen_at)
        except (TypeError, ValueError):
            continue
        if now_epoch - seen_at_int <= HMAC_NONCE_TTL_SECONDS:
            cleaned[str(nonce)] = seen_at_int
    return cleaned


def _clean_hmac_nonce_results(
    connector: AgentConnector, *, now_epoch: int, valid_nonces: set[str]
) -> dict[str, dict[str, Any]]:
    raw_results = connector.config.get("hmac_nonce_results")
    results = raw_results if isinstance(raw_results, dict) else {}
    cleaned: dict[str, dict[str, Any]] = {}
    for nonce, value in results.items():
        if str(nonce) not in valid_nonces or not isinstance(value, dict):
            continue
        try:
            expires_at = int(value["expires_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if expires_at > now_epoch:
            cleaned[str(nonce)] = value
    return cleaned


def _assert_hmac_v1_not_replayed(
    connector: AgentConnector,
    *,
    nonce: str,
    timestamp: int,
    body_sha256: str,
    now_epoch: int,
) -> HmacWebhookIdentity:
    history = _clean_hmac_nonce_history(connector, now_epoch)
    results = _clean_hmac_nonce_results(
        connector, now_epoch=now_epoch, valid_nonces=set(history)
    )
    if nonce in history:
        receipt = results.get(nonce)
        response: ConnectorWebhookResponse | None = None
        if (
            receipt is not None
            and receipt.get("timestamp") == timestamp
            and receipt.get("body_sha256") == body_sha256
            and receipt.get("received_at") == history[nonce]
            and int(receipt["expires_at"])
            == min(history[nonce] + HMAC_NONCE_TTL_SECONDS, timestamp + HMAC_TIMESTAMP_TOLERANCE_SECONDS)
            and int(receipt["expires_at"]) > now_epoch
        ):
            try:
                response = ConnectorWebhookResponse.model_validate(receipt["response"])
            except (KeyError, TypeError, ValueError):
                response = None
        if response is not None:
            return HmacWebhookIdentity(
                nonce=nonce,
                timestamp=timestamp,
                body_sha256=body_sha256,
                received_at=history[nonce],
                receipt_expires_at=int(receipt["expires_at"]),
                replay_response=response,
            )
        connector.config = {
            **connector.config,
            "hmac_nonce_history": history,
            "hmac_nonce_results": results,
        }
        connector.updated_at = now_utc()
        raise HTTPException(status_code=401, detail="Connector HMAC nonce has already been used")
    history[nonce] = now_epoch
    connector.config = {
        **connector.config,
        "hmac_nonce_history": history,
        "hmac_nonce_results": results,
    }
    connector.updated_at = now_utc()
    return HmacWebhookIdentity(
        nonce=nonce,
        timestamp=timestamp,
        body_sha256=body_sha256,
        received_at=now_epoch,
        receipt_expires_at=min(
            now_epoch + HMAC_NONCE_TTL_SECONDS,
            timestamp + HMAC_TIMESTAMP_TOLERANCE_SECONDS,
        ),
    )


def _save_hmac_webhook_result(
    connector: AgentConnector,
    identity: HmacWebhookIdentity | None,
    response: ConnectorWebhookResponse,
) -> None:
    if identity is None or identity.received_at >= identity.receipt_expires_at:
        return
    history = _clean_hmac_nonce_history(connector, identity.received_at)
    if identity.nonce not in history:
        raise RuntimeError("Cannot persist a webhook receipt without its nonce claim")
    results = _clean_hmac_nonce_results(
        connector, now_epoch=identity.received_at, valid_nonces=set(history)
    )
    results[identity.nonce] = {
        "timestamp": identity.timestamp,
        "body_sha256": identity.body_sha256,
        "received_at": identity.received_at,
        "expires_at": identity.receipt_expires_at,
        "response": response.model_dump(mode="json"),
    }
    connector.config = {
        **connector.config,
        "hmac_nonce_history": history,
        "hmac_nonce_results": results,
    }


def _authenticate_hmac_v1(
    connector: AgentConnector,
    *,
    stored_hash: str,
    body: bytes,
    signature: str | None,
    signature_version: str | None,
    timestamp: str | None,
    nonce: str | None,
) -> HmacWebhookIdentity:
    if signature_version != HMAC_SIGNATURE_VERSION:
        raise HTTPException(status_code=401, detail="Unsupported connector HMAC signature version")
    if not signature:
        raise HTTPException(status_code=401, detail="Missing connector HMAC signature")
    if not nonce or len(nonce) > 128:
        raise HTTPException(status_code=401, detail="Invalid connector HMAC nonce")
    timestamp_epoch = _parse_unix_timestamp(timestamp)
    now_epoch = int(time.time())
    if abs(now_epoch - timestamp_epoch) > HMAC_TIMESTAMP_TOLERANCE_SECONDS:
        raise HTTPException(status_code=401, detail="Connector HMAC timestamp is stale")
    expected = expected_hmac_v1_signature(stored_hash, timestamp=str(timestamp_epoch), nonce=nonce, body=body)
    if not safe_equal(_extract_hmac_signature(signature), expected):
        raise HTTPException(status_code=401, detail="Invalid connector HMAC signature")
    return _assert_hmac_v1_not_replayed(
        connector,
        nonce=nonce,
        timestamp=timestamp_epoch,
        body_sha256=_body_hash(body),
        now_epoch=now_epoch,
    )


def _authenticate_connector(
    connector: AgentConnector,
    *,
    body: bytes,
    authorization: str | None,
    x_aixion_connector_signature: str | None,
    x_aixion_signature_version: str | None,
    x_aixion_timestamp: str | None,
    x_aixion_nonce: str | None,
    persist_local: bool = True,
) -> HmacWebhookIdentity | None:
    if connector.status != ConnectorStatus.ENABLED:
        record_connector_auth_failure(connector, "connector disabled")
        if persist_local:
            store.persist()
        raise HTTPException(status_code=401, detail="Connector is disabled")
    stored_hash = connector.config.get("secret_hash")
    if connector.auth_type == ConnectorAuthType.NONE:
        raise HTTPException(status_code=401, detail="Connector webhook auth type NONE is not allowed")
    if not connector.secret_ref or not stored_hash:
        record_connector_auth_failure(connector, "missing connector secret")
        if persist_local:
            store.persist()
        raise HTTPException(status_code=401, detail="Connector secret is not configured")
    hmac_identity: HmacWebhookIdentity | None = None
    if connector.auth_type in {ConnectorAuthType.BEARER, ConnectorAuthType.API_KEY}:
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            record_connector_auth_failure(connector, "missing bearer token")
            if persist_local:
                store.persist()
            raise HTTPException(status_code=401, detail="Missing connector bearer token")
        supplied_secret = authorization.removeprefix(prefix).strip()
        if not safe_equal(str(stored_hash), hash_connector_secret(supplied_secret)):
            record_connector_auth_failure(connector, "invalid bearer token")
            if persist_local:
                store.persist()
            raise HTTPException(status_code=401, detail="Invalid connector bearer token")
    elif connector.auth_type == ConnectorAuthType.HMAC:
        try:
            hmac_identity = _authenticate_hmac_v1(
                connector,
                stored_hash=str(stored_hash),
                body=body,
                signature=x_aixion_connector_signature,
                signature_version=x_aixion_signature_version,
                timestamp=x_aixion_timestamp,
                nonce=x_aixion_nonce,
            )
        except HTTPException as error:
            record_connector_auth_failure(connector, str(error.detail))
            if persist_local:
                store.persist()
            raise
    else:
        raise HTTPException(status_code=401, detail="Unsupported connector auth type")
    if hmac_identity is None or hmac_identity.replay_response is None:
        record_connector_auth_success(connector)
    return hmac_identity


def _safe_metadata(connector: AgentConnector, payload: ConnectorWebhookPayload) -> dict[str, Any]:
    metadata = dict(payload.metadata or {})
    if len(metadata) > MAX_WEBHOOK_METADATA_KEYS:
        raise HTTPException(status_code=400, detail="Connector metadata has too many keys")
    metadata.update({"connector_id": connector.id, "connector_name": connector.name, "connector_provider_label": connector.provider_label, "connector_type": connector.connector_type, "connector_webhook": True})
    return metadata


def _create_task_from_connector(
    connector: AgentConnector,
    payload: ConnectorWebhookPayload,
    *,
    task_repository: AgentTaskRepository | None = None,
    project_repository: ProjectRepository | None = None,
    workspace_memberships: WorkspaceMembershipRepository | None = None,
    project_to_store: bool = True,
    task_connection: Connection | None = None,
) -> ConnectorWebhookResponse:
    if not connector_scope_is_consistent(
        connector, projects=project_repository, memberships=workspace_memberships
    ):
        raise HTTPException(status_code=404, detail="Connector not found")
    _assert_connector_action(connector, AgentAction.CREATE_AGENT_TASK)
    if not payload.project_id:
        raise HTTPException(status_code=400, detail="project_id is required for CREATE_AGENT_TASK")
    project = (
        project_repository.get(payload.project_id)
        if project_repository is not None
        else store.projects.get(payload.project_id)
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if connector.workspace_id is not None and project.workspace_id != connector.workspace_id:
        raise HTTPException(status_code=404, detail="Project not found")
    if not payload.title or not payload.goal:
        raise HTTPException(status_code=400, detail="title and goal are required for CREATE_AGENT_TASK")
    _assert_connector_scope(connector, project_id=payload.project_id, repository=payload.repository)
    task = AgentTask(provider=_provider_for_connector(connector), project_id=payload.project_id, title=payload.title.strip(), goal=payload.goal.strip(), context=payload.context, source_url=payload.source_url, source_session_id=payload.source_session_id, source_task_id=payload.source_task_id, requested_action=payload.requested_action, repository=payload.repository, branch_preference=payload.branch_preference, risk_hint=payload.risk_hint, requires_approval=payload.requires_approval, metadata=_safe_metadata(connector, payload), external_agent_id=connector.id if project_to_store else None, external_agent_name=connector.name, workspace_id=project.workspace_id)
    event = AgentTaskEvent(task_id=task.id, event_type=AgentTaskEventType.TASK_CREATED, message="AgentTask created by generic connector webhook.", status=task.status, actor=f"connector:{connector.id}", metadata={"connector_id": connector.id, "connector_webhook": True})
    audit_event = AuditEvent(
        event_type="connector.webhook_task_created",
        entity_id=task.id,
        workspace_id=task.workspace_id,
        actor=f"connector:{connector.id}",
        details={"connector_id": connector.id, "project_id": task.project_id, "repository": task.repository, "requested_action": task.requested_action},
    )
    try:
        selected_repository = task_repository or agent_task_repository
        if task_connection is None:
            selected_repository.create(task=task, event=event, audit=audit_event)
        else:
            selected_repository.create_in_transaction(
                task_connection, task=task, event=event, audit=audit_event
            )
    except (AgentTaskRepositoryConflict, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if project_to_store:
        store.project_agent_task_creation(task, event, audit_event)
    return ConnectorWebhookResponse(accepted=True, action=AgentAction.CREATE_AGENT_TASK, connector_id=connector.id, task_id=task.id, event_id=event.id, message="AgentTask created")


def _append_task_event_from_connector(
    connector: AgentConnector,
    payload: ConnectorWebhookPayload,
    *,
    task_repository: AgentTaskRepository | None = None,
    project_repository: ProjectRepository | None = None,
    workspace_memberships: WorkspaceMembershipRepository | None = None,
    project_to_store: bool = True,
    task_connection: Connection | None = None,
) -> ConnectorWebhookResponse:
    if payload.event_type in {AgentTaskEventType.APPROVED, AgentTaskEventType.DENIED} or payload.status in {
        AgentTaskStatus.APPROVED,
        AgentTaskStatus.DENIED,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Approval decisions must be recorded through the linked approval workflow",
        )
    if not connector_scope_is_consistent(
        connector, projects=project_repository, memberships=workspace_memberships
    ):
        raise HTTPException(status_code=404, detail="Connector not found")
    _assert_connector_action(connector, AgentAction.APPEND_AGENT_TASK_EVENT)
    if not payload.task_id:
        raise HTTPException(status_code=400, detail="task_id is required for APPEND_AGENT_TASK_EVENT")
    selected_task_repository = task_repository or agent_task_repository
    task = selected_task_repository.get(payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Agent task not found")
    if project_repository is not None:
        task_project = project_repository.get(task.project_id)
        task_scope_invalid = task_project is None or task.workspace_id != task_project.workspace_id
    else:
        task_scope_invalid = (
            not agent_task_scope_is_consistent(task)
            or agent_task_workspace_id(task) != connector_workspace_id(connector)
        )
    if task_scope_invalid or task.workspace_id != connector.workspace_id:
        raise HTTPException(status_code=404, detail="Agent task not found")
    if task.external_agent_id != connector.id and task.metadata.get("connector_id") != connector.id:
        raise HTTPException(status_code=403, detail="Connector is not allowed for this task")
    _assert_connector_scope(connector, project_id=task.project_id, repository=task.repository)
    updated_task = task.model_copy(deep=True)
    if payload.status is not None:
        updated_task.status = payload.status
        updated_task.updated_at = now_utc()
    event_create = AgentTaskEventCreate(event_type=payload.event_type, message=payload.message, status=payload.status, metadata=_safe_metadata(connector, payload))
    event_metadata = dict(event_create.metadata)
    event_metadata.pop("notification_id", None)
    event = AgentTaskEvent(
        task_id=updated_task.id,
        actor=f"connector:{connector.id}",
        **event_create.model_dump(exclude={"metadata"}),
        metadata=event_metadata,
    )
    audit_event = AuditEvent(
        event_type="connector.webhook_task_event_appended",
        entity_id=updated_task.id,
        workspace_id=updated_task.workspace_id,
        actor=f"connector:{connector.id}",
        details={
            "event_id": event.id,
            "connector_id": connector.id,
            "event_type": event.event_type,
            "status": event.status,
            "message": event.message,
            "notification_id": None,
        },
    )
    try:
        if task_connection is None:
            selected_task_repository.commit_event(
                expected=task,
                updated=updated_task,
                event=event,
                audit=audit_event,
            )
        else:
            selected_task_repository.commit_event_in_transaction(
                task_connection,
                expected=task,
                updated=updated_task,
                event=event,
                audit=audit_event,
            )
    except (AgentTaskRepositoryConflict, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if project_to_store:
        store.project_agent_task_transition(updated_task, event, audit_event)
    return ConnectorWebhookResponse(accepted=True, action=AgentAction.APPEND_AGENT_TASK_EVENT, connector_id=connector.id, task_id=task.id, event_id=event.id, message="AgentTask event appended")


async def handle_connector_webhook(
    connector: AgentConnector,
    request: Request,
    authorization: str | None = Header(default=None),
    x_aixion_connector_signature: str | None = Header(default=None),
    x_aixion_signature_version: str | None = Header(default=None),
    x_aixion_timestamp: str | None = Header(default=None),
    x_aixion_nonce: str | None = Header(default=None),
    connector_repository: ConnectorRepository | None = None,
    task_repository: AgentTaskRepository | None = None,
    project_repository: ProjectRepository | None = None,
    workspace_memberships: WorkspaceMembershipRepository | None = None,
) -> ConnectorWebhookResponse:
    repository_mode = connector_repository is not None
    if repository_mode and (
        task_repository is None or project_repository is None or workspace_memberships is None
    ):
        raise HTTPException(status_code=503, detail="PostgreSQL connector webhook repositories are incomplete")
    if not connector_scope_is_consistent(
        connector, projects=project_repository, memberships=workspace_memberships
    ):
        raise HTTPException(status_code=404, detail="Connector not found")
    body = await _read_webhook_body(request)
    expected_updated_at = connector.updated_at
    try:
        hmac_identity = _authenticate_connector(
            connector,
            body=body,
            authorization=authorization,
            x_aixion_connector_signature=x_aixion_connector_signature,
            x_aixion_signature_version=x_aixion_signature_version,
            x_aixion_timestamp=x_aixion_timestamp,
            x_aixion_nonce=x_aixion_nonce,
            persist_local=not repository_mode,
        )
    except HTTPException as error:
        if repository_mode:
            _commit_webhook_connector_update(
                connector_repository,
                connector,
                expected_updated_at=expected_updated_at,
                event_type="connector.webhook_auth_failed",
                details={"reason": str(error.detail)},
            )
        raise
    if hmac_identity is not None and hmac_identity.replay_response is not None:
        return hmac_identity.replay_response
    try:
        _enforce_connector_rate_limit(connector, persist_local=not repository_mode)
    except HTTPException:
        if repository_mode:
            _commit_webhook_connector_update(
                connector_repository,
                connector,
                expected_updated_at=expected_updated_at,
                event_type="connector.webhook_rate_limited",
                details={"limit": connector.rate_limit_per_minute},
            )
        raise
    try:
        raw_payload = json.loads(body)
        normalized_payload = normalize_connector_payload(connector, raw_payload)
        payload = ConnectorWebhookPayload.model_validate(normalized_payload)
    except Exception as error:
        details = {"reason": "invalid webhook payload", "error": type(error).__name__}
        if repository_mode:
            _commit_authenticated_refusal(
                connector_repository,
                connector,
                request_nonce=x_aixion_nonce,
                expected_updated_at=expected_updated_at,
                body=body,
                details=details,
            )
        else:
            record_connector_auth_failure(connector, "invalid webhook payload")
            _audit("connector.webhook_refused", connector.id, details, actor=f"connector:{connector.id}")
            store.persist()
        raise HTTPException(status_code=400, detail="Invalid connector webhook payload") from error

    if repository_mode:
        if connector.updated_at <= expected_updated_at:
            connector.updated_at = now_utc()
        connector_audit = AuditEvent(
            event_type="connector.webhook_authenticated",
            entity_id=connector.id,
            workspace_id=connector.workspace_id,
            actor=f"connector:{connector.id}",
            details={"body_sha256": _body_hash(body), "auth_type": connector.auth_type.value},
        )

        def write_action(conn: Connection) -> ConnectorWebhookResponse:
            if payload.action == AgentAction.CREATE_AGENT_TASK:
                return _create_task_from_connector(
                    connector, payload, task_repository=task_repository,
                    project_repository=project_repository,
                    workspace_memberships=workspace_memberships,
                    project_to_store=False, task_connection=conn,
                )
            if payload.action == AgentAction.APPEND_AGENT_TASK_EVENT:
                return _append_task_event_from_connector(
                    connector, payload, task_repository=task_repository,
                    project_repository=project_repository,
                    workspace_memberships=workspace_memberships,
                    project_to_store=False, task_connection=conn,
                )
            raise HTTPException(
                status_code=403,
                detail=f"Connector webhook action is not supported: {payload.action.value}",
            )

        try:
            return connector_repository.commit_webhook_action(
                connector=connector,
                request_nonce=x_aixion_nonce,
                request_receipt=(
                    {
                        "timestamp": hmac_identity.timestamp,
                        "body_sha256": hmac_identity.body_sha256,
                        "received_at": hmac_identity.received_at,
                        "expires_at": hmac_identity.receipt_expires_at,
                    }
                    if hmac_identity is not None
                    else None
                ),
                expected_updated_at=expected_updated_at,
                audit_event=connector_audit,
                write_action=write_action,
            )
        except ConnectorWebhookReplay as error:
            if error.response is not None:
                try:
                    return ConnectorWebhookResponse.model_validate(error.response)
                except ValueError:
                    pass
            raise HTTPException(status_code=401, detail=str(error)) from error
        except ConnectorRepositoryConflict as error:
            raise HTTPException(status_code=409, detail="Connector state changed; retry webhook") from error
        except ConnectorRepositoryScopeError as error:
            raise HTTPException(status_code=404, detail="Connector not found") from error
        except HTTPException as error:
            details = {"reason": str(error.detail), "action": payload.action.value}
            _commit_authenticated_refusal(
                connector_repository,
                connector,
                request_nonce=x_aixion_nonce,
                expected_updated_at=expected_updated_at,
                body=body,
                details=details,
            )
            raise

    try:
        if payload.action == AgentAction.CREATE_AGENT_TASK:
            response = _create_task_from_connector(
                connector, payload, task_repository=task_repository,
                project_repository=project_repository, workspace_memberships=workspace_memberships,
                project_to_store=not repository_mode,
            )
        elif payload.action == AgentAction.APPEND_AGENT_TASK_EVENT:
            response = _append_task_event_from_connector(
                connector, payload, task_repository=task_repository,
                project_repository=project_repository, workspace_memberships=workspace_memberships,
                project_to_store=not repository_mode,
            )
        else:
            raise HTTPException(status_code=403, detail=f"Connector webhook action is not supported: {payload.action.value}")
    except HTTPException as error:
        record_connector_auth_failure(connector, str(error.detail))
        details = {"reason": str(error.detail), "action": payload.action.value}
        if repository_mode:
            _commit_webhook_connector_update(
                connector_repository, connector,
                expected_updated_at=expected_updated_at,
                event_type="connector.webhook_refused", details=details,
            )
        else:
            _audit("connector.webhook_refused", connector.id, details, actor=f"connector:{connector.id}")
            store.persist()
        raise
    _save_hmac_webhook_result(connector, hmac_identity, response)
    if not repository_mode:
        store.persist()
    return response


async def _read_webhook_body(request: Request) -> bytes:
    """Read at most the configured number of bytes before authentication/parsing."""
    maximum = get_settings().connector_webhook_max_body_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid connector webhook content length") from error
        if declared_length < 0:
            raise HTTPException(status_code=400, detail="Invalid connector webhook content length")
        if declared_length > maximum:
            raise HTTPException(status_code=413, detail="Connector webhook payload is too large")

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise HTTPException(status_code=413, detail="Connector webhook payload is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _commit_webhook_connector_update(
    repository: ConnectorRepository | None,
    connector: AgentConnector,
    *,
    expected_updated_at: datetime,
    event_type: str,
    details: dict[str, Any],
) -> None:
    if repository is None or connector.workspace_id is None:
        raise HTTPException(status_code=503, detail="PostgreSQL connector runtime is unavailable")
    if connector.updated_at <= expected_updated_at:
        connector.updated_at = now_utc()
    audit = AuditEvent(
        event_type=event_type,
        entity_id=connector.id,
        workspace_id=connector.workspace_id,
        actor=f"connector:{connector.id}",
        details=details,
    )
    try:
        repository.update(
            connector=connector,
            expected_updated_at=expected_updated_at,
            audit_event=audit,
        )
    except ConnectorRepositoryConflict as error:
        raise HTTPException(status_code=409, detail="Connector state changed; retry webhook") from error
    except ConnectorRepositoryScopeError as error:
        raise HTTPException(status_code=404, detail="Connector not found") from error


def _commit_authenticated_refusal(
    repository: ConnectorRepository,
    connector: AgentConnector,
    *,
    request_nonce: str | None,
    expected_updated_at: datetime,
    body: bytes,
    details: dict[str, Any],
) -> None:
    """Atomically preserve connector state and both audit rows for a refusal."""
    record_connector_auth_failure(connector, str(details.get("reason", "webhook refused")))
    if connector.updated_at <= expected_updated_at:
        connector.updated_at = now_utc()
    authenticated_audit = AuditEvent(
        event_type="connector.webhook_authenticated",
        entity_id=connector.id,
        workspace_id=connector.workspace_id,
        actor=f"connector:{connector.id}",
        details={"body_sha256": _body_hash(body), "auth_type": connector.auth_type.value},
    )
    refusal_audit = AuditEvent(
        event_type="connector.webhook_refused",
        entity_id=connector.id,
        workspace_id=connector.workspace_id,
        actor=f"connector:{connector.id}",
        details=details,
    )
    try:
        repository.commit_webhook_refusal(
            connector=connector,
            request_nonce=request_nonce,
            expected_updated_at=expected_updated_at,
            authenticated_audit=authenticated_audit,
            refusal_audit=refusal_audit,
        )
    except ConnectorWebhookReplay as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except ConnectorRepositoryConflict as error:
        raise HTTPException(status_code=409, detail="Connector state changed; retry webhook") from error
    except ConnectorRepositoryScopeError as error:
        raise HTTPException(status_code=404, detail="Connector not found") from error

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta
from typing import Any

from fastapi import Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from .agent_task_models import (
    AgentTask,
    AgentTaskEvent,
    AgentTaskEventCreate,
    AgentTaskEventType,
    AgentTaskRequestedAction,
    AgentTaskStatus,
)
from .connector_credentials import (
    hash_connector_secret,
    record_connector_auth_failure,
    record_connector_auth_success,
    safe_equal,
)
from .connector_models import AgentConnector, ConnectorAuthType, ConnectorStatus
from .connector_schema_mapper import normalize_connector_payload
from .models import AgentAction, AgentProvider, AuditEvent, RiskLevel, now_utc
from .store import store

CONNECTOR_RATE_LIMIT_WINDOW = timedelta(minutes=1)
MAX_WEBHOOK_METADATA_KEYS = 50
HMAC_SIGNATURE_VERSION = "v1"
HMAC_TIMESTAMP_TOLERANCE_SECONDS = 300
HMAC_NONCE_TTL_SECONDS = 600
HMAC_MAX_TRACKED_NONCES = 200


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
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def _provider_for_connector(connector: AgentConnector) -> AgentProvider:
    mapping = {
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


def _enforce_connector_rate_limit(connector: AgentConnector) -> None:
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
    if len(cleaned) > HMAC_MAX_TRACKED_NONCES:
        cleaned = dict(sorted(cleaned.items(), key=lambda item: item[1])[-HMAC_MAX_TRACKED_NONCES:])
    return cleaned


def _assert_hmac_v1_not_replayed(connector: AgentConnector, nonce: str, now_epoch: int) -> None:
    history = _clean_hmac_nonce_history(connector, now_epoch)
    if nonce in history:
        connector.config = {**connector.config, "hmac_nonce_history": history}
        connector.updated_at = now_utc()
        raise HTTPException(status_code=401, detail="Connector HMAC nonce has already been used")
    history[nonce] = now_epoch
    connector.config = {**connector.config, "hmac_nonce_history": history}
    connector.updated_at = now_utc()


def _authenticate_hmac_v1(
    connector: AgentConnector,
    *,
    stored_hash: str,
    body: bytes,
    signature: str | None,
    signature_version: str | None,
    timestamp: str | None,
    nonce: str | None,
) -> None:
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
    _assert_hmac_v1_not_replayed(connector, nonce, now_epoch)


def _authenticate_connector(
    connector: AgentConnector,
    *,
    body: bytes,
    authorization: str | None,
    x_aixion_connector_signature: str | None,
    x_aixion_signature_version: str | None,
    x_aixion_timestamp: str | None,
    x_aixion_nonce: str | None,
) -> None:
    if connector.status != ConnectorStatus.ENABLED:
        record_connector_auth_failure(connector, "connector disabled")
        store.persist()
        raise HTTPException(status_code=401, detail="Connector is disabled")
    stored_hash = connector.config.get("secret_hash")
    if connector.auth_type == ConnectorAuthType.NONE:
        raise HTTPException(status_code=401, detail="Connector webhook auth type NONE is not allowed")
    if not connector.secret_ref or not stored_hash:
        record_connector_auth_failure(connector, "missing connector secret")
        store.persist()
        raise HTTPException(status_code=401, detail="Connector secret is not configured")
    if connector.auth_type in {ConnectorAuthType.BEARER, ConnectorAuthType.API_KEY}:
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            record_connector_auth_failure(connector, "missing bearer token")
            store.persist()
            raise HTTPException(status_code=401, detail="Missing connector bearer token")
        supplied_secret = authorization.removeprefix(prefix).strip()
        if not safe_equal(str(stored_hash), hash_connector_secret(supplied_secret)):
            record_connector_auth_failure(connector, "invalid bearer token")
            store.persist()
            raise HTTPException(status_code=401, detail="Invalid connector bearer token")
    elif connector.auth_type == ConnectorAuthType.HMAC:
        try:
            _authenticate_hmac_v1(
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
            store.persist()
            raise
    else:
        raise HTTPException(status_code=401, detail="Unsupported connector auth type")
    record_connector_auth_success(connector)


def _safe_metadata(connector: AgentConnector, payload: ConnectorWebhookPayload) -> dict[str, Any]:
    metadata = dict(payload.metadata or {})
    if len(metadata) > MAX_WEBHOOK_METADATA_KEYS:
        raise HTTPException(status_code=400, detail="Connector metadata has too many keys")
    metadata.update({"connector_id": connector.id, "connector_name": connector.name, "connector_provider_label": connector.provider_label, "connector_type": connector.connector_type, "connector_webhook": True})
    return metadata


def _create_task_from_connector(connector: AgentConnector, payload: ConnectorWebhookPayload) -> ConnectorWebhookResponse:
    _assert_connector_action(connector, AgentAction.CREATE_AGENT_TASK)
    if not payload.project_id:
        raise HTTPException(status_code=400, detail="project_id is required for CREATE_AGENT_TASK")
    if payload.project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    if not payload.title or not payload.goal:
        raise HTTPException(status_code=400, detail="title and goal are required for CREATE_AGENT_TASK")
    _assert_connector_scope(connector, project_id=payload.project_id, repository=payload.repository)
    task = AgentTask(provider=_provider_for_connector(connector), project_id=payload.project_id, title=payload.title.strip(), goal=payload.goal.strip(), context=payload.context, source_url=payload.source_url, source_session_id=payload.source_session_id, source_task_id=payload.source_task_id, requested_action=payload.requested_action, repository=payload.repository, branch_preference=payload.branch_preference, risk_hint=payload.risk_hint, requires_approval=payload.requires_approval, metadata=_safe_metadata(connector, payload), external_agent_id=connector.id, external_agent_name=connector.name)
    store.agent_tasks[task.id] = task
    event = AgentTaskEvent(task_id=task.id, event_type=AgentTaskEventType.TASK_CREATED, message="AgentTask created by generic connector webhook.", status=task.status, actor=f"connector:{connector.id}", metadata={"connector_id": connector.id, "connector_webhook": True})
    store.agent_task_events[event.id] = event
    _audit("connector.webhook_task_created", task.id, {"connector_id": connector.id, "project_id": task.project_id, "repository": task.repository, "requested_action": task.requested_action}, actor=f"connector:{connector.id}")
    return ConnectorWebhookResponse(accepted=True, action=AgentAction.CREATE_AGENT_TASK, connector_id=connector.id, task_id=task.id, event_id=event.id, message="AgentTask created")


def _append_task_event_from_connector(connector: AgentConnector, payload: ConnectorWebhookPayload) -> ConnectorWebhookResponse:
    _assert_connector_action(connector, AgentAction.APPEND_AGENT_TASK_EVENT)
    if not payload.task_id:
        raise HTTPException(status_code=400, detail="task_id is required for APPEND_AGENT_TASK_EVENT")
    task = store.agent_tasks.get(payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Agent task not found")
    if task.external_agent_id != connector.id and task.metadata.get("connector_id") != connector.id:
        raise HTTPException(status_code=403, detail="Connector is not allowed for this task")
    _assert_connector_scope(connector, project_id=task.project_id, repository=task.repository)
    if payload.status is not None:
        task.status = payload.status
        task.updated_at = now_utc()
    event_create = AgentTaskEventCreate(event_type=payload.event_type, message=payload.message, status=payload.status, metadata=_safe_metadata(connector, payload))
    event = AgentTaskEvent(task_id=task.id, actor=f"connector:{connector.id}", **event_create.model_dump())
    store.agent_task_events[event.id] = event
    _audit("connector.webhook_task_event_appended", task.id, {"connector_id": connector.id, "event_type": event.event_type, "status": event.status}, actor=f"connector:{connector.id}")
    return ConnectorWebhookResponse(accepted=True, action=AgentAction.APPEND_AGENT_TASK_EVENT, connector_id=connector.id, task_id=task.id, event_id=event.id, message="AgentTask event appended")


async def handle_connector_webhook(
    connector: AgentConnector,
    request: Request,
    authorization: str | None = Header(default=None),
    x_aixion_connector_signature: str | None = Header(default=None),
    x_aixion_signature_version: str | None = Header(default=None),
    x_aixion_timestamp: str | None = Header(default=None),
    x_aixion_nonce: str | None = Header(default=None),
) -> ConnectorWebhookResponse:
    body = await request.body()
    _authenticate_connector(
        connector,
        body=body,
        authorization=authorization,
        x_aixion_connector_signature=x_aixion_connector_signature,
        x_aixion_signature_version=x_aixion_signature_version,
        x_aixion_timestamp=x_aixion_timestamp,
        x_aixion_nonce=x_aixion_nonce,
    )
    _enforce_connector_rate_limit(connector)
    try:
        raw_payload = await request.json()
        normalized_payload = normalize_connector_payload(connector, raw_payload)
        payload = ConnectorWebhookPayload.model_validate(normalized_payload)
    except Exception as error:
        record_connector_auth_failure(connector, "invalid webhook payload")
        _audit("connector.webhook_refused", connector.id, {"reason": "invalid webhook payload", "error": str(error)}, actor=f"connector:{connector.id}")
        store.persist()
        raise HTTPException(status_code=400, detail="Invalid connector webhook payload") from error
    try:
        if payload.action == AgentAction.CREATE_AGENT_TASK:
            response = _create_task_from_connector(connector, payload)
        elif payload.action == AgentAction.APPEND_AGENT_TASK_EVENT:
            response = _append_task_event_from_connector(connector, payload)
        else:
            raise HTTPException(status_code=403, detail=f"Connector webhook action is not supported: {payload.action.value}")
    except HTTPException as error:
        record_connector_auth_failure(connector, str(error.detail))
        _audit("connector.webhook_refused", connector.id, {"reason": error.detail, "action": payload.action}, actor=f"connector:{connector.id}")
        store.persist()
        raise
    store.persist()
    return response

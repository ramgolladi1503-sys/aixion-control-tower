from __future__ import annotations

import secrets
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import AuthUser, now_utc
from .notifications import create_notification
from .relay_auth import issue_relay_token
from .relay_models import (
    RelayActionStatus,
    RelayAdapterManifest,
    RelayCommand,
    RelayCommandClaimRequest,
    RelayCommandStatus,
    RelayCommandType,
    RelayEvent,
    RelayEventCreate,
    RelayEventType,
    RelayHeartbeatRequest,
    RelayHost,
    RelayHostPublic,
    RelayPlatform,
    RelayRegistrationCreate,
    RelayRegistrationResponse,
    RelaySession,
    RelaySessionCreate,
    RelaySessionStatus,
    RelayStatus,
    RelaySummary,
)
from .store import store
from .trust_crypto import sha256_hex
from .trust_decisions import latest_policy_decision
from .trust_models import PolicyDecisionType, ProposedAction
from .trust_service import evaluate_gateway_action


class RelayConflict(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def relay_public(relay: RelayHost) -> RelayHostPublic:
    return RelayHostPublic.model_validate(relay.model_dump())


def _normalize_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        normalized = normalized[0].lower() + normalized[1:]
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return normalized


def _path_within_root(path: str, root: str) -> bool:
    normalized_path = _normalize_path(path)
    normalized_root = _normalize_path(root)
    return normalized_path == normalized_root or normalized_path.startswith(
        normalized_root.rstrip("/") + "/"
    )


def _matching_adapter(
    relay: RelayHost,
    *,
    provider: str,
    adapter_id: str,
) -> RelayAdapterManifest:
    adapter = next(
        (
            item
            for item in relay.adapters
            if item.adapter_id == adapter_id
            and item.provider.value == provider
        ),
        None,
    )
    if adapter is None:
        raise RelayConflict(
            f"Relay does not advertise adapter {adapter_id!r} for provider {provider}."
        )
    if not adapter.available:
        raise RelayConflict(f"Relay adapter {adapter_id!r} is unavailable.")
    return adapter


def register_relay(
    payload: RelayRegistrationCreate,
    *,
    user: AuthUser,
) -> RelayRegistrationResponse:
    existing = next(
        (
            relay
            for relay in store.relay_hosts.values()
            if relay.machine_fingerprint == payload.machine_fingerprint
        ),
        None,
    )
    if existing and existing.status != RelayStatus.DISABLED:
        raise RelayConflict(
            "An enabled relay already exists for this machine fingerprint. "
            "Rotate its token instead of registering a duplicate host."
        )

    token, token_hash = issue_relay_token()
    relay = RelayHost(
        name=payload.name,
        platform=payload.platform,
        hostname=payload.hostname,
        machine_fingerprint=payload.machine_fingerprint,
        token_hash=token_hash,
        status=RelayStatus.OFFLINE,
        workspace_roots=payload.workspace_roots,
        allowed_project_ids=payload.allowed_project_ids,
        allowed_repositories=payload.allowed_repositories,
        adapters=payload.adapters,
        created_by_user_id=user.id,
        metadata=payload.metadata,
    )
    store.relay_hosts[relay.id] = relay
    store.persist()
    return RelayRegistrationResponse(relay=relay_public(relay), relay_token=token)


def rotate_relay_token(relay: RelayHost, *, user: AuthUser) -> RelayRegistrationResponse:
    if relay.status == RelayStatus.DISABLED:
        raise RelayConflict("Disabled relay tokens cannot be rotated.")
    token, token_hash = issue_relay_token()
    relay.token_hash = token_hash
    relay.updated_at = now_utc()
    relay.metadata = {
        **relay.metadata,
        "last_token_rotation_by_user_id": user.id,
        "last_token_rotation_at": relay.updated_at.isoformat(),
    }
    store.persist()
    return RelayRegistrationResponse(relay=relay_public(relay), relay_token=token)


def disable_relay(relay: RelayHost, *, user: AuthUser, reason: str) -> RelayHost:
    relay.status = RelayStatus.DISABLED
    relay.disabled_at = now_utc()
    relay.disabled_reason = reason
    relay.updated_at = now_utc()
    for command in store.relay_commands.values():
        if command.relay_id != relay.id:
            continue
        if command.status in {
            RelayCommandStatus.PENDING,
            RelayCommandStatus.LEASED,
            RelayCommandStatus.ACKNOWLEDGED,
        }:
            command.status = RelayCommandStatus.CANCELLED
            command.error = "Relay disabled by operator."
            command.completed_at = now_utc()
            command.updated_at = now_utc()
    for session in store.relay_sessions.values():
        if session.relay_id == relay.id and session.status not in {
            RelaySessionStatus.COMPLETED,
            RelaySessionStatus.FAILED,
            RelaySessionStatus.CANCELLED,
        }:
            session.status = RelaySessionStatus.CANCELLED
            session.last_error = reason or "Relay disabled by operator."
            session.completed_at = now_utc()
            session.updated_at = now_utc()
    relay.metadata = {
        **relay.metadata,
        "disabled_by_user_id": user.id,
    }
    store.persist()
    return relay


def heartbeat_relay(relay: RelayHost, payload: RelayHeartbeatRequest) -> RelayHost:
    relay.status = RelayStatus.ONLINE
    relay.last_heartbeat_at = now_utc()
    relay.updated_at = relay.last_heartbeat_at
    relay.relay_version = payload.relay_version
    relay.active_session_count = payload.active_session_count
    relay.metadata = {**relay.metadata, **payload.metadata}
    if payload.adapters:
        relay.adapters = payload.adapters
    store.persist()
    return relay


def refresh_relay_statuses(*, offline_after_seconds: int = 90) -> int:
    cutoff = _utcnow() - timedelta(seconds=offline_after_seconds)
    changed = 0
    for relay in store.relay_hosts.values():
        if relay.status in {RelayStatus.DISABLED, RelayStatus.OFFLINE}:
            continue
        if relay.last_heartbeat_at is None or relay.last_heartbeat_at < cutoff:
            relay.status = RelayStatus.OFFLINE
            relay.updated_at = now_utc()
            changed += 1
    if changed:
        store.persist()
    return changed


def _session_idempotency_key(payload: RelaySessionCreate) -> str:
    return sha256_hex(
        {
            "relay_id": payload.relay_id,
            "provider": payload.provider,
            "adapter_id": payload.adapter_id,
            "workspace_path": _normalize_path(payload.workspace_path),
            "repository": payload.repository,
            "task_id": payload.task_id,
            "run_id": payload.run_id,
            "objective": payload.objective,
        }
    )


def _command_idempotency_key(
    session: RelaySession | None,
    command_type: RelayCommandType,
    payload: dict[str, Any],
) -> str:
    return sha256_hex(
        {
            "session_id": session.id if session else None,
            "command_type": command_type,
            "payload": payload,
        }
    )


def _find_existing_command(idempotency_key: str) -> RelayCommand | None:
    return next(
        (
            command
            for command in store.relay_commands.values()
            if command.idempotency_key == idempotency_key
            and command.status
            not in {RelayCommandStatus.FAILED, RelayCommandStatus.CANCELLED}
        ),
        None,
    )


def enqueue_command(
    *,
    relay_id: str,
    session: RelaySession | None,
    command_type: RelayCommandType,
    payload: dict[str, Any],
    max_attempts: int = 3,
) -> RelayCommand:
    idempotency_key = _command_idempotency_key(session, command_type, payload)
    existing = _find_existing_command(idempotency_key)
    if existing:
        return existing
    command = RelayCommand(
        relay_id=relay_id,
        session_id=session.id if session else None,
        command_type=command_type,
        payload=payload,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
    )
    store.relay_commands[command.id] = command
    store.persist()
    return command


def create_relay_session(
    payload: RelaySessionCreate,
    *,
    user: AuthUser,
) -> RelaySession:
    relay = store.relay_hosts.get(payload.relay_id)
    if relay is None:
        raise ValueError("Relay not found.")
    if relay.status == RelayStatus.DISABLED:
        raise RelayConflict("Disabled relay cannot receive sessions.")
    _matching_adapter(
        relay,
        provider=payload.provider.value,
        adapter_id=payload.adapter_id,
    )
    if relay.allowed_project_ids and payload.project_id not in relay.allowed_project_ids:
        raise RelayConflict("Project is outside the relay allowlist.")
    if relay.allowed_repositories:
        if not payload.repository or payload.repository not in relay.allowed_repositories:
            raise RelayConflict("Repository is outside the relay allowlist.")
    if not any(
        _path_within_root(payload.workspace_path, root)
        for root in relay.workspace_roots
    ):
        raise RelayConflict("Workspace path is outside the relay workspace roots.")
    if payload.project_id and payload.project_id not in store.projects:
        raise ValueError("Project not found.")
    if payload.task_id and payload.task_id not in store.agent_tasks:
        raise ValueError("AgentTask not found.")
    if payload.run_id and payload.run_id not in store.agent_runs:
        raise ValueError("AgentRun not found.")

    session_key = _session_idempotency_key(payload)
    existing = next(
        (
            session
            for session in store.relay_sessions.values()
            if session.metadata.get("session_request_hash") == session_key
            and session.status
            not in {
                RelaySessionStatus.COMPLETED,
                RelaySessionStatus.FAILED,
                RelaySessionStatus.CANCELLED,
            }
        ),
        None,
    )
    if existing:
        return existing

    session = RelaySession(
        **payload.model_dump(exclude={"metadata"}),
        metadata={
            **payload.metadata,
            "session_request_hash": session_key,
            "created_by_user_id": user.id,
        },
    )
    store.relay_sessions[session.id] = session
    enqueue_command(
        relay_id=relay.id,
        session=session,
        command_type=RelayCommandType.START_SESSION,
        payload={
            "objective": session.objective,
            "workspace_path": session.workspace_path,
            "repository": session.repository,
            "provider": session.provider,
            "adapter_id": session.adapter_id,
            "approval_mode": session.approval_mode,
            "model": session.model,
            "max_runtime_seconds": session.max_runtime_seconds,
            "metadata": session.metadata,
        },
    )
    return session


def enqueue_session_message(
    session: RelaySession,
    *,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> RelayCommand:
    if session.status in {
        RelaySessionStatus.COMPLETED,
        RelaySessionStatus.FAILED,
        RelaySessionStatus.CANCELLED,
    }:
        raise RelayConflict("Terminal relay session cannot receive a message.")
    return enqueue_command(
        relay_id=session.relay_id,
        session=session,
        command_type=RelayCommandType.SEND_MESSAGE,
        payload={"message": message, "metadata": metadata or {}},
    )


def enqueue_session_control(
    session: RelaySession,
    *,
    command_type: RelayCommandType,
    reason: str,
) -> RelayCommand:
    if command_type not in {
        RelayCommandType.PAUSE_SESSION,
        RelayCommandType.RESUME_SESSION,
        RelayCommandType.CANCEL_SESSION,
        RelayCommandType.SYNC_SESSION,
    }:
        raise RelayConflict("Unsupported session control command.")
    if session.status in {
        RelaySessionStatus.COMPLETED,
        RelaySessionStatus.FAILED,
        RelaySessionStatus.CANCELLED,
    }:
        raise RelayConflict("Terminal relay session cannot receive control commands.")
    return enqueue_command(
        relay_id=session.relay_id,
        session=session,
        command_type=command_type,
        payload={"reason": reason},
    )


def _recover_expired_commands(relay_id: str) -> int:
    now = _utcnow()
    recovered = 0
    for command in store.relay_commands.values():
        if command.relay_id != relay_id:
            continue
        if command.status != RelayCommandStatus.LEASED:
            continue
        if command.lease_expires_at is None or command.lease_expires_at > now:
            continue
        command.lease_owner = None
        command.lease_token = None
        command.lease_expires_at = None
        command.updated_at = now_utc()
        if command.attempt_count >= command.max_attempts:
            command.status = RelayCommandStatus.EXPIRED
            command.error = "Relay command lease expired and retry budget was exhausted."
            command.completed_at = now_utc()
            session = store.relay_sessions.get(command.session_id or "")
            if session and session.status not in {
                RelaySessionStatus.COMPLETED,
                RelaySessionStatus.CANCELLED,
            }:
                session.status = RelaySessionStatus.FAILED
                session.last_error = command.error
                session.completed_at = now_utc()
                session.updated_at = now_utc()
        else:
            command.status = RelayCommandStatus.PENDING
        recovered += 1
    if recovered:
        store.persist()
    return recovered


def claim_relay_command(
    relay: RelayHost,
    payload: RelayCommandClaimRequest,
) -> RelayCommand | None:
    _recover_expired_commands(relay.id)
    command = next(
        (
            item
            for item in sorted(
                store.relay_commands.values(),
                key=lambda value: (value.created_at, value.id),
            )
            if item.relay_id == relay.id
            and item.status == RelayCommandStatus.PENDING
        ),
        None,
    )
    if command is None:
        return None
    command.status = RelayCommandStatus.LEASED
    command.attempt_count += 1
    command.lease_owner = payload.worker_id
    command.lease_token = secrets.token_urlsafe(32)
    command.lease_expires_at = _utcnow() + timedelta(seconds=payload.lease_seconds)
    command.updated_at = now_utc()
    store.persist()
    return command


def _assert_command_lease(
    command: RelayCommand,
    *,
    relay: RelayHost,
    worker_id: str,
    lease_token: str,
) -> None:
    if command.relay_id != relay.id:
        raise RelayConflict("Command belongs to another relay.")
    if command.status not in {
        RelayCommandStatus.LEASED,
        RelayCommandStatus.ACKNOWLEDGED,
    }:
        raise RelayConflict(f"Command is not active: {command.status}.")
    if command.lease_owner != worker_id or command.lease_token != lease_token:
        raise RelayConflict("Relay command lease owner or token does not match.")
    if command.lease_expires_at is None or command.lease_expires_at <= _utcnow():
        raise RelayConflict("Relay command lease has expired.")


def heartbeat_command_lease(
    command: RelayCommand,
    *,
    relay: RelayHost,
    worker_id: str,
    lease_token: str,
    lease_seconds: int,
) -> RelayCommand:
    _assert_command_lease(
        command,
        relay=relay,
        worker_id=worker_id,
        lease_token=lease_token,
    )
    command.lease_expires_at = _utcnow() + timedelta(seconds=lease_seconds)
    command.updated_at = now_utc()
    store.persist()
    return command


def acknowledge_command(
    command: RelayCommand,
    *,
    relay: RelayHost,
    worker_id: str,
    lease_token: str,
) -> RelayCommand:
    _assert_command_lease(
        command,
        relay=relay,
        worker_id=worker_id,
        lease_token=lease_token,
    )
    if command.status == RelayCommandStatus.ACKNOWLEDGED:
        return command
    command.status = RelayCommandStatus.ACKNOWLEDGED
    command.acknowledged_at = now_utc()
    command.updated_at = now_utc()
    session = store.relay_sessions.get(command.session_id or "")
    if session and command.command_type == RelayCommandType.START_SESSION:
        session.status = RelaySessionStatus.STARTING
        session.updated_at = now_utc()
    store.persist()
    return command


def complete_command(
    command: RelayCommand,
    *,
    relay: RelayHost,
    worker_id: str,
    lease_token: str,
    success: bool,
    remote_session_id: str | None,
    error: str | None,
    result: dict[str, Any],
) -> RelayCommand:
    if command.status in {
        RelayCommandStatus.SUCCEEDED,
        RelayCommandStatus.FAILED,
    }:
        if command.result == result and command.error == error:
            return command
        raise RelayConflict("Completed relay command result is immutable.")
    _assert_command_lease(
        command,
        relay=relay,
        worker_id=worker_id,
        lease_token=lease_token,
    )
    command.status = (
        RelayCommandStatus.SUCCEEDED if success else RelayCommandStatus.FAILED
    )
    command.result = result
    command.error = error
    command.completed_at = now_utc()
    command.updated_at = now_utc()
    command.lease_owner = None
    command.lease_token = None
    command.lease_expires_at = None

    session = store.relay_sessions.get(command.session_id or "")
    if session:
        if remote_session_id:
            session.remote_session_id = remote_session_id
        if not success:
            session.status = RelaySessionStatus.FAILED
            session.last_error = error or "Relay command failed."
            session.completed_at = now_utc()
        elif command.command_type == RelayCommandType.CANCEL_SESSION:
            session.status = RelaySessionStatus.CANCELLED
            session.completed_at = now_utc()
        elif command.command_type == RelayCommandType.PAUSE_SESSION:
            session.status = RelaySessionStatus.PAUSED
        elif command.command_type in {
            RelayCommandType.RESUME_SESSION,
            RelayCommandType.SEND_MESSAGE,
        }:
            session.status = RelaySessionStatus.RUNNING
        session.updated_at = now_utc()
    store.persist()
    return command


def _relay_event_material(event: RelayEvent) -> dict[str, Any]:
    return {
        "relay_id": event.relay_id,
        "session_id": event.session_id,
        "provider": event.provider,
        "adapter_id": event.adapter_id,
        "event_id": event.event_id,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "message": event.message,
        "payload": event.payload,
        "remote_session_id": event.remote_session_id,
        "remote_turn_id": event.remote_turn_id,
        "previous_hash": event.previous_hash,
        "created_at": event.created_at,
    }


def _update_session_from_event(session: RelaySession, event: RelayEvent) -> None:
    if event.remote_session_id:
        session.remote_session_id = event.remote_session_id
    if event.remote_turn_id:
        session.current_turn_id = event.remote_turn_id
    mapping = {
        RelayEventType.SESSION_STARTING: RelaySessionStatus.STARTING,
        RelayEventType.SESSION_STARTED: RelaySessionStatus.RUNNING,
        RelayEventType.SESSION_RESUMED: RelaySessionStatus.RUNNING,
        RelayEventType.APPROVAL_REQUIRED: RelaySessionStatus.WAITING_FOR_APPROVAL,
        RelayEventType.APPROVAL_RESOLVED: RelaySessionStatus.RUNNING,
        RelayEventType.SESSION_PAUSED: RelaySessionStatus.PAUSED,
        RelayEventType.SESSION_COMPLETED: RelaySessionStatus.COMPLETED,
        RelayEventType.SESSION_FAILED: RelaySessionStatus.FAILED,
        RelayEventType.SESSION_CANCELLED: RelaySessionStatus.CANCELLED,
    }
    if event.event_type in mapping:
        session.status = mapping[event.event_type]
    if event.event_type == RelayEventType.SESSION_STARTED and session.started_at is None:
        session.started_at = event.created_at
    if event.event_type in {
        RelayEventType.SESSION_COMPLETED,
        RelayEventType.SESSION_FAILED,
        RelayEventType.SESSION_CANCELLED,
    }:
        session.completed_at = event.created_at
    if event.event_type == RelayEventType.SESSION_FAILED:
        session.last_error = event.message or str(event.payload.get("error") or "")
    session.latest_event_sequence = event.sequence
    session.updated_at = now_utc()


def append_relay_event(
    relay: RelayHost,
    session: RelaySession,
    payload: RelayEventCreate,
) -> RelayEvent:
    if session.relay_id != relay.id:
        raise RelayConflict("Relay session belongs to another relay.")
    existing = next(
        (
            event
            for event in store.relay_events.values()
            if event.session_id == session.id and event.event_id == payload.event_id
        ),
        None,
    )
    if existing:
        comparable_existing = existing.model_dump(
            mode="json",
            exclude={"id", "event_hash", "previous_hash", "received_at"},
        )
        comparable_payload = {
            "relay_id": relay.id,
            "session_id": session.id,
            "provider": session.provider.value,
            "adapter_id": session.adapter_id,
            **payload.model_dump(mode="json"),
        }
        if comparable_existing != comparable_payload:
            raise RelayConflict("Relay event id was reused with a different payload.")
        return existing

    expected_sequence = session.latest_event_sequence + 1
    if payload.sequence != expected_sequence:
        raise RelayConflict(
            f"Relay event sequence must be {expected_sequence}; got {payload.sequence}."
        )
    previous = next(
        (
            event
            for event in store.relay_events.values()
            if event.session_id == session.id
            and event.sequence == session.latest_event_sequence
        ),
        None,
    )
    event = RelayEvent(
        relay_id=relay.id,
        session_id=session.id,
        provider=session.provider,
        adapter_id=session.adapter_id,
        **payload.model_dump(),
        previous_hash=previous.event_hash if previous else None,
        event_hash="pending",
    )
    event.event_hash = sha256_hex(_relay_event_material(event))
    store.relay_events[event.id] = event
    _update_session_from_event(session, event)

    if event.event_type == RelayEventType.APPROVAL_REQUIRED:
        create_notification(
            title=f"{session.provider} approval required",
            body=event.message or "An agent action needs your decision.",
            entity_type="relay_session",
            entity_id=session.id,
        )
    elif event.event_type == RelayEventType.SESSION_FAILED:
        create_notification(
            title=f"{session.provider} session failed",
            body=event.message or "Open the relay timeline for evidence.",
            entity_type="relay_session",
            entity_id=session.id,
        )
    elif event.event_type == RelayEventType.SESSION_COMPLETED:
        create_notification(
            title=f"{session.provider} session completed",
            body=event.message or "The agent session completed.",
            entity_type="relay_session",
            entity_id=session.id,
        )
    store.persist()
    return event


def verify_session_event_chain(session: RelaySession) -> tuple[bool, str]:
    events = sorted(
        (
            event
            for event in store.relay_events.values()
            if event.session_id == session.id
        ),
        key=lambda item: item.sequence,
    )
    previous_hash: str | None = None
    for expected_sequence, event in enumerate(events, start=1):
        if event.sequence != expected_sequence:
            return False, f"Expected sequence {expected_sequence}, got {event.sequence}."
        if event.previous_hash != previous_hash:
            return False, f"Predecessor mismatch at sequence {event.sequence}."
        if sha256_hex(_relay_event_material(event)) != event.event_hash:
            return False, f"Hash mismatch at sequence {event.sequence}."
        previous_hash = event.event_hash
    return True, ""


def evaluate_relay_action(
    relay: RelayHost,
    session: RelaySession,
    action: ProposedAction,
) -> RelayActionStatus:
    if session.relay_id != relay.id:
        raise RelayConflict("Relay session belongs to another relay.")
    if action.run_id and session.run_id and action.run_id != session.run_id:
        raise RelayConflict("Proposed action run does not match the relay session.")
    if action.task_id and session.task_id and action.task_id != session.task_id:
        raise RelayConflict("Proposed action task does not match the relay session.")
    normalized = action.model_copy(
        update={
            "provider": session.provider,
            "run_id": session.run_id or action.run_id,
            "task_id": session.task_id or action.task_id,
            "project_id": session.project_id or action.project_id,
            "repository": session.repository or action.repository,
            "metadata": {
                **action.metadata,
                "relay_id": relay.id,
                "relay_session_id": session.id,
                "adapter_id": session.adapter_id,
            },
        }
    )
    result = evaluate_gateway_action(normalized, actor=f"relay:{relay.id}")
    return RelayActionStatus(action=result.action, decision=result.decision)


def get_relay_action_status(
    relay: RelayHost,
    session: RelaySession,
    action_id: str,
) -> RelayActionStatus:
    action = store.proposed_actions.get(action_id)
    if action is None:
        raise ValueError("Proposed action not found.")
    if action.metadata.get("relay_id") != relay.id:
        raise RelayConflict("Proposed action belongs to another relay.")
    if action.metadata.get("relay_session_id") != session.id:
        raise RelayConflict("Proposed action belongs to another relay session.")
    decision = latest_policy_decision(action.id)
    if decision is None:
        raise RelayConflict("Proposed action has no policy decision.")
    return RelayActionStatus(action=action, decision=decision)


def relay_summary() -> RelaySummary:
    refresh_relay_statuses()
    relay_statuses = Counter(relay.status for relay in store.relay_hosts.values())
    session_statuses = Counter(
        session.status for session in store.relay_sessions.values()
    )
    command_statuses = Counter(
        command.status for command in store.relay_commands.values()
    )
    return RelaySummary(
        total_relays=len(store.relay_hosts),
        online_relays=relay_statuses[RelayStatus.ONLINE],
        offline_relays=relay_statuses[RelayStatus.OFFLINE],
        degraded_relays=relay_statuses[RelayStatus.DEGRADED],
        total_sessions=len(store.relay_sessions),
        active_sessions=sum(
            session_statuses[status]
            for status in {
                RelaySessionStatus.QUEUED,
                RelaySessionStatus.STARTING,
                RelaySessionStatus.RUNNING,
                RelaySessionStatus.WAITING_FOR_APPROVAL,
                RelaySessionStatus.PAUSED,
            }
        ),
        waiting_for_approval=session_statuses[
            RelaySessionStatus.WAITING_FOR_APPROVAL
        ],
        failed_sessions=session_statuses[RelaySessionStatus.FAILED],
        pending_commands=command_statuses[RelayCommandStatus.PENDING],
        leased_commands=(
            command_statuses[RelayCommandStatus.LEASED]
            + command_statuses[RelayCommandStatus.ACKNOWLEDGED]
        ),
    )

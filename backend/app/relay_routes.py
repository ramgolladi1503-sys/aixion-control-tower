from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from .auth import require_maintainer, require_owner, require_reviewer
from .models import AuthUser
from .relay_auth import authenticate_relay
from .relay_models import (
    RelayActionEvaluationRequest,
    RelayActionStatus,
    RelayCommand,
    RelayCommandAckRequest,
    RelayCommandClaimRequest,
    RelayCommandClaimResponse,
    RelayCommandLeaseRequest,
    RelayCommandResultRequest,
    RelayCommandType,
    RelayEvent,
    RelayEventCreate,
    RelayHeartbeatRequest,
    RelayHost,
    RelayHostPublic,
    RelayRegistrationCreate,
    RelayRegistrationResponse,
    RelaySession,
    RelaySessionControlRequest,
    RelaySessionCreate,
    RelaySessionDetail,
    RelaySessionMessageCreate,
    RelaySessionStatus,
    RelaySummary,
)
from .relay_service import (
    RelayConflict,
    acknowledge_command,
    append_relay_event,
    claim_relay_command,
    complete_command,
    create_relay_session,
    disable_relay,
    enqueue_session_control,
    enqueue_session_message,
    evaluate_relay_action,
    get_relay_action_status,
    heartbeat_command_lease,
    heartbeat_relay,
    refresh_relay_statuses,
    register_relay,
    relay_public,
    relay_summary,
    rotate_relay_token,
    verify_session_event_chain,
)
from .store import store

router = APIRouter(tags=["agent-relay"])
OwnerDependency = Depends(require_owner)
MaintainerDependency = Depends(require_maintainer)
ReviewerDependency = Depends(require_reviewer)


def _relay_or_404(relay_id: str) -> RelayHost:
    relay = store.relay_hosts.get(relay_id)
    if relay is None:
        raise HTTPException(status_code=404, detail="Relay not found")
    return relay


def _session_or_404(session_id: str) -> RelaySession:
    session = store.relay_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Relay session not found")
    return session


def _command_or_404(command_id: str) -> RelayCommand:
    command = store.relay_commands.get(command_id)
    if command is None:
        raise HTTPException(status_code=404, detail="Relay command not found")
    return command


def _authenticated_relay(
    relay_id: str,
    token: str | None,
) -> RelayHost:
    return authenticate_relay(relay_id, token)


def _relay_error(error: Exception) -> HTTPException:
    if isinstance(error, ValueError):
        return HTTPException(status_code=404, detail=str(error))
    return HTTPException(status_code=409, detail=str(error))


def _session_detail(session: RelaySession) -> RelaySessionDetail:
    relay = _relay_or_404(session.relay_id)
    commands = sorted(
        (
            command
            for command in store.relay_commands.values()
            if command.session_id == session.id
        ),
        key=lambda command: (command.created_at, command.id),
    )
    events = sorted(
        (
            event
            for event in store.relay_events.values()
            if event.session_id == session.id
        ),
        key=lambda event: event.sequence,
    )
    return RelaySessionDetail(
        session=session,
        relay=relay_public(relay),
        commands=commands,
        events=events,
    )


@router.post("/relay-hosts/register", response_model=RelayRegistrationResponse)
def register_relay_host(
    payload: RelayRegistrationCreate,
    user: AuthUser = OwnerDependency,
) -> RelayRegistrationResponse:
    try:
        return register_relay(payload, user=user)
    except RelayConflict as error:
        raise _relay_error(error) from error


@router.get("/relay-hosts", response_model=list[RelayHostPublic])
def list_relay_hosts(_: AuthUser = ReviewerDependency) -> list[RelayHostPublic]:
    refresh_relay_statuses()
    return [
        relay_public(relay)
        for relay in sorted(
            store.relay_hosts.values(),
            key=lambda relay: relay.created_at,
            reverse=True,
        )
    ]


@router.get("/relay-hosts/summary", response_model=RelaySummary)
def get_relay_summary(_: AuthUser = ReviewerDependency) -> RelaySummary:
    return relay_summary()


@router.get("/relay-hosts/{relay_id}", response_model=RelayHostPublic)
def get_relay_host(
    relay_id: str,
    _: AuthUser = ReviewerDependency,
) -> RelayHostPublic:
    refresh_relay_statuses()
    return relay_public(_relay_or_404(relay_id))


@router.post(
    "/relay-hosts/{relay_id}/rotate-token",
    response_model=RelayRegistrationResponse,
)
def rotate_relay_host_token(
    relay_id: str,
    user: AuthUser = OwnerDependency,
) -> RelayRegistrationResponse:
    try:
        return rotate_relay_token(_relay_or_404(relay_id), user=user)
    except RelayConflict as error:
        raise _relay_error(error) from error


@router.post("/relay-hosts/{relay_id}/disable", response_model=RelayHostPublic)
def disable_relay_host(
    relay_id: str,
    payload: RelaySessionControlRequest,
    user: AuthUser = OwnerDependency,
) -> RelayHostPublic:
    return relay_public(
        disable_relay(
            _relay_or_404(relay_id),
            user=user,
            reason=payload.reason,
        )
    )


@router.post("/relay-hosts/{relay_id}/heartbeat", response_model=RelayHostPublic)
def relay_heartbeat(
    relay_id: str,
    payload: RelayHeartbeatRequest,
    x_aixion_relay_token: str | None = Header(default=None),
) -> RelayHostPublic:
    relay = _authenticated_relay(relay_id, x_aixion_relay_token)
    return relay_public(heartbeat_relay(relay, payload))


@router.post(
    "/relay-hosts/{relay_id}/commands/claim",
    response_model=RelayCommandClaimResponse,
)
def claim_command(
    relay_id: str,
    payload: RelayCommandClaimRequest,
    x_aixion_relay_token: str | None = Header(default=None),
) -> RelayCommandClaimResponse:
    relay = _authenticated_relay(relay_id, x_aixion_relay_token)
    return RelayCommandClaimResponse(command=claim_relay_command(relay, payload))


@router.post(
    "/relay-hosts/{relay_id}/commands/{command_id}/ack",
    response_model=RelayCommand,
)
def acknowledge_relay_command(
    relay_id: str,
    command_id: str,
    payload: RelayCommandAckRequest,
    x_aixion_relay_token: str | None = Header(default=None),
) -> RelayCommand:
    relay = _authenticated_relay(relay_id, x_aixion_relay_token)
    try:
        return acknowledge_command(
            _command_or_404(command_id),
            relay=relay,
            worker_id=payload.worker_id,
            lease_token=payload.lease_token,
        )
    except RelayConflict as error:
        raise _relay_error(error) from error


@router.post(
    "/relay-hosts/{relay_id}/commands/{command_id}/heartbeat",
    response_model=RelayCommand,
)
def heartbeat_relay_command(
    relay_id: str,
    command_id: str,
    payload: RelayCommandLeaseRequest,
    x_aixion_relay_token: str | None = Header(default=None),
) -> RelayCommand:
    relay = _authenticated_relay(relay_id, x_aixion_relay_token)
    try:
        return heartbeat_command_lease(
            _command_or_404(command_id),
            relay=relay,
            worker_id=payload.worker_id,
            lease_token=payload.lease_token,
            lease_seconds=payload.lease_seconds,
        )
    except RelayConflict as error:
        raise _relay_error(error) from error


@router.post(
    "/relay-hosts/{relay_id}/commands/{command_id}/complete",
    response_model=RelayCommand,
)
def complete_relay_command(
    relay_id: str,
    command_id: str,
    payload: RelayCommandResultRequest,
    x_aixion_relay_token: str | None = Header(default=None),
) -> RelayCommand:
    relay = _authenticated_relay(relay_id, x_aixion_relay_token)
    try:
        return complete_command(
            _command_or_404(command_id),
            relay=relay,
            worker_id=payload.worker_id,
            lease_token=payload.lease_token,
            success=payload.success,
            remote_session_id=payload.remote_session_id,
            error=payload.error,
            result=payload.result,
        )
    except RelayConflict as error:
        raise _relay_error(error) from error


@router.post("/relay-sessions", response_model=RelaySessionDetail)
def create_session(
    payload: RelaySessionCreate,
    user: AuthUser = MaintainerDependency,
) -> RelaySessionDetail:
    try:
        return _session_detail(create_relay_session(payload, user=user))
    except (ValueError, RelayConflict) as error:
        raise _relay_error(error) from error


@router.get("/relay-sessions", response_model=list[RelaySession])
def list_sessions(
    status: RelaySessionStatus | None = None,
    relay_id: str | None = None,
    provider: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    _: AuthUser = ReviewerDependency,
) -> list[RelaySession]:
    sessions = list(store.relay_sessions.values())
    if status:
        sessions = [session for session in sessions if session.status == status]
    if relay_id:
        sessions = [session for session in sessions if session.relay_id == relay_id]
    if provider:
        sessions = [session for session in sessions if session.provider.value == provider]
    return sorted(
        sessions,
        key=lambda session: session.updated_at,
        reverse=True,
    )[:limit]


@router.get("/relay-sessions/{session_id}", response_model=RelaySessionDetail)
def get_session(
    session_id: str,
    _: AuthUser = ReviewerDependency,
) -> RelaySessionDetail:
    return _session_detail(_session_or_404(session_id))


@router.get("/relay-sessions/{session_id}/events", response_model=list[RelayEvent])
def list_session_events(
    session_id: str,
    _: AuthUser = ReviewerDependency,
) -> list[RelayEvent]:
    _session_or_404(session_id)
    return sorted(
        (
            event
            for event in store.relay_events.values()
            if event.session_id == session_id
        ),
        key=lambda event: event.sequence,
    )


@router.get("/relay-sessions/{session_id}/events/verify")
def verify_session_events(
    session_id: str,
    _: AuthUser = ReviewerDependency,
) -> dict[str, object]:
    valid, reason = verify_session_event_chain(_session_or_404(session_id))
    return {"valid": valid, "reason": reason}


@router.post(
    "/relay-sessions/{session_id}/messages",
    response_model=RelayCommand,
)
def send_session_message(
    session_id: str,
    payload: RelaySessionMessageCreate,
    _: AuthUser = MaintainerDependency,
) -> RelayCommand:
    try:
        return enqueue_session_message(
            _session_or_404(session_id),
            message=payload.message,
            metadata=payload.metadata,
        )
    except RelayConflict as error:
        raise _relay_error(error) from error


def _enqueue_control(
    session_id: str,
    payload: RelaySessionControlRequest,
    command_type: RelayCommandType,
) -> RelayCommand:
    try:
        return enqueue_session_control(
            _session_or_404(session_id),
            command_type=command_type,
            reason=payload.reason,
        )
    except RelayConflict as error:
        raise _relay_error(error) from error


@router.post("/relay-sessions/{session_id}/pause", response_model=RelayCommand)
def pause_session(
    session_id: str,
    payload: RelaySessionControlRequest,
    _: AuthUser = MaintainerDependency,
) -> RelayCommand:
    return _enqueue_control(session_id, payload, RelayCommandType.PAUSE_SESSION)


@router.post("/relay-sessions/{session_id}/resume", response_model=RelayCommand)
def resume_session(
    session_id: str,
    payload: RelaySessionControlRequest,
    _: AuthUser = MaintainerDependency,
) -> RelayCommand:
    return _enqueue_control(session_id, payload, RelayCommandType.RESUME_SESSION)


@router.post("/relay-sessions/{session_id}/cancel", response_model=RelayCommand)
def cancel_session(
    session_id: str,
    payload: RelaySessionControlRequest,
    _: AuthUser = MaintainerDependency,
) -> RelayCommand:
    return _enqueue_control(session_id, payload, RelayCommandType.CANCEL_SESSION)


@router.post(
    "/relay-hosts/{relay_id}/sessions/{session_id}/events",
    response_model=RelayEvent,
)
def post_session_event(
    relay_id: str,
    session_id: str,
    payload: RelayEventCreate,
    x_aixion_relay_token: str | None = Header(default=None),
) -> RelayEvent:
    relay = _authenticated_relay(relay_id, x_aixion_relay_token)
    try:
        return append_relay_event(relay, _session_or_404(session_id), payload)
    except RelayConflict as error:
        raise _relay_error(error) from error


@router.post(
    "/relay-hosts/{relay_id}/sessions/{session_id}/actions",
    response_model=RelayActionStatus,
)
def propose_session_action(
    relay_id: str,
    session_id: str,
    payload: RelayActionEvaluationRequest,
    x_aixion_relay_token: str | None = Header(default=None),
) -> RelayActionStatus:
    relay = _authenticated_relay(relay_id, x_aixion_relay_token)
    try:
        return evaluate_relay_action(
            relay,
            _session_or_404(session_id),
            payload.action,
        )
    except (ValueError, RelayConflict) as error:
        raise _relay_error(error) from error


@router.get(
    "/relay-hosts/{relay_id}/sessions/{session_id}/actions/{action_id}",
    response_model=RelayActionStatus,
)
def get_session_action_status(
    relay_id: str,
    session_id: str,
    action_id: str,
    x_aixion_relay_token: str | None = Header(default=None),
) -> RelayActionStatus:
    relay = _authenticated_relay(relay_id, x_aixion_relay_token)
    try:
        return get_relay_action_status(
            relay,
            _session_or_404(session_id),
            action_id,
        )
    except (ValueError, RelayConflict) as error:
        raise _relay_error(error) from error

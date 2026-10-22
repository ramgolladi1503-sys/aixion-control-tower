from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request
import psycopg
from pydantic import BaseModel, Field

from .auth import require_maintainer, require_reviewer
from .approval_decisions import approval_decision_repository
from .mcp_child_router import MCPChildServerRouter
from .mcp_gateway import (
    MCP_PENDING_FORWARD_OUTCOME_UNKNOWN,
    MCPGatewayApprovalDecisionLayer,
    MCPGatewayDecision,
    MCPGatewayToolCall,
)
from .mcp_pending_repository import (
    MCPPendingRepositoryConflict,
    MCPPendingRepositoryError,
)
from .application_repository_access import workspace_id_for_request
from .models import AuditEvent, AuthUser, MCPPendingRequest, MCPPendingStatus, now_utc
from .store import store
from .settings import get_settings
from .workspace_access import (
    approval_scope_is_consistent,
    project_workspace_id,
    workspace_id_for_user,
)

router = APIRouter(prefix="/mcp-gateway", tags=["mcp-gateway"])
ReviewerDependency = Depends(require_reviewer)
MaintainerDependency = Depends(require_maintainer)
ReadResult = TypeVar("ReadResult")

_child_router = MCPChildServerRouter()
_gateways: dict[str, MCPGatewayApprovalDecisionLayer] = {}
MCP_PENDING_RETRIED = "MCP_PENDING_RETRIED"
MCP_PENDING_LEASE_RELEASED = "MCP_PENDING_LEASE_RELEASED"


class GatewaySubmitRequest(BaseModel):
    project_id: str
    request: MCPGatewayToolCall


class PendingRetryRequest(BaseModel):
    reason: str = Field(default="", max_length=500)
    reset_attempts: bool = True


class MCPPendingHealthSummary(BaseModel):
    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    waiting_for_approval: int = 0
    forwarding: int = 0
    active_leases: int = 0
    expired_leases: int = 0
    retryable: int = 0
    dead_letter: int = 0
    terminal: int = 0
    attention_required: int = 0
    oldest_waiting_created_at: datetime | None = None
    oldest_dead_letter_created_at: datetime | None = None


def gateway_for_project(project_id: str) -> MCPGatewayApprovalDecisionLayer:
    if project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    if project_id not in _gateways:
        _gateways[project_id] = MCPGatewayApprovalDecisionLayer(_child_router, project_id=project_id)
    return _gateways[project_id]


def reset_gateway_runtime_for_test() -> None:
    _child_router.reset_for_test()
    _gateways.clear()


def child_received_count_for_test() -> int:
    return _child_router.received_count_for_test()


def child_received_count_for_server_for_test(server_name: str) -> int:
    return _child_router.received_count_for_server_for_test(server_name)


def _get_pending_or_404(pending_request_id: str) -> MCPPendingRequest:
    pending = store.mcp_pending_requests.get(pending_request_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="MCP pending request not found")
    return pending


def _pending_for_user(pending_request_id: str, user: AuthUser) -> MCPPendingRequest:
    pending = _get_pending_or_404(pending_request_id)
    workspace_id = workspace_id_for_user(user)
    if not _pending_belongs_to_workspace(pending, workspace_id):
        raise HTTPException(status_code=404, detail="MCP pending request not found")
    return pending


def _pending_belongs_to_workspace(
    pending: MCPPendingRequest,
    workspace_id: str | None,
) -> bool:
    project_workspace = project_workspace_id(pending.project_id)
    if workspace_id is None:
        return pending.workspace_id is None and project_workspace is None
    if project_workspace != workspace_id:
        return False
    # A legacy pending row may inherit ownership from its parent project, but
    # an explicit child scope that conflicts with that project is corrupt and
    # must not be disclosed or mutated by either workspace.
    return pending.workspace_id is None or pending.workspace_id == project_workspace


def _active_lease_exists(pending: MCPPendingRequest) -> bool:
    return bool(
        pending.status == MCPPendingStatus.FORWARDING
        and pending.lease_expires_at is not None
        and pending.lease_expires_at > now_utc()
    )


def _expired_lease_exists(pending: MCPPendingRequest) -> bool:
    return bool(
        pending.status == MCPPendingStatus.FORWARDING
        and pending.lease_expires_at is not None
        and pending.lease_expires_at <= now_utc()
    )


def _pending_requests_for_project(project_id: str | None) -> list[MCPPendingRequest]:
    requests = list(store.mcp_pending_requests.values())
    if project_id is not None:
        requests = [request for request in requests if request.project_id == project_id]
    return requests


def _postgres_pending_repository_for_request(request: Request):
    """Return the app-owned PostgreSQL adapter, or None for local SQLite mode.

    A partially initialized PostgreSQL runtime is an error. Falling through to
    the process-local store in that state could disclose stale or incomplete
    approval data.
    """
    state = request.app.state
    runtime = getattr(state, "postgres_runtime", None)
    repositories = getattr(state, "postgres_repositories", None)
    if not get_settings().postgres_auth_enabled:
        if runtime is not None or repositories is not None:
            raise HTTPException(
                status_code=503,
                detail="MCP PostgreSQL reads require PostgreSQL-backed authentication",
            )
        return None
    if runtime is None or repositories is None:
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL MCP pending repository runtime is unavailable",
        )
    if getattr(runtime, "repositories", None) is not repositories:
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL MCP pending repository is not bound to the active runtime",
        )
    repository = getattr(repositories, "mcp_pending_requests", None)
    if repository is None:
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL MCP pending repository is unavailable",
        )
    return repository


def _postgres_pending_read(operation: Callable[[], ReadResult]) -> ReadResult:
    try:
        return operation()
    except (MCPPendingRepositoryError, RuntimeError, psycopg.Error) as error:
        raise HTTPException(status_code=503, detail="MCP pending request data is unavailable") from error


def _pending_read_workspace_id(user: AuthUser, request: Request | None) -> str | None:
    try:
        return workspace_id_for_request(user, request) if request is not None else workspace_id_for_user(user)
    except (RuntimeError, psycopg.Error) as error:
        raise HTTPException(status_code=503, detail="MCP pending request workspace is unavailable") from error


def _require_local_mcp_write_runtime(request: Request) -> None:
    """Keep process-local MCP mutations disabled under a PostgreSQL runtime."""
    if _postgres_pending_repository_for_request(request) is not None:
        raise HTTPException(
            status_code=503,
            detail="MCP request submission and forwarding are unavailable in PostgreSQL runtime",
        )


def _audit_pending_recovery(event_type: str, pending: MCPPendingRequest, details: dict, actor: str) -> None:
    store.audit_events.append(
        AuditEvent(
            event_type=event_type,
            actor=actor,
            entity_id=pending.approval_request_id,
            details={
                "mcp_pending_request_id": pending.id,
                "project_id": pending.project_id,
                "server_name": pending.server_name,
                "tool_name": pending.tool_name,
                **details,
            },
        )
    )


def _pending_health_summary(requests: list[MCPPendingRequest]) -> MCPPendingHealthSummary:
    by_status = {status.value: 0 for status in MCPPendingStatus}
    for pending in requests:
        by_status[pending.status.value] = by_status.get(pending.status.value, 0) + 1

    waiting = [pending for pending in requests if pending.status == MCPPendingStatus.WAITING_FOR_APPROVAL]
    dead_letters = [pending for pending in requests if pending.status == MCPPendingStatus.DEAD_LETTER]
    active_leases = [pending for pending in requests if _active_lease_exists(pending)]
    expired_leases = [pending for pending in requests if _expired_lease_exists(pending)]
    retryable = [
        pending
        for pending in requests
        if pending.status == MCPPendingStatus.WAITING_FOR_APPROVAL
        or (
            pending.status == MCPPendingStatus.DEAD_LETTER
            and pending.last_error != MCP_PENDING_FORWARD_OUTCOME_UNKNOWN
        )
    ]
    terminal_statuses = {
        MCPPendingStatus.FORWARDED,
        MCPPendingStatus.BLOCKED_BY_DECISION,
        MCPPendingStatus.ORPHANED,
    }
    terminal = [pending for pending in requests if pending.status in terminal_statuses]

    return MCPPendingHealthSummary(
        total=len(requests),
        by_status=by_status,
        waiting_for_approval=len(waiting),
        forwarding=by_status.get(MCPPendingStatus.FORWARDING.value, 0),
        active_leases=len(active_leases),
        expired_leases=len(expired_leases),
        retryable=len(retryable),
        dead_letter=len(dead_letters),
        terminal=len(terminal),
        attention_required=len(dead_letters) + len(expired_leases),
        oldest_waiting_created_at=min((pending.created_at for pending in waiting), default=None),
        oldest_dead_letter_created_at=min((pending.created_at for pending in dead_letters), default=None),
    )


@router.post("/requests", response_model=MCPGatewayDecision)
def submit_gateway_request(
    payload: GatewaySubmitRequest,
    user: AuthUser = MaintainerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> MCPGatewayDecision:
    if request is not None:
        _require_local_mcp_write_runtime(request)
    workspace_id = workspace_id_for_user(user)
    if workspace_id is not None and project_workspace_id(payload.project_id) != workspace_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return gateway_for_project(payload.project_id).submit_tool_call(payload.request)


@router.get("/pending-requests", response_model=list[MCPPendingRequest])
def list_pending_gateway_requests(
    project_id: str | None = Query(default=None),
    status: MCPPendingStatus | None = Query(default=None),
    approval_request_id: str | None = Query(default=None),
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> list[MCPPendingRequest]:
    repository = _postgres_pending_repository_for_request(request) if request is not None else None
    workspace_id = _pending_read_workspace_id(user, request)
    if repository is not None:
        if workspace_id is None:
            raise HTTPException(status_code=404, detail="MCP pending request not found")
        requests = _postgres_pending_read(
            lambda: repository.list_for_workspace(
                workspace_id,
                project_id=project_id,
                status=status,
                approval_id=approval_request_id,
            )
        )
        return requests

    requests = [
        pending for pending in _pending_requests_for_project(project_id)
        if _pending_belongs_to_workspace(pending, workspace_id)
    ]
    if status is not None:
        requests = [request for request in requests if request.status == status]
    if approval_request_id is not None:
        requests = [request for request in requests if request.approval_request_id == approval_request_id]
    return sorted(requests, key=lambda request: request.created_at, reverse=True)


@router.get("/pending-requests/health", response_model=MCPPendingHealthSummary)
def pending_gateway_health(
    project_id: str | None = Query(default=None),
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> MCPPendingHealthSummary:
    repository = _postgres_pending_repository_for_request(request) if request is not None else None
    workspace_id = _pending_read_workspace_id(user, request)
    if repository is not None:
        if workspace_id is None:
            raise HTTPException(status_code=404, detail="MCP pending request not found")
        requests = _postgres_pending_read(
            lambda: repository.list_for_workspace(workspace_id, project_id=project_id)
        )
        return _pending_health_summary(requests)
    if workspace_id is None and project_id is not None and project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    return _pending_health_summary([
        request for request in _pending_requests_for_project(project_id)
        if _pending_belongs_to_workspace(request, workspace_id)
    ])


@router.get("/pending-requests/{pending_request_id}", response_model=MCPPendingRequest)
def get_pending_gateway_request(
    pending_request_id: str,
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> MCPPendingRequest:
    repository = _postgres_pending_repository_for_request(request) if request is not None else None
    if repository is not None:
        workspace_id = _pending_read_workspace_id(user, request)
        if workspace_id is None:
            raise HTTPException(status_code=404, detail="MCP pending request not found")
        pending = _postgres_pending_read(lambda: repository.get(pending_request_id))
        if pending is None or pending.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="MCP pending request not found")
        return pending
    return _pending_for_user(pending_request_id, user)


@router.post("/pending-requests/{pending_request_id}/retry", response_model=MCPPendingRequest)
def retry_pending_gateway_request(
    pending_request_id: str,
    payload: PendingRetryRequest | None = None,
    user: AuthUser = MaintainerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> MCPPendingRequest:
    if request is not None:
        repository = _postgres_pending_repository_for_request(request)
        if repository is not None:
            workspace_id = _pending_read_workspace_id(user, request)
            if workspace_id is None:
                raise HTTPException(status_code=404, detail="MCP pending request not found")
            payload = payload or PendingRetryRequest()
            retry = getattr(repository, "retry_for_workspace", None)
            if not callable(retry):
                raise HTTPException(
                    status_code=503,
                    detail="PostgreSQL MCP retry transaction is unavailable",
                )
            try:
                pending = retry(
                    pending_id=pending_request_id,
                    workspace_id=workspace_id,
                    reason=payload.reason,
                    reset_attempts=payload.reset_attempts,
                    actor=user.email,
                    now=now_utc(),
                )
            except MCPPendingRepositoryConflict as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except (MCPPendingRepositoryError, RuntimeError, psycopg.Error) as error:
                raise HTTPException(
                    status_code=503,
                    detail="PostgreSQL MCP retry transaction failed",
                ) from error
            if pending is None:
                raise HTTPException(status_code=404, detail="MCP pending request not found")
            return pending
    pending = _pending_for_user(pending_request_id, user)
    payload = payload or PendingRetryRequest()
    previous_status = pending.status

    if pending.last_error == MCP_PENDING_FORWARD_OUTCOME_UNKNOWN:
        raise HTTPException(
            status_code=409,
            detail="MCP forwarding outcome is unknown; create a new request instead of replaying this invocation",
        )

    if previous_status in {
        MCPPendingStatus.FORWARDED,
        MCPPendingStatus.BLOCKED_BY_DECISION,
        MCPPendingStatus.ORPHANED,
    }:
        raise HTTPException(status_code=409, detail=f"MCP pending request cannot be retried from {previous_status}")

    if _active_lease_exists(pending):
        raise HTTPException(status_code=409, detail="MCP pending request has an active lease")

    if previous_status == MCPPendingStatus.FORWARDING:
        MCPGatewayApprovalDecisionLayer._mark_forward_outcome_unknown(pending)
        raise HTTPException(
            status_code=409,
            detail="MCP forwarding lease expired with an unknown child-server outcome; create a new request instead of replaying",
        )

    pending.status = MCPPendingStatus.WAITING_FOR_APPROVAL
    if payload.reset_attempts:
        pending.attempts = 0
    pending.lease_owner = None
    pending.lease_expires_at = None
    pending.last_error = None
    pending.updated_at = now_utc()

    _audit_pending_recovery(
        MCP_PENDING_RETRIED,
        pending,
        {
            "reason": payload.reason,
            "previous_status": previous_status,
            "reset_attempts": payload.reset_attempts,
            "attempts": pending.attempts,
        },
        actor=user.email,
    )
    store.persist()
    return pending


@router.post("/approvals/{approval_request_id}/resolve", response_model=MCPGatewayDecision)
def resolve_gateway_request(
    approval_request_id: str,
    user: AuthUser = MaintainerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> MCPGatewayDecision:
    if request is not None:
        _require_local_mcp_write_runtime(request)
    approval = approval_decision_repository.get(approval_request_id)
    if not approval or not approval_scope_is_consistent(approval):
        raise HTTPException(status_code=404, detail="Approval request not found")
    workspace_id = workspace_id_for_user(user)
    if workspace_id is not None and project_workspace_id(approval.project_id) != workspace_id:
        raise HTTPException(status_code=404, detail="Approval request not found")
    # Refresh the compatibility view from the durable record before the gateway
    # consumes the decision; another API process may have committed it.
    store.approval_requests[approval_request_id] = approval
    gateway = gateway_for_project(approval.project_id)
    gateway.wait_handle(approval_request_id).notify_decision_changed()
    return gateway.resolve_after_side_channel_decision(approval_request_id)

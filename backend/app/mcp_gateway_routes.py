from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import require_api_key
from .mcp_child_router import MCPChildServerRouter
from .mcp_gateway import MCPGatewayApprovalDecisionLayer, MCPGatewayDecision, MCPGatewayToolCall
from .models import AuditEvent, MCPPendingRequest, MCPPendingStatus, now_utc
from .store import store

router = APIRouter(prefix="/mcp-gateway", tags=["mcp-gateway"])
AuthDependency = Depends(require_api_key)

_child_router = MCPChildServerRouter()
_gateways: dict[str, MCPGatewayApprovalDecisionLayer] = {}
MCP_PENDING_RETRIED = "MCP_PENDING_RETRIED"
MCP_PENDING_LEASE_RELEASED = "MCP_PENDING_LEASE_RELEASED"


class GatewaySubmitRequest(BaseModel):
    project_id: str
    request: MCPGatewayToolCall


class PendingRetryRequest(BaseModel):
    reason: str = ""
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


def _audit_pending_recovery(event_type: str, pending: MCPPendingRequest, details: dict) -> None:
    store.audit_events.append(
        AuditEvent(
            event_type=event_type,
            actor="mcp-operator",
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
        if pending.status in {MCPPendingStatus.WAITING_FOR_APPROVAL, MCPPendingStatus.DEAD_LETTER}
        or _expired_lease_exists(pending)
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
    _: None = AuthDependency,
) -> MCPGatewayDecision:
    return gateway_for_project(payload.project_id).submit_tool_call(payload.request)


@router.get("/pending-requests", response_model=list[MCPPendingRequest])
def list_pending_gateway_requests(
    project_id: str | None = Query(default=None),
    status: MCPPendingStatus | None = Query(default=None),
    approval_request_id: str | None = Query(default=None),
    _: None = AuthDependency,
) -> list[MCPPendingRequest]:
    requests = _pending_requests_for_project(project_id)
    if status is not None:
        requests = [request for request in requests if request.status == status]
    if approval_request_id is not None:
        requests = [request for request in requests if request.approval_request_id == approval_request_id]
    return sorted(requests, key=lambda request: request.created_at, reverse=True)


@router.get("/pending-requests/health", response_model=MCPPendingHealthSummary)
def pending_gateway_health(
    project_id: str | None = Query(default=None),
    _: None = AuthDependency,
) -> MCPPendingHealthSummary:
    if project_id is not None and project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    return _pending_health_summary(_pending_requests_for_project(project_id))


@router.get("/pending-requests/{pending_request_id}", response_model=MCPPendingRequest)
def get_pending_gateway_request(
    pending_request_id: str,
    _: None = AuthDependency,
) -> MCPPendingRequest:
    return _get_pending_or_404(pending_request_id)


@router.post("/pending-requests/{pending_request_id}/retry", response_model=MCPPendingRequest)
def retry_pending_gateway_request(
    pending_request_id: str,
    payload: PendingRetryRequest | None = None,
    _: None = AuthDependency,
) -> MCPPendingRequest:
    pending = _get_pending_or_404(pending_request_id)
    payload = payload or PendingRetryRequest()
    previous_status = pending.status

    if previous_status in {
        MCPPendingStatus.FORWARDED,
        MCPPendingStatus.BLOCKED_BY_DECISION,
        MCPPendingStatus.ORPHANED,
    }:
        raise HTTPException(status_code=409, detail=f"MCP pending request cannot be retried from {previous_status}")

    if _active_lease_exists(pending):
        raise HTTPException(status_code=409, detail="MCP pending request has an active lease")

    if previous_status == MCPPendingStatus.FORWARDING:
        _audit_pending_recovery(
            MCP_PENDING_LEASE_RELEASED,
            pending,
            {
                "reason": payload.reason,
                "previous_status": previous_status,
                "previous_lease_owner": pending.lease_owner,
                "previous_lease_expires_at": pending.lease_expires_at.isoformat() if pending.lease_expires_at else None,
            },
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
    )
    store.persist()
    return pending


@router.post("/approvals/{approval_request_id}/resolve", response_model=MCPGatewayDecision)
def resolve_gateway_request(
    approval_request_id: str,
    _: None = AuthDependency,
) -> MCPGatewayDecision:
    approval = store.approval_requests.get(approval_request_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")
    gateway = gateway_for_project(approval.project_id)
    gateway.wait_handle(approval_request_id).notify_decision_changed()
    return gateway.resolve_after_side_channel_decision(approval_request_id)

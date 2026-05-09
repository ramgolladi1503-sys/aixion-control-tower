from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .auth import require_api_key
from .child_mcp_test_server import ChildMCPTestServer
from .mcp_gateway import MCPGatewayApprovalDecisionLayer, MCPGatewayDecision, MCPGatewayToolCall
from .models import MCPPendingRequest, MCPPendingStatus
from .store import store

router = APIRouter(prefix="/mcp-gateway", tags=["mcp-gateway"])
AuthDependency = Depends(require_api_key)

_child_server = ChildMCPTestServer()
_gateways: dict[str, MCPGatewayApprovalDecisionLayer] = {}


class GatewaySubmitRequest(BaseModel):
    project_id: str
    request: MCPGatewayToolCall


def gateway_for_project(project_id: str) -> MCPGatewayApprovalDecisionLayer:
    if project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    if project_id not in _gateways:
        _gateways[project_id] = MCPGatewayApprovalDecisionLayer(_child_server, project_id=project_id)
    return _gateways[project_id]


def reset_gateway_runtime_for_test() -> None:
    _child_server.received_tool_calls.clear()
    _gateways.clear()


def child_received_count_for_test() -> int:
    return len(_child_server.received_tool_calls)


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
    requests = list(store.mcp_pending_requests.values())
    if project_id is not None:
        requests = [request for request in requests if request.project_id == project_id]
    if status is not None:
        requests = [request for request in requests if request.status == status]
    if approval_request_id is not None:
        requests = [request for request in requests if request.approval_request_id == approval_request_id]
    return sorted(requests, key=lambda request: request.created_at, reverse=True)


@router.get("/pending-requests/{pending_request_id}", response_model=MCPPendingRequest)
def get_pending_gateway_request(
    pending_request_id: str,
    _: None = AuthDependency,
) -> MCPPendingRequest:
    pending = store.mcp_pending_requests.get(pending_request_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="MCP pending request not found")
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

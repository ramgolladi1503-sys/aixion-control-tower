from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Any

from pydantic import BaseModel, Field

from .approval_integrity import compute_approval_payload_hash
from .child_mcp_test_server import ChildMCPTestServer, ChildMCPToolCall
from .models import (
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    FileChange,
    RiskAssessment,
    RiskLevel,
    now_utc,
)
from .store import store


FORWARDED_AFTER_APPROVAL = "FORWARDED_AFTER_APPROVAL"
MCP_MUTATING_TOOL_NAMES = {
    "write_file",
    "edit_file",
    "delete_file",
    "apply_patch",
    "run_command",
    "git_commit",
    "github_create_pr",
}


class MCPGatewayToolCall(BaseModel):
    server_name: str = "child-test-server"
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    requested_by: str = "mcp-client"
    mutating: bool | None = None


class MCPGatewayDecision(BaseModel):
    forwarded: bool
    approval_required: bool
    approval_request_id: str | None = None
    status: ApprovalStatus | None = None
    result: dict[str, Any] | None = None
    reason: str = ""


@dataclass
class MCPGatewayWaitHandle:
    approval_request_id: str
    _event: Event

    def notify_decision_changed(self) -> None:
        self._event.set()


class MCPGatewayApprovalDecisionLayer:
    """Approval gate in front of a child MCP server.

    Mutating tool calls are converted into approval requests and held. The child
    server only receives the exact pending call after the linked approval reaches
    APPROVED. Denied, expired, cancelled, failed, or tampered requests fail closed.
    """

    def __init__(self, child_server: ChildMCPTestServer, *, project_id: str) -> None:
        self.child_server = child_server
        self.project_id = project_id
        self._pending_calls: dict[str, MCPGatewayToolCall] = {}
        self._wait_events: dict[str, Event] = {}

    def submit_tool_call(self, call: MCPGatewayToolCall) -> MCPGatewayDecision:
        if not self._is_mutating(call):
            result = self._forward(call)
            return MCPGatewayDecision(forwarded=True, approval_required=False, result=result)

        approval = self._create_approval_request(call)
        self._pending_calls[approval.id] = call
        self._wait_events[approval.id] = Event()
        return MCPGatewayDecision(
            forwarded=False,
            approval_required=True,
            approval_request_id=approval.id,
            status=approval.status,
            reason="Mutating MCP tool call is waiting for mobile approval.",
        )

    def wait_handle(self, approval_request_id: str) -> MCPGatewayWaitHandle:
        event = self._wait_events.setdefault(approval_request_id, Event())
        return MCPGatewayWaitHandle(approval_request_id=approval_request_id, _event=event)

    def resolve_after_side_channel_decision(
        self,
        approval_request_id: str,
        *,
        timeout_seconds: float = 0.0,
    ) -> MCPGatewayDecision:
        event = self._wait_events.setdefault(approval_request_id, Event())
        if timeout_seconds > 0:
            event.wait(timeout=timeout_seconds)

        approval = store.approval_requests.get(approval_request_id)
        if not approval:
            return MCPGatewayDecision(
                forwarded=False,
                approval_required=True,
                reason="Approval request no longer exists; request was not forwarded.",
            )

        if approval.status != ApprovalStatus.APPROVED:
            return MCPGatewayDecision(
                forwarded=False,
                approval_required=True,
                approval_request_id=approval.id,
                status=approval.status,
                reason=f"Approval status is {approval.status}; request was not forwarded.",
            )

        if not approval.approved_payload_hash:
            return MCPGatewayDecision(
                forwarded=False,
                approval_required=True,
                approval_request_id=approval.id,
                status=approval.status,
                reason="Approved payload hash is missing; request was not forwarded.",
            )

        current_hash = compute_approval_payload_hash(approval)
        if current_hash != approval.approved_payload_hash:
            return MCPGatewayDecision(
                forwarded=False,
                approval_required=True,
                approval_request_id=approval.id,
                status=approval.status,
                reason="Approval payload changed after approval; request was not forwarded.",
            )

        call = self._pending_calls.pop(approval.id, None)
        if call is None:
            return MCPGatewayDecision(
                forwarded=False,
                approval_required=True,
                approval_request_id=approval.id,
                status=approval.status,
                reason="No pending MCP tool call is linked to this approval.",
            )

        result = self._forward(call)
        self._audit_forwarded_after_approval(approval, call)
        return MCPGatewayDecision(
            forwarded=True,
            approval_required=True,
            approval_request_id=approval.id,
            status=approval.status,
            result=result,
            reason="Mutating MCP tool call forwarded after approval.",
        )

    @staticmethod
    def approve_for_test(approval_request_id: str, *, actor: str = "side-channel-test") -> ApprovalRequest:
        approval = store.approval_requests[approval_request_id]
        approval.approved_payload_hash = compute_approval_payload_hash(approval)
        approval.approved_at = now_utc()
        approval.approved_by_user_id = actor
        approval.status = ApprovalStatus.APPROVED
        approval.updated_at = now_utc()
        store.persist()
        return approval

    @staticmethod
    def deny_for_test(approval_request_id: str) -> ApprovalRequest:
        approval = store.approval_requests[approval_request_id]
        approval.status = ApprovalStatus.DENIED
        approval.updated_at = now_utc()
        store.persist()
        return approval

    @staticmethod
    def expire_for_test(approval_request_id: str) -> ApprovalRequest:
        approval = store.approval_requests[approval_request_id]
        approval.status = ApprovalStatus.EXPIRED
        approval.updated_at = now_utc()
        store.persist()
        return approval

    def _is_mutating(self, call: MCPGatewayToolCall) -> bool:
        if call.mutating is not None:
            return call.mutating
        return call.tool_name in MCP_MUTATING_TOOL_NAMES

    def _create_approval_request(self, call: MCPGatewayToolCall) -> ApprovalRequest:
        approval = ApprovalRequest(
            project_id=self.project_id,
            title=f"Approve MCP tool call: {call.tool_name}",
            summary=f"Gateway intercepted mutating MCP call for {call.server_name}.{call.tool_name}",
            agent_name="mcp-gateway",
            target_branch=f"mcp-gateway/{call.tool_name}",
            files=[
                FileChange(
                    path=f"mcp://{call.server_name}/{call.tool_name}",
                    change_type="mcp_tool_call",
                    diff=f"MCP mutating tool call requires approval: {call.model_dump()}",
                    new_content=str(call.model_dump(mode="json")),
                )
            ],
            test_plan=["MCP gateway must forward only after approval."],
            rollback_plan="Deny or expire the approval; the child MCP server receives nothing.",
            risk=RiskAssessment(
                level=RiskLevel.HIGH,
                reasons=["Mutating MCP tool call intercepted by gateway."],
            ),
            status=ApprovalStatus.REQUESTED,
            source_provider=AgentProvider.OTHER,
            source_agent_name="mcp-gateway",
            source_session_id=call.session_id,
            verified_source=True,
        )
        store.approval_requests[approval.id] = approval
        store.audit_events.append(
            AuditEvent(
                event_type="mcp.approval_requested",
                actor=call.requested_by,
                entity_id=approval.id,
                details={
                    "server_name": call.server_name,
                    "tool_name": call.tool_name,
                    "mutating": True,
                },
            )
        )
        store.persist()
        return approval

    def _forward(self, call: MCPGatewayToolCall) -> dict[str, Any]:
        return self.child_server.call_tool(
            ChildMCPToolCall(
                server_name=call.server_name,
                tool_name=call.tool_name,
                arguments=call.arguments,
                session_id=call.session_id,
                requested_by=call.requested_by,
            )
        )

    def _audit_forwarded_after_approval(
        self,
        approval: ApprovalRequest,
        call: MCPGatewayToolCall,
    ) -> None:
        store.audit_events.append(
            AuditEvent(
                event_type=FORWARDED_AFTER_APPROVAL,
                actor="mcp-gateway",
                entity_id=approval.id,
                details={
                    "server_name": call.server_name,
                    "tool_name": call.tool_name,
                    "approved_payload_hash": approval.approved_payload_hash,
                },
            )
        )
        store.persist()

from __future__ import annotations

from datetime import timedelta

from app.child_mcp_test_server import ChildMCPTestServer, ChildMCPToolCall
from app.mcp_gateway import (
    FORWARDED_AFTER_APPROVAL,
    MCP_PENDING_DEAD_LETTERED,
    MCP_PENDING_FORWARD_FAILED,
    MCPGatewayApprovalDecisionLayer,
    MCPGatewayToolCall,
)
from app.models import MCPPendingStatus, Project, ProjectMode, now_utc
from app.store import store


def setup_function() -> None:
    store.reset()


class FailingChildMCPTestServer(ChildMCPTestServer):
    def call_tool(self, call: ChildMCPToolCall) -> dict:
        self.received_tool_calls.append(call)
        return {
            "ok": False,
            "server_name": call.server_name,
            "tool_name": call.tool_name,
            "error": "simulated child forwarding failure",
        }


def _gateway(child: ChildMCPTestServer | None = None) -> tuple[ChildMCPTestServer, MCPGatewayApprovalDecisionLayer]:
    project = Project(
        name="MCP Gateway Proof",
        description="Proof that mutating MCP calls wait for mobile/backend approval.",
        mode=ProjectMode.STRICT,
    )
    store.projects[project.id] = project
    selected_child = child or ChildMCPTestServer()
    gateway = MCPGatewayApprovalDecisionLayer(selected_child, project_id=project.id)
    return selected_child, gateway


def _mutating_call() -> MCPGatewayToolCall:
    return MCPGatewayToolCall(
        server_name="child-test-server",
        tool_name="update_config",
        arguments={"key": "demo", "value": "approved-value"},
        session_id="session_test",
        requested_by="codex-test-client",
        mutating=True,
    )


def test_mutating_call_waits_until_side_channel_approval_then_forwards() -> None:
    child, gateway = _gateway()

    initial = gateway.submit_tool_call(_mutating_call())

    assert initial.approval_required is True
    assert initial.forwarded is False
    assert initial.approval_request_id is not None
    assert child.received_tool_calls == []
    assert len(store.mcp_pending_requests) == 1

    gateway.approve_for_test(initial.approval_request_id)
    gateway.wait_handle(initial.approval_request_id).notify_decision_changed()
    resolved = gateway.resolve_after_side_channel_decision(initial.approval_request_id)

    assert resolved.forwarded is True
    assert resolved.approval_required is True
    assert len(child.received_tool_calls) == 1
    assert child.received_tool_calls[0].tool_name == "update_config"
    assert child.received_tool_calls[0].arguments == {"key": "demo", "value": "approved-value"}
    pending = next(iter(store.mcp_pending_requests.values()))
    assert pending.status == MCPPendingStatus.FORWARDED
    assert pending.attempts == 1
    assert pending.lease_owner is None
    assert pending.lease_expires_at is None

    audit_event_types = [event.event_type for event in store.audit_events]
    assert FORWARDED_AFTER_APPROVAL in audit_event_types


def test_resolve_is_idempotent_after_forwarding() -> None:
    child, gateway = _gateway()
    initial = gateway.submit_tool_call(_mutating_call())
    assert initial.approval_request_id is not None

    gateway.approve_for_test(initial.approval_request_id)
    first = gateway.resolve_after_side_channel_decision(initial.approval_request_id)
    second = gateway.resolve_after_side_channel_decision(initial.approval_request_id)

    assert first.forwarded is True
    assert second.forwarded is False
    assert "already final" in second.reason
    assert len(child.received_tool_calls) == 1
    assert next(iter(store.mcp_pending_requests.values())).status == MCPPendingStatus.FORWARDED
    assert [event.event_type for event in store.audit_events].count(FORWARDED_AFTER_APPROVAL) == 1


def test_pending_call_recovers_after_gateway_memory_loss() -> None:
    child, gateway = _gateway()
    initial = gateway.submit_tool_call(_mutating_call())
    assert initial.approval_request_id is not None
    project_id = store.approval_requests[initial.approval_request_id].project_id

    recovered_gateway = MCPGatewayApprovalDecisionLayer(child, project_id=project_id)
    recovered_gateway.approve_for_test(initial.approval_request_id)
    resolved = recovered_gateway.resolve_after_side_channel_decision(initial.approval_request_id)

    assert resolved.forwarded is True
    assert len(child.received_tool_calls) == 1
    assert child.received_tool_calls[0].tool_name == "update_config"
    assert next(iter(store.mcp_pending_requests.values())).status == MCPPendingStatus.FORWARDED


def test_active_pending_lease_blocks_duplicate_resolve_without_forwarding() -> None:
    child, gateway = _gateway()
    initial = gateway.submit_tool_call(_mutating_call())
    assert initial.approval_request_id is not None
    gateway.approve_for_test(initial.approval_request_id)

    pending = next(iter(store.mcp_pending_requests.values()))
    pending.status = MCPPendingStatus.FORWARDING
    pending.lease_owner = "other-worker"
    pending.lease_expires_at = now_utc() + timedelta(seconds=60)
    store.persist()

    resolved = gateway.resolve_after_side_channel_decision(initial.approval_request_id)

    assert resolved.forwarded is False
    assert "already leased" in resolved.reason
    assert child.received_tool_calls == []
    assert pending.status == MCPPendingStatus.FORWARDING
    assert pending.lease_owner == "other-worker"


def test_expired_pending_lease_can_be_recovered_and_forwarded() -> None:
    child, gateway = _gateway()
    initial = gateway.submit_tool_call(_mutating_call())
    assert initial.approval_request_id is not None
    gateway.approve_for_test(initial.approval_request_id)

    pending = next(iter(store.mcp_pending_requests.values()))
    pending.status = MCPPendingStatus.FORWARDING
    pending.lease_owner = "dead-worker"
    pending.lease_expires_at = now_utc() - timedelta(seconds=1)
    store.persist()

    resolved = gateway.resolve_after_side_channel_decision(initial.approval_request_id)

    assert resolved.forwarded is True
    assert len(child.received_tool_calls) == 1
    assert pending.status == MCPPendingStatus.FORWARDED
    assert pending.lease_owner is None
    assert pending.lease_expires_at is None


def test_failed_forwarding_releases_lease_and_keeps_pending_for_retry() -> None:
    child, gateway = _gateway(FailingChildMCPTestServer())
    initial = gateway.submit_tool_call(_mutating_call())
    assert initial.approval_request_id is not None
    gateway.approve_for_test(initial.approval_request_id)

    resolved = gateway.resolve_after_side_channel_decision(initial.approval_request_id)

    assert resolved.forwarded is False
    assert "forwarding failed" in resolved.reason
    assert len(child.received_tool_calls) == 1
    pending = next(iter(store.mcp_pending_requests.values()))
    assert pending.status == MCPPendingStatus.WAITING_FOR_APPROVAL
    assert pending.attempts == 1
    assert pending.lease_owner is None
    assert pending.lease_expires_at is None
    assert pending.last_error == "simulated child forwarding failure"
    assert MCP_PENDING_FORWARD_FAILED in [event.event_type for event in store.audit_events]
    assert FORWARDED_AFTER_APPROVAL not in [event.event_type for event in store.audit_events]


def test_failed_forwarding_dead_letters_after_max_attempts() -> None:
    child, gateway = _gateway(FailingChildMCPTestServer())
    initial = gateway.submit_tool_call(_mutating_call())
    assert initial.approval_request_id is not None
    gateway.approve_for_test(initial.approval_request_id)

    pending = next(iter(store.mcp_pending_requests.values()))
    pending.max_attempts = 1
    store.persist()

    resolved = gateway.resolve_after_side_channel_decision(initial.approval_request_id)
    second = gateway.resolve_after_side_channel_decision(initial.approval_request_id)

    assert resolved.forwarded is False
    assert pending.status == MCPPendingStatus.DEAD_LETTER
    assert pending.attempts == 1
    assert len(child.received_tool_calls) == 1
    assert MCP_PENDING_DEAD_LETTERED in [event.event_type for event in store.audit_events]
    assert second.forwarded is False
    assert "already final" in second.reason
    assert len(child.received_tool_calls) == 1


def test_denied_mutating_call_is_never_forwarded() -> None:
    child, gateway = _gateway()

    initial = gateway.submit_tool_call(_mutating_call())
    assert initial.approval_request_id is not None
    gateway.deny_for_test(initial.approval_request_id)
    gateway.wait_handle(initial.approval_request_id).notify_decision_changed()

    resolved = gateway.resolve_after_side_channel_decision(initial.approval_request_id)

    assert resolved.forwarded is False
    assert "request was not forwarded" in resolved.reason
    assert child.received_tool_calls == []
    assert next(iter(store.mcp_pending_requests.values())).status == MCPPendingStatus.BLOCKED_BY_DECISION
    assert FORWARDED_AFTER_APPROVAL not in [event.event_type for event in store.audit_events]


def test_resolve_is_idempotent_after_denial() -> None:
    child, gateway = _gateway()
    initial = gateway.submit_tool_call(_mutating_call())
    assert initial.approval_request_id is not None

    gateway.deny_for_test(initial.approval_request_id)
    first = gateway.resolve_after_side_channel_decision(initial.approval_request_id)
    second = gateway.resolve_after_side_channel_decision(initial.approval_request_id)

    assert first.forwarded is False
    assert second.forwarded is False
    assert "already final" in second.reason
    assert child.received_tool_calls == []
    assert next(iter(store.mcp_pending_requests.values())).status == MCPPendingStatus.BLOCKED_BY_DECISION


def test_expired_mutating_call_is_never_forwarded() -> None:
    child, gateway = _gateway()

    initial = gateway.submit_tool_call(_mutating_call())
    assert initial.approval_request_id is not None
    gateway.expire_for_test(initial.approval_request_id)
    gateway.wait_handle(initial.approval_request_id).notify_decision_changed()

    resolved = gateway.resolve_after_side_channel_decision(initial.approval_request_id)

    assert resolved.forwarded is False
    assert "request was not forwarded" in resolved.reason
    assert child.received_tool_calls == []
    assert next(iter(store.mcp_pending_requests.values())).status == MCPPendingStatus.BLOCKED_BY_DECISION
    assert FORWARDED_AFTER_APPROVAL not in [event.event_type for event in store.audit_events]


def test_read_only_call_forwards_without_approval() -> None:
    child, gateway = _gateway()

    resolved = gateway.submit_tool_call(
        MCPGatewayToolCall(
            server_name="child-test-server",
            tool_name="read_status",
            arguments={"key": "demo"},
            mutating=False,
        )
    )

    assert resolved.forwarded is True
    assert resolved.approval_required is False
    assert resolved.approval_request_id is None
    assert len(child.received_tool_calls) == 1
    assert child.received_tool_calls[0].tool_name == "read_status"
    assert store.mcp_pending_requests == {}

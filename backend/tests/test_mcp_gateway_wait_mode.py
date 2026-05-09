from __future__ import annotations

from app.child_mcp_test_server import ChildMCPTestServer
from app.mcp_gateway import (
    FORWARDED_AFTER_APPROVAL,
    MCPGatewayApprovalDecisionLayer,
    MCPGatewayToolCall,
)
from app.models import Project, ProjectMode
from app.store import store


def setup_function() -> None:
    store.reset()


def _gateway() -> tuple[ChildMCPTestServer, MCPGatewayApprovalDecisionLayer]:
    project = Project(
        name="MCP Gateway Proof",
        description="Proof that mutating MCP calls wait for mobile/backend approval.",
        mode=ProjectMode.STRICT,
    )
    store.projects[project.id] = project
    child = ChildMCPTestServer()
    gateway = MCPGatewayApprovalDecisionLayer(child, project_id=project.id)
    return child, gateway


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

    gateway.approve_for_test(initial.approval_request_id)
    gateway.wait_handle(initial.approval_request_id).notify_decision_changed()
    resolved = gateway.resolve_after_side_channel_decision(initial.approval_request_id)

    assert resolved.forwarded is True
    assert resolved.approval_required is True
    assert len(child.received_tool_calls) == 1
    assert child.received_tool_calls[0].tool_name == "update_config"
    assert child.received_tool_calls[0].arguments == {"key": "demo", "value": "approved-value"}

    audit_event_types = [event.event_type for event in store.audit_events]
    assert FORWARDED_AFTER_APPROVAL in audit_event_types


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
    assert FORWARDED_AFTER_APPROVAL not in [event.event_type for event in store.audit_events]


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

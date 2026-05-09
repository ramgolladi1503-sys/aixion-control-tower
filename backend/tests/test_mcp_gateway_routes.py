from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.mcp_gateway import FORWARDED_AFTER_APPROVAL
from app.mcp_gateway_routes import child_received_count_for_test, reset_gateway_runtime_for_test
from app.models import MCPPendingStatus
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()
    reset_gateway_runtime_for_test()


def _create_project(name: str = "MCP Gateway HTTP Proof") -> str:
    response = client.post(
        "/projects",
        json={"name": name, "description": "HTTP adapter proof"},
    )
    assert response.status_code == 200
    return response.json()["id"]


def _submit_mutating_request(project_id: str) -> dict:
    response = client.post(
        "/mcp-gateway/requests",
        json={
            "project_id": project_id,
            "request": {
                "server_name": "child-test-server",
                "tool_name": "update_config",
                "arguments": {"key": "demo", "value": "approved-value"},
                "session_id": "http_session_test",
                "requested_by": "http-test-client",
                "mutating": True,
            },
        },
    )
    assert response.status_code == 200
    return response.json()


def test_http_mcp_gateway_waits_then_forwards_after_mobile_decision() -> None:
    project_id = _create_project()

    initial = _submit_mutating_request(project_id)

    assert initial["forwarded"] is False
    assert initial["approval_required"] is True
    approval_id = initial["approval_request_id"]
    assert approval_id is not None
    assert child_received_count_for_test() == 0
    assert len(store.mcp_pending_requests) == 1

    decision = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approve", "reason": "HTTP wait-mode proof"},
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "APPROVED"

    resolved = client.post(f"/mcp-gateway/approvals/{approval_id}/resolve")

    assert resolved.status_code == 200
    resolved_body = resolved.json()
    assert resolved_body["forwarded"] is True
    assert resolved_body["approval_required"] is True
    assert child_received_count_for_test() == 1
    assert next(iter(store.mcp_pending_requests.values())).status == MCPPendingStatus.FORWARDED

    audit_response = client.get("/audit")
    assert audit_response.status_code == 200
    assert FORWARDED_AFTER_APPROVAL in [event["event_type"] for event in audit_response.json()]


def test_http_mcp_gateway_resolve_is_idempotent_after_forwarding() -> None:
    project_id = _create_project()
    initial = _submit_mutating_request(project_id)
    approval_id = initial["approval_request_id"]
    assert approval_id is not None

    decision = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approve", "reason": "Approve once"},
    )
    assert decision.status_code == 200

    first = client.post(f"/mcp-gateway/approvals/{approval_id}/resolve")
    second = client.post(f"/mcp-gateway/approvals/{approval_id}/resolve")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["forwarded"] is True
    assert second.json()["forwarded"] is False
    assert "already final" in second.json()["reason"]
    assert child_received_count_for_test() == 1
    assert next(iter(store.mcp_pending_requests.values())).status == MCPPendingStatus.FORWARDED
    assert [event.event_type for event in store.audit_events].count(FORWARDED_AFTER_APPROVAL) == 1


def test_http_mcp_gateway_recovers_pending_request_after_runtime_reset() -> None:
    project_id = _create_project()
    initial = _submit_mutating_request(project_id)
    approval_id = initial["approval_request_id"]
    assert approval_id is not None
    assert child_received_count_for_test() == 0
    assert len(store.mcp_pending_requests) == 1

    reset_gateway_runtime_for_test()
    assert child_received_count_for_test() == 0

    decision = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approve", "reason": "Recovered after runtime reset"},
    )
    assert decision.status_code == 200

    resolved = client.post(f"/mcp-gateway/approvals/{approval_id}/resolve")

    assert resolved.status_code == 200
    assert resolved.json()["forwarded"] is True
    assert child_received_count_for_test() == 1
    assert next(iter(store.mcp_pending_requests.values())).status == MCPPendingStatus.FORWARDED


def test_http_mcp_gateway_denied_request_never_forwards() -> None:
    project_id = _create_project()
    initial = _submit_mutating_request(project_id)
    approval_id = initial["approval_request_id"]

    decision = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "deny", "reason": "Do not allow this call"},
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "DENIED"

    resolved = client.post(f"/mcp-gateway/approvals/{approval_id}/resolve")

    assert resolved.status_code == 200
    assert resolved.json()["forwarded"] is False
    assert child_received_count_for_test() == 0
    assert next(iter(store.mcp_pending_requests.values())).status == MCPPendingStatus.BLOCKED_BY_DECISION
    assert FORWARDED_AFTER_APPROVAL not in [event.event_type for event in store.audit_events]


def test_http_mcp_gateway_resolve_is_idempotent_after_denial() -> None:
    project_id = _create_project()
    initial = _submit_mutating_request(project_id)
    approval_id = initial["approval_request_id"]

    client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "deny", "reason": "Deny once"},
    )
    first = client.post(f"/mcp-gateway/approvals/{approval_id}/resolve")
    second = client.post(f"/mcp-gateway/approvals/{approval_id}/resolve")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["forwarded"] is False
    assert second.json()["forwarded"] is False
    assert "already final" in second.json()["reason"]
    assert child_received_count_for_test() == 0
    assert next(iter(store.mcp_pending_requests.values())).status == MCPPendingStatus.BLOCKED_BY_DECISION


def test_http_mcp_pending_status_api_lists_filters_and_reads_detail() -> None:
    project_one = _create_project("Project One")
    project_two = _create_project("Project Two")
    first = _submit_mutating_request(project_one)
    second = _submit_mutating_request(project_two)

    all_pending = client.get("/mcp-gateway/pending-requests")
    assert all_pending.status_code == 200
    assert len(all_pending.json()) == 2

    project_one_pending = client.get(f"/mcp-gateway/pending-requests?project_id={project_one}")
    assert project_one_pending.status_code == 200
    assert len(project_one_pending.json()) == 1
    assert project_one_pending.json()[0]["project_id"] == project_one

    waiting = client.get(
        f"/mcp-gateway/pending-requests?status={MCPPendingStatus.WAITING_FOR_APPROVAL}"
    )
    assert waiting.status_code == 200
    assert len(waiting.json()) == 2

    by_approval = client.get(
        f"/mcp-gateway/pending-requests?approval_request_id={first['approval_request_id']}"
    )
    assert by_approval.status_code == 200
    assert len(by_approval.json()) == 1
    pending_id = by_approval.json()[0]["id"]

    detail = client.get(f"/mcp-gateway/pending-requests/{pending_id}")
    assert detail.status_code == 200
    assert detail.json()["approval_request_id"] == first["approval_request_id"]
    assert detail.json()["status"] == MCPPendingStatus.WAITING_FOR_APPROVAL

    client.post(
        f"/approvals/{second['approval_request_id']}/decision",
        json={"decision": "deny", "reason": "block second"},
    )
    client.post(f"/mcp-gateway/approvals/{second['approval_request_id']}/resolve")

    blocked = client.get(f"/mcp-gateway/pending-requests?status={MCPPendingStatus.BLOCKED_BY_DECISION}")
    assert blocked.status_code == 200
    assert len(blocked.json()) == 1
    assert blocked.json()[0]["approval_request_id"] == second["approval_request_id"]


def test_http_mcp_pending_detail_returns_404_for_missing_id() -> None:
    response = client.get("/mcp-gateway/pending-requests/mcp_pending_missing")

    assert response.status_code == 404

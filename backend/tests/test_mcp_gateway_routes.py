from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.mcp_gateway import FORWARDED_AFTER_APPROVAL
from app.mcp_gateway_routes import child_received_count_for_test, reset_gateway_runtime_for_test
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()
    reset_gateway_runtime_for_test()


def _create_project() -> str:
    response = client.post(
        "/projects",
        json={"name": "MCP Gateway HTTP Proof", "description": "HTTP adapter proof"},
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

    audit_response = client.get("/audit")
    assert audit_response.status_code == 200
    assert FORWARDED_AFTER_APPROVAL in [event["event_type"] for event in audit_response.json()]


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
    assert FORWARDED_AFTER_APPROVAL not in [event.event_type for event in store.audit_events]

from __future__ import annotations

import os
from datetime import timedelta
from types import SimpleNamespace

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.mcp_gateway import FORWARDED_AFTER_APPROVAL, MCP_PENDING_FORWARD_OUTCOME_UNKNOWN
from app.mcp_gateway_routes import (
    MCP_PENDING_RETRIED,
    child_received_count_for_test,
    reset_gateway_runtime_for_test,
)
from app.models import ApprovalStatus, MCPPendingStatus, now_utc
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


def _pending() -> object:
    return next(iter(store.mcp_pending_requests.values()))


def _select_production_without_postgres_auth(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.mcp_gateway_routes.get_settings",
        lambda: SimpleNamespace(is_production=True, postgres_auth_enabled=False),
    )


def test_production_gateway_submit_fails_closed_without_postgres_runtime(monkeypatch) -> None:
    project_id = _create_project()
    _select_production_without_postgres_auth(monkeypatch)

    response = client.post(
        "/mcp-gateway/requests",
        json={
            "project_id": project_id,
            "request": {
                "server_name": "child-test-server",
                "tool_name": "update_config",
                "arguments": {"key": "demo", "value": "must-not-be-local"},
                "mutating": True,
            },
        },
    )

    assert response.status_code == 503
    assert "active PostgreSQL runtime" in response.json()["detail"]
    assert store.mcp_pending_requests == {}
    assert store.approval_requests == {}
    assert child_received_count_for_test() == 0


def test_production_gateway_retry_fails_closed_without_mutating_local_pending(monkeypatch) -> None:
    project_id = _create_project()
    _submit_mutating_request(project_id)
    pending = _pending()
    pending.status = MCPPendingStatus.DEAD_LETTER
    pending.last_error = "retry fixture"
    store.persist()
    previous = pending.model_copy(deep=True)
    previous_audit = list(store.audit_events)
    _select_production_without_postgres_auth(monkeypatch)

    response = client.post(
        f"/mcp-gateway/pending-requests/{pending.id}/retry",
        json={"reason": "must not mutate local state"},
    )

    assert response.status_code == 503
    assert pending == previous
    assert store.audit_events == previous_audit


def test_production_gateway_resolve_fails_closed_without_forwarding_locally(monkeypatch) -> None:
    project_id = _create_project()
    submitted = _submit_mutating_request(project_id)
    approval_id = submitted["approval_request_id"]
    pending = _pending()
    pending_before = pending.model_copy(deep=True)
    decision = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approve", "reason": "guard regression fixture"},
    )
    assert decision.status_code == 200
    audit_before = list(store.audit_events)
    _select_production_without_postgres_auth(monkeypatch)

    response = client.post(f"/mcp-gateway/approvals/{approval_id}/resolve")

    assert response.status_code == 503
    assert pending == pending_before
    assert store.audit_events == audit_before
    assert child_received_count_for_test() == 0


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

    # Exercise the API path as if this process missed the committed decision.
    store.approval_requests.pop(approval_id)
    resolved = client.post(f"/mcp-gateway/approvals/{approval_id}/resolve")

    assert resolved.status_code == 200
    assert store.approval_requests[approval_id].status.value == "APPROVED"
    resolved_body = resolved.json()
    assert resolved_body["forwarded"] is True
    assert resolved_body["approval_required"] is True
    assert child_received_count_for_test() == 1
    assert _pending().status == MCPPendingStatus.FORWARDED

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
    assert _pending().status == MCPPendingStatus.FORWARDED
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
    assert _pending().status == MCPPendingStatus.FORWARDED


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
    assert _pending().status == MCPPendingStatus.BLOCKED_BY_DECISION
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
    assert _pending().status == MCPPendingStatus.BLOCKED_BY_DECISION


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


def test_http_mcp_pending_health_summary_counts_queue_risk_states() -> None:
    project_id = _create_project()
    for _ in range(5):
        _submit_mutating_request(project_id)
    pending_requests = list(store.mcp_pending_requests.values())
    pending_requests[0].status = MCPPendingStatus.WAITING_FOR_APPROVAL
    pending_requests[1].status = MCPPendingStatus.FORWARDING
    pending_requests[1].lease_owner = "active-worker"
    pending_requests[1].lease_expires_at = now_utc() + timedelta(seconds=60)
    pending_requests[2].status = MCPPendingStatus.FORWARDING
    pending_requests[2].lease_owner = "dead-worker"
    pending_requests[2].lease_expires_at = now_utc() - timedelta(seconds=1)
    pending_requests[3].status = MCPPendingStatus.DEAD_LETTER
    pending_requests[4].status = MCPPendingStatus.FORWARDED
    store.persist()

    response = client.get("/mcp-gateway/pending-requests/health")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert body["by_status"][MCPPendingStatus.WAITING_FOR_APPROVAL] == 1
    assert body["by_status"][MCPPendingStatus.FORWARDING] == 2
    assert body["by_status"][MCPPendingStatus.DEAD_LETTER] == 1
    assert body["by_status"][MCPPendingStatus.FORWARDED] == 1
    assert body["waiting_for_approval"] == 1
    assert body["forwarding"] == 2
    assert body["active_leases"] == 1
    assert body["expired_leases"] == 1
    assert body["dead_letter"] == 1
    assert body["terminal"] == 1
    assert body["retryable"] == 2
    assert body["attention_required"] == 2
    assert body["oldest_waiting_created_at"] is not None
    assert body["oldest_dead_letter_created_at"] is not None


def test_http_mcp_pending_health_summary_filters_by_project() -> None:
    project_one = _create_project("Project One")
    project_two = _create_project("Project Two")
    _submit_mutating_request(project_one)
    _submit_mutating_request(project_two)
    pending_requests = list(store.mcp_pending_requests.values())
    pending_requests[0].status = MCPPendingStatus.DEAD_LETTER
    pending_requests[1].status = MCPPendingStatus.WAITING_FOR_APPROVAL
    store.persist()

    project_one_health = client.get(f"/mcp-gateway/pending-requests/health?project_id={project_one}")
    project_two_health = client.get(f"/mcp-gateway/pending-requests/health?project_id={project_two}")

    assert project_one_health.status_code == 200
    assert project_two_health.status_code == 200
    assert project_one_health.json()["total"] == 1
    assert project_one_health.json()["dead_letter"] == 1
    assert project_one_health.json()["attention_required"] == 1
    assert project_two_health.json()["total"] == 1
    assert project_two_health.json()["waiting_for_approval"] == 1
    assert project_two_health.json()["attention_required"] == 0


def test_http_mcp_pending_health_summary_returns_404_for_unknown_project() -> None:
    response = client.get("/mcp-gateway/pending-requests/health?project_id=project_missing")

    assert response.status_code == 404


def test_http_mcp_pending_retry_dead_letter_clears_error_and_resets_attempts() -> None:
    project_id = _create_project()
    _submit_mutating_request(project_id)
    pending = _pending()
    pending.status = MCPPendingStatus.DEAD_LETTER
    pending.attempts = 3
    pending.max_attempts = 3
    pending.last_error = "child server unavailable"
    pending.lease_owner = "old-worker"
    pending.lease_expires_at = now_utc() - timedelta(seconds=1)
    store.persist()

    response = client.post(
        f"/mcp-gateway/pending-requests/{pending.id}/retry",
        json={"reason": "operator fixed child endpoint", "reset_attempts": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == MCPPendingStatus.WAITING_FOR_APPROVAL
    assert body["attempts"] == 0
    assert body["last_error"] is None
    assert body["lease_owner"] is None
    assert body["lease_expires_at"] is None
    assert MCP_PENDING_RETRIED in [event.event_type for event in store.audit_events]


def test_http_mcp_pending_retry_dead_letters_expired_forwarding_lease_without_replay() -> None:
    project_id = _create_project()
    _submit_mutating_request(project_id)
    pending = _pending()
    pending.status = MCPPendingStatus.FORWARDING
    pending.attempts = 2
    pending.lease_owner = "dead-worker"
    pending.lease_expires_at = now_utc() - timedelta(seconds=1)
    pending.last_error = "lease expired mid-forward"
    store.persist()

    response = client.post(
        f"/mcp-gateway/pending-requests/{pending.id}/retry",
        json={"reason": "release stale lease", "reset_attempts": False},
    )

    assert response.status_code == 409
    body = response.json()
    assert "unknown child-server outcome" in body["detail"]
    assert pending.status == MCPPendingStatus.DEAD_LETTER
    assert pending.attempts == 2
    assert pending.lease_owner is None
    assert pending.lease_expires_at is None
    assert pending.last_error == MCP_PENDING_FORWARD_OUTCOME_UNKNOWN
    event_types = [event.event_type for event in store.audit_events]
    assert "mcp.forward_outcome_unknown" in event_types
    assert MCP_PENDING_RETRIED not in event_types
    assert child_received_count_for_test() == 0


def test_http_mcp_resolver_dead_letters_expired_forwarding_lease_without_replay() -> None:
    project_id = _create_project()
    submitted = _submit_mutating_request(project_id)
    pending = _pending()
    pending.status = MCPPendingStatus.FORWARDING
    pending.attempts = 1
    pending.lease_owner = "dead-worker"
    pending.lease_expires_at = now_utc() - timedelta(seconds=1)
    approval = store.approval_requests[submitted["approval_request_id"]]
    approval.status = ApprovalStatus.APPROVED
    from app.approval_integrity import compute_approval_payload_hash

    approval.approved_payload_hash_version = 2
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    store.persist()

    response = client.post(f"/mcp-gateway/approvals/{approval.id}/resolve")

    assert response.status_code == 200
    assert response.json()["forwarded"] is False
    assert "unknown child-server outcome" in response.json()["reason"]
    assert pending.status == MCPPendingStatus.DEAD_LETTER
    assert pending.last_error == MCP_PENDING_FORWARD_OUTCOME_UNKNOWN
    assert child_received_count_for_test() == 0
    assert sum(event.event_type == "mcp.forward_outcome_unknown" for event in store.audit_events) == 1


def test_http_mcp_pending_retry_blocks_active_forwarding_lease() -> None:
    project_id = _create_project()
    _submit_mutating_request(project_id)
    pending = _pending()
    pending.status = MCPPendingStatus.FORWARDING
    pending.lease_owner = "active-worker"
    pending.lease_expires_at = now_utc() + timedelta(seconds=60)
    store.persist()

    response = client.post(
        f"/mcp-gateway/pending-requests/{pending.id}/retry",
        json={"reason": "should not steal active lease"},
    )

    assert response.status_code == 409
    assert "active lease" in response.json()["detail"]
    assert pending.status == MCPPendingStatus.FORWARDING
    assert pending.lease_owner == "active-worker"


def test_http_mcp_pending_retry_blocks_terminal_non_retriable_statuses() -> None:
    project_id = _create_project()
    _submit_mutating_request(project_id)
    pending = _pending()

    for status in [
        MCPPendingStatus.FORWARDED,
        MCPPendingStatus.BLOCKED_BY_DECISION,
        MCPPendingStatus.ORPHANED,
    ]:
        pending.status = status
        store.persist()
        response = client.post(
            f"/mcp-gateway/pending-requests/{pending.id}/retry",
            json={"reason": "invalid retry"},
        )
        assert response.status_code == 409
        assert "cannot be retried" in response.json()["detail"]


def test_http_mcp_pending_detail_returns_404_for_missing_id() -> None:
    response = client.get("/mcp-gateway/pending-requests/mcp_pending_missing")

    assert response.status_code == 404


def test_http_mcp_pending_retry_returns_404_for_missing_id() -> None:
    response = client.post("/mcp-gateway/pending-requests/mcp_pending_missing/retry", json={})

    assert response.status_code == 404

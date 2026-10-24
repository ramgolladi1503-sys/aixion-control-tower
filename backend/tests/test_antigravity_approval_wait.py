from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.approval_integrity import compute_approval_payload_hash
from app.approval_decisions import approval_decision_repository
from app.agent_auth import revoke_external_agent_token
from app.models import ApprovalRequest
from app.main import app
from app.models import ApprovalStatus, Project, Workspace, now_utc
from app.store import store

client = TestClient(app)
HOOK_PATH = Path(__file__).parents[2] / "integrations" / "antigravity" / "aixion_pre_tool_hook.py"
HOOK_SPEC = importlib.util.spec_from_file_location("antigravity_expiry_hook", HOOK_PATH)
assert HOOK_SPEC and HOOK_SPEC.loader
antigravity_hook = importlib.util.module_from_spec(HOOK_SPEC)
HOOK_SPEC.loader.exec_module(antigravity_hook)


def setup_function() -> None:
    store.reset()


def _setup_agent(*, actions: list[str] | None = None) -> tuple[Project, str, str]:
    workspace = next(iter(store.workspaces.values()), None)
    if workspace is None:
        workspace = Workspace(name="Antigravity approval test", owner_user_id="dev_user")
        store.workspaces[workspace.id] = workspace
    project = Project(
        name="Antigravity approval", description="test", workspace_id=workspace.id
    )
    store.projects[project.id] = project
    store.persist()
    response = client.post(
        "/agents",
        json={
            "provider": "ANTIGRAVITY",
            "display_name": "Antigravity local hook",
            "auth_type": "API_KEY",
            "allowed_project_ids": [project.id],
            "allowed_actions": actions or [
                "CREATE_APPROVAL", "READ_APPROVAL_DECISION", "CONSUME_APPROVAL_DECISION",
                "EXPIRE_OWN_APPROVAL"
            ],
            "enabled": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    return project, body["agent"]["id"], body["agent_token"]


def _headers(agent_id: str, token: str) -> dict[str, str]:
    return {"X-Aixion-Agent-Id": agent_id, "X-Aixion-Agent-Token": token}


def _approval(project_id: str, headers: dict[str, str]) -> str:
    response = client.post(
        "/agents/approvals",
        headers=headers,
        json={
            "project_id": project_id,
            "title": "Antigravity requests tool permission",
            "summary": "Review exact tool input before allowing this invocation.",
            "agent_name": "Antigravity",
            "target_branch": "current-workspace",
            "files": [{
                "path": "antigravity://tool/run_command",
                "change_type": "tool_call",
                "diff": '{"command":"echo approval-test"}',
            }],
            "test_plan": ["Review the displayed tool and arguments"],
            "rollback_plan": "Deny or let the request expire; no tool runs without approval.",
            "source_session_id": "session-test-1",
            "source_invocation_id": "antigravity:v1:hook-test",
        },
    )
    assert response.status_code == 200
    assert response.json()["source_invocation_id"] == "antigravity:v1:hook-test"
    created_audit = next(
        event for event in store.audit_events
        if event.event_type == "approval.created_by_agent" and event.entity_id == response.json()["id"]
    )
    assert created_audit.details["source_invocation_id"] == "antigravity:v1:hook-test"
    return response.json()["id"]


def test_invocation_retry_reuses_one_approval_and_changed_payload_conflicts() -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    payload = {
        "project_id": project.id,
        "title": "Antigravity requests tool permission",
        "summary": "Review exact tool input before allowing this invocation.",
        "agent_name": "Antigravity",
        "target_branch": "current-workspace",
        "files": [{
            "path": "antigravity://tool/run_command",
            "change_type": "tool_call",
            "diff": '{"command":"echo retry-safe"}',
        }],
        "test_plan": ["Review the displayed tool and arguments"],
        "rollback_plan": "Deny or let the request expire; no tool runs without approval.",
        "source_session_id": "session-retry-test",
        "source_invocation_id": "antigravity:v1:retry-test",
    }

    first = client.post("/agents/approvals", headers=headers, json=payload)
    retry = client.post("/agents/approvals", headers=headers, json=payload)
    changed = client.post(
        "/agents/approvals",
        headers=headers,
        json={**payload, "summary": "Different action on the same invocation."},
    )

    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json()["id"] == first.json()["id"]
    assert changed.status_code == 409
    assert "different approval payload" in changed.json()["detail"]
    approval_id = first.json()["id"]
    assert sum(
        event.event_type == "approval.created_by_agent" and event.entity_id == approval_id
        for event in store.audit_events
    ) == 1
    assert sum(item.entity_id == approval_id for item in store.notifications.values()) == 1


def test_local_agent_approval_accepts_work_order_from_sqlite_repository() -> None:
    project, agent_id, token = _setup_agent()
    work_order_response = client.post(
        "/work-orders",
        json={
            "project_id": project.id,
            "goal": "Review a local linked work order",
            "context": "SQLite local mode keeps its existing project scope checks.",
        },
    )
    assert work_order_response.status_code == 200, work_order_response.text

    response = client.post(
        "/agents/approvals",
        headers=_headers(agent_id, token),
        json={
            "project_id": project.id,
            "work_order_id": work_order_response.json()["id"],
            "title": "Review linked local work order",
            "summary": "The local approval validates its linked SQLite work order.",
            "agent_name": "Antigravity",
            "target_branch": "current-workspace",
            "files": [{
                "path": "antigravity://tool/run_command",
                "change_type": "tool_call",
                "diff": '{"command":"echo linked-local-work-order"}',
            }],
            "source_session_id": "linked-local-work-order-session",
            "source_invocation_id": f"linked-local-work-order-{uuid.uuid4().hex}",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["work_order_id"] == work_order_response.json()["id"]


def test_approval_decision_is_visible_to_only_owning_scoped_agent() -> None:
    project, owner_id, owner_token = _setup_agent()
    _, other_id, other_token = _setup_agent()
    approval_id = _approval(project.id, _headers(owner_id, owner_token))

    assert client.get(
        f"/agents/approvals/{approval_id}/decision",
        headers=_headers(owner_id, owner_token),
    ).json()["status"] == "PENDING_REVIEW"
    denied = client.get(
        f"/agents/approvals/{approval_id}/decision",
        headers=_headers(other_id, other_token),
    )
    assert denied.status_code == 403


def test_invocation_heartbeat_returns_exact_scoped_lease_contract() -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)

    heartbeat = client.post(
        f"/agents/approvals/{approval_id}/heartbeat",
        headers=headers,
        json={"source_invocation_id": "antigravity:v1:hook-test"},
    )

    assert heartbeat.status_code == 200
    body = heartbeat.json()
    assert set(body) == {
        "approval_id", "source_agent_id", "source_invocation_id",
        "heartbeat_at", "expires_at", "live",
    }
    assert body["approval_id"] == approval_id
    assert body["source_agent_id"] == agent_id
    assert body["source_invocation_id"] == "antigravity:v1:hook-test"
    assert isinstance(body["heartbeat_at"], str) and body["heartbeat_at"]
    assert isinstance(body["expires_at"], str) and body["expires_at"]
    assert body["live"] is True


def test_invocation_heartbeat_rejects_scope_mismatch_and_revoked_credential() -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)
    mismatch = client.post(
        f"/agents/approvals/{approval_id}/heartbeat",
        headers=headers,
        json={"source_invocation_id": "another-invocation"},
    )
    assert mismatch.status_code == 409

    assert client.post(f"/agents/{agent_id}/token/revoke").status_code == 200
    revoked = client.post(
        f"/agents/approvals/{approval_id}/heartbeat",
        headers=headers,
        json={"source_invocation_id": "antigravity:v1:hook-test"},
    )
    assert revoked.status_code == 401


def test_invocation_heartbeat_cannot_refresh_a_consumed_approval() -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)
    assert client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "approve"}
    ).status_code == 200
    assert client.post(f"/agents/approvals/{approval_id}/consume", headers=headers).status_code == 200

    heartbeat = client.post(
        f"/agents/approvals/{approval_id}/heartbeat",
        headers=headers,
        json={"source_invocation_id": "antigravity:v1:hook-test"},
    )
    assert heartbeat.status_code == 409


def test_missing_read_decision_capability_is_denied() -> None:
    project, agent_id, token = _setup_agent(actions=["CREATE_APPROVAL"])
    approval_id = _approval(project.id, _headers(agent_id, token))

    response = client.get(
        f"/agents/approvals/{approval_id}/decision",
        headers=_headers(agent_id, token),
    )
    assert response.status_code == 403


def test_connector_approval_without_invocation_identity_is_rejected() -> None:
    project, agent_id, token = _setup_agent()
    response = client.post(
        "/agents/approvals",
        headers=_headers(agent_id, token),
        json={
            "project_id": project.id,
            "title": "Missing invocation identity",
            "summary": "The connector must bind this approval to one invocation.",
            "agent_name": "Antigravity",
            "target_branch": "current-workspace",
            "files": [{
                "path": "antigravity://tool/run_command",
                "change_type": "tool_call",
                "diff": '{"CommandLine":"echo gated"}',
            }],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Connector approval requests require a source_invocation_id"
    assert store.approval_requests == {}


def test_unscoped_connector_approval_is_rejected_before_persistence() -> None:
    project, agent_id, token = _setup_agent()
    unscoped_project = project.model_copy(update={"workspace_id": None})
    unscoped_agent = store.external_agents[agent_id].model_copy(update={"workspace_id": None})
    store.projects[project.id] = unscoped_project
    store.external_agents[agent_id] = unscoped_agent
    store.persist()

    response = client.post(
        "/agents/approvals",
        headers=_headers(agent_id, token),
        json={
            "project_id": project.id,
            "title": "Unscoped connector approval",
            "summary": "Unscoped connector work must not reach a phone as actionable.",
            "agent_name": "Antigravity",
            "target_branch": "current-workspace",
            "files": [{
                "path": "antigravity://tool/run_command",
                "change_type": "tool_call",
                "diff": '{"CommandLine":"echo gated"}',
            }],
            "source_invocation_id": "antigravity:unscoped-test",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Connector approvals require a workspace-scoped project"
    assert not any(
        request.source_invocation_id == "antigravity:unscoped-test"
        for request in store.approval_requests.values()
    )


def test_approved_invocation_delivery_is_claimed_once_and_audited() -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)
    decision = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approve"},
    )
    assert decision.status_code == 200
    assert decision.json()["approved_payload_hash_version"] == 2
    assert decision.json()["approved_payload_hash"].startswith("sha256:v2:")

    claimed = client.post(f"/agents/approvals/{approval_id}/consume", headers=headers)
    duplicate = client.post(f"/agents/approvals/{approval_id}/consume", headers=headers)

    assert claimed.status_code == 200
    assert claimed.json() == {"consumed": True}
    assert duplicate.status_code == 409
    delivery_events = [
        event for event in store.audit_events
        if event.event_type == "approval.delivery_claimed" and event.entity_id == approval_id
    ]
    assert len(delivery_events) == 1
    assert delivery_events[0].details["delivery"] == "at_most_once"


def test_credential_revoked_after_authentication_cannot_claim_delivery(monkeypatch) -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)
    decision = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approve"},
    )
    assert decision.status_code == 200

    auth_repository = app.state.external_agent_auth_repository

    class RevokeImmediatelyAfterAuthentication:
        def authenticate_and_consume(self, **kwargs):
            result = auth_repository.authenticate_and_consume(**kwargs)
            if result.reason is None:
                assert result.agent is not None and result.credential is not None
                revoke_external_agent_token(
                    result.agent,
                    credential=result.credential,
                    repository=auth_repository,
                    actor="test:revoke-between-auth-and-claim",
                )
            return result

    monkeypatch.setattr(
        app.state,
        "external_agent_auth_repository",
        RevokeImmediatelyAfterAuthentication(),
    )
    response = client.post(f"/agents/approvals/{approval_id}/consume", headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "External agent credential is no longer active"
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
            (approval_id,),
        ).fetchone()[0] == 0
        audit_rows = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type='audit_event'"
        ).fetchall()
    assert not any(
        json.loads(row[0]).get("event_type") == "approval.delivery_claimed"
        and json.loads(row[0]).get("entity_id") == approval_id
        for row in audit_rows
    )


def test_hook_denies_when_consume_commits_but_response_is_lost_and_retry_replays_claim() -> None:
    project, agent_id, token = _setup_agent(actions=[
        "CREATE_APPROVAL",
        "READ_APPROVAL_DECISION",
        "CONSUME_APPROVAL_DECISION",
        "REPORT_APPROVAL_DELIVERY_FAILURE",
        "EXPIRE_OWN_APPROVAL",
    ])
    state = {"approval_id": None, "drop_consume_response": True, "consume_calls": 0}

    def request(
        method: str,
        url: str,
        request_agent_id: str,
        request_token: str,
        body: dict | None = None,
        **_kwargs: object,
    ) -> dict:
        assert request_agent_id == agent_id
        assert request_token == token
        parsed = urlparse(url)
        response = client.request(
            method,
            parsed.path,
            headers={
                **_headers(agent_id, token),
                "X-Aixion-Approval-Protocol": "1",
            },
            json=body,
        )
        if method == "POST" and parsed.path == "/agents/approvals":
            assert response.status_code == 200, response.text
            approval = response.json()
            if state["approval_id"] is None:
                state["approval_id"] = approval["id"]
                decision = client.post(
                    f"/approvals/{approval['id']}/decision",
                    json={"decision": "approve"},
                )
                assert decision.status_code == 200, decision.text
            else:
                assert approval["id"] == state["approval_id"]
        if method == "POST" and parsed.path.endswith("/consume"):
            state["consume_calls"] += 1
            if response.status_code == 200 and state["drop_consume_response"]:
                state["drop_consume_response"] = False
                # The endpoint has committed its unique claim. Only the response
                # is lost, matching the ambiguous transport failure in production.
                raise antigravity_hook.AmbiguousRequestError(
                    "simulated connection loss after server commit"
                )
        if response.status_code >= 400:
            raise antigravity_hook.GateError(
                f"Control Tower returned HTTP {response.status_code}"
            )
        return response.json()

    hook_payload = {
        "conversationId": "session-lost-consume-response",
        "stepIdx": 7,
        "toolCall": {"name": "run_command", "args": {"CommandLine": "echo gated"}},
    }
    hook_environment = {
        "AIXION_API_BASE_URL": "http://localhost:8000",
        "AIXION_AGENT_ID": agent_id,
        "AIXION_AGENT_TOKEN": token,
        "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
        "AIXION_PROJECT_ID": project.id,
        "AIXION_APPROVAL_TIMEOUT_SECONDS": "5",
    }

    with patch.object(antigravity_hook, "_request", side_effect=request), patch.object(
        antigravity_hook.time, "sleep"
    ):
        first = antigravity_hook.run(hook_payload, hook_environment)
        retry = antigravity_hook.run(hook_payload, hook_environment)

    assert first["decision"] == "deny"
    assert "ambiguous" in first["reason"]
    assert retry["decision"] == "deny"
    assert state["consume_calls"] == 2
    approval_id = state["approval_id"]
    assert isinstance(approval_id, str)
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
            (approval_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_delivery_failures WHERE approval_id=?",
            (approval_id,),
        ).fetchone()[0] == 1
    assert sum(
        event.event_type == "approval.created_by_agent" and event.entity_id == approval_id
        for event in store.audit_events
    ) == 1


def test_revocation_after_phone_approval_read_blocks_delivery_claim() -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)
    assert client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "approve"}
    ).status_code == 200

    polled = client.get(
        f"/agents/approvals/{approval_id}/decision", headers=headers
    )
    assert polled.status_code == 200
    assert polled.json()["status"] == "APPROVED"

    revoked = client.post(f"/agents/{agent_id}/token/revoke")
    consume = client.post(f"/agents/approvals/{approval_id}/consume", headers=headers)

    assert revoked.status_code == 200
    assert consume.status_code == 401
    assert consume.json()["detail"] == "External agent token is revoked or not configured"
    with store._connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM approval_delivery_claims WHERE approval_id=?",
            (approval_id,),
        ).fetchone()[0] == 0
    assert not any(
        event.event_type == "approval.delivery_claimed" and event.entity_id == approval_id
        for event in store.audit_events
    )


def test_legacy_v1_approved_invocation_cannot_be_claimed() -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)
    assert client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "approve"}
    ).status_code == 200

    legacy = store.approval_requests[approval_id].model_copy(
        update={"approved_payload_hash_version": 1, "approved_payload_hash": None}
    )
    legacy.approved_payload_hash = compute_approval_payload_hash(legacy)
    store.approval_requests[approval_id] = legacy
    store.persist()

    response = client.post(f"/agents/approvals/{approval_id}/consume", headers=headers)

    assert response.status_code == 409
    with store._connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM approval_delivery_claims WHERE approval_id=?",
            (approval_id,),
        ).fetchone()[0] == 0


def test_ambiguous_delivery_failure_requires_explicit_capability_and_claim() -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)
    assert client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "approve"}
    ).status_code == 200
    approved = client.get(
        f"/agents/approvals/{approval_id}/decision", headers=headers
    ).json()
    report = {
        "source_invocation_id": approved["source_invocation_id"],
        "approved_payload_hash": approved["approved_payload_hash"],
        "reason_code": "CLAIM_RESPONSE_AMBIGUOUS",
    }

    response = client.post(
        f"/agents/approvals/{approval_id}/delivery-failure", headers=headers, json=report
    )

    assert response.status_code == 403
    assert not any(
        event.event_type == "approval.delivery_failed" and event.entity_id == approval_id
        for event in store.audit_events
    )

    _, capable_id, capable_token = _setup_agent(actions=[
        "CREATE_APPROVAL", "READ_APPROVAL_DECISION", "CONSUME_APPROVAL_DECISION",
        "REPORT_APPROVAL_DELIVERY_FAILURE",
    ])
    assert client.post(
        f"/agents/approvals/{approval_id}/delivery-failure",
        headers=_headers(capable_id, capable_token),
        json=report,
    ).status_code == 403


def test_ambiguous_delivery_failure_is_identity_bound_idempotent_and_audited() -> None:
    project, agent_id, token = _setup_agent(actions=[
        "CREATE_APPROVAL", "READ_APPROVAL_DECISION", "CONSUME_APPROVAL_DECISION",
        "REPORT_APPROVAL_DELIVERY_FAILURE",
    ])
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)
    assert client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "approve"}
    ).status_code == 200
    approved = client.get(
        f"/agents/approvals/{approval_id}/decision", headers=headers
    ).json()
    assert client.post(f"/agents/approvals/{approval_id}/consume", headers=headers).status_code == 200
    report = {
        "source_invocation_id": approved["source_invocation_id"],
        "approved_payload_hash": approved["approved_payload_hash"],
        "reason_code": "CLAIM_RESPONSE_AMBIGUOUS",
    }

    first = client.post(
        f"/agents/approvals/{approval_id}/delivery-failure", headers=headers, json=report
    )
    retry = client.post(
        f"/agents/approvals/{approval_id}/delivery-failure", headers=headers, json=report
    )
    changed = client.post(
        f"/agents/approvals/{approval_id}/delivery-failure",
        headers=headers,
        json={**report, "source_invocation_id": "another-invocation"},
    )

    assert first.status_code == retry.status_code == 200
    assert first.json() == retry.json() == {"recorded": True}
    assert changed.status_code == 409
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_delivery_failures WHERE approval_id=?",
            (approval_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' "
            "AND json_extract(payload,'$.event_type')='approval.delivery_failed' "
            "AND json_extract(payload,'$.entity_id')=?",
            (approval_id,),
        ).fetchone()[0] == 1


def test_ambiguous_delivery_failure_requires_a_committed_claim() -> None:
    project, agent_id, token = _setup_agent(actions=[
        "CREATE_APPROVAL", "READ_APPROVAL_DECISION", "CONSUME_APPROVAL_DECISION",
        "REPORT_APPROVAL_DELIVERY_FAILURE",
    ])
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)
    assert client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "approve"}
    ).status_code == 200
    approved = client.get(
        f"/agents/approvals/{approval_id}/decision", headers=headers
    ).json()

    response = client.post(
        f"/agents/approvals/{approval_id}/delivery-failure",
        headers=headers,
        json={
            "source_invocation_id": approved["source_invocation_id"],
            "approved_payload_hash": approved["approved_payload_hash"],
            "reason_code": "CLAIM_RESPONSE_AMBIGUOUS",
        },
    )

    assert response.status_code == 409
    assert not any(
        event.event_type == "approval.delivery_failed" and event.entity_id == approval_id
        for event in store.audit_events
    )


def test_unapproved_invocation_cannot_be_claimed() -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)

    response = client.post(f"/agents/approvals/{approval_id}/consume", headers=headers)

    assert response.status_code == 409
    assert not any(
        event.event_type == "approval.delivery_claimed" and event.entity_id == approval_id
        for event in store.audit_events
    )


def test_delivery_claim_requires_explicit_connector_capability() -> None:
    project, agent_id, token = _setup_agent(
        actions=["CREATE_APPROVAL", "READ_APPROVAL_DECISION"]
    )
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)
    client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"})

    response = client.post(f"/agents/approvals/{approval_id}/consume", headers=headers)

    assert response.status_code == 403
    assert not any(
        event.event_type == "approval.delivery_claimed" and event.entity_id == approval_id
        for event in store.audit_events
    )


def test_missing_expire_own_approval_capability_is_denied() -> None:
    project, agent_id, token = _setup_agent(actions=["CREATE_APPROVAL", "READ_APPROVAL_DECISION"])
    approval_id = _approval(project.id, _headers(agent_id, token))

    response = client.post(
        f"/agents/approvals/{approval_id}/expire",
        headers=_headers(agent_id, token),
    )

    assert response.status_code == 403
    assert store.approval_requests[approval_id].status == "PENDING_REVIEW"


def test_agent_can_expire_only_its_own_pending_approval_and_late_approval_is_rejected() -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)

    response = client.post(f"/agents/approvals/{approval_id}/expire", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "EXPIRED"
    assert any(
        event.event_type == "approval.expired" and event.entity_id == approval_id
        for event in store.audit_events
    )
    late_decision = client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"})
    assert late_decision.status_code == 409
    assert store.approval_requests[approval_id].status == "EXPIRED"
    assert store.approval_requests[approval_id].approved_payload_hash is None


def test_timeout_cannot_overwrite_a_durable_phone_approval_with_stale_local_state() -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)
    stale_pending = store.approval_requests[approval_id].model_copy(deep=True)

    decision = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approve"},
    )
    assert decision.status_code == 200

    # Model another API process retaining the pre-decision object in memory.
    store.approval_requests[approval_id] = stale_pending
    timeout = client.post(f"/agents/approvals/{approval_id}/expire", headers=headers)

    assert timeout.status_code == 200
    assert timeout.json()["status"] == "APPROVED"
    assert store.approval_requests[approval_id].status == ApprovalStatus.APPROVED
    assert not any(
        event.event_type == "approval.expired" and event.entity_id == approval_id
        for event in store.audit_events
    )


def test_agent_poll_returns_durable_state_committed_during_expiry_check(monkeypatch) -> None:
    project, agent_id, token = _setup_agent()
    approval_id = _approval(project.id, _headers(agent_id, token))
    original_repository = approval_decision_repository
    approved = original_repository.get(approval_id)
    assert approved is not None
    approved = approved.model_copy(
        update={
            "status": ApprovalStatus.APPROVED,
            "approved_payload_hash": compute_approval_payload_hash(approved),
        }
    )

    class DecisionCommittedDuringPoll:
        reads = 0

        def get(self, requested_id: str) -> ApprovalRequest | None:
            self.reads += 1
            if requested_id == approval_id and self.reads > 1:
                return approved
            return original_repository.get(requested_id)

    concurrent_repository = DecisionCommittedDuringPoll()
    monkeypatch.setattr(
        app.state,
        "approval_decision_repository",
        concurrent_repository,
    )

    response = client.get(
        f"/agents/approvals/{approval_id}/decision",
        headers=_headers(agent_id, token),
    )

    assert response.status_code == 200
    assert response.json()["status"] == ApprovalStatus.APPROVED
    assert response.json()["approved_payload_hash"] == approved.approved_payload_hash
    assert concurrent_repository.reads == 3


def test_agent_cannot_expire_another_agents_approval() -> None:
    project, owner_id, owner_token = _setup_agent()
    _, other_id, other_token = _setup_agent()
    approval_id = _approval(project.id, _headers(owner_id, owner_token))

    response = client.post(
        f"/agents/approvals/{approval_id}/expire",
        headers=_headers(other_id, other_token),
    )

    assert response.status_code == 403
    assert store.approval_requests[approval_id].status == "PENDING_REVIEW"


def test_agent_poll_expires_request_after_server_deadline() -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)
    store.approval_requests[approval_id].expires_at = now_utc() - timedelta(seconds=1)
    store.persist()

    response = client.get(f"/agents/approvals/{approval_id}/decision", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "EXPIRED"
    assert store.approval_requests[approval_id].status == "EXPIRED"


def test_antigravity_hook_denies_when_real_api_poll_expires_request(monkeypatch) -> None:
    project, agent_id, token = _setup_agent()
    calls: list[str] = []
    approval_ids: list[str] = []

    def request(method: str, url: str, request_agent_id: str, request_token: str,
                body: dict | None = None, **_kwargs: object) -> dict:
        path = urlparse(url).path
        calls.append(f"{method} {path}")
        response = client.request(
            method,
            path,
            headers={
                **_headers(request_agent_id, request_token),
                "X-Aixion-Approval-Protocol": "1",
            },
            json=body,
        )
        if response.status_code >= 400:
            raise antigravity_hook.GateError(
                f"Control Tower returned HTTP {response.status_code}"
            )
        result = response.json()
        if method == "POST" and path == "/agents/approvals":
            approval_ids.append(result["id"])
            pending = approval_decision_repository.get(result["id"])
            assert pending is not None
            pending.expires_at = now_utc() - timedelta(seconds=1)
            store.approval_requests[pending.id] = pending
            store.persist()
        return result

    monkeypatch.setattr(antigravity_hook, "_request", request)
    result = antigravity_hook.run(
        {
            "conversationId": "session-real-api-expiry",
            "stepIdx": 1,
            "toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}},
        },
        {
            "AIXION_API_BASE_URL": "http://127.0.0.1:8000",
            "AIXION_AGENT_ID": agent_id,
            "AIXION_AGENT_TOKEN": token,
            "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
            "AIXION_PROJECT_ID": project.id,
        },
    )

    assert len(approval_ids) == 1
    created_id = approval_ids[0]
    assert result["decision"] == "deny"
    assert store.approval_requests[created_id].status == ApprovalStatus.EXPIRED
    assert calls == [
        "POST /agents/approvals",
        f"GET /agents/approvals/{created_id}/decision",
    ]
    with store._connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM approval_delivery_claims WHERE approval_id=?",
            (created_id,),
        ).fetchone()[0] == 0


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_isolated_api(
    db_path: Path,
    port: int,
    *,
    access_log_path: Path | None = None,
    auth_enabled: bool = False,
) -> subprocess.Popen:
    environment = os.environ.copy()
    environment.update(
        {
            "AIXION_PROFILE": "test",
            "AIXION_AUTH_ENABLED": "true" if auth_enabled else "false",
            "AIXION_DB_PATH": str(db_path),
            "AIXION_POSTGRES_RUNTIME_PROBE_ENABLED": "false",
            "AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED": "false",
            "AIXION_POSTGRES_AUTH_ENABLED": "false",
        }
    )
    environment.pop("AIXION_POSTGRES_DSN", None)
    backend_root = Path(__file__).parents[1]
    access_log = access_log_path.open("a", encoding="utf-8") if access_log_path else None
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "info",
            "--access-log",
        ],
        cwd=backend_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=access_log if access_log else subprocess.DEVNULL,
    )
    if access_log:
        access_log.close()
    return process


def _wait_for_isolated_api(process: subprocess.Popen, base_url: str) -> None:
    import httpx

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"Local API process exited with code {process.returncode}")
        try:
            response = httpx.get(f"{base_url}/health", timeout=0.5)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("Local API process did not become healthy before timeout")


def _stop_isolated_api(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_in_flight_antigravity_hook_recovers_after_api_process_restart(tmp_path) -> None:
    import httpx

    db_path = tmp_path / "api-restart.sqlite3"
    access_log_path = tmp_path / "api-restart-access.log"
    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    # Hold the hook's first decision read at a local forwarding proxy. This
    # makes the crash window deterministic: the owner commits APPROVE, the API
    # process is restarted against the same database, and only then can the
    # still-running hook read and consume the decision.
    decision_read_blocked = threading.Event()
    release_decision_read = threading.Event()
    proxy_upstream: dict[str, str] = {"base_url": base_url}

    class ApprovalWaitProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            if self.path.endswith("/decision") and not decision_read_blocked.is_set():
                decision_read_blocked.set()
                if not release_decision_read.wait(timeout=30):
                    self.send_error(504, "Test decision-read gate timed out")
                    return
            self._forward()

        def do_POST(self) -> None:
            self._forward()

        def _forward(self) -> None:
            import httpx

            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            forwarded_headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"host", "connection", "content-length"}
            }
            try:
                response = httpx.request(
                    self.command,
                    f"{proxy_upstream['base_url']}{self.path}",
                    headers=forwarded_headers,
                    content=body,
                    timeout=10,
                    trust_env=False,
                )
            except httpx.HTTPError:
                self.send_error(502, "Test proxy could not reach the API")
                return
            self.send_response(response.status_code)
            if "content-type" in response.headers:
                self.send_header("Content-Type", response.headers["content-type"])
            self.send_header("Content-Length", str(len(response.content)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response.content)
            self.close_connection = True

        def log_message(self, _format: str, *_args: object) -> None:
            return

    proxy_server = ThreadingHTTPServer(("127.0.0.1", 0), ApprovalWaitProxyHandler)
    proxy_port = int(proxy_server.server_address[1])
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    proxy_thread = threading.Thread(target=proxy_server.serve_forever, daemon=True)
    proxy_thread.start()
    api_process = _start_isolated_api(
        db_path, port, access_log_path=access_log_path, auth_enabled=True
    )
    hook_process: subprocess.Popen | None = None
    restarted_api: subprocess.Popen | None = None
    owner_headers: dict[str, str] = {}
    try:
        _wait_for_isolated_api(api_process, base_url)
        with httpx.Client(base_url=base_url, timeout=5) as api:
            email = f"restart-{uuid.uuid4().hex}@example.com"
            registered_user = api.post(
                "/auth/register",
                json={"email": email, "password": "Restart-test-password-123!", "display_name": "Restart owner"},
            )
            assert registered_user.status_code == 200, registered_user.text
            verification_code = registered_user.json().get("dev_verification_code")
            assert verification_code
            verified = api.post(
                "/auth/verify-email", json={"email": email, "code": verification_code}
            )
            assert verified.status_code == 200, verified.text
            logged_in = api.post(
                "/auth/login", json={"email": email, "password": "Restart-test-password-123!"}
            )
            assert logged_in.status_code == 200, logged_in.text
            owner_headers = {"Authorization": f"Bearer {logged_in.json()['access_token']}"}
            project_response = api.post(
                "/projects",
                headers=owner_headers,
                json={"name": "Antigravity restart project", "description": "Workspace-scoped hook restart test"},
            )
            assert project_response.status_code == 200, project_response.text
            project_id = project_response.json()["id"]
            registered = api.post(
                "/agents",
                headers=owner_headers,
                json={
                    "provider": "ANTIGRAVITY",
                    "display_name": "Antigravity restart integration",
                    "auth_type": "API_KEY",
                    "allowed_project_ids": [project_id],
                    "allowed_actions": [
                        "CREATE_APPROVAL",
                        "READ_APPROVAL_DECISION",
                        "CONSUME_APPROVAL_DECISION",
                        "EXPIRE_OWN_APPROVAL",
                    ],
                    "enabled": True,
                },
            )
            assert registered.status_code == 200, registered.text
            agent_id = registered.json()["agent"]["id"]
            agent_token = registered.json()["agent_token"]

        hook_environment = os.environ.copy()
        hook_environment.update(
            {
                "AIXION_API_BASE_URL": proxy_url,
                "AIXION_AGENT_ID": agent_id,
                "AIXION_AGENT_TOKEN": agent_token,
                "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
                "AIXION_PROJECT_ID": project_id,
                "AIXION_APPROVAL_TIMEOUT_SECONDS": "45",
            }
        )
        hook_payload = {
            "conversationId": "session-api-process-restart",
            "stepIdx": 41,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "echo restart-recovery"},
            },
        }

        def expected_invocation_id(payload: dict) -> str:
            call = payload["toolCall"]
            material = json.dumps(
                [
                    payload["conversationId"],
                    payload["stepIdx"],
                    call["name"],
                    call["args"],
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return "antigravity:v1:" + hashlib.sha256(material).hexdigest()

        original_invocation_id = expected_invocation_id(hook_payload)

        def launch_hook(payload: dict) -> subprocess.Popen[str]:
            process = subprocess.Popen(
                [sys.executable, str(HOOK_PATH)],
                env=hook_environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.close()
            # communicate() may flush stdin even after close().
            process.stdin = None
            return process

        hook_process = launch_hook(hook_payload)

        approval: dict | None = None
        deadline = time.monotonic() + 10
        with httpx.Client(base_url=base_url, timeout=5) as api:
            while time.monotonic() < deadline:
                response = api.get("/approvals", headers=owner_headers)
                assert response.status_code == 200, response.text
                approval = next(
                    (
                        item
                        for item in response.json()
                        if item.get("source_session_id")
                        == hook_payload["conversationId"]
                    ),
                    None,
                )
                if approval is not None:
                    break
                assert hook_process.poll() is None, (
                    "Hook exited before approval was observed: "
                    f"stdout={hook_process.stdout.read() if hook_process.stdout else ''}; "
                    f"stderr={hook_process.stderr.read() if hook_process.stderr else ''}; "
                    f"api_access={access_log_path.read_text(encoding='utf-8')[-4000:]}"
                )
                time.sleep(0.1)
        assert approval is not None, "Hook did not create an approval through the local API"
        assert approval["source_session_id"] == hook_payload["conversationId"]
        assert approval["source_invocation_id"] == original_invocation_id
        assert hook_process.poll() is None, "Hook did not remain blocked on the pending approval"
        assert decision_read_blocked.wait(timeout=10), (
            "Hook did not reach the gated decision read after creating its approval"
        )

        with httpx.Client(base_url=base_url, timeout=5) as api:
            matching = [
                item
                for item in api.get("/approvals", headers=owner_headers).json()
                if item.get("source_invocation_id") == original_invocation_id
            ]
        assert [item["id"] for item in matching] == [approval["id"]]

        with httpx.Client(base_url=base_url, timeout=5) as api:
            decision = api.post(
                f"/approvals/{approval['id']}/decision",
                headers=owner_headers,
                json={"decision": "approve"},
            )
            assert decision.status_code == 200, decision.text

        old_pid = api_process.pid
        _stop_isolated_api(api_process)
        assert api_process.poll() is not None, "Original API subprocess did not stop"
        assert hook_process.poll() is None, "Hook failed open or exited while the API was unavailable"

        restarted_api = _start_isolated_api(
            db_path, port, access_log_path=access_log_path, auth_enabled=True
        )
        assert restarted_api.pid != old_pid
        _wait_for_isolated_api(restarted_api, base_url)
        release_decision_read.set()

        stdout, stderr = hook_process.communicate(timeout=20)
        assert hook_process.returncode == 0, stderr
        result = json.loads(stdout.strip().splitlines()[-1])
        assert result["decision"] == "auto_approve"
        assert result["reason"] == "Approved from Aixion Control Tower"
        with httpx.Client(base_url=base_url, timeout=5) as api:
            recovered = api.get("/approvals", headers=owner_headers)
        assert recovered.status_code == 200, recovered.text
        recovered_approval = next(
            item for item in recovered.json() if item["id"] == approval["id"]
        )
        assert recovered_approval["source_session_id"] == hook_payload["conversationId"]
        assert recovered_approval["source_invocation_id"] == original_invocation_id
        assert recovered_approval["files"] == [
            {
                "path": "antigravity://tool/run_command",
                "change_type": "tool_call",
                "diff": json.dumps(
                    hook_payload["toolCall"]["args"],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "new_content": None,
            }
        ]

        # A different provider invocation after the restart must receive its
        # own approval row. The approved original invocation cannot authorize it.
        changed_payload = json.loads(json.dumps(hook_payload))
        changed_payload["conversationId"] = "session-api-process-restart-new-invocation"
        changed_invocation_id = expected_invocation_id(changed_payload)
        assert changed_invocation_id != original_invocation_id
        second_hook_process = launch_hook(changed_payload)
        try:
            second_approval: dict | None = None
            deadline = time.monotonic() + 10
            with httpx.Client(base_url=base_url, timeout=5) as api:
                while time.monotonic() < deadline:
                    response = api.get("/approvals", headers=owner_headers)
                    assert response.status_code == 200, response.text
                    second_approval = next(
                        (
                            item
                            for item in response.json()
                            if item.get("source_invocation_id") == changed_invocation_id
                        ),
                        None,
                    )
                    if second_approval is not None:
                        break
                    assert second_hook_process.poll() is None, (
                        "Hook exited before its distinct invocation approval was observed"
                    )
                    time.sleep(0.1)
                assert second_approval is not None, (
                    "Restarted API did not create an approval for the distinct invocation"
                )
                assert second_approval["id"] != approval["id"]
                assert second_approval["source_session_id"] == changed_payload["conversationId"]
                second_matches = [
                    item
                    for item in response.json()
                    if item.get("source_invocation_id") == changed_invocation_id
                ]
                assert [item["id"] for item in second_matches] == [second_approval["id"]]

                rejected = api.post(
                    f"/approvals/{second_approval['id']}/decision",
                    headers=owner_headers,
                    json={"decision": "deny"},
                )
                assert rejected.status_code == 200, rejected.text

            second_stdout, second_stderr = second_hook_process.communicate(timeout=20)
            assert second_hook_process.returncode == 0, second_stderr
            second_result = json.loads(second_stdout.strip().splitlines()[-1])
            assert second_result["decision"] == "deny"
            assert second_result["reason"] == "Control Tower decision: DENIED"
        finally:
            if second_hook_process.poll() is None:
                second_hook_process.kill()
                second_hook_process.wait(timeout=5)

        with sqlite3.connect(db_path) as connection:
            claim_count = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
                (approval["id"],),
            ).fetchone()[0]
            claim_identity = connection.execute(
                "SELECT source_agent_id,source_invocation_id FROM approval_delivery_claims "
                "WHERE approval_id=?",
                (approval["id"],),
            ).fetchone()
            second_claim_count = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
                (second_approval["id"],),
            ).fetchone()[0]
        assert claim_count == 1
        assert claim_identity == (agent_id, original_invocation_id)
        assert second_claim_count == 0
    finally:
        if hook_process is not None and hook_process.poll() is None:
            hook_process.kill()
            hook_process.wait(timeout=5)
        if restarted_api is not None:
            _stop_isolated_api(restarted_api)
        _stop_isolated_api(api_process)
        release_decision_read.set()
        proxy_server.shutdown()
        proxy_server.server_close()
        proxy_thread.join(timeout=5)


def test_mobile_approval_list_expires_overdue_agent_requests() -> None:
    project, agent_id, token = _setup_agent()
    approval_id = _approval(project.id, _headers(agent_id, token))
    store.approval_requests[approval_id].expires_at = now_utc() - timedelta(seconds=1)
    store.persist()

    response = client.get("/approvals")

    assert response.status_code == 200
    assert next(item for item in response.json() if item["id"] == approval_id)["status"] == "EXPIRED"


def test_mobile_approval_list_backfills_deadline_for_legacy_agent_request() -> None:
    project, agent_id, token = _setup_agent()
    approval_id = _approval(project.id, _headers(agent_id, token))
    approval = store.approval_requests[approval_id]
    approval.expires_at = None
    approval.created_at = now_utc() - timedelta(minutes=11)
    store.persist()

    response = client.get("/approvals")

    assert response.status_code == 200
    serialized = next(item for item in response.json() if item["id"] == approval_id)
    assert serialized["status"] == ApprovalStatus.EXPIRED
    assert serialized["expires_at"] is not None


def test_approved_decision_returns_the_hash_of_frozen_tool_payload() -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)
    approval = store.approval_requests[approval_id]
    expected_hash = compute_approval_payload_hash(
        approval.model_copy(update={"approved_payload_hash_version": 2})
    )

    decision = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approve"},
    )
    assert decision.status_code == 200
    polled = client.get(f"/agents/approvals/{approval_id}/decision", headers=headers)
    assert polled.status_code == 200
    assert polled.json()["status"] == "APPROVED"
    assert polled.json()["approved_payload_hash"] == expected_hash


def test_unapproved_or_denied_decision_never_has_approval_hash() -> None:
    project, agent_id, token = _setup_agent()
    headers = _headers(agent_id, token)
    approval_id = _approval(project.id, headers)
    client.post(f"/approvals/{approval_id}/decision", json={"decision": "deny"})

    response = client.get(f"/agents/approvals/{approval_id}/decision", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "DENIED"
    assert response.json()["approved_payload_hash"] is None


def test_antigravity_shell_tool_is_never_labeled_medium_or_low_risk() -> None:
    project, agent_id, token = _setup_agent()
    response = client.post(
        "/agents/approvals",
        headers=_headers(agent_id, token),
        json={
            "project_id": project.id,
            "title": "Antigravity shell request",
            "summary": "Review the exact command before allowing the session to continue.",
            "agent_name": "Antigravity",
            "target_branch": "current-workspace",
            "files": [{
                "path": "antigravity://tool/run_command",
                "change_type": "tool_call",
                "diff": '{"CommandLine":"echo harmless"}',
            }],
            "test_plan": ["Review the exact command"],
            "rollback_plan": "Deny to stop this command.",
            "source_invocation_id": "antigravity:risk-shell-1",
        },
    )
    assert response.status_code == 200
    assert response.json()["risk"]["level"] == "CRITICAL"


def test_antigravity_file_edit_is_labeled_high_risk() -> None:
    project, agent_id, token = _setup_agent()
    response = client.post(
        "/agents/approvals",
        headers=_headers(agent_id, token),
        json={
            "project_id": project.id,
            "title": "Antigravity file edit",
            "summary": "Review the exact file content before allowing the session to continue.",
            "agent_name": "Antigravity",
            "target_branch": "current-workspace",
            "files": [{
                "path": "antigravity://tool/write_to_file",
                "change_type": "tool_call",
                "diff": '{"TargetFile":"README.md","CodeContent":"hello"}',
            }],
            "test_plan": ["Review the requested file change"],
            "rollback_plan": "Deny to stop the file write.",
            "source_invocation_id": "antigravity:risk-write-1",
        },
    )
    assert response.status_code == 200
    assert response.json()["risk"]["level"] == "HIGH"


def test_antigravity_tool_marker_cannot_bypass_sensitive_path_block() -> None:
    project, agent_id, token = _setup_agent()
    response = client.post(
        "/agents/approvals",
        headers=_headers(agent_id, token),
        json={
            "project_id": project.id,
            "title": "Mixed Antigravity payload",
            "summary": "Must retain the sensitive-path hard block.",
            "agent_name": "Antigravity",
            "target_branch": "current-workspace",
            "files": [
                {
                    "path": "antigravity://tool/run_command",
                    "change_type": "tool_call",
                    "diff": '{"command":"echo safe"}',
                },
                {
                    "path": "config/.env.production",
                    "change_type": "update",
                    "diff": "-SECRET=old\n+SECRET=new",
                },
            ],
            "test_plan": ["Review the exact command"],
            "rollback_plan": "Deny this request.",
            "source_invocation_id": "antigravity:risk-sensitive-path-1",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "BLOCKED"


def test_antigravity_tool_marker_cannot_bypass_protected_branch_block() -> None:
    project, agent_id, token = _setup_agent()
    response = client.post(
        "/agents/approvals",
        headers=_headers(agent_id, token),
        json={
            "project_id": project.id,
            "title": "Protected branch Antigravity command",
            "summary": "Must retain the protected-branch hard block.",
            "agent_name": "Antigravity",
            "target_branch": "main",
            "files": [{
                "path": "antigravity://tool/run_command",
                "change_type": "tool_call",
                "diff": '{"command":"echo safe"}',
            }],
            "test_plan": ["Review the exact command"],
            "rollback_plan": "Deny this request.",
            "source_invocation_id": "antigravity:risk-protected-branch-1",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "BLOCKED"

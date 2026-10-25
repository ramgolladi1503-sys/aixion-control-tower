from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
os.environ.setdefault("AIXION_PROFILE", "test")
os.environ.setdefault("AIXION_LEASE_SIGNING_KEY", "route-test-signing-key-with-sufficient-entropy")

from fastapi.testclient import TestClient

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.approval_integrity import compute_approval_payload_hash
from app.main import app
from app.models import (
    AuthUser,
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
    UserRole,
)
from app.store import store
from app.trust_models import (
    CapabilityActionType,
    ProposedAction,
    ReviewerAttestationCreate,
    ReviewerAttestationDecision,
)
from app.trust_routes import (
    create_attestation,
    evaluate_manual_action,
    list_attestations,
    list_flight_recorder_events,
)

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _seed_approval() -> ApprovalRequest:
    project = Project(name="Trust API", description="route tests")
    store.projects[project.id] = project
    approval = ApprovalRequest(
        project_id=project.id,
        title="Trust API approval",
        summary="Approve one file and one test.",
        agent_name="codex",
        target_branch="feature/trust-api",
        files=[
            FileChange(
                path="backend/app/trust_api.py",
                change_type="create",
                diff="+trusted",
                new_content="trusted = True\n",
            )
        ],
        test_plan=["python -m pytest backend/tests/test_trust_api.py"],
        rollback_plan="Close PR.",
        risk=RiskAssessment(level=RiskLevel.HIGH),
        status=ApprovalStatus.APPROVED,
        created_by_user_id="creator",
        approved_by_user_id="reviewer",
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Trust API task",
        goal="Create one safe PR.",
        repository="owner/repo",
        branch_preference="feature/trust-api",
        approval_request_id=approval.id,
        status=AgentTaskStatus.APPROVED,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return approval


def _create_lease(approval: ApprovalRequest) -> dict:
    response = client.post(
        "/trust/leases",
        json={
            "approval_request_id": approval.id,
            "provider": "CODEX",
            "mode": "BOUNDED",
            "scope": {
                "repository": "owner/repo",
                "branch": "feature/trust-api",
                "allowed_actions": [
                    "MODIFY_FILES",
                    "RUN_COMMAND",
                    "CREATE_PULL_REQUEST",
                ],
                "allowed_path_prefixes": ["backend/app/trust_api.py"],
                "allowed_commands": [
                    "python -m pytest backend/tests/test_trust_api.py"
                ],
                "allowed_network_domains": [],
                "max_runtime_seconds": 300,
                "max_cost_usd": 2.0,
                "max_retries": 2,
                "max_pull_requests": 1,
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_reviewer_attestation_create_and_list_keep_sqlite_local_behavior() -> None:
    approval = _seed_approval()
    reviewer = AuthUser(
        id="reviewer-local",
        email="reviewer-local@example.test",
        display_name="Local reviewer",
        role=UserRole.REVIEWER,
    )
    payload = ReviewerAttestationCreate(
        approval_request_id=approval.id,
        decision=ReviewerAttestationDecision.APPROVE,
        reason="Reviewed the exact request.",
    )

    created = create_attestation(payload, user=reviewer, request=None)
    listed = list_attestations(
        approval_request_id=approval.id,
        user=reviewer,
        request=None,
    )

    assert created.id in store.reviewer_attestations
    assert [item.id for item in listed] == [created.id]


def test_flight_recorder_fails_closed_for_incomplete_postgres_bundle(monkeypatch) -> None:
    reviewer = AuthUser(
        id="reviewer-incomplete-postgres",
        email="reviewer@example.test",
        display_name="Reviewer",
        role=UserRole.REVIEWER,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(postgres_repositories=object()),
        )
    )
    monkeypatch.setattr(
        "app.trust_routes.get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )

    with pytest.raises(HTTPException) as error:
        list_flight_recorder_events(user=reviewer, request=request)

    assert error.value.status_code == 503
    assert "PostgreSQL" in error.value.detail


def test_lease_gateway_consumption_scorecard_and_flight_recorder_routes() -> None:
    approval = _seed_approval()
    lease = _create_lease(approval)

    evaluated = client.post(
        "/trust/gateway/actions/manual",
        json={
            "lease_id": lease["id"],
            "provider": "CODEX",
            "action_type": "MODIFY_FILES",
            "repository": "owner/repo",
            "branch": "feature/trust-api",
            "paths": ["backend/app/trust_api.py"],
            "estimated_runtime_seconds": 20,
            "estimated_cost_usd": 0.1,
        },
    )
    assert evaluated.status_code == 200, evaluated.text
    action_id = evaluated.json()["action"]["id"]
    assert evaluated.json()["decision"]["decision"] == "ALLOW"

    consumed = client.post(
        f"/trust/gateway/actions/{action_id}/consume/manual",
        json={
            "actual_runtime_seconds": 18,
            "actual_cost_usd": 0.08,
            "evidence": {"commit": "abc123"},
        },
    )
    assert consumed.status_code == 200

    listed = client.get("/trust/leases")
    scorecards = client.get("/trust/scorecards")
    flight = client.get("/trust/flight-recorder")
    verified = client.get("/trust/flight-recorder/verify")

    assert listed.status_code == 200 and listed.json()[0]["id"] == lease["id"]
    assert scorecards.status_code == 200
    assert scorecards.json()[0]["evaluated_actions"] == 1
    assert flight.status_code == 200 and len(flight.json()) >= 3
    assert verified.status_code == 200 and verified.json()["valid"] is True


def test_blocked_action_appears_in_mobile_exception_queue() -> None:
    approval = _seed_approval()
    lease = _create_lease(approval)

    blocked = client.post(
        "/trust/gateway/actions/manual",
        json={
            "lease_id": lease["id"],
            "provider": "CODEX",
            "action_type": "MODIFY_FILES",
            "repository": "owner/repo",
            "branch": "feature/trust-api",
            "paths": [".env"],
        },
    )
    assert blocked.status_code == 200
    assert blocked.json()["decision"]["decision"] == "BLOCK"

    queue = client.get("/trust/exceptions")
    assert queue.status_code == 200
    assert any(item["action_id"] == blocked.json()["action"]["id"] for item in queue.json())


def test_short_lived_credential_is_returned_once_and_introspected() -> None:
    approval = _seed_approval()
    lease = _create_lease(approval)

    issued = client.post(
        "/trust/credentials",
        json={
            "lease_id": lease["id"],
            "grant_type": "INTERNAL_CAPABILITY_TOKEN",
            "audience": "aixion-worker",
            "subject": "agent:codex",
            "scope": ["run:execute"],
            "expires_in_seconds": 120,
        },
    )
    assert issued.status_code == 200, issued.text
    token = issued.json()["bearer_token"]
    grant_id = issued.json()["grant"]["id"]
    assert len(token) > 20

    introspected = client.post(
        "/trust/credentials/introspect",
        json={
            "bearer_token": token,
            "audience": "aixion-worker",
            "required_scope": ["run:execute"],
        },
    )
    assert introspected.status_code == 200
    assert introspected.json()["active"] is True

    revoked = client.post(
        f"/trust/credentials/{grant_id}/revoke",
        json={"reason": "Operator cancelled the run."},
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True

    after = client.post(
        "/trust/credentials/introspect",
        json={
            "bearer_token": token,
            "audience": "aixion-worker",
            "required_scope": [],
        },
    )
    assert after.status_code == 200
    assert after.json()["active"] is False


def test_external_credential_brokers_fail_honestly_until_configured() -> None:
    approval = _seed_approval()
    lease = _create_lease(approval)
    response = client.post(
        "/trust/credentials",
        json={
            "lease_id": lease["id"],
            "grant_type": "OIDC_FEDERATION",
            "audience": "cloud-provider",
            "subject": "agent:codex",
            "scope": ["deploy:staging"],
        },
    )
    assert response.status_code == 501
    assert "deployment-specific issuer" in response.json()["detail"]


def test_postgres_task_only_action_route_fails_closed_when_flag_is_off(monkeypatch) -> None:
    workspace_id = "workspace-task-action-route"
    trust_repository = SimpleNamespace(task_actions_enabled=lambda: False)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                postgres_repositories=SimpleNamespace(
                    trust=trust_repository,
                    projects=SimpleNamespace(
                        get=lambda _project_id: SimpleNamespace(workspace_id=workspace_id)
                    ),
                )
            )
        )
    )
    user = AuthUser(
        id="maintainer-task-action-route",
        email="maintainer@example.test",
        display_name="Task action reviewer",
        role=UserRole.MAINTAINER,
        workspace_id=workspace_id,
        email_verified=True,
    )
    payload = ProposedAction(
        task_id="task-route-test",
        project_id="project-route-test",
        workspace_id=workspace_id,
        action_type=CapabilityActionType.RUN_COMMAND,
        repository="owner/repo",
        branch="feature/task-action",
        command="pytest -q",
        metadata={
            "approval_request_id": "approval-route-test",
            "approved_payload_hash": "sha256:" + "a" * 64,
        },
    )
    monkeypatch.setattr("app.trust_routes._postgres_action_runtime", lambda _request: object())
    monkeypatch.setattr(
        "app.trust_routes.workspace_id_for_request",
        lambda _user, _request: workspace_id,
    )
    with pytest.raises(HTTPException) as error:
        evaluate_manual_action(payload, user, request)
    assert error.value.status_code == 409
    assert "disabled" in str(error.value.detail)

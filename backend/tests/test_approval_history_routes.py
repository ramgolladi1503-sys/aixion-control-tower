from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app import main
from app.approval_integrity import compute_approval_payload_hash
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
    UserRole,
    AuthUser,
    now_utc,
)


class FakeAuditRepository:
    def __init__(self, events: list[AuditEvent]) -> None:
        self.events = events
        self.queries: list[tuple[str, dict[str, object]]] = []

    def list_for_workspace(self, workspace_id: str, **filters: object) -> list[AuditEvent]:
        self.queries.append((workspace_id, filters))
        matching = [
            event
            for event in self.events
            if event.event_type == filters.get("event_type")
            and event.workspace_id in {None, workspace_id}
        ]
        matching.sort(key=lambda event: (event.created_at, event.id), reverse=True)
        return matching[: int(filters["limit"])]


class FakeApprovalRepository:
    def __init__(self, approvals: list[ApprovalRequest]) -> None:
        self.approvals = {approval.id: approval for approval in approvals}

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self.approvals.get(approval_id)


class FakeProjectRepository:
    def __init__(self, projects: list[Project]) -> None:
        self.projects = {project.id: project for project in projects}

    def get(self, project_id: str) -> Project | None:
        return self.projects.get(project_id)


def _approval(approval_id: str, *, workspace_id: str = "workspace-one") -> ApprovalRequest:
    return ApprovalRequest(
        id=approval_id,
        project_id="project-one",
        title=f"Review {approval_id}",
        summary="Update the approved configuration.",
        agent_name="Antigravity",
        target_branch="feature/review",
        files=[
            FileChange(
                path="config.yml",
                diff="-old\n+new",
                new_content="new",
            )
        ],
        test_plan=["run the configuration checks"],
        rollback_plan="restore the previous configuration",
        risk=RiskAssessment(level=RiskLevel.MEDIUM, reasons=["configuration change"]),
        source_provider="ANTIGRAVITY",
        source_agent_id="agent-one",
        source_session_id="session-one",
        source_invocation_id=f"invocation-{approval_id}",
        workspace_id=workspace_id,
        status=ApprovalStatus.DENIED,
    )


def _reviewed_hash(approval: ApprovalRequest) -> str:
    candidate = approval.model_copy(deep=True)
    candidate.approved_payload_hash_version = 2
    return compute_approval_payload_hash(candidate)


def _decision_event(
    approval: ApprovalRequest,
    *,
    decision: str,
    actor: str,
    created_at=None,
    reviewed_payload_hash: str | None = None,
    approved_payload_hash: str | None = None,
    workspace_id: str | None = None,
) -> AuditEvent:
    details: dict[str, object] = {"decision": decision, "reason": "Reviewed from phone"}
    if reviewed_payload_hash is not None:
        details["reviewed_payload_hash"] = reviewed_payload_hash
    if approved_payload_hash is not None:
        details["approved_payload_hash"] = approved_payload_hash
    return AuditEvent(
        event_type="approval.decision",
        entity_id=approval.id,
        workspace_id=workspace_id or approval.workspace_id,
        actor=actor,
        details=details,
        created_at=created_at or now_utc(),
    )


def _request(
    monkeypatch,
    *,
    events: list[AuditEvent],
    approvals: list[ApprovalRequest],
    projects: list[Project] | None = None,
    workspace_id: str = "workspace-one",
    user_email: str = "owner@example.test",
) -> tuple[TestClient, FakeAuditRepository]:
    audit_repository = FakeAuditRepository(events)
    user = AuthUser(
        id="owner-one",
        email=user_email,
        display_name="Owner",
        role=UserRole.OWNER,
        workspace_id=workspace_id,
    )
    monkeypatch.setattr(
        main,
        "audit_event_repository_for_request",
        lambda _request: audit_repository,
    )
    monkeypatch.setattr(
        main,
        "_approval_repository_for_route",
        lambda _request: FakeApprovalRepository(approvals),
    )
    monkeypatch.setattr(
        main,
        "_project_repository_for_route",
        lambda _request: FakeProjectRepository(
            projects
            or [Project(id="project-one", name="Project one", workspace_id=workspace_id)]
        ),
    )
    monkeypatch.setattr(main, "workspace_id_for_request", lambda _user, _request: workspace_id)
    monkeypatch.setitem(main.app.dependency_overrides, main.require_reviewer, lambda: user)
    return TestClient(main.app), audit_repository


def test_history_uses_immutable_decision_audit_and_redacts_reviewer_email(monkeypatch) -> None:
    approval = _approval("approval-one")
    event = _decision_event(
        approval,
        decision="approve",
        actor="owner@example.test",
        reviewed_payload_hash=_reviewed_hash(approval),
    )
    client, audit_repository = _request(
        monkeypatch,
        events=[event],
        approvals=[approval],
    )

    response = client.get("/approvals/history")

    assert response.status_code == 200
    payload = response.json()
    assert payload["events"][0]["decision"] == "approve"
    assert payload["events"][0]["request_snapshot_verified"] is True
    assert payload["events"][0]["reviewer_label"] == "You"
    assert payload["events"][0]["request_snapshot"]["title"] == "Review approval-one"
    assert "status" not in payload["events"][0]["request_snapshot"]
    assert "delivery_outcome" not in payload["events"][0]
    assert "actor" not in payload["events"][0]
    assert "owner@example.test" not in response.text
    assert audit_repository.queries == [
        ("workspace-one", {"event_type": "approval.decision", "limit": 51})
    ]


def test_history_does_not_infer_decision_from_current_status_or_unverified_snapshot(
    monkeypatch,
) -> None:
    approval = _approval("approval-tampered")
    tampered_hash = _reviewed_hash(approval)
    approval.title = "Changed after the audit event"
    event = _decision_event(
        approval,
        decision="approve",
        actor="other-reviewer@example.test",
        reviewed_payload_hash=tampered_hash,
    )
    client, _ = _request(monkeypatch, events=[event], approvals=[approval])

    response = client.get("/approvals/history")

    entry = response.json()["events"][0]
    assert response.status_code == 200
    assert entry["decision"] == "approve"
    assert entry["reviewer_label"] == "Workspace reviewer"
    assert entry["request_snapshot_verified"] is False
    assert entry["request_snapshot"] is None
    assert "other-reviewer@example.test" not in response.text


def test_history_does_not_verify_unknown_decision_even_with_matching_hash(monkeypatch) -> None:
    approval = _approval("approval-unknown-decision")
    event = _decision_event(
        approval,
        decision="execute",
        actor="owner@example.test",
        reviewed_payload_hash=_reviewed_hash(approval),
    )
    client, _ = _request(monkeypatch, events=[event], approvals=[approval])

    response = client.get("/approvals/history")

    assert response.status_code == 200
    entry = response.json()["events"][0]
    assert entry["decision"] == "unknown"
    assert entry["request_snapshot_verified"] is False
    assert entry["request_snapshot"] is None


def test_history_legacy_approve_hash_is_supported_but_legacy_deny_is_unverified(
    monkeypatch,
) -> None:
    approved = _approval("legacy-approved")
    approved.approved_payload_hash = compute_approval_payload_hash(approved)
    approved.approved_payload_hash_version = 1
    denied = _approval("legacy-denied")
    base_time = now_utc()
    events = [
        _decision_event(
            approved,
            decision="approve",
            actor="owner@example.test",
            approved_payload_hash=approved.approved_payload_hash,
            created_at=base_time - timedelta(seconds=1),
        ),
        _decision_event(
            denied,
            decision="reject",
            actor="owner@example.test",
            created_at=base_time,
        ),
    ]
    client, _ = _request(monkeypatch, events=events, approvals=[approved, denied])

    response = client.get("/approvals/history")

    entries = response.json()["events"]
    assert [entry["decision"] for entry in entries] == ["deny", "approve"]
    assert entries[0]["request_snapshot_verified"] is False
    assert entries[0]["request_snapshot"] is None
    assert entries[1]["request_snapshot_verified"] is True
    assert entries[1]["request_snapshot"]["id"] == "legacy-approved"


def test_history_enforces_workspace_scope_and_bounded_limit(monkeypatch) -> None:
    approvals = [_approval(f"approval-{index}") for index in range(3)]
    base_time = now_utc()
    events = [
        _decision_event(
            approval,
            decision="approve",
            actor="owner@example.test",
            reviewed_payload_hash=_reviewed_hash(approval),
            created_at=base_time - timedelta(seconds=index),
        )
        for index, approval in enumerate(approvals)
    ]
    other_approval = _approval("foreign-approval", workspace_id="workspace-two")
    events.append(
        _decision_event(
            other_approval,
            decision="deny",
            actor="foreign@example.test",
            reviewed_payload_hash=_reviewed_hash(other_approval),
            workspace_id="workspace-two",
        )
    )
    client, audit_repository = _request(
        monkeypatch,
        events=events,
        approvals=[*approvals, other_approval],
    )

    response = client.get("/approvals/history?limit=2")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["events"]) == 2
    assert payload["has_more"] is True
    assert payload["limit"] == 2
    assert all(
        entry["approval_id"] != "foreign-approval" for entry in payload["events"]
    )
    assert [entry["approval_id"] for entry in payload["events"]] == [
        "approval-0",
        "approval-1",
    ]
    assert audit_repository.queries[0][0] == "workspace-one"
    assert audit_repository.queries[0][1]["limit"] == 3


def test_history_rechecks_approval_and_project_workspace_before_snapshot(monkeypatch) -> None:
    approval = _approval("mismatched-project-approval")
    approval.project_id = "foreign-project"
    event = _decision_event(
        approval,
        decision="approve",
        actor="owner@example.test",
        reviewed_payload_hash=_reviewed_hash(approval),
        workspace_id="workspace-one",
    )
    client, _ = _request(
        monkeypatch,
        events=[event],
        approvals=[approval],
        projects=[
            Project(
                id="foreign-project",
                name="Project from another workspace",
                workspace_id="workspace-two",
            )
        ],
    )

    response = client.get("/approvals/history")

    assert response.status_code == 200
    entry = response.json()["events"][0]
    assert entry["decision"] == "approve"
    assert entry["request_snapshot_verified"] is False
    assert entry["request_snapshot"] is None


def test_history_fails_closed_when_selected_audit_repository_is_unavailable(monkeypatch) -> None:
    user = AuthUser(
        id="owner-one",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        workspace_id="workspace-one",
    )
    monkeypatch.setattr(main, "workspace_id_for_request", lambda _user, _request: "workspace-one")

    def unavailable(_request):
        raise RuntimeError("mismatched runtime bundle")

    monkeypatch.setattr(main, "audit_event_repository_for_request", unavailable)
    monkeypatch.setitem(main.app.dependency_overrides, main.require_reviewer, lambda: user)

    with TestClient(main.app) as client:
        response = client.get("/approvals/history")

    assert response.status_code == 503


def test_history_limit_must_be_between_one_and_one_hundred(monkeypatch) -> None:
    user = AuthUser(
        id="owner-one",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        workspace_id="workspace-one",
    )
    monkeypatch.setitem(main.app.dependency_overrides, main.require_reviewer, lambda: user)

    with TestClient(main.app) as client:
        assert client.get("/approvals/history?limit=0").status_code == 422
        assert client.get("/approvals/history?limit=101").status_code == 422

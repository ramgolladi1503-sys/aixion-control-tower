from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient
from fastapi import HTTPException
from starlette.requests import Request

from app.main import app
from app import audit_routes
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    FileChange,
    Idea,
    Project,
    ProjectMode,
    RiskAssessment,
    RiskLevel,
    AuthUser,
    User,
    UserRole,
    Workspace,
    WorkOrder,
)
from app.recovery_routes import (
    EXCLUDED_COLLECTIONS,
    EXCLUDED_SENSITIVE_COLLECTIONS,
    RECOVERY_FORMAT_VERSION,
    _safe_entities,
    export_recovery_snapshot,
)
from app import recovery_routes
from app.settings import get_settings
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _seed_recovery_data() -> None:
    project = Project(
        name="MCP Shield",
        description="Policy gateway",
        mode=ProjectMode.STRICT,
        rules=["Approval required for mutating tools"],
    )
    idea = Idea(project_id=project.id, title="Recover safely", raw_text="Make backup validation explicit.")
    work_order = WorkOrder(
        project_id=project.id,
        idea_id=idea.id,
        goal="Add recovery proof",
        tasks=["Export operational data", "Validate snapshot shape"],
    )
    audit_event = AuditEvent(
        event_type="recovery.seeded",
        actor="test",
        entity_id=project.id,
        details={"reason": "route test"},
    )
    store.projects[project.id] = project
    store.ideas[idea.id] = idea
    store.work_orders[work_order.id] = work_order
    store.audit_events.append(audit_event)
    store.persist()


def test_recovery_export_returns_safe_operational_snapshot() -> None:
    _seed_recovery_data()

    response = client.get("/audit/recovery/export")

    assert response.status_code == 200
    body = response.json()
    assert body["format_version"] == RECOVERY_FORMAT_VERSION
    assert body["applied_migrations"]
    assert all(
        set(item) == {"migration_id", "name", "applied_at"}
        for item in body["applied_migrations"]
    )
    assert body["counts"]["projects"] == 1
    assert body["counts"]["ideas"] == 1
    assert body["counts"]["work_orders"] == 1
    assert body["counts"]["audit_events"] == 1
    assert body["counts"] == {
        entity_name: len(items)
        for entity_name, items in body["entities"].items()
    }
    assert body["entities"]["projects"][0]["name"] == "MCP Shield"
    assert body["entities"]["ideas"][0]["title"] == "Recover safely"
    assert body["excluded_collections"] == EXCLUDED_COLLECTIONS
    assert body["excluded_sensitive_collections"] == EXCLUDED_SENSITIVE_COLLECTIONS
    assert "users" not in body["entities"]
    assert "sessions" not in body["entities"]


def test_recovery_export_redacts_nested_and_list_audit_secrets() -> None:
    _seed_recovery_data()
    event = store.audit_events[-1]
    store.audit_events[-1] = event.model_copy(
        update={
            "details": {
                "lease_token": "raw-lease-token",
                "nested": [
                    {"authorization": "Bearer raw", "password": "raw-password"},
                    {"safe": "visible", "project_id": "project-safe"},
                ],
            }
        }
    )
    store.persist()

    response = client.get("/audit/recovery/export")

    assert response.status_code == 200
    details = response.json()["entities"]["audit_events"][0]["details"]
    assert details["lease_token"] == "[REDACTED]"
    assert details["nested"][0] == {
        "authorization": "[REDACTED]",
        "password": "[REDACTED]",
    }
    assert details["nested"][1] == {"safe": "visible", "project_id": "project-safe"}


def test_direct_recovery_export_fails_closed_when_postgres_auth_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = AuthUser(
        id="owner-postgres-mode",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        workspace_id="workspace-postgres-mode",
    )

    def unexpected_snapshot_read(*args: object, **kwargs: object) -> object:
        raise AssertionError("SQLite recovery snapshot must not be read in PostgreSQL mode")

    monkeypatch.setattr(
        recovery_routes,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )
    monkeypatch.setattr(
        recovery_routes.recovery_snapshot_repository,
        "read_snapshot",
        unexpected_snapshot_read,
    )

    with pytest.raises(HTTPException) as raised:
        recovery_routes.export_recovery_snapshot(user)
    assert raised.value.status_code == 503
    assert "authoritative recovery repository" in raised.value.detail


def test_direct_recovery_export_fails_closed_when_request_has_postgres_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = AuthUser(
        id="owner-probe-mode",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        workspace_id="workspace-probe-mode",
    )

    def unexpected_snapshot_read(*args: object, **kwargs: object) -> object:
        raise AssertionError("SQLite recovery snapshot must not be read during PostgreSQL runtime")

    monkeypatch.setattr(
        recovery_routes.recovery_snapshot_repository,
        "read_snapshot",
        unexpected_snapshot_read,
    )
    scope = {"type": "http", "app": app}
    request = Request(scope)
    monkeypatch.setattr(app.state, "postgres_runtime", SimpleNamespace(), raising=False)

    with pytest.raises(HTTPException) as raised:
        recovery_routes.export_recovery_snapshot(user, request=request)
    assert raised.value.status_code == 503
    assert "authoritative recovery repository" in raised.value.detail


def test_direct_recovery_export_fails_closed_when_postgres_repository_bundle_is_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = AuthUser(
        id="owner-postgres-repository-mode",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        workspace_id="workspace-postgres-repository-mode",
    )

    def unexpected_snapshot_read(*args: object, **kwargs: object) -> object:
        raise AssertionError("SQLite recovery snapshot must not be read with PostgreSQL repositories")

    monkeypatch.setattr(
        recovery_routes,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=False),
    )
    monkeypatch.setattr(
        recovery_routes.recovery_snapshot_repository,
        "read_snapshot",
        unexpected_snapshot_read,
    )
    previous_runtime = getattr(app.state, "postgres_runtime", None)
    previous_repositories = getattr(app.state, "postgres_repositories", None)
    app.state.postgres_runtime = None
    app.state.postgres_repositories = SimpleNamespace()
    request = Request({"type": "http", "app": app})

    try:
        with pytest.raises(HTTPException) as raised:
            recovery_routes.export_recovery_snapshot(user, request=request)
        assert raised.value.status_code == 503
        assert "authoritative recovery repository" in raised.value.detail
    finally:
        app.state.postgres_runtime = previous_runtime
        app.state.postgres_repositories = None
        if previous_repositories is not None:
            app.state.postgres_repositories = previous_repositories


def test_audit_recovery_alias_uses_authoritative_postgres_snapshot(monkeypatch) -> None:
    workspace = Workspace(id="workspace-pg-recovery", name="PG recovery", owner_user_id="user-pg-recovery")
    persisted_user = User(
        id="user-pg-recovery",
        email="pg-recovery@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        password_hash="hash",
        password_salt="salt",
        workspace_id=workspace.id,
    )
    actor = AuthUser(
        id=persisted_user.id,
        email=persisted_user.email,
        display_name=persisted_user.display_name,
        role=persisted_user.role,
        workspace_id=workspace.id,
    )

    class RecoveryRepository:
        def read_snapshot(self, entity_types, **kwargs):
            assert kwargs["workspace_id"] == workspace.id
            assert kwargs["membership_user_id"] == persisted_user.id
            return (
                {name: [] for name in entity_types},
                [("pg_0001", "baseline", "2026-01-01T00:00:00Z")],
                [],
                {
                    "user": [(persisted_user.id, persisted_user.model_dump_json())],
                    "workspace": [(workspace.id, workspace.model_dump_json())],
                },
            )

    recovery_repository = RecoveryRepository()
    repositories = SimpleNamespace(recovery_snapshots=recovery_repository)
    runtime = SimpleNamespace(
        repositories=repositories,
        recovery_snapshots=recovery_repository,
        verify_schema=lambda: SimpleNamespace(ready=True),
    )
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    postgres_runtime=runtime,
                    postgres_repositories=repositories,
                )
            ),
        }
    )
    settings = replace(get_settings(), auth_enabled=True, postgres_auth_enabled=True)
    monkeypatch.setattr(recovery_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(audit_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(
        recovery_routes.recovery_snapshot_repository,
        "read_snapshot",
        lambda *args, **kwargs: pytest.fail("SQLite recovery fallback was used"),
    )

    snapshot = audit_routes.export_recovery(request=request, _=actor)

    assert snapshot.format_version == RECOVERY_FORMAT_VERSION
    assert snapshot.entities == {name: [] for name in recovery_routes.SAFE_ENTITY_MODELS}


def test_recovery_export_includes_durable_approval_missing_from_compatibility_cache() -> None:
    workspace_id = "workspace_recovery_authority"
    project = Project(name="Recovery authority", workspace_id=workspace_id)
    approval = ApprovalRequest(
        project_id=project.id,
        workspace_id=workspace_id,
        title="Durable approval",
        summary="Must remain in the recovery snapshot if the cache is stale.",
        target_branch="main",
        files=[FileChange(path="README.md", diff="+recovery")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    store.projects[project.id] = project
    store.approval_requests[approval.id] = approval
    store.persist()
    store.approval_requests.pop(approval.id)
    store.projects.pop(project.id)
    cache_only_project = Project(name="Must not enter the durable snapshot", workspace_id=workspace_id)
    store.projects[cache_only_project.id] = cache_only_project

    entities = _safe_entities(workspace_id)

    assert [item["id"] for item in entities["projects"]] == [project.id]
    assert [item["id"] for item in entities["approval_requests"]] == [approval.id]


def test_recovery_export_checks_durable_user_and_workspace_membership() -> None:
    workspace = Workspace(name="Recovery account", owner_user_id="owner-recovery")
    account = User(
        id="owner-recovery",
        email="owner-recovery@example.test",
        display_name="Recovery owner",
        role=UserRole.OWNER,
        password_hash="test-hash",
        password_salt="test-salt",
        workspace_id=workspace.id,
    )
    project = Project(name="Scoped recovery project", workspace_id=workspace.id)
    store.workspaces[workspace.id] = workspace
    store.users[account.id] = account
    store.projects[project.id] = project
    store.persist()
    store.users.pop(account.id)
    actor = AuthUser(
        id=account.id,
        email=account.email,
        display_name=account.display_name,
        role=account.role,
        workspace_id=workspace.id,
    )

    snapshot = export_recovery_snapshot(actor)

    assert [item["id"] for item in snapshot.entities["projects"]] == [project.id]


def test_recovery_export_rejects_stale_workspace_claim() -> None:
    workspace = Workspace(name="Recovery account", owner_user_id="owner-recovery")
    other_workspace = Workspace(name="Other account", owner_user_id="other-owner")
    account = User(
        id="owner-recovery",
        email="owner-recovery@example.test",
        display_name="Recovery owner",
        role=UserRole.OWNER,
        password_hash="test-hash",
        password_salt="test-salt",
        workspace_id=workspace.id,
    )
    store.workspaces.update({workspace.id: workspace, other_workspace.id: other_workspace})
    store.users[account.id] = account
    store.persist()
    actor = AuthUser(
        id=account.id,
        email=account.email,
        display_name=account.display_name,
        role=account.role,
        workspace_id=other_workspace.id,
    )

    try:
        export_recovery_snapshot(actor)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("Recovery export accepted a stale workspace claim")


def test_recovery_validate_accepts_exported_snapshot_with_warning_for_sensitive_exclusions() -> None:
    _seed_recovery_data()
    snapshot = client.get("/audit/recovery/export").json()

    response = client.post("/audit/recovery/validate", json={"snapshot": snapshot})

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["counts"]["projects"] == 1
    assert body["warnings"] == [
        "Snapshot excludes auth/session/device/agent secrets and is not a full credential restore artifact."
    ]


def test_recovery_validate_warns_when_legacy_snapshot_has_no_exclusion_inventory() -> None:
    snapshot = client.get("/audit/recovery/export").json()
    snapshot.pop("excluded_collections")

    response = client.post("/audit/recovery/validate", json={"snapshot": snapshot})

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert "Snapshot has no complete excluded-collection inventory; data completeness is not established." in body["warnings"]


def test_recovery_validate_rejects_unknown_entity_collections() -> None:
    snapshot = client.get("/audit/recovery/export").json()
    snapshot["entities"]["users"] = []

    response = client.post("/audit/recovery/validate", json={"snapshot": snapshot})

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert "Unknown entity collection: users" in body["errors"]


def test_recovery_validate_rejects_invalid_entity_payload() -> None:
    snapshot = client.get("/audit/recovery/export").json()
    snapshot["entities"]["projects"] = [{"id": "project_missing_required_fields"}]

    response = client.post("/audit/recovery/validate", json={"snapshot": snapshot})

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any(error.startswith("projects[0] is invalid") for error in body["errors"])


def test_recovery_validate_rejects_unsupported_format_version() -> None:
    snapshot = client.get("/audit/recovery/export").json()
    snapshot["format_version"] = "unsupported"

    response = client.post("/audit/recovery/validate", json={"snapshot": snapshot})

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert "Unsupported recovery format_version: unsupported" in body["errors"]


def test_recovery_validate_rejects_missing_collection() -> None:
    snapshot = client.get("/audit/recovery/export").json()
    snapshot["entities"].pop("ideas")

    response = client.post("/audit/recovery/validate", json={"snapshot": snapshot})

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert "Missing entity collection: ideas" in body["errors"]


def test_recovery_validate_rejects_count_mismatch() -> None:
    _seed_recovery_data()
    snapshot = client.get("/audit/recovery/export").json()
    snapshot["counts"]["projects"] = 99

    response = client.post("/audit/recovery/validate", json={"snapshot": snapshot})

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert "projects count mismatch: declared 99, found 1" in body["errors"]


def test_recovery_validate_rejects_duplicate_entity_ids() -> None:
    _seed_recovery_data()
    snapshot = client.get("/audit/recovery/export").json()
    snapshot["entities"]["projects"].append(snapshot["entities"]["projects"][0])
    snapshot["counts"]["projects"] = 2

    response = client.post("/audit/recovery/validate", json={"snapshot": snapshot})

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any(error.startswith("projects[1] duplicates id ") for error in body["errors"])


def test_recovery_validate_rejects_missing_parent_reference() -> None:
    _seed_recovery_data()
    snapshot = client.get("/audit/recovery/export").json()
    snapshot["entities"]["work_orders"][0]["project_id"] = "project_missing"

    response = client.post("/audit/recovery/validate", json={"snapshot": snapshot})

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert (
        "work_orders[0] references missing projects id project_missing"
        in body["errors"]
    )

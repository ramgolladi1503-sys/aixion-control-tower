from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import application_repository_access, github_runner, main, workspace_access
from app.application_repositories import ApplicationRepositories
from app.approval_integrity import compute_approval_payload_hash
from app.auth import require_maintainer
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuthUser,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
    User,
    UserRole,
    Workspace,
)
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_patch_plan_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Callable[[], Connection], Workspace, Project, AuthUser]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL patch-plan tests")

    schema = f"ct_github_patch_plan_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    settings = SimpleNamespace(is_production=False, postgres_auth_enabled=True)
    owner = User(
        id=f"user_{uuid.uuid4().hex}",
        email=f"owner-{uuid.uuid4().hex}@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        password_hash="0" * 64,
        password_salt="0" * 32,
        workspace_id=f"workspace_{uuid.uuid4().hex}",
        email_verified=True,
    )
    workspace = Workspace(
        id=owner.workspace_id,
        name="GitHub patch-plan workspace",
        owner_user_id=owner.id,
    )
    project = Project(
        id=f"project_{uuid.uuid4().hex}",
        name="Patch-plan project",
        workspace_id=workspace.id,
        github_repo="example/patch-plan",
    )
    user = AuthUser(
        id=owner.id,
        email=owner.email,
        display_name=owner.display_name,
        role=owner.role,
        workspace_id=workspace.id,
    )

    try:
        run_postgres_migrations(connect)
        with connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (
                    owner.id,
                    workspace.id,
                    owner.email,
                    owner.email.lower(),
                    owner.role.value,
                    Jsonb(owner.model_dump(mode="json")),
                ),
            )
            connection.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
            )

        repositories = ApplicationRepositories.from_connector(connect)
        runtime = SimpleNamespace(repositories=repositories)
        monkeypatch.setattr(github_runner, "get_settings", lambda: settings)
        monkeypatch.setattr(application_repository_access, "get_settings", lambda: settings)
        monkeypatch.setattr(workspace_access, "get_settings", lambda: settings)
        monkeypatch.setattr(main.app.state, "postgres_runtime", runtime, raising=False)
        monkeypatch.setattr(
            main.app.state,
            "postgres_repositories",
            repositories,
            raising=False,
        )
        monkeypatch.setitem(
            main.app.dependency_overrides,
            require_maintainer,
            lambda: user,
        )
        yield connect, workspace, project, user
    finally:
        main.app.dependency_overrides.pop(require_maintainer, None)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _insert_approved_request(
    connect: Callable[[], Connection],
    *,
    workspace_id: str,
    project_id: str,
) -> ApprovalRequest:
    approval = ApprovalRequest(
        id=f"approval_{uuid.uuid4().hex[:12]}",
        project_id=project_id,
        workspace_id=workspace_id,
        title="Approved docs change",
        summary="Update approved docs content",
        agent_name="test-agent",
        target_branch="feature/docs-change",
        files=[
            FileChange(
                path="docs/README.md",
                change_type="update",
                diff="+approved",
                new_content="approved content\n",
            )
        ],
        test_plan=["pytest"],
        rollback_plan="Revert docs commit",
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
        approved_by_user_id="postgres-test-reviewer",
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    with connect() as connection:
        connection.execute(
            "INSERT INTO approval_requests "
            "(approval_id,project_id,workspace_id,work_order_id,source_agent_id,status,"
            "expires_at,created_at,updated_at,version,payload) "
            "VALUES(%s,%s,%s,NULL,NULL,%s,%s,%s,%s,0,%s)",
            (
                approval.id,
                approval.project_id,
                approval.workspace_id,
                approval.status.value,
                approval.expires_at,
                approval.created_at,
                approval.updated_at,
                Jsonb(approval.model_dump(mode="json")),
            ),
        )
    return approval


def test_github_patch_plan_reads_approved_request_from_scoped_postgres_bundle(
    postgres_patch_plan_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project, _user = postgres_patch_plan_runtime
    approval = _insert_approved_request(
        connect,
        workspace_id=workspace.id,
        project_id=project.id,
    )

    class RejectLocalApprovalRead(dict):
        def get(self, *_args, **_kwargs):
            pytest.fail("PostgreSQL patch planning must not read SQLite approvals")

    monkeypatch.setattr(store, "approval_requests", RejectLocalApprovalRead())

    response = TestClient(main.app).post(
        "/github-runner/patch-plan",
        json={
            "approval_request_id": approval.id,
            "repository_full_name": "example/patch-plan",
            "base_branch": "main",
            "feature_branch": "feature/docs-change",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["ready"] is False
    assert response.json()["files"] == ["docs/README.md"]
    assert response.json()["blockers"] == [
        "The PostgreSQL project record does not persist a GitHub repository binding; "
        "GitHub execution planning is unavailable."
    ]


def test_github_patch_plan_hides_another_workspace_approval(
    postgres_patch_plan_runtime,
) -> None:
    connect, _workspace, _project, _user = postgres_patch_plan_runtime
    other_owner = User(
        id=f"user_{uuid.uuid4().hex}",
        email=f"other-{uuid.uuid4().hex}@example.test",
        display_name="Other owner",
        role=UserRole.OWNER,
        password_hash="0" * 64,
        password_salt="0" * 32,
        workspace_id=f"workspace_{uuid.uuid4().hex}",
        email_verified=True,
    )
    other_workspace = Workspace(
        id=other_owner.workspace_id,
        name="Other workspace",
        owner_user_id=other_owner.id,
    )
    other_project = Project(
        id=f"project_{uuid.uuid4().hex}",
        name="Other project",
        workspace_id=other_workspace.id,
    )
    with connect() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (other_workspace.id, Jsonb(other_workspace.model_dump(mode="json"))),
        )
        connection.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
            "VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
            (
                other_owner.id,
                other_workspace.id,
                other_owner.email,
                other_owner.email.lower(),
                other_owner.role.value,
                Jsonb(other_owner.model_dump(mode="json")),
            ),
        )
        connection.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (
                other_project.id,
                other_workspace.id,
                Jsonb(other_project.model_dump(mode="json")),
            ),
        )
    approval = _insert_approved_request(
        connect,
        workspace_id=other_workspace.id,
        project_id=other_project.id,
    )

    response = TestClient(main.app).post(
        "/github-runner/patch-plan",
        json={
            "approval_request_id": approval.id,
            "repository_full_name": "example/patch-plan",
            "base_branch": "main",
            "feature_branch": "feature/docs-change",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Approval request not found"

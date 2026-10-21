from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import main
from app.agent_task_routes import MaintainerDependency, ReviewerDependency
from app.application_repositories import ApplicationRepositories
from app.models import (
    ApprovalRequest,
    AuditEvent,
    AuthUser,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
    TestRun as _TestRun,
    User,
    UserRole,
    Workspace,
)
from app import postgres_database
from app.postgres_database import run_postgres_migrations
from app.store import store
from app.test_run_repository import PostgresTestRunRepository
from app.test_run_repository import TestRunRepositoryScopeError as _TestRunRepositoryScopeError


@pytest.fixture
def postgres_test_run_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Callable[[], Connection], Workspace, Project]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL test-run route tests")
    schema = f"ct_test_run_routes_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        owner = User(
            id=f"user_{uuid.uuid4().hex}",
            email=f"test-run-owner-{uuid.uuid4().hex}@example.test",
            display_name="Test run owner",
            role=UserRole.OWNER,
            password_hash="hash",
            password_salt="salt",
            workspace_id=f"workspace_{uuid.uuid4().hex}",
            email_verified=True,
        )
        workspace = Workspace(
            id=owner.workspace_id, name="Test run workspace", owner_user_id=owner.id
        )
        project = Project(
            id=f"project_{uuid.uuid4().hex}",
            name="Evidence project",
            workspace_id=workspace.id,
        )
        with connect() as conn:
            conn.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (owner.id, workspace.id, owner.email, owner.email.lower(), owner.role.value,
                 Jsonb(owner.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
            )
        repositories = ApplicationRepositories.from_connector(connect)
        for name, value in {
            "approval_decision_repository": repositories.approval_decisions,
            "project_repository": repositories.projects,
            "workspace_membership_repository": repositories.workspace_membership,
            "test_run_repository": repositories.test_runs,
        }.items():
            monkeypatch.setattr(main.app.state, name, value, raising=False)
        user = AuthUser(
            id=owner.id,
            email=owner.email,
            display_name=owner.display_name,
            role=owner.role,
            workspace_id=workspace.id,
        )
        monkeypatch.setitem(main.app.dependency_overrides, MaintainerDependency.dependency, lambda: user)
        monkeypatch.setitem(main.app.dependency_overrides, ReviewerDependency.dependency, lambda: user)
        monkeypatch.setitem(main.app.dependency_overrides, main.ReviewerDependency.dependency, lambda: user)
        yield connect, workspace, project
    finally:
        main.app.dependency_overrides.pop(MaintainerDependency.dependency, None)
        main.app.dependency_overrides.pop(ReviewerDependency.dependency, None)
        main.app.dependency_overrides.pop(main.ReviewerDependency.dependency, None)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_test_run_create_and_list_are_workspace_scoped_and_audited(
    postgres_test_run_runtime,
) -> None:
    connect, workspace, project = postgres_test_run_runtime
    client = TestClient(main.app)
    before_test_runs = set(store.test_runs)
    before_audits = {event.id for event in store.audit_events}
    approval_response = client.post("/approvals", json={
        "project_id": project.id,
        "title": "Approval with test evidence",
        "summary": "Persist validation evidence against the owning workspace.",
        "target_branch": "feature/test-run-persistence",
        "files": [{"path": "src/evidence.txt", "change_type": "modified", "diff": "-old\n+new"}],
    })
    assert approval_response.status_code == 200, approval_response.text
    approval_id = approval_response.json()["id"]

    created = client.post("/test-runs", json={
        "approval_request_id": approval_id,
        "command": "pytest -q",
        "status": "PASSED",
        "output_summary": "All targeted checks passed.",
    })

    assert created.status_code == 200, created.text
    test_run = created.json()
    assert client.get("/test-runs").json() == [test_run]
    repository = main.app.state.test_run_repository
    assert repository.list_for_workspace("workspace_elsewhere") == []
    rejected = _TestRun(
        approval_request_id=approval_id,
        command="must not cross workspace",
        status="PASSED",
    )
    with pytest.raises(_TestRunRepositoryScopeError, match="outside its workspace"):
        repository.create(
            test_run=rejected,
            workspace_id="workspace_elsewhere",
            audit_event=AuditEvent(
                event_type="test_run.recorded",
                entity_id=rejected.id,
                workspace_id="workspace_elsewhere",
                details={
                    "approval_request_id": approval_id,
                    "status": rejected.status,
                    "command": rejected.command,
                },
            ),
        )
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM test_runs WHERE test_run_id=%s AND approval_id=%s "
            "AND project_id=%s AND workspace_id=%s",
            (test_run["id"], approval_id, project.id, workspace.id),
        ).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM test_runs").fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_type='test_run' "
            "AND entity_id=%s AND event_type='test_run.recorded'",
            (workspace.id, test_run["id"]),
        ).fetchone()[0] == 1
    assert set(store.test_runs) == before_test_runs
    assert {event.id for event in store.audit_events} == before_audits


def test_pg_0013_upgrades_pg_0012_schema_without_losing_approval_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL migration tests")
    schema = f"ct_test_run_upgrade_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    migrations = postgres_database.POSTGRES_MIGRATIONS
    try:
        monkeypatch.setattr(postgres_database, "POSTGRES_MIGRATIONS", migrations[:-1])
        run_postgres_migrations(connect)
        workspace = Workspace(
            id=f"workspace_{uuid.uuid4().hex}",
            name="Upgrade fixture",
            owner_user_id=f"owner_{uuid.uuid4().hex}",
        )
        project = Project(
            id=f"project_{uuid.uuid4().hex}",
            name="Existing project",
            workspace_id=workspace.id,
        )
        approval = ApprovalRequest(
            project_id=project.id,
            workspace_id=workspace.id,
            title="Pre-upgrade approval",
            summary="This row must survive the additive migration.",
            target_branch="feature/migration-upgrade",
            files=[FileChange(path="src/keep.txt", diff="+keep")],
            risk=RiskAssessment(level=RiskLevel.LOW),
        )
        with connect() as conn:
            conn.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,"
                "source_agent_id,status,expires_at,created_at,updated_at,version,payload) "
                "VALUES(%s,%s,%s,NULL,NULL,%s,%s,%s,%s,0,%s)",
                (approval.id, project.id, workspace.id, approval.status.value,
                 approval.expires_at, approval.created_at, approval.updated_at,
                 Jsonb(approval.model_dump(mode="json"))),
            )

        monkeypatch.setattr(postgres_database, "POSTGRES_MIGRATIONS", migrations)
        run_postgres_migrations(connect)
        with connect() as conn:
            assert conn.execute(
                "SELECT migration_id FROM schema_migrations WHERE migration_id=%s",
                ("pg_0013_workspace_scoped_test_runs",),
            ).fetchone() == ("pg_0013_workspace_scoped_test_runs",)
            saved_approval = conn.execute(
                "SELECT payload FROM approval_requests WHERE approval_id=%s",
                (approval.id,),
            ).fetchone()[0]
            assert ApprovalRequest.model_validate(saved_approval) == approval

        test_run = _TestRun(
            approval_request_id=approval.id,
            command="pytest -q",
            status="PASSED",
        )
        event = AuditEvent(
            event_type="test_run.recorded",
            entity_id=test_run.id,
            workspace_id=workspace.id,
            details={
                "approval_request_id": approval.id,
                "status": test_run.status,
                "command": test_run.command,
            },
        )
        PostgresTestRunRepository(connect).create(
            test_run=test_run,
            workspace_id=workspace.id,
            audit_event=event,
        )
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

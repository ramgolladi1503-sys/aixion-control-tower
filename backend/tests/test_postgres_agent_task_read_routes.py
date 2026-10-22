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

from app import main
from app.approval_decisions import agent_task_repository as sqlite_agent_task_repository
from app.agent_task_routes import MaintainerDependency, ReviewerDependency
from app.approval_recovery import reconcile_linked_approval_tasks
from app.application_repositories import ApplicationRepositories
from app.models import AuthUser, Project, User, UserRole, Workspace
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_agent_task_route_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[
    tuple[Callable[[], Connection], Workspace, Project]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL AgentTask route tests")
    schema = f"ct_agent_task_routes_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        owner = User(
            id=f"user_{uuid.uuid4().hex}", email=f"owner-{uuid.uuid4().hex}@example.test",
            display_name="Owner", role=UserRole.OWNER, password_hash="hash",
            password_salt="salt", workspace_id=f"workspace_{uuid.uuid4().hex}",
            email_verified=True,
        )
        workspace = Workspace(id=owner.workspace_id, name="AgentTask routes", owner_user_id=owner.id)
        project = Project(id=f"project_{uuid.uuid4().hex}", name="Task project", workspace_id=workspace.id)
        with connect() as conn:
            conn.execute("INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                         (workspace.id, Jsonb(workspace.model_dump(mode="json"))))
            conn.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (owner.id, workspace.id, owner.email, owner.email.lower(), owner.role.value,
                 Jsonb(owner.model_dump(mode="json"))),
            )
            conn.execute("INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                         (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))))
        repositories = ApplicationRepositories.from_connector(connect)
        for name, value in {
            "agent_task_repository": repositories.agent_tasks,
            "approval_decision_repository": repositories.approval_decisions,
            "agent_run_repository": repositories.agent_runs,
            "project_repository": repositories.projects,
            "workspace_membership_repository": repositories.workspace_membership,
        }.items():
            monkeypatch.setattr(main.app.state, name, value, raising=False)
        user = AuthUser(id=owner.id, email=owner.email, display_name=owner.display_name,
                        role=owner.role, workspace_id=workspace.id)
        monkeypatch.setitem(main.app.dependency_overrides, MaintainerDependency.dependency, lambda: user)
        monkeypatch.setitem(main.app.dependency_overrides, ReviewerDependency.dependency, lambda: user)
        monkeypatch.setitem(
            main.app.dependency_overrides,
            main.ReviewerDependency.dependency,
            lambda: user,
        )
        yield connect, workspace, project
    finally:
        main.app.dependency_overrides.pop(MaintainerDependency.dependency, None)
        main.app.dependency_overrides.pop(ReviewerDependency.dependency, None)
        main.app.dependency_overrides.pop(main.ReviewerDependency.dependency, None)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_agent_task_create_list_get_are_workspace_scoped_and_do_not_project_to_sqlite(
    postgres_agent_task_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project = postgres_agent_task_route_runtime
    monkeypatch.setattr(
        main.app.state,
        "postgres_repositories",
        ApplicationRepositories.from_connector(connect),
        raising=False,
    )
    monkeypatch.setattr(
        "app.application_repository_access.get_settings",
        lambda: SimpleNamespace(auth_enabled=True, postgres_auth_enabled=True),
    )
    before_tasks, before_events = set(store.agent_tasks), set(store.agent_task_events)
    before_audits = {event.id for event in store.audit_events}
    client = TestClient(main.app)
    created = client.post("/agent/tasks", json={
        "provider": "MANUAL", "project_id": project.id,
        "title": "Scoped task", "goal": "Persist task state in PostgreSQL",
        "requested_action": "CREATE_WORK_ORDER", "requires_approval": True,
    })
    assert created.status_code == 200, created.text
    task = created.json()
    assert task["workspace_id"] == workspace.id
    assert client.get("/agent/tasks").json() == [task]
    assert client.get(f"/agent/tasks/{task['id']}").json() == task
    events = client.get(f"/agent/tasks/{task['id']}/events")
    assert events.status_code == 200
    assert len(events.json()) == 1
    assert events.json()[0]["event_type"] == "TASK_CREATED"
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
                            (task["id"], workspace.id)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM agent_task_events WHERE task_id=%s",
                            (task["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_id=%s",
                            (workspace.id, task["id"])).fetchone()[0] == 1
    assert set(store.agent_tasks) == before_tasks
    assert set(store.agent_task_events) == before_events
    assert {event.id for event in store.audit_events} == before_audits


def test_postgres_agent_task_requires_workspace_project_scope(
    postgres_agent_task_route_runtime,
) -> None:
    _connect, _workspace, _project = postgres_agent_task_route_runtime
    client = TestClient(main.app)
    response = client.post("/agent/tasks", json={
        "provider": "MANUAL", "title": "Unscoped", "goal": "Must fail closed",
    })
    assert response.status_code == 422


def test_postgres_task_linked_approval_does_not_project_to_sqlite(
    postgres_agent_task_route_runtime,
) -> None:
    connect, workspace, project = postgres_agent_task_route_runtime
    client = TestClient(main.app)
    before_task_ids = set(store.agent_tasks)
    before_event_ids = set(store.agent_task_events)
    before_approval_ids = set(store.approval_requests)
    before_audit_ids = {event.id for event in store.audit_events}
    before_notification_ids = set(store.notifications)

    task_response = client.post("/agent/tasks", json={
        "provider": "MANUAL", "project_id": project.id,
        "title": "Task needing approval", "goal": "Keep approval persistence unified",
        "requested_action": "CREATE_WORK_ORDER", "requires_approval": True,
    })
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()
    approval_response = client.post(f"/agent/tasks/{task['id']}/approval", json={
        "project_id": project.id,
        "title": "Approve task action",
        "summary": "The linked approval and task transition must be durable together.",
        "target_branch": "feature/approval-route-test",
        "files": [{
            "path": "src/task-action.txt",
            "change_type": "modified",
            "diff": "-old\n+new",
        }],
    })
    assert approval_response.status_code == 200, approval_response.text
    approval_id = approval_response.json()["id"]

    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
            (approval_id, workspace.id),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT approval_request_id, status FROM agent_tasks WHERE task_id=%s",
            (task["id"],),
        ).fetchone() == (approval_id, "WAITING_FOR_APPROVAL")
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE task_id=%s",
            (task["id"],),
        ).fetchone()[0] == 1
    assert set(store.agent_tasks) == before_task_ids
    assert set(store.agent_task_events) == before_event_ids
    assert set(store.approval_requests) == before_approval_ids
    assert {event.id for event in store.audit_events} == before_audit_ids
    assert set(store.notifications) == before_notification_ids


def test_postgres_approval_decision_schedules_linked_run_in_postgres(
    postgres_agent_task_route_runtime,
) -> None:
    connect, workspace, project = postgres_agent_task_route_runtime
    client = TestClient(main.app)
    before_run_ids = set(store.agent_runs)
    before_run_step_ids = set(store.agent_run_steps)
    before_run_event_ids = set(store.agent_run_events)
    before_audit_ids = {event.id for event in store.audit_events}

    task_response = client.post("/agent/tasks", json={
        "provider": "MANUAL", "project_id": project.id,
        "title": "Approved task run", "goal": "Create durable run after approval",
        "requested_action": "CREATE_WORK_ORDER", "requires_approval": True,
    })
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()
    approval_response = client.post(f"/agent/tasks/{task['id']}/approval", json={
        "project_id": project.id,
        "title": "Approve task run",
        "summary": "The run and scheduling audit must use the same PostgreSQL store.",
        "target_branch": "feature/approval-route-test",
        "files": [{"path": "src/run.txt", "change_type": "modified", "diff": "-old\n+new"}],
    })
    assert approval_response.status_code == 200, approval_response.text
    approval_id = approval_response.json()["id"]

    decision = client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"})

    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "APPROVED"
    run_repository = main.app.state.agent_run_repository
    run = run_repository.get_active_for_task(task["id"])
    assert run is not None and run.approval_request_id == approval_id
    steps = run_repository.list_steps_for_run(run.id)
    assert len(steps) > 0
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_runs WHERE run_id=%s AND workspace_id=%s",
            (run.id, workspace.id),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM agent_run_steps WHERE run_id=%s",
            (run.id,),
        ).fetchone()[0] == len(steps)
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_type='agent_task' "
            "AND entity_id=%s AND event_type='agent_task.run_scheduled' "
            "AND details->>'agent_run_id'=%s",
            (workspace.id, task["id"], run.id),
        ).fetchone()[0] == 1
    assert set(store.agent_runs) == before_run_ids
    assert set(store.agent_run_steps) == before_run_step_ids
    assert set(store.agent_run_events) == before_run_event_ids
    assert {event.id for event in store.audit_events} == before_audit_ids


def test_postgres_recovery_repairs_interrupted_approval_propagation(
    postgres_agent_task_route_runtime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project = postgres_agent_task_route_runtime
    client = TestClient(main.app, raise_server_exceptions=False)
    task_response = client.post("/agent/tasks", json={
        "provider": "MANUAL", "project_id": project.id,
        "title": "Interrupted approval", "goal": "Recover committed decision safely",
        "requested_action": "CREATE_WORK_ORDER", "requires_approval": True,
    })
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()
    approval_response = client.post(f"/agent/tasks/{task['id']}/approval", json={
        "project_id": project.id,
        "title": "Recover approval decision",
        "summary": "Simulate a process interruption after durable approval commit.",
        "target_branch": "feature/approval-recovery-test",
        "files": [{"path": "src/recovery.txt", "change_type": "modified", "diff": "-old\n+new"}],
    })
    assert approval_response.status_code == 200, approval_response.text
    approval_id = approval_response.json()["id"]

    original_propagator = main.propagate_approval_decision_to_agent_task

    def simulate_process_interruption(*_args, **_kwargs) -> None:
        raise RuntimeError("simulated interruption after approval commit")

    monkeypatch.setattr(
        main, "propagate_approval_decision_to_agent_task", simulate_process_interruption
    )
    decision = client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"})
    assert decision.status_code == 500
    monkeypatch.setattr(main, "propagate_approval_decision_to_agent_task", original_propagator)

    approval_repository = main.app.state.approval_decision_repository
    task_repository = main.app.state.agent_task_repository
    run_repository = main.app.state.agent_run_repository
    assert reconcile_linked_approval_tasks(
        approval_repository=approval_repository,
        task_repository=task_repository,
        run_repository=run_repository,
    ) == 1
    run = run_repository.get_active_for_task(task["id"])
    assert run is not None and run.approval_request_id == approval_id
    assert task_repository.get(task["id"]).status.value == "APPROVED"
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_type='agent_task' "
            "AND entity_id=%s AND event_type='agent_task.run_scheduled' "
            "AND details->>'agent_run_id'=%s",
            (workspace.id, task["id"], run.id),
        ).fetchone()[0] == 1
    assert reconcile_linked_approval_tasks(
        approval_repository=approval_repository,
        task_repository=task_repository,
        run_repository=run_repository,
    ) == 0
    assert run_repository.list_for_task(task["id"]) == [run]


def test_task_linked_approval_fails_closed_for_mixed_repository_profiles(
    postgres_agent_task_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect, _workspace, project = postgres_agent_task_route_runtime
    client = TestClient(main.app)
    task_response = client.post("/agent/tasks", json={
        "provider": "MANUAL", "project_id": project.id,
        "title": "Task for adapter mismatch", "goal": "Reject mixed stores",
        "requested_action": "CREATE_WORK_ORDER", "requires_approval": True,
    })
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()
    monkeypatch.setattr(main.app.state, "agent_task_repository", sqlite_agent_task_repository)

    response = client.post(f"/agent/tasks/{task['id']}/approval", json={
        "project_id": project.id,
        "title": "Must fail before persistence",
        "summary": "Approval and task adapters must share one persistence profile.",
        "target_branch": "feature/approval-route-test",
        "files": [{"path": "src/task-action.txt", "change_type": "modified", "diff": "-a\n+b"}],
    })

    assert response.status_code == 503
    assert response.json()["detail"] == "Approval and AgentTask persistence adapters are not aligned"

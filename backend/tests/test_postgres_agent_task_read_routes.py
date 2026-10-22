from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import approval_recovery, main
from app.approval_decisions import agent_task_repository as sqlite_agent_task_repository
from app.agent_task_models import AgentTaskStatus
from app.agent_task_routes import MaintainerDependency, ReviewerDependency
from app.approval_recovery import reconcile_linked_approval_tasks
from app.application_repositories import ApplicationRepositories
from app.models import AuthUser, Project, User, UserRole, Workspace
from app.postgres_database import run_postgres_migrations
from app.postgres_runtime import PostgresRuntime
from app.settings import get_settings
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
        monkeypatch.setattr(
            "app.application_repository_access.get_settings",
            lambda: SimpleNamespace(postgres_auth_enabled=True),
        )
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
            "postgres_runtime": SimpleNamespace(repositories=repositories),
            "postgres_repositories": repositories,
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
        main.app.state,
        "postgres_runtime",
        SimpleNamespace(repositories=main.app.state.postgres_repositories),
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


def test_postgres_recovery_is_idempotent_after_interrupted_route_propagation(
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
    # Approval, task, notification, audit and initial run committed atomically.
    # Recovery therefore has no stranded decision to repair after route failure.
    assert reconcile_linked_approval_tasks(
        approval_repository=approval_repository,
        task_repository=task_repository,
        run_repository=run_repository,
    ) == 0
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


def test_postgres_recovery_failure_is_observable_and_retry_is_idempotent(
    postgres_agent_task_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _connect, _workspace, project = postgres_agent_task_route_runtime
    client = TestClient(main.app, raise_server_exceptions=False)
    task_response = client.post("/agent/tasks", json={
        "provider": "MANUAL", "project_id": project.id,
        "title": "Retry interrupted recovery", "goal": "Retry the scheduling boundary",
        "requested_action": "CREATE_WORK_ORDER", "requires_approval": True,
    })
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()
    approval_response = client.post(f"/agent/tasks/{task['id']}/approval", json={
        "project_id": project.id,
        "title": "Retry recovery decision",
        "summary": "Scheduling may fail after the approval decision is committed.",
        "target_branch": "feature/approval-recovery-retry-test",
        "files": [{"path": "src/retry.txt", "change_type": "modified", "diff": "-old\n+new"}],
    })
    assert approval_response.status_code == 200, approval_response.text
    approval_id = approval_response.json()["id"]

    original_propagator = main.propagate_approval_decision_to_agent_task
    monkeypatch.setattr(
        main,
        "propagate_approval_decision_to_agent_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated interruption after approval commit")
        ),
    )
    decision = client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"})
    assert decision.status_code == 500
    monkeypatch.setattr(main, "propagate_approval_decision_to_agent_task", original_propagator)

    # Model a pre-atomic legacy row state: the decision committed but its
    # initial run was absent. Recovery remains available for these records.
    with _connect() as conn:
        conn.execute("DELETE FROM agent_run_events WHERE task_id=%s", (task["id"],))
        conn.execute("DELETE FROM agent_run_steps WHERE task_id=%s", (task["id"],))
        # Audit rows are append-only and intentionally remain as evidence of
        # the legacy run that this fixture removes.
        conn.execute("DELETE FROM agent_runs WHERE task_id=%s", (task["id"],))

    approval_repository = main.app.state.approval_decision_repository
    task_repository = main.app.state.agent_task_repository
    run_repository = main.app.state.agent_run_repository
    original_create_run = approval_recovery.create_agent_run
    attempts = 0

    def fail_first_schedule(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated transient AgentRun persistence failure")
        return original_create_run(*args, **kwargs)

    monkeypatch.setattr(approval_recovery, "create_agent_run", fail_first_schedule)
    with caplog.at_level("ERROR", logger="app.approval_recovery"):
        assert reconcile_linked_approval_tasks(
            approval_repository=approval_repository,
            task_repository=task_repository,
            run_repository=run_repository,
        ) == 0

    assert task_repository.get(task["id"]).status.value == "APPROVED"
    assert run_repository.list_for_task(task["id"]) == []
    assert any(
        f"approval_id={approval_id}" in record.message
        and f"task_id={task['id']}" in record.message
        and "phase=schedule_first_run" in record.message
        and "error_type=RuntimeError" in record.message
        for record in caplog.records
    )

    assert reconcile_linked_approval_tasks(
        approval_repository=approval_repository,
        task_repository=task_repository,
        run_repository=run_repository,
    ) == 1
    runs = run_repository.list_for_task(task["id"])
    assert len(runs) == 1
    assert runs[0].approval_request_id == approval_id
    with _connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s AND event_type='APPROVED'",
            (task["id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type='agent_task' "
            "AND entity_id=%s AND event_type='agent_task.run_scheduled'",
            (task["id"],),
        ).fetchone()[0] == 2  # the removed legacy run's immutable audit remains

    assert reconcile_linked_approval_tasks(
        approval_repository=approval_repository,
        task_repository=task_repository,
        run_repository=run_repository,
    ) == 0
    assert run_repository.list_for_task(task["id"]) == runs


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
    monkeypatch.setattr(
        "app.application_repository_access.get_settings",
        lambda: SimpleNamespace(auth_enabled=True, postgres_auth_enabled=False),
    )

    response = client.post(f"/agent/tasks/{task['id']}/approval", json={
        "project_id": project.id,
        "title": "Must fail before persistence",
        "summary": "Approval and task adapters must share one persistence profile.",
        "target_branch": "feature/approval-route-test",
        "files": [{"path": "src/task-action.txt", "change_type": "modified", "diff": "-a\n+b"}],
    })

    assert response.status_code == 503
    assert response.json()["detail"] == "Approval and AgentTask persistence adapters are not aligned"


def _bind_postgres_agent_task_runtime(
    connect: Callable[[], Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = ApplicationRepositories.from_connector(connect)
    monkeypatch.setattr(main.app.state, "postgres_repositories", repositories, raising=False)
    monkeypatch.setattr(
        main.app.state,
        "postgres_runtime",
        SimpleNamespace(repositories=repositories),
        raising=False,
    )
    monkeypatch.setattr(
        "app.application_repository_access.get_settings",
        lambda: SimpleNamespace(auth_enabled=True, postgres_auth_enabled=True),
    )


def _create_postgres_task_with_pending_approval(
    client: TestClient,
    project: Project,
) -> tuple[dict, str]:
    task_response = client.post("/agent/tasks", json={
        "provider": "MANUAL", "project_id": project.id,
        "title": "Task to cancel", "goal": "Cancel task and linked approval atomically",
        "requested_action": "CREATE_WORK_ORDER", "requires_approval": True,
    })
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()
    approval_response = client.post(f"/agent/tasks/{task['id']}/approval", json={
        "project_id": project.id,
        "title": "Approval to cancel",
        "summary": "The task and approval must be cancelled in one transaction.",
        "target_branch": "feature/cancel-route-test",
        "files": [{"path": "src/cancel.txt", "change_type": "modified", "diff": "-old\n+new"}],
    })
    assert approval_response.status_code == 200, approval_response.text
    return task, approval_response.json()["id"]


def test_postgres_cancel_route_atomically_cancels_task_approval_audits_and_notification(
    postgres_agent_task_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project = postgres_agent_task_route_runtime
    _bind_postgres_agent_task_runtime(connect, monkeypatch)
    before_task_ids = set(store.agent_tasks)
    before_event_ids = set(store.agent_task_events)
    before_approval_ids = set(store.approval_requests)
    before_audit_ids = {event.id for event in store.audit_events}
    before_notification_ids = set(store.notifications)
    client = TestClient(main.app)
    task, approval_id = _create_postgres_task_with_pending_approval(client, project)
    with connect() as conn:
        notifications_before = conn.execute(
            "SELECT count(*) FROM notifications WHERE approval_id=%s", (approval_id,)
        ).fetchone()[0]

    response = client.post(f"/agent/tasks/{task['id']}/cancel", json={"reason": "operator request"})

    assert response.status_code == 200, response.text
    event = response.json()
    assert event["event_type"] == "CANCELLED"
    assert event["metadata"]["approval_decision_changed"] is True
    with connect() as conn:
        assert conn.execute(
            "SELECT status FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
            (task["id"], workspace.id),
        ).fetchone() == ("CANCELLED",)
        assert conn.execute(
            "SELECT status FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
            (approval_id, workspace.id),
        ).fetchone() == ("CANCELLED",)
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s AND event_type='CANCELLED'",
            (task["id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT event_type,entity_type,entity_id FROM audit_events "
            "WHERE workspace_id=%s AND entity_id IN (%s,%s) "
            "AND event_type IN ('approval.cancelled','agent_task.cancelled') ORDER BY entity_type",
            (workspace.id, task["id"], approval_id),
        ).fetchall() == [
            ("agent_task.cancelled", "agent_task", task["id"]),
            ("approval.cancelled", "approval_request", approval_id),
        ]
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE approval_id=%s AND workspace_id=%s",
            (approval_id, workspace.id),
        ).fetchone()[0] == notifications_before + 1
    assert set(store.agent_tasks) == before_task_ids
    assert set(store.agent_task_events) == before_event_ids
    assert set(store.approval_requests) == before_approval_ids
    assert {event.id for event in store.audit_events} == before_audit_ids
    assert set(store.notifications) == before_notification_ids


def test_postgres_retry_summary_and_transition_use_durable_retry_history(
    postgres_agent_task_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project = postgres_agent_task_route_runtime
    _bind_postgres_agent_task_runtime(connect, monkeypatch)
    client = TestClient(main.app)
    task, _approval_id = _create_postgres_task_with_pending_approval(client, project)
    task_id = task["id"]

    def mark_task_failed() -> None:
        # Seed the worker's terminal task state directly; this test focuses on
        # route retry-history reads and the repository's existing retry CAS.
        with connect() as conn:
            conn.execute(
                "UPDATE agent_tasks SET status=%s, payload=jsonb_set(payload, '{status}', %s::jsonb) "
                "WHERE task_id=%s AND workspace_id=%s",
                (AgentTaskStatus.FAILED.value, '"FAILED"', task_id, workspace.id),
            )

    before_task_ids = set(store.agent_tasks)
    before_event_ids = set(store.agent_task_events)
    before_audit_ids = {event.id for event in store.audit_events}

    mark_task_failed()
    first_retry = client.post(
        f"/agent/tasks/{task_id}/retry",
        json={"reason": "first durable retry", "max_retries": 3},
    )
    assert first_retry.status_code == 200, first_retry.text
    assert first_retry.json()["metadata"]["previous_retry_count"] == 0
    assert first_retry.json()["metadata"]["retry_count"] == 1

    mark_task_failed()
    summary = client.get(f"/agent/tasks/{task_id}/retry", params={"max_retries": 3})
    assert summary.status_code == 200, summary.text
    assert summary.json()["retry_count"] == 1
    assert summary.json()["retry_allowed"] is True

    second_retry = client.post(
        f"/agent/tasks/{task_id}/retry",
        json={"reason": "second durable retry", "max_retries": 3},
    )
    assert second_retry.status_code == 200, second_retry.text
    assert second_retry.json()["metadata"]["previous_retry_count"] == 1
    assert second_retry.json()["metadata"]["retry_count"] == 2

    mark_task_failed()
    limited_summary = client.get(
        f"/agent/tasks/{task_id}/retry", params={"max_retries": 2}
    )
    assert limited_summary.status_code == 200, limited_summary.text
    assert limited_summary.json()["retry_count"] == 2
    assert limited_summary.json()["retry_allowed"] is False

    with connect() as conn:
        audit_count_before_denial = conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_id=%s "
            "AND event_type='agent_task.retry_requested'",
            (workspace.id, task_id),
        ).fetchone()[0]
    denied = client.post(
        f"/agent/tasks/{task_id}/retry",
        json={"reason": "must honor durable limit", "max_retries": 2},
    )
    assert denied.status_code == 409
    assert denied.json()["detail"] == "Retry limit reached: 2 >= 2."

    task_repository = main.app.state.agent_task_repository
    assert task_repository.count_retries(task_id) == 2
    assert task_repository.get(task_id).status == AgentTaskStatus.FAILED
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_id=%s "
            "AND event_type='agent_task.retry_requested'",
            (workspace.id, task_id),
        ).fetchone()[0] == audit_count_before_denial
    assert set(store.agent_tasks) == before_task_ids
    assert set(store.agent_task_events) == before_event_ids
    assert {event.id for event in store.audit_events} == before_audit_ids


def test_postgres_cancel_route_rolls_back_all_records_when_notification_insert_fails(
    postgres_agent_task_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project = postgres_agent_task_route_runtime
    _bind_postgres_agent_task_runtime(connect, monkeypatch)
    client = TestClient(main.app, raise_server_exceptions=False)
    task, approval_id = _create_postgres_task_with_pending_approval(client, project)
    with connect() as conn:
        notifications_before = conn.execute(
            "SELECT count(*) FROM notifications WHERE approval_id=%s", (approval_id,)
        ).fetchone()[0]
        conn.execute("""
            CREATE FUNCTION reject_cancel_notification() RETURNS trigger AS $$
            BEGIN
                IF NEW.payload->>'body' LIKE '%linked agent task was cancelled%' THEN
                    RAISE EXCEPTION 'injected notification failure';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        conn.execute("""
            CREATE TRIGGER reject_cancel_notification_insert
            BEFORE INSERT ON notifications FOR EACH ROW
            EXECUTE FUNCTION reject_cancel_notification()
        """)

    response = client.post(f"/agent/tasks/{task['id']}/cancel", json={"reason": "rollback test"})

    assert response.status_code == 500
    with connect() as conn:
        assert conn.execute(
            "SELECT status FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
            (task["id"], workspace.id),
        ).fetchone() == ("WAITING_FOR_APPROVAL",)
        assert conn.execute(
            "SELECT status FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
            (approval_id, workspace.id),
        ).fetchone() in (("REQUESTED",), ("PENDING_REVIEW",))
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s AND event_type='CANCELLED'",
            (task["id"],),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id IN (%s,%s) "
            "AND event_type IN ('agent_task.cancelled','approval.cancelled')",
            (task["id"], approval_id),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE approval_id=%s",
            (approval_id,),
        ).fetchone()[0] == notifications_before


def test_postgres_cancel_route_refuses_task_with_active_run_without_projection(
    postgres_agent_task_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project = postgres_agent_task_route_runtime
    _bind_postgres_agent_task_runtime(connect, monkeypatch)
    client = TestClient(main.app)
    task, approval_id = _create_postgres_task_with_pending_approval(client, project)
    decision = client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"})
    assert decision.status_code == 200, decision.text
    task_repository = main.app.state.agent_task_repository
    run_repository = main.app.state.agent_run_repository
    run = run_repository.get_active_for_task(task["id"])
    assert run is not None
    before_task = task_repository.get(task["id"])
    before_event_ids = {event.id for event in task_repository.list_events(task["id"])}
    before_audit_ids = {event.id for event in store.audit_events}

    response = client.post(f"/agent/tasks/{task['id']}/cancel", json={"reason": "must refuse active run"})

    assert response.status_code == 409
    assert "Cancel or reconcile the run first" in response.json()["detail"]
    assert task_repository.get(task["id"]) == before_task
    assert {event.id for event in task_repository.list_events(task["id"])} == before_event_ids
    assert run_repository.get_active_for_task(task["id"]) == run
    with connect() as conn:
        assert conn.execute(
            "SELECT status FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
            (task["id"], workspace.id),
        ).fetchone() == ("APPROVED",)
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_type='agent_task' "
            "AND entity_id=%s AND event_type='agent_task.cancelled'",
            (workspace.id, task["id"]),
        ).fetchone()[0] == 0
    assert {event.id for event in store.audit_events} == before_audit_ids


def test_postgres_agent_task_event_route_commits_event_audit_and_notification_atomically(
    postgres_agent_task_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project = postgres_agent_task_route_runtime
    _bind_postgres_agent_task_runtime(connect, monkeypatch)
    before_task_ids = set(store.agent_tasks)
    before_event_ids = set(store.agent_task_events)
    before_audit_ids = {event.id for event in store.audit_events}
    before_notification_ids = set(store.notifications)
    client = TestClient(main.app)

    task_response = client.post("/agent/tasks", json={
        "provider": "MANUAL", "project_id": project.id,
        "title": "PostgreSQL event result", "goal": "Verify atomic event persistence",
        "requested_action": "OTHER", "requires_approval": False,
    })
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()

    response = client.post(
        f"/agent/tasks/{task['id']}/events",
        json={
            "event_type": "RESULT_READY",
            "message": "The result is ready for review.",
            "status": "DONE",
        },
    )

    assert response.status_code == 200, response.text
    event = response.json()
    notification_id = event["metadata"]["notification_id"]
    with connect() as conn:
        task_row = conn.execute(
            "SELECT status,payload FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
            (task["id"], workspace.id),
        ).fetchone()
        event_row = conn.execute(
            "SELECT task_id,workspace_id,event_type,payload FROM agent_task_events WHERE event_id=%s",
            (event["id"],),
        ).fetchone()
        audit_row = conn.execute(
            "SELECT workspace_id,event_type,details,payload FROM audit_events "
            "WHERE entity_type='agent_task' AND entity_id=%s AND event_type='agent_task.event_recorded' "
            "AND details->>'event_id'=%s",
            (task["id"], event["id"]),
        ).fetchone()
        notification_row = conn.execute(
            "SELECT workspace_id,user_id,task_id,entity_type,payload FROM notifications "
            "WHERE notification_id=%s",
            (notification_id,),
        ).fetchone()

    assert task_row is not None
    assert task_row[0] == "DONE"
    assert task_row[1]["status"] == "DONE"
    assert event_row is not None
    assert event_row[:3] == (task["id"], workspace.id, "RESULT_READY")
    assert event_row[3]["metadata"]["notification_id"] == notification_id
    assert audit_row is not None
    assert audit_row[0:2] == (workspace.id, "agent_task.event_recorded")
    assert audit_row[2]["event_id"] == event["id"]
    assert audit_row[2]["notification_id"] == notification_id
    assert audit_row[3]["details"]["event_id"] == event["id"]
    assert notification_row is not None
    assert notification_row[:4] == (
        workspace.id,
        task["created_by_user_id"],
        task["id"],
        "agent_task",
    )
    assert notification_row[4]["id"] == notification_id
    assert notification_row[4]["entity_id"] == task["id"]
    assert notification_row[4]["title"] == "Agent task done: PostgreSQL event result"
    assert set(store.agent_tasks) == before_task_ids
    assert set(store.agent_task_events) == before_event_ids
    assert {audit.id for audit in store.audit_events} == before_audit_ids
    assert set(store.notifications) == before_notification_ids


def test_postgres_agent_task_event_route_rolls_back_all_records_when_notification_insert_fails(
    postgres_agent_task_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project = postgres_agent_task_route_runtime
    _bind_postgres_agent_task_runtime(connect, monkeypatch)
    before_task_ids = set(store.agent_tasks)
    before_event_ids = set(store.agent_task_events)
    before_audit_ids = {event.id for event in store.audit_events}
    before_notification_ids = set(store.notifications)
    client = TestClient(main.app, raise_server_exceptions=False)

    task_response = client.post("/agent/tasks", json={
        "provider": "MANUAL", "project_id": project.id,
        "title": "PostgreSQL rollback event", "goal": "Verify event transaction rollback",
        "requested_action": "OTHER", "requires_approval": False,
    })
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()
    with connect() as conn:
        initial_task_payload = conn.execute(
            "SELECT status,payload FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
            (task["id"], workspace.id),
        ).fetchone()
        initial_event_count = conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s",
            (task["id"],),
        ).fetchone()[0]
        initial_audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_type='agent_task' "
            "AND entity_id=%s",
            (workspace.id, task["id"]),
        ).fetchone()[0]
        initial_notification_count = conn.execute(
            "SELECT count(*) FROM notifications WHERE workspace_id=%s AND task_id=%s",
            (workspace.id, task["id"]),
        ).fetchone()[0]
        conn.execute("""
            CREATE FUNCTION reject_agent_task_done_notification() RETURNS trigger AS $$
            BEGIN
                IF NEW.entity_type = 'agent_task'
                   AND NEW.payload->>'title' LIKE 'Agent task done: %' THEN
                    RAISE EXCEPTION 'injected AgentTask notification failure';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        conn.execute("""
            CREATE TRIGGER reject_agent_task_done_notification_insert
            BEFORE INSERT ON notifications FOR EACH ROW
            EXECUTE FUNCTION reject_agent_task_done_notification()
        """)

    response = client.post(
        f"/agent/tasks/{task['id']}/events",
        json={
            "event_type": "RESULT_READY",
            "message": "This transaction must roll back.",
            "status": "DONE",
        },
    )

    assert response.status_code == 500
    with connect() as conn:
        assert conn.execute(
            "SELECT status,payload FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
            (task["id"], workspace.id),
        ).fetchone() == initial_task_payload
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s",
            (task["id"],),
        ).fetchone()[0] == initial_event_count
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_type='agent_task' "
            "AND entity_id=%s",
            (workspace.id, task["id"]),
        ).fetchone()[0] == initial_audit_count
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE workspace_id=%s AND task_id=%s",
            (workspace.id, task["id"]),
        ).fetchone()[0] == initial_notification_count
    assert set(store.agent_tasks) == before_task_ids
    assert set(store.agent_task_events) == before_event_ids
    assert {audit.id for audit in store.audit_events} == before_audit_ids
    assert set(store.notifications) == before_notification_ids


def test_postgres_agent_task_event_route_uses_lifespan_runtime_and_survives_restart(
    postgres_agent_task_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project = postgres_agent_task_route_runtime
    settings = replace(
        get_settings(),
        profile="test",
        auth_enabled=True,
        postgres_auth_enabled=True,
        postgres_runtime_probe_enabled=False,
        postgres_auth_rate_limits_enabled=False,
        outbox_dispatch_mode="scheduled",
        push_dispatch_enabled=False,
        email_dispatch_enabled=False,
    )
    runtimes: list[PostgresRuntime] = []

    def create_runtime() -> PostgresRuntime:
        runtime = PostgresRuntime(
            _dsn="isolated-test-schema",
            profile="test",
            settings=settings,
            _connector=lambda *_args, **_kwargs: connect(),
        )
        runtimes.append(runtime)
        return runtime

    # The base route fixture prepares the isolated schema, user overrides, and
    # local repository bundle. Clear only the temporary bindings it installed
    # so the application's real lifespan can supply the runtime repositories.
    for state_name in main._POSTGRES_APP_STATE_BINDINGS:
        monkeypatch.setattr(main.app.state, state_name, None, raising=False)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main.PostgresRuntime, "from_environment", create_runtime)
    before_tasks = set(store.agent_tasks)
    before_events = set(store.agent_task_events)
    before_audits = {event.id for event in store.audit_events}
    before_notifications = set(store.notifications)

    with TestClient(main.app) as client:
        runtime = client.app.state.postgres_runtime
        repositories = runtime.repositories
        assert client.app.state.postgres_repositories is repositories
        assert client.app.state.agent_task_repository is repositories.agent_tasks
        assert client.app.state.audit_event_repository is repositories.audit_events

        created = client.post(
            "/agent/tasks",
            json={
                "provider": "MANUAL",
                "project_id": project.id,
                "title": "Lifespan-bound PostgreSQL event",
                "goal": "Persist task events through the application-owned runtime",
                "requested_action": "OTHER",
                "requires_approval": False,
            },
        )
        assert created.status_code == 200, created.text
        task = created.json()
        response = client.post(
            f"/agent/tasks/{task['id']}/events",
            json={
                "event_type": "RESULT_READY",
                "message": "Lifespan-bound result is ready.",
                "status": "DONE",
            },
        )
        assert response.status_code == 200, response.text
        event = response.json()
        event_id = event["id"]

    assert len(runtimes) == 1
    assert main.app.state.postgres_runtime is None
    assert main.app.state.postgres_repositories is None
    with TestClient(main.app) as restarted_client:
        restarted_runtime = restarted_client.app.state.postgres_runtime
        assert restarted_runtime is not runtimes[0]
        assert restarted_client.app.state.agent_task_repository is (
            restarted_runtime.repositories.agent_tasks
        )
        durable_task = restarted_client.get(f"/agent/tasks/{task['id']}")
        durable_events = restarted_client.get(f"/agent/tasks/{task['id']}/events")
        assert durable_task.status_code == 200, durable_task.text
        assert durable_task.json()["status"] == "DONE"
        assert durable_events.status_code == 200, durable_events.text
        persisted_events = durable_events.json()
        assert len(persisted_events) == 2
        assert persisted_events[0]["event_type"] == "TASK_CREATED"
        assert persisted_events[1]["id"] == event_id

    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_type='agent_task' "
            "AND entity_id=%s AND event_type='agent_task.event_recorded' AND details->>'event_id'=%s",
            (workspace.id, task["id"], event_id),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE workspace_id=%s AND task_id=%s AND entity_type='agent_task'",
            (workspace.id, task["id"]),
        ).fetchone()[0] == 1
    assert set(store.agent_tasks) == before_tasks
    assert set(store.agent_task_events) == before_events
    assert {event.id for event in store.audit_events} == before_audits
    assert set(store.notifications) == before_notifications

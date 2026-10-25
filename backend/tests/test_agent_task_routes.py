from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import agent_task_routes
from app.agent_task_models import (
    AgentProvider,
    AgentTask,
    AgentTaskEventType,
    AgentTaskRequestedAction,
    AgentTaskStatus,
)
from app.main import app
from app.models import ApprovalRequestCreate, AuthUser, Project, UserRole, WorkOrder
from app.store import store
from app.work_order_repository import PostgresWorkOrderRepository

client = TestClient(app)


def setup_function() -> None:
    store.reset()


@pytest.mark.parametrize(
    "settings",
    [
        SimpleNamespace(is_production=True, postgres_auth_enabled=False),
        SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    ],
)
def test_missing_request_fails_closed_before_returning_sqlite_task_repository(
    monkeypatch: pytest.MonkeyPatch, settings: SimpleNamespace
) -> None:
    before = (
        dict(store.agent_tasks),
        dict(store.agent_task_events),
        dict(store.approval_requests),
        list(store.audit_events),
    )
    monkeypatch.setattr(agent_task_routes, "get_settings", lambda: settings)

    with pytest.raises(HTTPException) as raised:
        agent_task_routes._task_repository_for_route(None)

    assert raised.value.status_code == 503
    assert raised.value.headers == {"Retry-After": "1"}
    assert store.agent_tasks == before[0]
    assert store.agent_task_events == before[1]
    assert store.approval_requests == before[2]
    assert store.audit_events == before[3]


def test_request_bound_task_repository_failure_is_retryable_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    def reject_missing_runtime(_request: object) -> object:
        raise RuntimeError("PostgreSQL AgentTask repository is unavailable or mismatched")

    monkeypatch.setattr(agent_task_routes, "agent_task_repository_for_request", reject_missing_runtime)

    with pytest.raises(HTTPException) as raised:
        agent_task_routes._task_repository_for_route(request)

    assert raised.value.status_code == 503
    assert raised.value.headers == {"Retry-After": "1"}
    assert raised.value.detail == "AgentTask repository is unavailable"


def test_request_bound_task_repository_rejects_unsupported_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    monkeypatch.setattr(
        agent_task_routes, "agent_task_repository_for_request", lambda _request: object()
    )

    with pytest.raises(HTTPException) as raised:
        agent_task_routes._task_repository_for_route(request)

    assert raised.value.status_code == 503
    assert raised.value.detail == "AgentTask repository is unsupported"


def test_request_bound_local_task_repository_remains_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = agent_task_routes.agent_task_repository
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent_task_repository=repository))
    )
    monkeypatch.setattr(
        "app.application_repository_access.get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=False),
    )

    assert agent_task_routes._task_repository_for_route(request) is repository


def test_system_task_event_rejects_process_local_write_in_postgres_mode_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = AgentTask(
        provider=AgentProvider.CODEX,
        title="Persist worker transition",
        goal="Keep task state durable",
        requested_action=AgentTaskRequestedAction.OTHER,
        status=AgentTaskStatus.RECEIVED,
    )
    before_events = dict(store.agent_task_events)
    monkeypatch.setattr(
        agent_task_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    )

    with pytest.raises(RuntimeError, match="Process-local AgentTask event writes"):
        agent_task_routes.append_system_task_event(
            task,
            AgentTaskEventType.EXECUTION_STARTED,
            "worker started",
            status=AgentTaskStatus.EXECUTING,
        )

    assert task.status == AgentTaskStatus.RECEIVED
    assert store.agent_task_events == before_events


def test_system_task_event_keeps_local_mode_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = AgentTask(
        provider=AgentProvider.CODEX,
        title="Persist worker transition",
        goal="Keep task state durable",
        requested_action=AgentTaskRequestedAction.OTHER,
        status=AgentTaskStatus.RECEIVED,
    )
    monkeypatch.setattr(
        agent_task_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )

    event = agent_task_routes.append_system_task_event(
        task,
        AgentTaskEventType.EXECUTION_STARTED,
        "worker started",
        status=AgentTaskStatus.EXECUTING,
    )

    assert task.status == AgentTaskStatus.EXECUTING
    assert store.agent_task_events[event.id] is event


@pytest.mark.parametrize(
    "settings",
    [
        SimpleNamespace(is_production=True, postgres_auth_enabled=False),
        SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    ],
)
@pytest.mark.parametrize(
    "helper_name",
    [
        "_approval_repository_for_route",
        "_project_repository_for_route",
        "_work_order_repository_for_route",
    ],
)
def test_missing_request_fails_closed_for_all_sqlite_repository_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    settings: SimpleNamespace,
    helper_name: str,
) -> None:
    class _MustNotBeAccessed:
        def __getattr__(self, _name: str):
            raise AssertionError("SQLite compatibility repository accessed in PostgreSQL mode")

    before = (
        dict(store.agent_tasks),
        dict(store.agent_task_events),
        dict(store.approval_requests),
        list(store.audit_events),
    )
    monkeypatch.setattr(agent_task_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_task_routes, "approval_decision_repository", _MustNotBeAccessed())
    monkeypatch.setattr(agent_task_routes, "project_repository", _MustNotBeAccessed())
    monkeypatch.setattr(agent_task_routes, "store", _MustNotBeAccessed())

    with pytest.raises(HTTPException) as raised:
        getattr(agent_task_routes, helper_name)(None)

    assert raised.value.status_code == 503
    assert raised.value.headers == {"Retry-After": "1"}
    assert store.agent_tasks == before[0]
    assert store.agent_task_events == before[1]
    assert store.approval_requests == before[2]
    assert store.audit_events == before[3]


@pytest.mark.parametrize(
    ("helper_name", "fallback_name"),
    [
        ("_approval_repository_for_route", "approval_decision_repository"),
        ("_project_repository_for_route", "project_repository"),
        ("_work_order_repository_for_route", "work_order_repository"),
    ],
)
def test_missing_request_keeps_local_repository_fallback_outside_production(
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
    fallback_name: str,
) -> None:
    monkeypatch.setattr(
        agent_task_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )
    expected = (
        store.work_order_repository
        if fallback_name == "work_order_repository"
        else getattr(agent_task_routes, fallback_name)
    )

    assert getattr(agent_task_routes, helper_name)(None) is expected


def test_create_agent_task_records_task_event_and_audit() -> None:
    response = client.post(
        "/agent/tasks",
        json={
            "provider": "CHATGPT",
            "title": "Prepare RTI app MVP scope",
            "goal": "Create product scope, backend plan, Android flow, risks, and demo checklist.",
            "requested_action": "GENERATE_DOCS",
            "requires_approval": True,
            "metadata": {"source": "test"},
        },
    )

    assert response.status_code == 200
    task = response.json()
    assert task["provider"] == "CHATGPT"
    assert task["status"] == "RECEIVED"
    assert task["requires_approval"] is True
    assert task["id"] in store.agent_tasks
    assert len(store.agent_task_events) == 1
    assert store.agent_task_events[next(iter(store.agent_task_events))].event_type == "TASK_CREATED"
    assert any(event.event_type == "agent_task.created" for event in store.audit_events)


def test_list_get_and_filter_agent_tasks() -> None:
    first = client.post(
        "/agent/tasks",
        json={"provider": "CHATGPT", "title": "Docs", "goal": "Generate docs", "requested_action": "GENERATE_DOCS"},
    ).json()
    client.post(
        "/agent/tasks",
        json={"provider": "CLAUDE", "title": "Review", "goal": "Review architecture", "requested_action": "REVIEW_ARCHITECTURE"},
    )

    by_provider = client.get("/agent/tasks?provider=CHATGPT")
    detail = client.get(f"/agent/tasks/{first['id']}")

    assert by_provider.status_code == 200
    assert len(by_provider.json()) == 1
    assert by_provider.json()[0]["provider"] == "CHATGPT"
    assert detail.status_code == 200
    assert detail.json()["id"] == first["id"]


def test_append_agent_task_event_updates_status_and_timeline() -> None:
    task = client.post(
        "/agent/tasks",
        json={"provider": "CODEX", "title": "Fix tests", "goal": "Fix failing tests", "requested_action": "CREATE_APPROVAL"},
    ).json()

    response = client.post(
        f"/agent/tasks/{task['id']}/events",
        json={"event_type": "TESTS_STARTED", "message": "Running backend tests", "status": "TESTING"},
    )
    events = client.get(f"/agent/tasks/{task['id']}/events")
    updated = client.get(f"/agent/tasks/{task['id']}")

    assert response.status_code == 200
    assert response.json()["event_type"] == "TESTS_STARTED"
    assert updated.json()["status"] == "TESTING"
    assert len(events.json()) == 2
    assert any(event.event_type == "agent_task.event_recorded" for event in store.audit_events)


def test_append_agent_task_worker_done_event_creates_notification() -> None:
    task = client.post(
        "/agent/tasks",
        json={"provider": "CODEX", "title": "Worker result", "goal": "Report worker completion", "requested_action": "OTHER"},
    ).json()

    response = client.post(
        f"/agent/tasks/{task['id']}/events",
        json={"event_type": "RESULT_READY", "message": "Dry-run result ready", "status": "DONE"},
    )

    assert response.status_code == 200
    event = response.json()
    notification_id = event["metadata"]["notification_id"]
    notification = store.notifications[notification_id]
    assert notification.entity_type == "agent_task"
    assert notification.entity_id == task["id"]
    assert notification.title == "Agent task done: Worker result"
    assert notification.user_id == task["created_by_user_id"]
    assert any(audit_event.details.get("notification_id") == notification_id for audit_event in store.audit_events)


def test_append_agent_task_testing_event_does_not_create_notification() -> None:
    task = client.post(
        "/agent/tasks",
        json={"provider": "CODEX", "title": "Test only", "goal": "Report tests", "requested_action": "OTHER"},
    ).json()

    response = client.post(
        f"/agent/tasks/{task['id']}/events",
        json={"event_type": "TESTS_STARTED", "message": "Running backend tests", "status": "TESTING"},
    )

    assert response.status_code == 200
    assert "notification_id" not in response.json()["metadata"]
    assert store.notifications == {}


def test_create_agent_task_rejects_missing_project() -> None:
    response = client.post(
        "/agent/tasks",
        json={"project_id": "project_missing", "title": "Bad", "goal": "Missing project"},
    )

    assert response.status_code == 404


def test_create_agent_task_accepts_existing_project() -> None:
    project = Project(name="Connected Agent Demo", description="demo")
    store.projects[project.id] = project
    store.persist()

    response = client.post(
        "/agent/tasks",
        json={"project_id": project.id, "provider": "CURSOR", "title": "UI polish", "goal": "Polish Agent Tasks screen"},
    )

    assert response.status_code == 200
    assert response.json()["project_id"] == project.id


def test_create_agent_task_resolves_project_after_compatibility_cache_clear() -> None:
    project = Project(name="Durable Project Lookup", description="persisted project")
    store.projects[project.id] = project
    store.persist()
    store.projects.clear()

    response = client.post(
        "/agent/tasks",
        json={"project_id": project.id, "provider": "CURSOR", "title": "Task", "goal": "Use durable project"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == project.id


def test_postgres_task_approval_rejects_missing_work_order_workspace_without_sqlite_fallback(
    monkeypatch,
) -> None:
    project = Project(id="project_pg_scope", name="PG project", workspace_id="workspace_pg_scope")
    work_order = WorkOrder(
        id="work_order_pg_scope",
        project_id=project.id,
        workspace_id=None,
        goal="Legacy or malformed row must fail closed",
    )
    task = AgentTask(
        provider=AgentProvider.ANTIGRAVITY,
        project_id=project.id,
        title="Task",
        goal="Create approval",
        requested_action=AgentTaskRequestedAction.CREATE_APPROVAL,
        workspace_id=project.workspace_id,
    )
    actor = AuthUser(
        id="owner_pg_scope",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        workspace_id=project.workspace_id,
    )
    work_orders = PostgresWorkOrderRepository(lambda: None)  # type: ignore[arg-type]
    work_orders.get = lambda _work_order_id: work_order  # type: ignore[method-assign]

    class _ApprovalRepository:
        def create_for_agent_task(self, **_kwargs):
            raise AssertionError("invalid PostgreSQL WorkOrder must be rejected before approval commit")

    class _Projects:
        def get(self, _project_id: str):
            return project

    monkeypatch.setattr(agent_task_routes, "_task_repository_for_route", lambda _request: object())
    monkeypatch.setattr(agent_task_routes, "_approval_repository_for_route", lambda _request: _ApprovalRepository())
    monkeypatch.setattr(agent_task_routes, "_project_repository_for_route", lambda _request: _Projects())
    monkeypatch.setattr(agent_task_routes, "work_order_repository_for_request", lambda _request: work_orders)
    monkeypatch.setattr(agent_task_routes, "workspace_id_for_request", lambda _user, _request: project.workspace_id)
    monkeypatch.setattr(agent_task_routes, "_task_for_user", lambda *_args, **_kwargs: task)
    monkeypatch.setattr(
        agent_task_routes,
        "project_workspace_id",
        lambda _project_id: (_ for _ in ()).throw(
            AssertionError("PostgreSQL scope must not read the SQLite compatibility project store")
        ),
    )

    from fastapi import HTTPException

    try:
        agent_task_routes.create_agent_task_approval(
            task.id,
            ApprovalRequestCreate(
                project_id=project.id,
                work_order_id=work_order.id,
                title="Approve task",
                summary="Validate scoped persistence",
                target_branch="main",
                files=[],
            ),
            user=actor,
            http_request=object(),  # type: ignore[arg-type]
        )
    except HTTPException as error:
        assert error.status_code == 404
    else:
        raise AssertionError("PostgreSQL WorkOrder without a workspace must be rejected")

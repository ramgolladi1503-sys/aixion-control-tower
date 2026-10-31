from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.agent_run_models import (
    AgentRun,
    AgentRunFaultConfig,
    AgentRunFaultCreate,
    AgentRunFaultKind,
    AgentRunStepType,
)
from app import agent_run_routes
from app.agent_task_models import AgentTask
from app.main import app
from app.models import AuthUser, Project, UserRole, Workspace
from app.postgres_agent_run_fault_repository import PostgresAgentRunFaultRepository
from app.postgres_database import run_postgres_migrations
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


@pytest.fixture
def postgres_fault_repository(
) -> Iterator[tuple[Callable[..., Connection], PostgresAgentRunFaultRepository, str, str]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL fault rollback tests")

    schema = f"ct_agent_run_fault_rollback_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect(**kwargs: object) -> Connection:
        connection = psycopg.connect(dsn, **kwargs)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    workspace = Workspace(name="Fault rollback workspace", owner_user_id="owner-fault-test")
    project = Project(name="Fault rollback project", workspace_id=workspace.id)
    task = AgentTask(
        title="Fault rollback task",
        goal="Provide a valid parent row for PostgreSQL fault controls.",
        project_id=project.id,
        workspace_id=workspace.id,
    )
    run = AgentRun(task_id=task.id, project_id=project.id, workspace_id=workspace.id)
    try:
        run_postgres_migrations(connect)
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
                "INSERT INTO agent_tasks(task_id,project_id,workspace_id,external_agent_id,"
                "approval_request_id,status,updated_at,payload) "
                "VALUES(%s,%s,%s,NULL,NULL,%s,%s,%s)",
                (
                    task.id,
                    project.id,
                    workspace.id,
                    task.status.value,
                    task.updated_at,
                    Jsonb(task.model_dump(mode="json")),
                ),
            )
            conn.execute(
                "INSERT INTO agent_runs(run_id,task_id,project_id,workspace_id,approval_id,"
                "status,current_step_id,lease_owner,lease_token,lease_expires_at,updated_at,payload) "
                "VALUES(%s,%s,%s,%s,NULL,%s,NULL,NULL,NULL,NULL,%s,%s)",
                (
                    run.id,
                    task.id,
                    project.id,
                    workspace.id,
                    run.status.value,
                    run.updated_at,
                    Jsonb(run.model_dump(mode="json")),
                ),
            )

        yield connect, PostgresAgentRunFaultRepository(connect), workspace.id, run.id
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _fault(run_id: str, *, uses: int = 2) -> AgentRunFaultConfig:
    return AgentRunFaultConfig(
        id=f"fault_{uuid.uuid4().hex}",
        run_id=run_id,
        step_type=AgentRunStepType.CREATE_BRANCH,
        fault_kind=AgentRunFaultKind.GITHUB_TIMEOUT,
        remaining_uses=uses,
        created_by="owner-fault-test",
    )


def _reject_audit_event(connect, event_type: str) -> None:
    with connect() as conn:
        conn.execute(
            sql.SQL("ALTER TABLE audit_events ADD CONSTRAINT reject_target_fault_audit "
                    "CHECK (event_type <> {})").format(sql.Literal(event_type))
        )


def _fault_state(connect, fault_id: str) -> tuple[bool, int] | None:
    with connect() as conn:
        return conn.execute(
            "SELECT enabled,remaining_uses FROM agent_run_faults WHERE fault_id=%s",
            (fault_id,),
        ).fetchone()


def _fault_audit_count(connect, fault_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type='agent_run_fault' "
            "AND entity_id=%s",
            (fault_id,),
        ).fetchone()[0]


@pytest.mark.parametrize("selected_state", ["postgres_runtime", "postgres_repositories"])
def test_fault_admin_routes_fail_closed_when_postgres_runtime_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    selected_state: str,
) -> None:
    fault = AgentRunFaultConfig(
        id="agent_run_fault_postgres_guard_test",
        run_id="local-run-must-not-be-read",
        fault_kind=AgentRunFaultKind.GITHUB_TIMEOUT,
        enabled=True,
        remaining_uses=1,
    )
    store.agent_run_faults[fault.id] = fault
    before_faults = {
        fault_id: value.model_copy(deep=True)
        for fault_id, value in store.agent_run_faults.items()
    }
    before_audit_events = list(store.audit_events)
    persist_calls: list[bool] = []
    monkeypatch.setattr(store, "persist", lambda: persist_calls.append(True))
    monkeypatch.setattr(app.state, selected_state, object(), raising=False)
    postgres_settings = SimpleNamespace(is_production=False, postgres_auth_enabled=True)
    monkeypatch.setattr(
        "app.agent_run_routes.get_settings",
        lambda: postgres_settings,
    )
    monkeypatch.setattr(
        "app.application_repository_access.get_settings",
        lambda: postgres_settings,
    )

    responses = [
        client.get("/agent/runs/faults"),
        client.post(
            "/agent/runs/faults",
            json={
                "run_id": "postgres-run-not-in-local-store",
                "fault_kind": "GITHUB_TIMEOUT",
                "remaining_uses": 1,
            },
        ),
        client.delete(f"/agent/runs/faults/{fault.id}"),
    ]

    for response in responses:
        assert response.status_code == 503
        assert "repository is unavailable" in response.json()["detail"]
    assert store.agent_run_faults == before_faults
    assert store.audit_events == before_audit_events
    assert persist_calls == []


def test_fault_admin_direct_calls_fail_closed_without_app_bound_postgres_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = AuthUser(
        id="owner-postgres-fault-direct",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        workspace_id="workspace-postgres-fault-direct",
    )
    payload = AgentRunFaultCreate(
        run_id="must-not-read-local-run",
        fault_kind=AgentRunFaultKind.GITHUB_TIMEOUT,
        remaining_uses=1,
    )
    fault = AgentRunFaultConfig(
        id="must-not-read-local-fault",
        run_id="must-not-read-local-run",
        fault_kind=AgentRunFaultKind.GITHUB_TIMEOUT,
        enabled=True,
        remaining_uses=1,
    )
    store.agent_run_faults[fault.id] = fault
    before = {
        fault_id: value.model_copy(deep=True)
        for fault_id, value in store.agent_run_faults.items()
    }
    persist_calls: list[bool] = []
    monkeypatch.setattr(store, "persist", lambda: persist_calls.append(True))
    monkeypatch.setattr(
        agent_run_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    )
    monkeypatch.setattr(
        "app.application_repository_access.get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    )

    direct_calls = (
        lambda: agent_run_routes.list_faults(user=actor),
        lambda: agent_run_routes.create_fault(payload=payload, user=actor),
        lambda: agent_run_routes.disable_fault(fault_id=fault.id, user=actor),
    )
    for call in direct_calls:
        with pytest.raises(HTTPException) as error:
            call()
        assert error.value.status_code == 503
        assert "repository is unavailable" in str(error.value.detail)

    assert store.agent_run_faults == before
    assert store.audit_events == []
    assert persist_calls == []


def test_probe_only_postgres_runtime_keeps_sqlite_fault_controls_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_run_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )
    monkeypatch.setattr(app.state, "postgres_runtime", object(), raising=False)

    assert agent_run_routes.list_faults(
        user=AuthUser(
            id="owner-probe-only",
            email="owner@example.test",
            display_name="Owner",
            role=UserRole.OWNER,
            workspace_id="workspace-probe-only",
        )
    ) == []


def test_postgres_fault_create_rolls_back_when_audit_insert_fails(
    postgres_fault_repository,
) -> None:
    connect, repository, workspace_id, run_id = postgres_fault_repository
    fault = _fault(run_id)
    _reject_audit_event(connect, "agent_run.fault_configured")

    with pytest.raises(psycopg.errors.CheckViolation):
        repository.create(fault, workspace_id=workspace_id)

    assert _fault_state(connect, fault.id) is None
    assert _fault_audit_count(connect, fault.id) == 0


def test_postgres_fault_disable_rolls_back_when_audit_insert_fails(
    postgres_fault_repository,
) -> None:
    connect, repository, workspace_id, run_id = postgres_fault_repository
    fault = _fault(run_id)
    assert repository.create(fault, workspace_id=workspace_id) == fault
    assert _fault_audit_count(connect, fault.id) == 1
    _reject_audit_event(connect, "agent_run.fault_disabled")

    with pytest.raises(psycopg.errors.CheckViolation):
        repository.disable(fault.id, workspace_id=workspace_id, actor="owner-fault-test")

    assert _fault_state(connect, fault.id) == (True, 2)
    assert _fault_audit_count(connect, fault.id) == 1


def test_postgres_fault_consume_rolls_back_when_audit_insert_fails(
    postgres_fault_repository,
) -> None:
    connect, repository, workspace_id, run_id = postgres_fault_repository
    fault = _fault(run_id)
    assert repository.create(fault, workspace_id=workspace_id) == fault
    assert _fault_audit_count(connect, fault.id) == 1
    _reject_audit_event(connect, "agent_run.fault_injected")

    with pytest.raises(psycopg.errors.CheckViolation):
        repository.consume(run_id, AgentRunStepType.CREATE_BRANCH)

    assert _fault_state(connect, fault.id) == (True, 2)
    assert _fault_audit_count(connect, fault.id) == 1

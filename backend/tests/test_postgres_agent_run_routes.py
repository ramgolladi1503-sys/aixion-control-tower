from __future__ import annotations

import os
import multiprocessing
import queue as queue_module
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import agent_run_metrics, main
from app.agent_run_models import (
    AgentRunEvent,
    AgentRunEventType,
    AgentRunStatus,
    AgentRunStepExecutionResult,
    AgentRunStepStatus,
    AgentRunStepType,
    VerificationDecision,
)
from app.agent_run_repository import AgentRunRepositoryConflict
from app.agent_run_supervisor import _build_postgres_scope_validation_records
from app.agent_run_verifier import canonical_evidence_hash
from app.agent_run_routes import MaintainerDependency, MetricsDependency, ReviewerDependency
from app.agent_task_models import (
    AgentTask,
    AgentTaskEvent,
    AgentTaskEventType,
    AgentTaskStatus,
)
from app.approval_repository import SQLiteApprovalDecisionRepository
from app.approval_integrity import compute_approval_payload_hash
from app.application_repositories import ApplicationRepositories
from app.auth import require_maintainer, require_owner, require_reviewer
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
    now_utc,
)
from app.postgres_database import run_postgres_migrations
from app.postgres_agent_run_apply_changes import (
    AgentRunApplyChangeIntent,
    ApplyChangeIntentStatus,
)
from app.store import store
from app.trust_crypto import sign_payload
from app.trust_models import (
    CapabilityActionType,
    ActionConsumption,
    CapabilityLease,
    CapabilityLeaseMode,
    CapabilityLeaseStatus,
    CapabilityScope,
    CredentialGrant,
    CredentialGrantType,
    TrustEventType,
    ProposedAction,
)
from app.postgres_trust_repository import PostgresTrustRepository, PostgresTrustRepositoryConflict
from app.trust_policy import lease_receipt_payload


def _claim_apply_change_intent_in_process(
    dsn: str,
    schema: str,
    intent_data: dict,
    run_data: dict,
    step_data: dict,
    worker_lease_token: str,
    barrier,
    results,
) -> None:
    """Race a durable APPLY_CHANGES claim from an independent DB process."""
    from app.agent_run_models import AgentRun, AgentRunStep
    from app.postgres_agent_run_apply_changes import (
        AgentRunApplyChangeIntent,
        PostgresAgentRunApplyChangeIntentRepository,
    )

    def process_connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    intent = AgentRunApplyChangeIntent.model_validate(intent_data)
    expected_run = AgentRun.model_validate(run_data)
    expected_step = AgentRunStep.model_validate(step_data)
    barrier.wait(timeout=20)
    try:
        claimed = PostgresAgentRunApplyChangeIntentRepository(process_connect).claim_write_once(
            intent=intent,
            expected_run=expected_run,
            expected_step=expected_step,
            worker_lease_token=worker_lease_token,
            now=now_utc(),
        )
        outcome = "claimed"
        error = None
        assert claimed.status == ApplyChangeIntentStatus.POST_CLAIMED
    except PostgresTrustRepositoryConflict as exc:
        outcome = "conflict"
        error = f"{type(exc).__name__}: {exc}"
    except BaseException as exc:
        outcome = "error"
        error = f"{type(exc).__name__}: {exc}"
    durable = PostgresAgentRunApplyChangeIntentRepository(process_connect).get_for_step(
        workspace_id=intent.workspace_id,
        run_id=intent.run_id,
        step_id=intent.step_id,
        idempotency_key=intent.idempotency_key,
    )
    results.put(
        (
            outcome,
            os.getpid(),
            durable.status.value if durable is not None else None,
            error,
        )
    )


@pytest.fixture
def postgres_agent_run_route_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Callable[[], Connection], Workspace, Project, Workspace, Project, dict]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL AgentRun route tests")

    schema = f"ct_agent_run_routes_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect(_runtime_dsn: str = dsn, **connect_kwargs: object) -> Connection:
        # PostgresRuntime receives a deliberately invalid placeholder DSN in
        # tests; this fixture must stay bound to its isolated schema database.
        connection = psycopg.connect(dsn, **connect_kwargs)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    workspaces = [
        Workspace(
            id=f"workspace_{uuid.uuid4().hex}",
            name=f"AgentRun workspace {index}",
            owner_user_id=f"user_{uuid.uuid4().hex}",
        )
        for index in (1, 2)
    ]
    projects = [
        Project(
            id=f"project_{uuid.uuid4().hex}",
            name=f"AgentRun project {index}",
            workspace_id=workspace.id,
        )
        for index, workspace in enumerate(workspaces, start=1)
    ]
    users = [
        AuthUser(
            id=workspace.owner_user_id,
            email=f"owner-{index}-{uuid.uuid4().hex}@example.test",
            display_name=f"Owner {index}",
            role=UserRole.OWNER,
            workspace_id=workspace.id,
            email_verified=True,
        )
        for index, workspace in enumerate(workspaces, start=1)
    ]
    current_user = {"value": users[0]}
    task_ids_by_workspace: dict[str, str] = {}

    try:
        run_postgres_migrations(connect)
        with connect() as conn:
            for workspace, project in zip(workspaces, projects, strict=True):
                conn.execute(
                    "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                    (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
                )
                conn.execute(
                    "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                    (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
                )

            for workspace, project in zip(workspaces, projects, strict=True):
                approval = ApprovalRequest(
                    project_id=project.id,
                    workspace_id=workspace.id,
                    title="Approved AgentRun fixture",
                    summary="Seed a valid approved task for AgentRun route coverage.",
                    target_branch="feature/postgres-agent-run-routes",
                    files=[FileChange(path="docs/run.md", diff="+approved\n")],
                    status=ApprovalStatus.APPROVED,
                    risk=RiskAssessment(level=RiskLevel.LOW),
                    approved_by_user_id=workspace.owner_user_id,
                )
                approval.approved_payload_hash = compute_approval_payload_hash(approval)
                conn.execute(
                    "INSERT INTO approval_requests "
                    "(approval_id,project_id,workspace_id,work_order_id,source_agent_id,status,"
                    "expires_at,created_at,updated_at,version,payload) "
                    "VALUES(%s,%s,%s,NULL,NULL,%s,%s,%s,%s,0,%s)",
                    (
                        approval.id,
                        project.id,
                        workspace.id,
                        approval.status.value,
                        approval.expires_at,
                        approval.created_at,
                        approval.updated_at,
                        Jsonb(approval.model_dump(mode="json")),
                    ),
                )

                task = AgentTask(
                    workspace_id=workspace.id,
                    project_id=project.id,
                    title="Approved AgentRun task",
                    goal="Exercise the durable AgentRun HTTP routes.",
                    approval_request_id=approval.id,
                    status=AgentTaskStatus.APPROVED,
                    created_by_user_id=workspace.owner_user_id,
                )
                conn.execute(
                    "INSERT INTO agent_tasks "
                    "(task_id,project_id,workspace_id,external_agent_id,approval_request_id,"
                    "status,updated_at,payload) "
                    "VALUES(%s,%s,%s,NULL,%s,%s,%s,%s)",
                    (
                        task.id,
                        project.id,
                        workspace.id,
                        approval.id,
                        task.status.value,
                        task.updated_at,
                        Jsonb(task.model_dump(mode="json")),
                    ),
                )
                task_ids_by_workspace[workspace.id] = task.id

        repositories = ApplicationRepositories.from_connector(connect)
        for name, value in {
            "approval_decision_repository": repositories.approval_decisions,
            "agent_task_repository": repositories.agent_tasks,
            "agent_run_repository": repositories.agent_runs,
            "agent_run_validation_intent_repository": repositories.agent_run_validation_intents,
            "workspace_membership_repository": repositories.workspace_membership,
        }.items():
            monkeypatch.setattr(main.app.state, name, value, raising=False)

        settings = SimpleNamespace(
            auth_enabled=False,
            is_production=False,
            postgres_auth_enabled=False,
        )
        monkeypatch.setattr("app.agent_run_routes.get_settings", lambda: settings)
        monkeypatch.setattr("app.application_repository_access.get_settings", lambda: settings)
        monkeypatch.setitem(
            main.app.dependency_overrides,
            require_maintainer,
            lambda: current_user["value"],
        )
        monkeypatch.setitem(
            main.app.dependency_overrides,
            require_reviewer,
            lambda: current_user["value"],
        )
        monkeypatch.setitem(
            main.app.dependency_overrides,
            MaintainerDependency.dependency,
            lambda: current_user["value"],
        )
        monkeypatch.setitem(
            main.app.dependency_overrides,
            ReviewerDependency.dependency,
            lambda: current_user["value"],
        )
        monkeypatch.setitem(
            main.app.dependency_overrides,
            MetricsDependency.dependency,
            lambda: current_user["value"],
        )

        yield (
            connect,
            workspaces[0],
            projects[0],
            workspaces[1],
            projects[1],
            {
                "repositories": repositories,
                "users": users,
                "current_user": current_user,
                "task_ids_by_workspace": task_ids_by_workspace,
            },
        )
    finally:
        main.app.dependency_overrides.pop(require_maintainer, None)
        main.app.dependency_overrides.pop(require_reviewer, None)
        main.app.dependency_overrides.pop(MaintainerDependency.dependency, None)
        main.app.dependency_overrides.pop(ReviewerDependency.dependency, None)
        main.app.dependency_overrides.pop(MetricsDependency.dependency, None)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_agent_run_http_routes_create_read_and_scope_durable_records(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    other_task_id = state["task_ids_by_workspace"][other_workspace.id]
    client = TestClient(main.app)

    sqlite_run_ids_before = set(store.agent_runs)
    sqlite_step_ids_before = set(store.agent_run_steps)
    sqlite_event_ids_before = set(store.agent_run_events)
    sqlite_audit_ids_before = {event.id for event in store.audit_events}

    created = client.post("/agent/runs", json={"task_id": task_id})
    assert created.status_code == 200, created.text
    payload = created.json()
    run_id = payload["run"]["id"]
    assert payload["run"]["workspace_id"] == workspace.id
    assert len(payload["steps"]) == 7

    listed = client.get("/agent/runs")
    detail = client.get(f"/agent/runs/{run_id}")
    steps = client.get(f"/agent/runs/{run_id}/steps")
    events = client.get(f"/agent/runs/{run_id}/events")
    summary = client.get("/agent/runs/summary")

    assert listed.status_code == 200
    assert [run["id"] for run in listed.json()] == [run_id]
    assert detail.status_code == 200 and detail.json()["run"]["id"] == run_id
    assert steps.status_code == 200 and len(steps.json()) == 7
    assert events.status_code == 200 and events.json()
    assert summary.status_code == 200 and summary.json()["total"] == 1

    durable_run = repositories.agent_runs.get(run_id)
    assert durable_run is not None
    assert durable_run.workspace_id == workspace.id
    assert repositories.agent_runs.get_active_for_task(task_id).id == run_id
    assert repositories.agent_runs.list_for_workspace(workspace_id=other_workspace.id) == []

    assert run_id not in store.agent_runs
    assert not any(step.run_id == run_id for step in store.agent_run_steps.values())
    assert not any(event.run_id == run_id for event in store.agent_run_events.values())
    assert set(store.agent_runs) == sqlite_run_ids_before
    assert set(store.agent_run_steps) == sqlite_step_ids_before
    assert set(store.agent_run_events) == sqlite_event_ids_before
    assert {event.id for event in store.audit_events} == sqlite_audit_ids_before

    state["current_user"]["value"] = state["users"][1]
    foreign_create = client.post("/agent/runs", json={"task_id": other_task_id})
    assert foreign_create.status_code == 200, foreign_create.text
    foreign_run_id = foreign_create.json()["run"]["id"]

    state["current_user"]["value"] = state["users"][0]
    scoped_list = client.get("/agent/runs")
    hidden_detail = client.get(f"/agent/runs/{foreign_run_id}")
    assert scoped_list.status_code == 200
    assert [run["id"] for run in scoped_list.json()] == [run_id]
    assert hidden_detail.status_code == 404


def test_postgres_agent_run_summary_never_reads_sqlite_maps(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    current_user = state["current_user"]
    current_user["value"] = state["users"][0]
    client = TestClient(main.app)

    created = client.post(
        "/agent/runs",
        json={"task_id": state["task_ids_by_workspace"][workspace.id]},
    )
    assert created.status_code == 200, created.text

    def reject_sqlite_fallback(_workspace_id: str | None):
        raise AssertionError("PostgreSQL AgentRun summary must not read SQLite maps")

    monkeypatch.setattr(agent_run_metrics, "_workspace_runs", reject_sqlite_fallback)

    summary = client.get("/agent/runs/summary")

    assert summary.status_code == 200, summary.text
    assert summary.json()["total"] == 1


def test_postgres_agent_run_metrics_use_one_workspace_scoped_durable_snapshot(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, _project, other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    current_user = state["current_user"]
    client = TestClient(main.app)

    for selected_workspace in (workspace, other_workspace):
        current_user["value"] = next(
            user for user in state["users"] if user.workspace_id == selected_workspace.id
        )
        task_id = state["task_ids_by_workspace"][selected_workspace.id]
        created = client.post("/agent/runs", json={"task_id": task_id})
        assert created.status_code == 200, created.text

    runtime = SimpleNamespace(repositories=repositories, connect=connect)
    monkeypatch.setattr(
        "app.agent_run_routes.get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )
    monkeypatch.setattr(
        "app.agent_run_routes.workspace_id_for_request",
        lambda user, _request: user.workspace_id,
    )
    monkeypatch.setattr(main.app.state, "postgres_runtime", runtime, raising=False)
    monkeypatch.setattr(
        main.app.state, "postgres_repositories", repositories, raising=False
    )
    for state_name, repository_name in {
        "agent_run_repository": "agent_runs",
        "notification_outbox_repository": "notification_outbox",
        "trust_repository": "trust",
        "trust_event_repository": "trust_events",
    }.items():
        monkeypatch.setattr(
            main.app.state,
            state_name,
            getattr(repositories, repository_name),
            raising=False,
        )

    def reject_process_map_fallback(*_args, **_kwargs):
        raise AssertionError("PostgreSQL metrics must not read process-local maps")

    monkeypatch.setattr(
        "app.agent_run_routes.prometheus_agent_run_metrics",
        reject_process_map_fallback,
    )

    current_user["value"] = state["users"][0]
    own_metrics = client.get("/agent/runs/metrics")
    assert own_metrics.status_code == 200, own_metrics.text
    own_body = own_metrics.text
    assert 'aixion_agent_runs_total{status="SCHEDULED"} 1' in own_body
    assert "aixion_capability_leases_total" in own_body
    assert "aixion_postgres_push_outbox_oldest_pending_age_seconds" in own_body
    assert workspace.id not in own_body
    assert other_workspace.id not in own_body

    current_user["value"] = state["users"][1]
    other_metrics = client.get("/agent/runs/metrics")
    assert other_metrics.status_code == 200, other_metrics.text
    assert 'aixion_agent_runs_total{status="SCHEDULED"} 1' in other_metrics.text

    # Every repository in the scrape must be the exact lifespan-owned instance.
    monkeypatch.setattr(main.app.state, "trust_event_repository", object())
    mismatch = client.get("/agent/runs/metrics")
    assert mismatch.status_code == 503


def test_postgres_agent_run_metrics_fail_closed_on_query_error(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    current_user = state["current_user"]
    current_user["value"] = state["users"][0]
    runtime = SimpleNamespace(repositories=repositories, connect=connect)
    monkeypatch.setattr(
        "app.agent_run_routes.get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )
    monkeypatch.setattr(
        "app.agent_run_routes.workspace_id_for_request",
        lambda user, _request: user.workspace_id,
    )
    monkeypatch.setattr(main.app.state, "postgres_runtime", runtime, raising=False)
    monkeypatch.setattr(
        main.app.state, "postgres_repositories", repositories, raising=False
    )
    for state_name, repository_name in {
        "agent_run_repository": "agent_runs",
        "notification_outbox_repository": "notification_outbox",
        "trust_repository": "trust",
        "trust_event_repository": "trust_events",
    }.items():
        monkeypatch.setattr(
            main.app.state,
            state_name,
            getattr(repositories, repository_name),
            raising=False,
        )

    def fail_snapshot(*_args, **_kwargs):
        raise RuntimeError("database read failed")

    monkeypatch.setattr(repositories.agent_runs, "metrics_snapshot", fail_snapshot)
    response = TestClient(main.app).get("/agent/runs/metrics")
    assert response.status_code == 503
    assert "database read failed" not in response.text


def test_postgres_agent_run_metrics_reject_local_auth_maintainer(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, _workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    state["current_user"]["value"] = state["users"][0]
    monkeypatch.setattr(
        main.app.state,
        "postgres_runtime",
        SimpleNamespace(repositories=repositories, connect=connect),
        raising=False,
    )
    monkeypatch.setattr(
        main.app.state, "postgres_repositories", repositories, raising=False
    )

    response = TestClient(main.app).get("/agent/runs/metrics")
    assert response.status_code == 503
    assert "PostgreSQL-backed authentication" in response.json()["detail"]


def test_postgres_agent_run_http_control_transitions_are_durable_and_audited(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)

    created = client.post("/agent/runs", json={"task_id": task_id})
    assert created.status_code == 200, created.text
    run_id = created.json()["run"]["id"]
    paused = client.post(f"/agent/runs/{run_id}/pause", json={"reason": "operator pause"})
    assert paused.status_code == 200, paused.text
    assert paused.json()["run"]["status"] == AgentRunStatus.PAUSED.value

    resumed = client.post(f"/agent/runs/{run_id}/resume", json={"reason": "operator resume"})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["run"]["status"] == AgentRunStatus.SCHEDULED.value

    cancelled = client.post(f"/agent/runs/{run_id}/cancel", json={"reason": "operator cancel"})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["run"]["status"] == AgentRunStatus.CANCELLED.value

    durable = repositories.agent_runs.get(run_id)
    assert durable is not None and durable.status == AgentRunStatus.CANCELLED
    assert all(
        step.status.value == "CANCELLED"
        for step in repositories.agent_runs.list_steps_for_run(run_id)
    )

    with connect() as conn:
        audit_rows = conn.execute(
            "SELECT event_type FROM audit_events "
            "WHERE workspace_id=%s AND entity_type='agent_run' AND entity_id=%s",
            (workspace.id, run_id),
        ).fetchall()
        event_rows = conn.execute(
            "SELECT event_type FROM agent_run_events WHERE workspace_id=%s AND run_id=%s",
            (workspace.id, run_id),
        ).fetchall()

    audit_types = {row[0] for row in audit_rows}
    event_types = {row[0] for row in event_rows}
    assert {
        "agent_run.created",
        "agent_run.paused",
        "agent_run.resumed",
        "agent_run.cancelled",
    } <= audit_types
    assert "OPERATOR_ACTION" in event_types
    assert "STEP_CANCELLED" in event_types
    assert run_id not in store.agent_runs


def test_postgres_stale_lease_watchdog_http_route_recovers_only_request_workspace(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    client = TestClient(main.app)
    current_user = state["current_user"]
    current_user["value"] = state["users"][0]

    run_ids: dict[str, str] = {}
    for index, target_workspace in enumerate((workspace, other_workspace)):
        current_user["value"] = state["users"][index]
        task_id = state["task_ids_by_workspace"][target_workspace.id]
        created = client.post("/agent/runs", json={"task_id": task_id})
        assert created.status_code == 200, created.text
        run_id = created.json()["run"]["id"]
        run_ids[target_workspace.id] = run_id
        stale_at = now_utc() - timedelta(minutes=5)
        run = repositories.agent_runs.get(run_id)
        assert run is not None and run.current_step_id is not None
        step = next(
            item for item in repositories.agent_runs.list_steps_for_run(run_id)
            if item.id == run.current_step_id
        )
        stale_run = run.model_copy(
            update={
                "status": AgentRunStatus.RUNNING,
                "lease_owner": "stale-worker",
                "lease_token": f"stale-token-{target_workspace.id}",
                "lease_expires_at": stale_at,
                "heartbeat_at": stale_at,
                "updated_at": stale_at,
            }
        )
        stale_step = step.model_copy(
            update={
                "status": AgentRunStepStatus.RUNNING,
                "lease_owner": "stale-worker",
                "lease_token": stale_run.lease_token,
                "lease_expires_at": stale_at,
                "updated_at": stale_at,
            }
        )
        with connect() as conn:
            conn.execute(
                "UPDATE agent_runs SET status=%s,lease_owner=%s,lease_token=%s,"
                "lease_expires_at=%s,updated_at=%s,payload=%s WHERE run_id=%s AND workspace_id=%s",
                (
                    stale_run.status.value,
                    stale_run.lease_owner,
                    stale_run.lease_token,
                    stale_run.lease_expires_at,
                    stale_run.updated_at,
                    Jsonb(stale_run.model_dump(mode="json")),
                    run_id,
                    target_workspace.id,
                ),
            )
            conn.execute(
                "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
                "WHERE step_id=%s AND workspace_id=%s",
                (
                    stale_step.status.value,
                    stale_step.updated_at,
                    Jsonb(stale_step.model_dump(mode="json")),
                    stale_step.id,
                    target_workspace.id,
                ),
            )

    current_user["value"] = state["users"][0]
    before_sqlite_runs = set(store.agent_runs)
    response = client.post("/agent/runs/watchdog/recover-stale")

    assert response.status_code == 200, response.text
    assert response.json() == {"recovered": 1}
    recovered = repositories.agent_runs.get(run_ids[workspace.id])
    untouched = repositories.agent_runs.get(run_ids[other_workspace.id])
    assert recovered is not None and recovered.lease_token is None
    assert recovered.status == AgentRunStatus.RETRY_WAIT
    assert untouched is not None and untouched.lease_token is not None
    assert untouched.status == AgentRunStatus.RUNNING
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE workspace_id=%s AND run_id=%s "
            "AND event_type='STALE_LEASE_RECOVERED'",
            (workspace.id, recovered.id),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_id=%s "
            "AND event_type='agent_run.stale_lease_recovered'",
            (workspace.id, recovered.id),
        ).fetchone()[0] == 1
    assert set(store.agent_runs) == before_sqlite_runs


def test_postgres_stale_lease_watchdog_http_route_rolls_back_audit_failure(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    client = TestClient(main.app, raise_server_exceptions=False)
    current_user = state["current_user"]
    current_user["value"] = state["users"][0]
    task_id = state["task_ids_by_workspace"][workspace.id]
    created = client.post("/agent/runs", json={"task_id": task_id})
    assert created.status_code == 200, created.text
    run_id = created.json()["run"]["id"]
    stale_at = now_utc() - timedelta(minutes=5)
    run = repositories.agent_runs.get(run_id)
    assert run is not None and run.current_step_id is not None
    step = next(
        item for item in repositories.agent_runs.list_steps_for_run(run_id)
        if item.id == run.current_step_id
    )
    stale_run = run.model_copy(
        update={
            "status": AgentRunStatus.RUNNING,
            "lease_owner": "stale-worker",
            "lease_token": "stale-token",
            "lease_expires_at": stale_at,
            "heartbeat_at": stale_at,
            "updated_at": stale_at,
        }
    )
    stale_step = step.model_copy(
        update={
            "status": AgentRunStepStatus.RUNNING,
            "lease_owner": "stale-worker",
            "lease_token": stale_run.lease_token,
            "lease_expires_at": stale_at,
            "updated_at": stale_at,
        }
    )
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET status=%s,lease_owner=%s,lease_token=%s,"
            "lease_expires_at=%s,updated_at=%s,payload=%s WHERE run_id=%s AND workspace_id=%s",
            (
                stale_run.status.value,
                stale_run.lease_owner,
                stale_run.lease_token,
                stale_run.lease_expires_at,
                stale_run.updated_at,
                Jsonb(stale_run.model_dump(mode="json")),
                run_id,
                workspace.id,
            ),
        )
        conn.execute(
            "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
            "WHERE step_id=%s AND workspace_id=%s",
            (
                stale_step.status.value,
                stale_step.updated_at,
                Jsonb(stale_step.model_dump(mode="json")),
                stale_step.id,
                workspace.id,
            ),
        )
        conn.execute(
            "CREATE FUNCTION reject_stale_lease_audit() RETURNS trigger AS $$ "
            "BEGIN IF NEW.event_type='agent_run.stale_lease_recovered' THEN "
            "RAISE EXCEPTION 'injected stale lease audit failure'; END IF; "
            "RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        conn.execute(
            "CREATE TRIGGER reject_stale_lease_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_stale_lease_audit()"
        )

    response = client.post("/agent/runs/watchdog/recover-stale")

    assert response.status_code == 500
    durable_run = repositories.agent_runs.get(run_id)
    durable_step = next(
        item for item in repositories.agent_runs.list_steps_for_run(run_id)
        if item.id == step.id
    )
    assert durable_run == stale_run
    assert durable_step == stale_step
    assert not any(
        event.run_id == run_id
        and event.event_type.value == "STALE_LEASE_RECOVERED"
        for event in repositories.agent_runs.list_events_for_run(run_id)
    )


def _seed_postgres_verify_result_ready(
    *,
    connect: Callable[[], Connection],
    workspace: Workspace,
    task_id: str,
    repositories: ApplicationRepositories,
    client: TestClient,
    include_evidence: bool = True,
) -> str:
    if include_evidence:
        seeded = _seed_postgres_validation_completion_records(
            connect=connect,
            workspace=workspace,
            task_id=task_id,
            state={},
            repositories=repositories,
            client=client,
        )
        seeded_run, seeded_steps, worker, intent_id, task_event, build_records = seeded
        repositories.agent_runs.commit_authoritative_task_step(
            expected_run=seeded_run,
            expected_steps=seeded_steps,
            worker_id=worker,
            lease_token=seeded_run.lease_token or "",
            step_type=AgentRunStepType.RUN_VALIDATION,
            validation_intent_id=intent_id,
            validation_task_event=task_event,
            build_records=build_records,
        )
        run_id = seeded_run.id
        task = repositories.agent_tasks.get(task_id)
        assert task is not None
    else:
        with connect() as conn:
            task_row = conn.execute(
                "SELECT payload FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
                (task_id, workspace.id),
            ).fetchone()
            task = AgentTask.model_validate(task_row[0])
            task.repository = "owner/approved-repository"
            task.branch_preference = "feature/approved-branch"
            conn.execute(
                "UPDATE agent_tasks SET payload=%s WHERE task_id=%s AND workspace_id=%s",
                (Jsonb(task.model_dump(mode="json")), task_id, workspace.id),
            )
            approval_row = conn.execute(
                "SELECT payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
                (task.approval_request_id, workspace.id),
            ).fetchone()
            approval = ApprovalRequest.model_validate(approval_row[0])
            approval.test_plan = ["python -m pytest"]
            approval.approved_payload_hash = compute_approval_payload_hash(approval)
            conn.execute(
                "UPDATE approval_requests SET payload=%s WHERE approval_id=%s AND workspace_id=%s",
                (Jsonb(approval.model_dump(mode="json")), approval.id, workspace.id),
            )
        created = client.post("/agent/runs", json={"task_id": task_id})
        assert created.status_code == 200, created.text
        run_id = created.json()["run"]["id"]
    run = repositories.agent_runs.get(run_id)
    assert run is not None
    steps = repositories.agent_runs.list_steps_for_run(run_id)
    verify_step = next(step for step in steps if step.step_type == AgentRunStepType.VERIFY_RESULT)
    now = now_utc()
    run.status = AgentRunStatus.RUNNING
    run.current_step_id = verify_step.id
    run.current_step_index = verify_step.sequence
    run.started_at = now
    run.updated_at = now
    task.status = AgentTaskStatus.READY_FOR_PR
    task.updated_at = now
    validation_success_event = None
    if include_evidence:
        validation_step = next(
            step for step in steps if step.step_type == AgentRunStepType.RUN_VALIDATION
        )
        validation_success_event = next(
            (
                event for event in repositories.agent_runs.list_events_for_run(run_id)
                if event.step_id == validation_step.id
                and event.event_type == AgentRunEventType.STEP_SUCCEEDED
            ),
            None,
        )
        if (
            validation_success_event is None
            or validation_step.evidence.get("validation_intent_id") != intent_id
            or validation_step.evidence.get("bundle_revision") is None
            or validation_step.evidence.get("bundle_git_tree_sha") is None
            or validation_success_event.metadata.get("bundle_revision")
            != validation_step.evidence["bundle_revision"]
            or validation_success_event.metadata.get("bundle_git_tree_sha")
            != validation_step.evidence["bundle_git_tree_sha"]
        ):
            raise AssertionError("Completed validation provenance is missing from the run snapshot.")
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET status=%s,current_step_id=%s,updated_at=%s,payload=%s "
            "WHERE run_id=%s AND workspace_id=%s",
            (
                run.status.value,
                run.current_step_id,
                now,
                Jsonb(run.model_dump(mode="json")),
                run_id,
                workspace.id,
            ),
        )
        conn.execute(
            "UPDATE agent_tasks SET status=%s,updated_at=%s,payload=%s "
            "WHERE task_id=%s AND workspace_id=%s",
            (
                task.status.value,
                now,
                Jsonb(task.model_dump(mode="json")),
                task_id,
                workspace.id,
            ),
        )
        for step in steps:
            if step.sequence < verify_step.sequence:
                step.status = AgentRunStepStatus.SUCCEEDED
                step.decision = VerificationDecision.PASS
                if step.step_type != AgentRunStepType.RUN_VALIDATION:
                    step.reason = "Prior worker step fixture completed."
                    step.evidence = {"fixture": "prior_worker_step_completed"}
                    step.evidence_hash = canonical_evidence_hash(step.evidence)
                    step.completed_at = now
            elif step.id == verify_step.id:
                step.status = AgentRunStepStatus.READY
            else:
                step.status = AgentRunStepStatus.PENDING
            step.updated_at = now
            conn.execute(
                "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
                "WHERE step_id=%s AND run_id=%s AND workspace_id=%s",
                (
                    step.status.value,
                    now,
                    Jsonb(step.model_dump(mode="json")),
                    step.id,
                    run_id,
                    workspace.id,
                ),
            )
            if step.sequence < verify_step.sequence:
                if step.step_type == AgentRunStepType.RUN_VALIDATION and include_evidence:
                    continue
                success_event = AgentRunEvent(
                    run_id=run_id,
                    task_id=task_id,
                    step_id=step.id,
                    correlation_id=run.correlation_id,
                    event_type=AgentRunEventType.STEP_SUCCEEDED,
                    previous_status=AgentRunStepStatus.RUNNING.value,
                    new_status=AgentRunStepStatus.SUCCEEDED.value,
                    message="Fixture step completed with durable evidence.",
                    actor="postgres-agent-run-fixture",
                    attempt_number=step.attempt_count,
                    output_evidence_hash=step.evidence_hash,
                    created_at=now + timedelta(seconds=step.sequence + 1),
                )
                conn.execute(
                    "INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,"
                    "created_at,event_type,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        success_event.id,
                        run_id,
                        task_id,
                        workspace.id,
                        success_event.created_at,
                        success_event.event_type.value,
                        Jsonb(success_event.model_dump(mode="json")),
                    ),
                )

        if include_evidence:
            pr_worker = f"mission-control:{run_id}:pr"
            pr_event = AgentTaskEvent(
                task_id=task_id,
                event_type=AgentTaskEventType.PR_CREATED,
                message="PR worker created the review pull request.",
                status=AgentTaskStatus.READY_FOR_PR,
                actor=pr_worker,
                created_at=now + timedelta(seconds=1),
                metadata={
                    "worker_id": pr_worker,
                    "approval_request_id": task.approval_request_id,
                    "repository": task.repository,
                    "head_branch": task.branch_preference,
                    "pull_request_number": 42,
                    "pull_request_url": "https://github.com/owner/approved-repository/pull/42",
                    "pull_request_opened": True,
                    "merged": False,
                    "operation_type": "github_pull_request_creation",
                },
            )
            for event in (pr_event,):
                conn.execute(
                    "INSERT INTO agent_task_events(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        event.id,
                        task_id,
                        workspace.id,
                        event.event_type.value,
                        event.actor,
                        event.created_at,
                        Jsonb(event.model_dump(mode="json")),
                    ),
                )
    return run_id


def _advance_postgres_agent_run_to_seal(
    *,
    connect: Callable[[], Connection],
    workspace: Workspace,
    task_id: str,
    repositories: ApplicationRepositories,
    client: TestClient,
) -> str:
    run_id = _seed_postgres_verify_result_ready(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-result-verifier", "timeout_seconds": 10},
    )
    assert response.status_code == 200, response.text
    verified = next(
        step
        for step in response.json()["steps"]
        if step["step_type"] == AgentRunStepType.VERIFY_RESULT.value
    )
    seal = next(
        step
        for step in response.json()["steps"]
        if step["step_type"] == AgentRunStepType.SEAL_EVIDENCE.value
    )
    assert verified["status"] == AgentRunStepStatus.SUCCEEDED.value
    assert seal["status"] == AgentRunStepStatus.READY.value
    return run_id


def test_postgres_agent_run_executes_only_durable_scope_validation(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    with connect() as conn:
        task_row = conn.execute(
            "SELECT payload FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
            (task_id, workspace.id),
        ).fetchone()
        task = AgentTask.model_validate(task_row[0])
        task.repository = "owner/approved-repository"
        task.branch_preference = "feature/approved-branch"
        conn.execute(
            "UPDATE agent_tasks SET payload=%s WHERE task_id=%s AND workspace_id=%s",
            (Jsonb(task.model_dump(mode="json")), task_id, workspace.id),
        )
        approval_row = conn.execute(
            "SELECT payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
            (task.approval_request_id, workspace.id),
        ).fetchone()
        approval = ApprovalRequest.model_validate(approval_row[0])
        approval.test_plan = ["python -m pytest"]
        approval.approved_payload_hash = compute_approval_payload_hash(approval)
        conn.execute(
            "UPDATE approval_requests SET payload=%s WHERE approval_id=%s AND workspace_id=%s",
            (Jsonb(approval.model_dump(mode="json")), approval.id, workspace.id),
        )

    client = TestClient(main.app)
    created = client.post("/agent/runs", json={"task_id": task_id})
    assert created.status_code == 200, created.text
    run_id = created.json()["run"]["id"]
    assert created.json()["steps"][0]["step_type"] == AgentRunStepType.VALIDATE_SCOPE.value

    executed = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-scope-validator", "timeout_seconds": 10},
    )
    assert executed.status_code == 200, executed.text
    body = executed.json()
    assert body["run"]["status"] == AgentRunStatus.RUNNING.value
    assert body["run"]["current_step_id"] == body["steps"][1]["id"]
    assert body["steps"][0]["status"] == AgentRunStepStatus.SUCCEEDED.value
    assert body["steps"][1]["status"] == AgentRunStepStatus.READY.value
    assert any(
        event["event_type"] == AgentRunEventType.STEP_SUCCEEDED.value for event in body["events"]
    )
    durable_run = repositories.agent_runs.get(run_id)
    assert durable_run is not None and durable_run.status == AgentRunStatus.RUNNING
    assert repositories.agent_tasks.get(task_id).status == AgentTaskStatus.APPROVED
    durable_steps = repositories.agent_runs.list_steps_for_run(run_id)
    assert durable_steps[0].status == AgentRunStepStatus.SUCCEEDED

    rejected = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-scope-validator", "timeout_seconds": 10},
    )
    assert rejected.status_code == 503
    assert "lifespan-owned repository bundle" in rejected.json()["detail"]
    assert repositories.agent_runs.get(run_id) == durable_run
    assert repositories.agent_runs.list_steps_for_run(run_id) == durable_steps


def test_postgres_scope_validation_rolls_back_all_completion_writes_on_audit_conflict(
    postgres_agent_run_route_runtime,
) -> None:
    connect, _workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][_workspace.id]
    client = TestClient(main.app)
    created = client.post("/agent/runs", json={"task_id": task_id})
    assert created.status_code == 200, created.text
    run_id = created.json()["run"]["id"]
    _leased_run, lease_event, lease_audit = repositories.agent_runs.acquire_lease(
        run_id=run_id,
        worker_id="scope-rollback-worker",
        lease_seconds=120,
        now=now_utc(),
    )
    assert lease_event is not None and lease_audit is not None
    before_run = repositories.agent_runs.get(run_id)
    assert before_run is not None
    before_steps = repositories.agent_runs.list_steps_for_run(run_id)
    before_events = repositories.agent_runs.list_events_for_run(run_id)
    with connect() as conn:
        before_completion_audits = conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type='agent_run' "
            "AND entity_id=%s AND event_type='agent_run.step_completed'",
            (run_id,),
        ).fetchone()[0]

    def duplicate_audit_id(task, approval, durable_run, durable_steps):
        records = _build_postgres_scope_validation_records(
            task=task,
            approval=approval,
            run=durable_run,
            steps=durable_steps,
            worker_id="scope-rollback-worker",
            lease_token=durable_run.lease_token or "",
        )
        updated_run, updated_steps, events, audit = records
        return updated_run, updated_steps, events, audit.model_copy(update={"id": lease_audit.id})

    with pytest.raises(psycopg.errors.UniqueViolation):
        repositories.agent_runs.commit_scope_validation(
            expected_run=before_run,
            expected_steps=before_steps,
            worker_id="scope-rollback-worker",
            lease_token=before_run.lease_token or "",
            build_records=duplicate_audit_id,
        )

    assert repositories.agent_runs.get(run_id) == before_run
    assert repositories.agent_runs.list_steps_for_run(run_id) == before_steps
    assert repositories.agent_runs.list_events_for_run(run_id) == before_events
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type='agent_run' "
            "AND entity_id=%s AND event_type='agent_run.step_completed'",
            (run_id,),
        ).fetchone()[0] == before_completion_audits


def test_postgres_scope_validation_rejects_stale_lease_without_completion_mutation(
    postgres_agent_run_route_runtime,
) -> None:
    connect, _workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][_workspace.id]
    client = TestClient(main.app)
    created = client.post("/agent/runs", json={"task_id": task_id})
    assert created.status_code == 200, created.text
    run_id = created.json()["run"]["id"]
    repositories.agent_runs.acquire_lease(
        run_id=run_id,
        worker_id="scope-stale-worker",
        lease_seconds=120,
        now=now_utc(),
    )
    leased = repositories.agent_runs.get(run_id)
    assert leased is not None
    stale = leased.model_copy(update={"lease_expires_at": now_utc() - timedelta(seconds=5)})
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET lease_expires_at=%s,updated_at=%s,payload=%s WHERE run_id=%s",
            (stale.lease_expires_at, stale.updated_at, Jsonb(stale.model_dump(mode="json")), run_id),
        )
    stale = repositories.agent_runs.get(run_id)
    assert stale is not None
    before_steps = repositories.agent_runs.list_steps_for_run(run_id)
    before_events = repositories.agent_runs.list_events_for_run(run_id)
    callback_invoked = False

    def should_not_build(*_args):
        nonlocal callback_invoked
        callback_invoked = True
        raise AssertionError("stale lease must not build completion records")

    with pytest.raises(AgentRunRepositoryConflict, match="lease is stale"):
        repositories.agent_runs.commit_scope_validation(
            expected_run=stale,
            expected_steps=before_steps,
            worker_id="scope-stale-worker",
            lease_token=stale.lease_token or "",
            build_records=should_not_build,
        )
    assert callback_invoked is False
    assert repositories.agent_runs.get(run_id) == stale
    assert repositories.agent_runs.list_steps_for_run(run_id) == before_steps
    assert repositories.agent_runs.list_events_for_run(run_id) == before_events


def test_postgres_scope_validation_rejects_run_revision_mismatch_without_completion(
    postgres_agent_run_route_runtime,
) -> None:
    connect, _workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][_workspace.id]
    created = TestClient(main.app).post("/agent/runs", json={"task_id": task_id})
    assert created.status_code == 200, created.text
    run_id = created.json()["run"]["id"]
    repositories.agent_runs.acquire_lease(
        run_id=run_id,
        worker_id="scope-cas-worker",
        lease_seconds=120,
        now=now_utc(),
    )
    stale_snapshot = repositories.agent_runs.get(run_id)
    assert stale_snapshot is not None
    before_steps = repositories.agent_runs.list_steps_for_run(run_id)
    before_events = repositories.agent_runs.list_events_for_run(run_id)
    changed_snapshot = stale_snapshot.model_copy(
        update={
            "metadata": {**stale_snapshot.metadata, "concurrent_revision": "test"},
            "updated_at": now_utc() + timedelta(seconds=1),
        }
    )
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET updated_at=%s,payload=%s WHERE run_id=%s",
            (
                changed_snapshot.updated_at,
                Jsonb(changed_snapshot.model_dump(mode="json")),
                run_id,
            ),
        )
    expected_steps = repositories.agent_runs.list_steps_for_run(run_id)
    callback_invoked = False

    def should_not_build(*_args):
        nonlocal callback_invoked
        callback_invoked = True
        raise AssertionError("a mismatched AgentRun revision must not build completion records")

    with pytest.raises(AgentRunRepositoryConflict, match="revision changed"):
        repositories.agent_runs.commit_scope_validation(
            expected_run=stale_snapshot,
            expected_steps=expected_steps,
            worker_id="scope-cas-worker",
            lease_token=stale_snapshot.lease_token or "",
            build_records=should_not_build,
        )
    assert callback_invoked is False
    assert repositories.agent_runs.get(run_id) == changed_snapshot
    assert repositories.agent_runs.list_steps_for_run(run_id) == before_steps
    assert repositories.agent_runs.list_events_for_run(run_id) == before_events


def test_postgres_scope_validation_holds_approval_revision_lock_through_completion(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    created = client.post("/agent/runs", json={"task_id": task_id})
    assert created.status_code == 200, created.text
    run_id = created.json()["run"]["id"]
    repositories.agent_runs.acquire_lease(
        run_id=run_id,
        worker_id="scope-concurrency-worker",
        lease_seconds=120,
        now=now_utc(),
    )
    expected_run = repositories.agent_runs.get(run_id)
    assert expected_run is not None
    expected_steps = repositories.agent_runs.list_steps_for_run(run_id)
    approval_id = expected_run.approval_request_id
    assert approval_id is not None
    builder_entered = Event()
    allow_completion = Event()
    writer_started = Event()
    writer_finished = Event()

    def held_builder(task, approval, durable_run, durable_steps):
        builder_entered.set()
        assert allow_completion.wait(timeout=10), "test did not release scope completion"
        return _build_postgres_scope_validation_records(
            task=task,
            approval=approval,
            run=durable_run,
            steps=durable_steps,
            worker_id="scope-concurrency-worker",
            lease_token=durable_run.lease_token or "",
        )

    def concurrent_approval_revision() -> None:
        writer_started.set()
        with connect() as conn:
            conn.execute(
                "UPDATE approval_requests SET version=version+1 WHERE approval_id=%s",
                (approval_id,),
            )
        writer_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        completion = pool.submit(
            repositories.agent_runs.commit_scope_validation,
            expected_run=expected_run,
            expected_steps=expected_steps,
            worker_id="scope-concurrency-worker",
            lease_token=expected_run.lease_token or "",
            build_records=held_builder,
        )
        assert builder_entered.wait(timeout=10), "scope builder did not enter its transaction"
        writer = pool.submit(concurrent_approval_revision)
        assert writer_started.wait(timeout=10), "approval revision writer did not start"
        assert not writer_finished.wait(timeout=0.25), (
            "approval revision changed while scope completion held its authority lock"
        )
        allow_completion.set()
        completion.result(timeout=10)
        writer.result(timeout=10)
    assert writer_finished.is_set()
    durable_steps = repositories.agent_runs.list_steps_for_run(run_id)
    assert durable_steps[0].attempt_count == 1
    assert durable_steps[0].decision is not None
    assert durable_steps[0].status not in {
        AgentRunStepStatus.READY,
        AgentRunStepStatus.RUNNING,
    }


def test_postgres_scope_validation_rejects_sqlite_approval_authority(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    created = client.post("/agent/runs", json={"task_id": task_id})
    assert created.status_code == 200, created.text
    run_id = created.json()["run"]["id"]
    durable_run = repositories.agent_runs.get(run_id)
    durable_steps = repositories.agent_runs.list_steps_for_run(run_id)
    durable_events = repositories.agent_runs.list_events_for_run(run_id)
    assert durable_run is not None

    # Simulate a mixed runtime: the run/task repositories are PostgreSQL, but
    # the linked approval reader is the local SQLite adapter. The supervisor
    # must reject before the default executor can inspect process-local tasks.
    monkeypatch.setattr(
        main.app.state,
        "approval_decision_repository",
        SQLiteApprovalDecisionRepository(store._connect),
    )

    class RejectProcessTaskReads(dict):
        def __getitem__(self, key):
            raise AssertionError(f"Process-local AgentTask fallback read: {key}")

        def get(self, key, default=None):
            raise AssertionError(f"Process-local AgentTask fallback read: {key}")

    monkeypatch.setattr(store, "agent_tasks", RejectProcessTaskReads())
    rejected = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-scope-validator", "timeout_seconds": 10},
    )

    assert rejected.status_code == 409
    assert "repository-bound task and approval reads" in rejected.json()["detail"]
    assert repositories.agent_runs.get(run_id) == durable_run
    assert repositories.agent_runs.list_steps_for_run(run_id) == durable_steps
    assert repositories.agent_runs.list_events_for_run(run_id) == durable_events


def test_postgres_agent_run_verifies_run_bound_durable_evidence_once(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _seed_postgres_verify_result_ready(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )

    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-result-verifier", "timeout_seconds": 10},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    verify_step = next(
        step for step in body["steps"] if step["step_type"] == AgentRunStepType.VERIFY_RESULT.value
    )
    seal_step = next(
        step for step in body["steps"] if step["step_type"] == AgentRunStepType.SEAL_EVIDENCE.value
    )
    assert verify_step["status"] == AgentRunStepStatus.SUCCEEDED.value
    assert verify_step["evidence"]["validation_passed"] is True
    assert verify_step["evidence"]["pr_event_count"] == 1
    assert seal_step["status"] == AgentRunStepStatus.READY.value
    durable_run = repositories.agent_runs.get(run_id)
    assert durable_run is not None and durable_run.lease_token is None
    assert (
        next(
            step
            for step in repositories.agent_runs.list_steps_for_run(run_id)
            if step.step_type == AgentRunStepType.VERIFY_RESULT
        ).status
        == AgentRunStepStatus.SUCCEEDED
    )
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_id=%s "
                "AND event_type='agent_run.step_completed'",
                (workspace.id, run_id),
            ).fetchone()[0]
            == 2
        )

    sealed = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-seal-worker", "timeout_seconds": 10},
    )
    assert sealed.status_code == 200, sealed.text
    assert sealed.json()["run"]["status"] == AgentRunStatus.SUCCEEDED.value
    sealed_run = repositories.agent_runs.get(run_id)
    assert sealed_run is not None

    replay = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-seal-worker", "timeout_seconds": 10},
    )
    assert replay.status_code == 409
    assert repositories.agent_runs.get(run_id) == sealed_run
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_id=%s "
                "AND event_type='agent_run.step_completed'",
                (workspace.id, run_id),
            ).fetchone()[0]
            == 3
        )


def test_postgres_run_validation_requires_bundle_provider_before_acquiring_lease(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.postgres_runtime import PostgresRuntime

    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    command = "python -m pytest"
    with connect() as conn:
        task_row = conn.execute(
            "SELECT payload FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
            (task_id, workspace.id),
        ).fetchone()
        task = AgentTask.model_validate(task_row[0])
        task.repository = "owner/approved-repository"
        task.branch_preference = "feature/approved-branch"
        conn.execute(
            "UPDATE agent_tasks SET payload=%s WHERE task_id=%s AND workspace_id=%s",
            (Jsonb(task.model_dump(mode="json")), task_id, workspace.id),
        )
        approval_row = conn.execute(
            "SELECT payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
            (task.approval_request_id, workspace.id),
        ).fetchone()
        approval = ApprovalRequest.model_validate(approval_row[0])
        approval.test_plan = [command]
        approval.approved_payload_hash = compute_approval_payload_hash(approval)
        conn.execute(
            "UPDATE approval_requests SET payload=%s WHERE approval_id=%s AND workspace_id=%s",
            (Jsonb(approval.model_dump(mode="json")), approval.id, workspace.id),
        )

    run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        state=state,
        client=client,
        lease_actions=[CapabilityActionType.RUN_COMMAND],
        allowed_commands=[command],
    )
    attached = client.post(
        f"/agent/runs/{run_id}/capability-lease",
        json={"capability_lease_id": lease.id},
    )
    assert attached.status_code == 200, attached.text
    run = repositories.agent_runs.get(run_id)
    steps = repositories.agent_runs.list_steps_for_run(run_id)
    validation_step = next(step for step in steps if step.step_type == AgentRunStepType.RUN_VALIDATION)
    now = now_utc()
    run.status = AgentRunStatus.RUNNING
    run.current_step_id = validation_step.id
    run.current_step_index = validation_step.sequence
    run.started_at = now
    run.updated_at = now
    for step in steps:
        step.updated_at = now
        if step.sequence < validation_step.sequence:
            step.status = AgentRunStepStatus.SUCCEEDED
        elif step.id == validation_step.id:
            step.status = AgentRunStepStatus.READY
        else:
            step.status = AgentRunStepStatus.PENDING
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET status=%s,current_step_id=%s,updated_at=%s,payload=%s "
            "WHERE run_id=%s AND workspace_id=%s",
            (run.status.value, run.current_step_id, now, Jsonb(run.model_dump(mode="json")), run.id, workspace.id),
        )
        for step in steps:
            conn.execute(
                "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
                "WHERE step_id=%s AND workspace_id=%s",
                (step.status.value, step.updated_at, Jsonb(step.model_dump(mode="json")), step.id, workspace.id),
            )

    runtime = PostgresRuntime(_dsn="postgresql://fixture", profile="test", _connector=connect)
    monkeypatch.setenv("AIXION_POSTGRES_RUN_VALIDATION_ENABLED", "true")
    monkeypatch.setenv(
        "AIXION_VALIDATION_CONTAINER_IMAGE",
        "python:3.12-slim@sha256:" + "a" * 64,
    )
    for name, value in {
        "postgres_runtime": runtime,
        "postgres_repositories": runtime.repositories,
        "agent_run_repository": runtime.repositories.agent_runs,
        "agent_task_repository": runtime.repositories.agent_tasks,
        "approval_decision_repository": runtime.repositories.approval_decisions,
        "trust_repository": runtime.repositories.trust,
        "agent_run_validation_intent_repository": runtime.repositories.agent_run_validation_intents,
        "agent_run_validation_bundle_provider": None,
    }.items():
        monkeypatch.setattr(main.app.state, name, value, raising=False)

    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "validation-route-test", "timeout_seconds": 10},
    )

    assert response.status_code == 503
    assert "bundle provider" in response.json()["detail"]
    persisted_run = runtime.repositories.agent_runs.get(run_id)
    assert persisted_run is not None
    assert persisted_run.lease_token is None
    assert persisted_run.lease_owner is None


def test_postgres_verify_result_serializes_concurrent_task_evidence_insert(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agent_run_supervisor as supervisor

    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _seed_postgres_verify_result_ready(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    builder_entered = Event()
    allow_completion = Event()
    writer_started = Event()
    writer_finished = Event()
    original_builder = supervisor._build_postgres_agent_run_step_completion_records

    def held_builder(**kwargs):
        if kwargs["step_type"] == AgentRunStepType.VERIFY_RESULT:
            builder_entered.set()
            assert allow_completion.wait(timeout=10), "test did not release VERIFY_RESULT"
        return original_builder(**kwargs)

    monkeypatch.setattr(
        supervisor,
        "_build_postgres_agent_run_step_completion_records",
        held_builder,
    )
    event = AgentTaskEvent(
        task_id=task_id,
        workspace_id=workspace.id,
        event_type=AgentTaskEventType.TESTS_FAILED,
        message="Concurrent evidence inserted after verification snapshot.",
        status=AgentTaskStatus.READY_FOR_PR,
        actor="concurrent-evidence-writer",
        metadata={"results": [{"exit_code": 1}]},
    )

    def execute_verification():
        return client.post(
            f"/agent/runs/{run_id}/execute-next",
            json={"worker_id": "postgres-result-verifier", "timeout_seconds": 10},
        )

    def insert_concurrent_evidence() -> None:
        writer_started.set()
        with connect() as conn:
            conn.execute(
                "INSERT INTO agent_task_events(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    event.id,
                    task_id,
                    workspace.id,
                    event.event_type.value,
                    event.actor,
                    event.created_at,
                    Jsonb(event.model_dump(mode="json")),
                ),
            )
        writer_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        completion = pool.submit(execute_verification)
        assert builder_entered.wait(timeout=10), "VERIFY_RESULT did not reach its transaction builder"
        writer = pool.submit(insert_concurrent_evidence)
        assert writer_started.wait(timeout=10), "task evidence writer did not start"
        assert not writer_finished.wait(timeout=0.25), (
            "task evidence insert was not serialized behind the VERIFY_RESULT task lock"
        )
        allow_completion.set()
        response = completion.result(timeout=10)
        writer.result(timeout=10)

    assert response.status_code == 200, response.text
    verify_step = next(
        item
        for item in response.json()["steps"]
        if item["step_type"] == AgentRunStepType.VERIFY_RESULT.value
    )
    assert verify_step["status"] == AgentRunStepStatus.SUCCEEDED.value
    assert writer_finished.is_set()
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE event_id=%s",
            (event.id,),
        ).fetchone()[0] == 1


def test_postgres_verify_result_rejects_stale_lease_without_completion_mutation(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _seed_postgres_verify_result_ready(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    repositories.agent_runs.acquire_lease(
        run_id=run_id,
        worker_id="verify-stale-lease-worker",
        lease_seconds=120,
        now=now_utc(),
    )
    before_run = repositories.agent_runs.get(run_id)
    before_steps = repositories.agent_runs.list_steps_for_run(run_id)
    assert before_run is not None and before_run.lease_token is not None
    stale_run = before_run.model_copy(deep=True)
    stale_run.lease_expires_at = now_utc() - timedelta(seconds=1)
    stale_run.updated_at = now_utc()
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET lease_expires_at=%s,updated_at=%s,payload=%s "
            "WHERE run_id=%s AND workspace_id=%s",
            (
                stale_run.lease_expires_at,
                stale_run.updated_at,
                Jsonb(stale_run.model_dump(mode="json")),
                run_id,
                workspace.id,
            ),
        )
    expected_stale_run = repositories.agent_runs.get(run_id)
    assert expected_stale_run is not None
    before_events = repositories.agent_runs.list_events_for_run(run_id)
    builder_called = False

    def forbidden_builder(*_args, **_kwargs):
        nonlocal builder_called
        builder_called = True
        raise AssertionError("stale lease reached result builder")

    with pytest.raises(AgentRunRepositoryConflict, match="lease is stale"):
        repositories.agent_runs.commit_authoritative_task_step(
            expected_run=expected_stale_run,
            expected_steps=before_steps,
            worker_id="verify-stale-lease-worker",
            lease_token=expected_stale_run.lease_token or "",
            step_type=AgentRunStepType.VERIFY_RESULT,
            build_records=forbidden_builder,
        )

    assert not builder_called
    assert repositories.agent_runs.get(run_id) == expected_stale_run
    assert repositories.agent_runs.list_steps_for_run(run_id) == before_steps
    assert repositories.agent_runs.list_events_for_run(run_id) == before_events


def test_postgres_verify_result_rejects_stale_run_snapshot_without_completion_mutation(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _seed_postgres_verify_result_ready(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    repositories.agent_runs.acquire_lease(
        run_id=run_id,
        worker_id="verify-cas-worker",
        lease_seconds=120,
        now=now_utc(),
    )
    expected_run = repositories.agent_runs.get(run_id)
    expected_steps = repositories.agent_runs.list_steps_for_run(run_id)
    before_events = repositories.agent_runs.list_events_for_run(run_id)
    assert expected_run is not None and expected_run.lease_token is not None
    changed_run = expected_run.model_copy(deep=True)
    changed_run.updated_at = now_utc() + timedelta(seconds=1)
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET updated_at=%s,payload=%s WHERE run_id=%s AND workspace_id=%s",
            (
                changed_run.updated_at,
                Jsonb(changed_run.model_dump(mode="json")),
                run_id,
                workspace.id,
            ),
        )
    builder_called = False

    def forbidden_builder(*_args, **_kwargs):
        nonlocal builder_called
        builder_called = True
        raise AssertionError("stale run snapshot reached result builder")

    with pytest.raises(AgentRunRepositoryConflict, match="revision changed"):
        repositories.agent_runs.commit_authoritative_task_step(
            expected_run=expected_run,
            expected_steps=expected_steps,
            worker_id="verify-cas-worker",
            lease_token=expected_run.lease_token,
            step_type=AgentRunStepType.VERIFY_RESULT,
            build_records=forbidden_builder,
        )

    assert not builder_called
    assert repositories.agent_runs.get(run_id) == changed_run
    assert repositories.agent_runs.list_steps_for_run(run_id) == expected_steps
    assert repositories.agent_runs.list_events_for_run(run_id) == before_events


def test_postgres_agent_run_seals_durable_evidence_and_rejects_replay(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _advance_postgres_agent_run_to_seal(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    original_commit = repositories.agent_runs.commit_evidence_seal
    locked_events: list[AgentRunEvent] = []
    fake_local_event = AgentRunEvent(
        run_id=run_id,
        task_id=task_id,
        correlation_id=repositories.agent_runs.get(run_id).correlation_id,
        event_type=AgentRunEventType.OPERATOR_ACTION,
        message="Process-local event must not enter the PostgreSQL seal manifest.",
        actor="test-local-projection",
    )

    def commit_capturing_durable_events(**kwargs):
        builder = kwargs["build_records"]

        def capture_locked_events(run, steps, events):
            locked_events.extend(events)
            return builder(run, steps, events)

        kwargs["build_records"] = capture_locked_events
        store.agent_run_events[fake_local_event.id] = fake_local_event
        return original_commit(**kwargs)

    monkeypatch.setattr(
        repositories.agent_runs,
        "commit_evidence_seal",
        commit_capturing_durable_events,
    )

    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-seal-worker", "timeout_seconds": 10},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    seal = next(
        item
        for item in body["steps"]
        if item["step_type"] == AgentRunStepType.SEAL_EVIDENCE.value
    )
    assert body["run"]["status"] == AgentRunStatus.SUCCEEDED.value
    assert seal["status"] == AgentRunStepStatus.SUCCEEDED.value
    assert seal["decision"] == VerificationDecision.PASS.value
    final_hash = seal["evidence"]["final_evidence_hash"]
    assert final_hash == canonical_evidence_hash(
        {key: value for key, value in seal["evidence"].items() if key != "final_evidence_hash"}
    )
    expected_manifest = [
        {
            "event_id": event.id,
            "event_type": event.event_type.value,
            "step_id": event.step_id,
            "previous_status": event.previous_status,
            "new_status": event.new_status,
            "actor": event.actor,
            "attempt_number": event.attempt_number,
            "input_evidence_hash": event.input_evidence_hash,
            "output_evidence_hash": event.output_evidence_hash,
            "metadata_hash": canonical_evidence_hash(event.metadata),
            "reason_hash": canonical_evidence_hash({"reason": event.reason}),
            "created_at": event.created_at.isoformat(),
        }
        for event in locked_events
    ]
    assert seal["evidence"]["events"] == expected_manifest
    assert fake_local_event.id not in {event["event_id"] for event in expected_manifest}
    store.agent_run_events.pop(fake_local_event.id, None)
    durable_run = repositories.agent_runs.get(run_id)
    assert durable_run is not None
    assert durable_run.status == AgentRunStatus.SUCCEEDED
    assert durable_run.final_evidence_hash == final_hash
    durable_events = repositories.agent_runs.list_events_for_run(run_id)
    assert any(
        event.event_type == AgentRunEventType.STEP_SUCCEEDED
        and event.step_id == seal["id"]
        and event.output_evidence_hash == seal["evidence_hash"]
        for event in durable_events
    )
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_id=%s "
                "AND event_type='agent_run.step_completed'",
                (workspace.id, run_id),
            ).fetchone()[0]
            == 3
        )

    replay = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-seal-worker", "timeout_seconds": 10},
    )
    assert replay.status_code == 409
    assert repositories.agent_runs.get(run_id) == durable_run


def test_postgres_agent_run_serializes_concurrent_seal_requests(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _advance_postgres_agent_run_to_seal(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    barrier = Barrier(2)

    def seal() -> int:
        barrier.wait()
        return client.post(
            f"/agent/runs/{run_id}/execute-next",
            json={"worker_id": "postgres-seal-worker", "timeout_seconds": 10},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _index: seal(), (1, 2)))

    assert sorted(statuses) == [200, 409]
    durable_run = repositories.agent_runs.get(run_id)
    assert durable_run is not None and durable_run.status == AgentRunStatus.SUCCEEDED
    seal_step = next(
        step
        for step in repositories.agent_runs.list_steps_for_run(run_id)
        if step.step_type == AgentRunStepType.SEAL_EVIDENCE
    )
    assert seal_step.status == AgentRunStepStatus.SUCCEEDED
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_run_events WHERE run_id=%s "
                "AND event_type='STEP_SUCCEEDED' AND payload->>'step_id'=%s",
                (run_id, seal_step.id),
            ).fetchone()[0]
            == 1
        )


def test_postgres_agent_run_rejects_noncanonical_seal_step_plan(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _advance_postgres_agent_run_to_seal(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    durable_steps = repositories.agent_runs.list_steps_for_run(run_id)
    changed = durable_steps[1]
    changed.step_type = AgentRunStepType.APPLY_CHANGES
    changed.idempotency_key = f"{run_id}:{changed.sequence}:APPLY_CHANGES"
    with connect() as conn:
        conn.execute(
            "UPDATE agent_run_steps SET payload=%s WHERE step_id=%s AND run_id=%s",
            (
                Jsonb(changed.model_dump(mode="json")),
                changed.id,
                run_id,
            ),
        )

    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-seal-worker", "timeout_seconds": 10},
    )

    assert response.status_code == 409
    assert "canonical step plan" in response.json()["detail"]
    durable_run = repositories.agent_runs.get(run_id)
    assert durable_run is not None and durable_run.lease_token is None
    assert repositories.agent_runs.list_steps_for_run(run_id) == [
        item.model_copy(deep=True) if item.id != changed.id else changed
        for item in durable_steps
    ]


def test_postgres_agent_run_rejects_incomplete_prior_step_before_sealing(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _advance_postgres_agent_run_to_seal(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    durable_steps = repositories.agent_runs.list_steps_for_run(run_id)
    changed = durable_steps[-2]
    changed.status = AgentRunStepStatus.READY
    with connect() as conn:
        conn.execute(
            "UPDATE agent_run_steps SET status=%s,payload=%s WHERE step_id=%s AND run_id=%s",
            (
                changed.status.value,
                Jsonb(changed.model_dump(mode="json")),
                changed.id,
                run_id,
            ),
        )

    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-seal-worker", "timeout_seconds": 10},
    )

    assert response.status_code == 409
    assert "every prior step succeeded" in response.json()["detail"]
    durable_run = repositories.agent_runs.get(run_id)
    assert durable_run is not None and durable_run.lease_token is None
    assert repositories.agent_runs.list_steps_for_run(run_id)[-2].status == AgentRunStepStatus.READY


def test_postgres_agent_run_seal_audit_failure_rolls_back_completion_and_preserves_lease(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _advance_postgres_agent_run_to_seal(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    with connect() as conn:
        conn.execute(
            "CREATE FUNCTION reject_seal_evidence_audit() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "IF NEW.event_type='agent_run.step_completed' "
            "AND NEW.details->>'step_type'='SEAL_EVIDENCE' THEN "
            "RAISE EXCEPTION 'injected seal evidence audit failure'; "
            "END IF; RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER reject_seal_evidence_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_seal_evidence_audit()"
        )

    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-seal-worker", "timeout_seconds": 10},
    )

    assert response.status_code == 409
    assert "completion could not be committed" in response.json()["detail"]
    durable_run = repositories.agent_runs.get(run_id)
    assert durable_run is not None and durable_run.lease_token is not None
    assert durable_run.final_evidence_hash is None
    seal = next(
        step
        for step in repositories.agent_runs.list_steps_for_run(run_id)
        if step.step_type == AgentRunStepType.SEAL_EVIDENCE
    )
    assert seal.status == AgentRunStepStatus.RUNNING
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_run_events WHERE run_id=%s "
                "AND event_type='STEP_SUCCEEDED' AND payload->>'step_id'=%s",
                (run_id, seal.id),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_id=%s "
                "AND event_type='agent_run.step_completed' "
                "AND details->>'step_id'=%s",
                (workspace.id, run_id, seal.id),
            ).fetchone()[0]
            == 0
        )


def test_postgres_agent_run_seal_rejects_stale_snapshot_without_completion(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _advance_postgres_agent_run_to_seal(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    original_commit = repositories.agent_runs.commit_evidence_seal

    def commit_with_stale_snapshot(**kwargs):
        kwargs["expected_run"] = kwargs["expected_run"].model_copy(
            update={"heartbeat_at": now_utc()}
        )
        return original_commit(**kwargs)

    monkeypatch.setattr(
        repositories.agent_runs, "commit_evidence_seal", commit_with_stale_snapshot
    )
    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-seal-worker", "timeout_seconds": 10},
    )

    assert response.status_code == 409
    durable_run = repositories.agent_runs.get(run_id)
    assert durable_run is not None and durable_run.final_evidence_hash is None
    seal = next(
        item
        for item in repositories.agent_runs.list_steps_for_run(run_id)
        if item.step_type == AgentRunStepType.SEAL_EVIDENCE
    )
    assert seal.status == AgentRunStepStatus.RUNNING
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id=%s AND event_type='STEP_SUCCEEDED' "
            "AND payload->>'step_id'=%s",
            (run_id, seal.id),
        ).fetchone()[0] == 0


def test_postgres_agent_run_seal_rejects_expired_worker_lease(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _advance_postgres_agent_run_to_seal(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    original_commit = repositories.agent_runs.commit_evidence_seal

    def interrupt_seal(**_kwargs):
        raise RuntimeError("stop after the durable RUNNING checkpoint")

    monkeypatch.setattr(repositories.agent_runs, "commit_evidence_seal", interrupt_seal)
    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-seal-worker", "timeout_seconds": 10},
    )

    assert response.status_code == 409
    monkeypatch.setattr(repositories.agent_runs, "commit_evidence_seal", original_commit)
    expected_run = repositories.agent_runs.get(run_id)
    assert expected_run is not None and expected_run.lease_token is not None
    expected_steps = repositories.agent_runs.list_steps_for_run(run_id)
    current = next(item for item in expected_steps if item.id == expected_run.current_step_id)
    assert current.status == AgentRunStepStatus.RUNNING
    expired_at = now_utc() - timedelta(seconds=2)
    expired_run = expected_run.model_copy(update={"lease_expires_at": expired_at})
    expired_steps = [item.model_copy(deep=True) for item in expected_steps]
    expired_current = next(item for item in expired_steps if item.id == expired_run.current_step_id)
    expired_current.lease_expires_at = expired_at
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET lease_expires_at=%s,payload=%s WHERE run_id=%s AND workspace_id=%s",
            (
                expired_at,
                Jsonb(expired_run.model_dump(mode="json")),
                run_id,
                workspace.id,
            ),
        )
        conn.execute(
            "UPDATE agent_run_steps SET payload=%s "
            "WHERE step_id=%s AND run_id=%s AND workspace_id=%s",
            (
                Jsonb(expired_current.model_dump(mode="json")),
                expired_current.id,
                run_id,
                workspace.id,
            ),
        )
    callback_reached = Event()

    def forbidden_builder(*_args):
        callback_reached.set()
        pytest.fail("expired lease reached the evidence builder")

    with pytest.raises(AgentRunRepositoryConflict, match="lease is stale"):
        original_commit(
            expected_run=expired_run,
            expected_steps=expired_steps,
            worker_id="postgres-seal-worker",
            lease_token=expired_run.lease_token,
            build_records=forbidden_builder,
        )
    assert not callback_reached.is_set()
    durable_run = repositories.agent_runs.get(run_id)
    assert durable_run is not None and durable_run.final_evidence_hash is None


def test_postgres_agent_run_seal_rejects_malformed_locked_event_payload(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _advance_postgres_agent_run_to_seal(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    original_commit = repositories.agent_runs.commit_evidence_seal

    def commit_with_malformed_event(**kwargs):
        with connect() as conn:
            conn.execute(
                "UPDATE agent_run_events SET payload=jsonb_set(payload,'{event_type}',%s::jsonb) "
                "WHERE run_id=%s AND workspace_id=%s",
                (Jsonb("NOT_A_REAL_EVENT"), run_id, workspace.id),
            )
        return original_commit(**kwargs)

    monkeypatch.setattr(
        repositories.agent_runs, "commit_evidence_seal", commit_with_malformed_event
    )
    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-seal-worker", "timeout_seconds": 10},
    )

    assert response.status_code == 409
    durable_run = repositories.agent_runs.get(run_id)
    assert durable_run is not None and durable_run.final_evidence_hash is None
    seal = next(
        item
        for item in repositories.agent_runs.list_steps_for_run(run_id)
        if item.step_type == AgentRunStepType.SEAL_EVIDENCE
    )
    assert seal.status == AgentRunStepStatus.RUNNING
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id=%s AND event_type='STEP_SUCCEEDED' "
            "AND payload->>'step_id'=%s",
            (run_id, seal.id),
        ).fetchone()[0] == 0


def test_postgres_agent_run_seal_locks_event_snapshot_until_commit(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agent_run_supervisor as supervisor

    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _advance_postgres_agent_run_to_seal(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    event = repositories.agent_runs.list_events_for_run(run_id)[0]
    snapshot_locked = Event()
    release_builder = Event()
    update_started = Event()
    update_applied = Event()
    original_seal = supervisor._seal_postgres_agent_run_evidence

    def hold_builder_until_writer_is_blocked(**kwargs):
        snapshot_locked.set()
        assert release_builder.wait(timeout=10), "test did not release the evidence builder"
        return original_seal(**kwargs)

    def update_event_then_rollback() -> None:
        update_started.set()
        with connect() as conn:
            try:
                with conn.transaction():
                    conn.execute(
                        "UPDATE agent_run_events SET payload=jsonb_set(payload,'{message}',%s::jsonb) "
                        "WHERE event_id=%s",
                        (Jsonb("concurrent test mutation"), event.id),
                    )
                    update_applied.set()
                    raise RuntimeError("rollback test-only event mutation")
            except RuntimeError:
                pass

    monkeypatch.setattr(
        supervisor, "_seal_postgres_agent_run_evidence", hold_builder_until_writer_is_blocked
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        seal_future = pool.submit(
            client.post,
            f"/agent/runs/{run_id}/execute-next",
            json={"worker_id": "postgres-seal-worker", "timeout_seconds": 10},
        )
        assert snapshot_locked.wait(timeout=10), "sealer did not acquire its durable snapshot"
        writer_future = pool.submit(update_event_then_rollback)
        assert update_started.wait(timeout=10)
        assert not update_applied.wait(timeout=0.25), "event update bypassed the seal row lock"
        release_builder.set()
        response = seal_future.result(timeout=10)
        writer_future.result(timeout=10)

    assert response.status_code == 200, response.text
    body = response.json()
    seal = next(
        item
        for item in body["steps"]
        if item["step_type"] == AgentRunStepType.SEAL_EVIDENCE.value
    )
    assert seal["status"] == AgentRunStepStatus.SUCCEEDED.value
    assert event.id in {item["event_id"] for item in seal["evidence"]["events"]}
    with connect() as conn:
        persisted_message = conn.execute(
            "SELECT payload->>'message' FROM agent_run_events WHERE event_id=%s",
            (event.id,),
        ).fetchone()[0]
    assert persisted_message == event.message


def test_postgres_agent_run_verify_result_ignores_process_local_fake_events(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _seed_postgres_verify_result_ready(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
        include_evidence=False,
    )
    task = repositories.agent_tasks.get(task_id)
    assert task is not None
    approval_id = task.approval_request_id
    now = now_utc()
    validation_worker = f"mission-control:{run_id}:validation"
    pr_worker = f"mission-control:{run_id}:pr"
    fake_events = [
        AgentTaskEvent(
            task_id=task_id,
            event_type=AgentTaskEventType.TESTS_PASSED,
            status=AgentTaskStatus.APPROVED,
            actor=validation_worker,
            created_at=now,
            metadata={
                "worker_id": validation_worker,
                "approval_request_id": approval_id,
                "command_count": 1,
                "commands_executed": True,
                "results": [
                    {"command": "python -c 'print(1)'", "exit_code": 0, "timed_out": False}
                ],
            },
        ),
        AgentTaskEvent(
            task_id=task_id,
            event_type=AgentTaskEventType.PR_CREATED,
            status=AgentTaskStatus.READY_FOR_PR,
            actor=pr_worker,
            created_at=now,
            metadata={
                "worker_id": pr_worker,
                "approval_request_id": approval_id,
                "repository": "owner/approved-repository",
                "head_branch": "feature/approved-branch",
                "pull_request_number": 42,
                "pull_request_url": "https://github.com/owner/approved-repository/pull/42",
                "pull_request_opened": True,
                "merged": False,
                "operation_type": "github_pull_request_creation",
            },
        ),
    ]
    for event in fake_events:
        store.agent_task_events[event.id] = event
    try:
        response = client.post(
            f"/agent/runs/{run_id}/execute-next",
            json={"worker_id": "postgres-result-verifier", "timeout_seconds": 10},
        )
    finally:
        for event in fake_events:
            store.agent_task_events.pop(event.id, None)

    assert response.status_code == 200, response.text
    verify_step = next(
        step
        for step in response.json()["steps"]
        if step["step_type"] == AgentRunStepType.VERIFY_RESULT.value
    )
    assert verify_step["status"] != AgentRunStepStatus.SUCCEEDED.value
    assert verify_step["evidence"]["validation_passed"] is False
    assert verify_step["evidence"]["pr_event_count"] == 0


def test_postgres_agent_run_verify_result_requires_all_prior_steps_succeeded(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _seed_postgres_verify_result_ready(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
        include_evidence=False,
    )
    first = repositories.agent_runs.list_steps_for_run(run_id)[0]
    first.status = AgentRunStepStatus.SKIPPED
    with connect() as conn:
        conn.execute(
            "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
            "WHERE step_id=%s AND run_id=%s AND workspace_id=%s",
            (
                first.status.value,
                now_utc(),
                Jsonb(first.model_dump(mode="json")),
                first.id,
                run_id,
                workspace.id,
            ),
        )

    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-result-verifier", "timeout_seconds": 10},
    )

    assert response.status_code == 409
    assert "every preceding AgentRun step to have succeeded" in response.json()["detail"]
    assert repositories.agent_runs.get(run_id).status == AgentRunStatus.RUNNING
    assert (
        repositories.agent_runs.list_steps_for_run(run_id)[0].status == AgentRunStepStatus.SKIPPED
    )


def test_postgres_agent_run_releases_lease_if_prerequisite_changes_after_claim(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agent_run_supervisor as supervisor

    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _seed_postgres_verify_result_ready(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
        include_evidence=False,
    )
    original_acquire = supervisor.acquire_run_lease

    def acquire_then_invalidate(run, **kwargs):
        token = original_acquire(run, **kwargs)
        prior = repositories.agent_runs.list_steps_for_run(run_id)[0]
        prior.status = AgentRunStepStatus.SKIPPED
        with connect() as conn:
            conn.execute(
                "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s WHERE step_id=%s",
                (prior.status.value, now_utc(), Jsonb(prior.model_dump(mode="json")), prior.id),
            )
        return token

    monkeypatch.setattr(supervisor, "acquire_run_lease", acquire_then_invalidate)
    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-result-verifier", "timeout_seconds": 10},
    )
    assert response.status_code == 409
    durable_run = repositories.agent_runs.get(run_id)
    assert durable_run is not None and durable_run.lease_token is None
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type='agent_run.lease_released'",
                (run_id,),
            ).fetchone()[0]
            >= 1
        )


@pytest.mark.parametrize(
    ("evidence_field", "invalid_value"),
    [("command_index", True), ("exit_code", False)],
)
def test_postgres_verify_result_rejects_boolean_as_json_integer(
    postgres_agent_run_route_runtime,
    evidence_field: str,
    invalid_value: bool,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _seed_postgres_verify_result_ready(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    event = next(
        item
        for item in repositories.agent_tasks.list_events(task_id, workspace_id=workspace.id)
        if item.event_type == AgentTaskEventType.TESTS_PASSED
    )
    event.metadata["command_results"][0][evidence_field] = invalid_value
    with connect() as conn:
        conn.execute(
            "UPDATE agent_task_events SET payload=%s WHERE event_id=%s",
            (Jsonb(event.model_dump(mode="json")), event.id),
        )

    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-result-verifier", "timeout_seconds": 10},
    )
    assert response.status_code == 200, response.text
    verify = next(
        step
        for step in response.json()["steps"]
        if step["step_type"] == AgentRunStepType.VERIFY_RESULT.value
    )
    assert verify["status"] != AgentRunStepStatus.SUCCEEDED.value
    assert verify["decision"] != VerificationDecision.PASS.value
    assert verify["output_reference"] is None


@pytest.mark.parametrize("mutated_field", ["repository", "branch_preference"])
def test_postgres_verify_result_rejects_task_snapshot_mutation(
    postgres_agent_run_route_runtime,
    mutated_field: str,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _seed_postgres_verify_result_ready(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    task = repositories.agent_tasks.get(task_id)
    setattr(
        task,
        mutated_field,
        "attacker/changed-value" if mutated_field == "repository" else "feature/changed-after-run",
    )
    with connect() as conn:
        conn.execute(
            "UPDATE agent_tasks SET payload=%s WHERE task_id=%s",
            (Jsonb(task.model_dump(mode="json")), task_id),
        )

    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-result-verifier", "timeout_seconds": 10},
    )
    assert response.status_code == 200, response.text
    verify = next(
        step
        for step in response.json()["steps"]
        if step["step_type"] == AgentRunStepType.VERIFY_RESULT.value
    )
    assert verify["status"] != AgentRunStepStatus.SUCCEEDED.value
    assert verify["decision"] != VerificationDecision.PASS.value
    assert verify["output_reference"] is None


def test_postgres_agent_run_verify_result_rolls_back_completion_when_audit_fails(
    postgres_agent_run_route_runtime,
) -> None:
    from app.agent_run_supervisor import (
        _build_postgres_agent_run_step_completion_records,
        _verify_postgres_agent_run_result,
    )

    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id = _seed_postgres_verify_result_ready(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        repositories=repositories,
        client=client,
    )
    repositories.agent_runs.acquire_lease(
        run_id=run_id,
        worker_id="postgres-result-verifier",
        lease_seconds=120,
        now=now_utc(),
    )
    before_run = repositories.agent_runs.get(run_id)
    before_steps = repositories.agent_runs.list_steps_for_run(run_id)
    before_events = repositories.agent_runs.list_events_for_run(run_id)
    assert before_run is not None and before_run.lease_token is not None
    with connect() as conn:
        before_step_completed_audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_id=%s "
            "AND event_type='agent_run.step_completed'",
            (workspace.id, run_id),
        ).fetchone()[0]
        conn.execute(
            "CREATE FUNCTION reject_verify_result_audit() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "IF NEW.event_type='agent_run.step_completed' THEN "
            "RAISE EXCEPTION 'injected verify result audit failure'; "
            "END IF; RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER reject_verify_result_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_verify_result_audit()"
        )

    def build_records(task, approval, task_events, durable_run, durable_steps):
        result = _verify_postgres_agent_run_result(
            run=durable_run,
            task=task,
            approval=approval,
            events=task_events,
            task_repository=repositories.agent_tasks,
            approval_repository=repositories.approval_decisions,
        )
        return _build_postgres_agent_run_step_completion_records(
            run=durable_run,
            steps=durable_steps,
            step_type=AgentRunStepType.VERIFY_RESULT,
            result=result,
            worker_id="postgres-result-verifier",
            lease_token=durable_run.lease_token or "",
        )

    with pytest.raises(psycopg.Error, match="injected verify result audit failure"):
        repositories.agent_runs.commit_authoritative_task_step(
            expected_run=before_run,
            expected_steps=before_steps,
            worker_id="postgres-result-verifier",
            lease_token=before_run.lease_token,
            step_type=AgentRunStepType.VERIFY_RESULT,
            build_records=build_records,
        )

    assert repositories.agent_runs.get(run_id) == before_run
    assert repositories.agent_runs.list_steps_for_run(run_id) == before_steps
    assert repositories.agent_runs.list_events_for_run(run_id) == before_events
    verify_step = next(
        step for step in before_steps if step.step_type == AgentRunStepType.VERIFY_RESULT
    )
    assert verify_step.status == AgentRunStepStatus.READY
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_run_events WHERE run_id=%s "
                "AND event_type='STEP_SUCCEEDED' AND payload->>'step_id'=%s",
                (run_id, verify_step.id),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_id=%s "
                "AND event_type='agent_run.step_completed'",
                (workspace.id, run_id),
            ).fetchone()[0]
            == before_step_completed_audit_count
        )


def test_postgres_scope_validation_blocks_approval_changed_after_run_creation(
    postgres_agent_run_route_runtime,
) -> None:
    connect, _workspace, _project, other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][other_workspace.id]
    state["current_user"]["value"] = state["users"][1]
    with connect() as conn:
        task_row = conn.execute(
            "SELECT payload FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
            (task_id, other_workspace.id),
        ).fetchone()
        task = AgentTask.model_validate(task_row[0])
        task.repository = "owner/approved-repository"
        task.branch_preference = "feature/approved-branch"
        conn.execute(
            "UPDATE agent_tasks SET payload=%s WHERE task_id=%s AND workspace_id=%s",
            (Jsonb(task.model_dump(mode="json")), task_id, other_workspace.id),
        )
        approval_row = conn.execute(
            "SELECT payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
            (task.approval_request_id, other_workspace.id),
        ).fetchone()
        approval = ApprovalRequest.model_validate(approval_row[0])
        approval.test_plan = ["python -m pytest"]
        approval.approved_payload_hash = compute_approval_payload_hash(approval)
        conn.execute(
            "UPDATE approval_requests SET payload=%s WHERE approval_id=%s AND workspace_id=%s",
            (Jsonb(approval.model_dump(mode="json")), approval.id, other_workspace.id),
        )

    client = TestClient(main.app)
    created = client.post("/agent/runs", json={"task_id": task_id})
    assert created.status_code == 200, created.text
    run_id = created.json()["run"]["id"]

    approval.summary = "Changed after the AgentRun was created."
    with connect() as conn:
        conn.execute(
            "UPDATE approval_requests SET payload=%s WHERE approval_id=%s AND workspace_id=%s",
            (Jsonb(approval.model_dump(mode="json")), approval.id, other_workspace.id),
        )

    executed = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-scope-validator", "timeout_seconds": 10},
    )

    assert executed.status_code == 200, executed.text
    assert executed.json()["run"]["status"] == AgentRunStatus.BLOCKED.value
    assert executed.json()["steps"][0]["status"] == AgentRunStepStatus.BLOCKED.value
    assert "approved payload hash" in executed.json()["steps"][0]["reason"].lower()
    assert repositories.agent_runs.get(run_id).status == AgentRunStatus.BLOCKED
    assert repositories.agent_tasks.get(task_id).status == AgentTaskStatus.APPROVED
    assert any(
        event.event_type == AgentRunEventType.STEP_BLOCKED
        for event in repositories.agent_runs.list_events_for_run(run_id)
    )


def _create_run_with_postgres_capability_lease(
    *,
    connect: Callable[[], Connection],
    workspace: Workspace,
    task_id: str,
    state: dict,
    client: TestClient,
    status: CapabilityLeaseStatus = CapabilityLeaseStatus.ACTIVE,
    approved_payload_hash: str | None = None,
    expires_at=None,
    valid_signature: bool = True,
    lease_branch: str = "feature/approved-branch",
    lease_max_retries: int = 3,
    lease_actions: list[CapabilityActionType] | None = None,
    allowed_path_prefixes: list[str] | None = None,
    allowed_commands: list[str] | None = None,
) -> tuple[str, CapabilityLease]:
    with connect() as conn:
        task_row = conn.execute(
            "SELECT payload FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
            (task_id, workspace.id),
        ).fetchone()
        task = AgentTask.model_validate(task_row[0])
        task.repository = "owner/approved-repository"
        task.branch_preference = "feature/approved-branch"
        conn.execute(
            "UPDATE agent_tasks SET payload=%s WHERE task_id=%s AND workspace_id=%s",
            (Jsonb(task.model_dump(mode="json")), task_id, workspace.id),
        )
        approval_row = conn.execute(
            "SELECT payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
            (task.approval_request_id, workspace.id),
        ).fetchone()
        approval = ApprovalRequest.model_validate(approval_row[0])

    created = client.post("/agent/runs", json={"task_id": task_id})
    assert created.status_code == 200, created.text
    run_id = created.json()["run"]["id"]
    now = now_utc()
    lease = CapabilityLease(
        approval_request_id=approval.id,
        workspace_id=workspace.id,
        approved_payload_hash=approved_payload_hash or approval.approved_payload_hash,
        provider=task.provider,
        mode=CapabilityLeaseMode.BOUNDED,
        scope=CapabilityScope(
            repository="owner/approved-repository",
            branch=lease_branch,
            allowed_actions=lease_actions or [CapabilityActionType.CREATE_BRANCH],
            allowed_path_prefixes=allowed_path_prefixes or [],
            allowed_commands=allowed_commands or [],
            max_retries=lease_max_retries,
        ),
        issued_by_user_id=workspace.owner_user_id,
        status=status,
        expires_at=expires_at or now + timedelta(minutes=5),
        receipt_nonce=f"nonce_{uuid.uuid4().hex}",
        receipt_signature="test-signature",
    )
    lease.receipt_signature = sign_payload(lease_receipt_payload(lease))
    if not valid_signature:
        lease.receipt_signature = "invalid-signature"
    with connect() as conn:
        owner = User(
            id=workspace.owner_user_id,
            email=f"{workspace.owner_user_id}@example.test",
            display_name="Lease issuer",
            role=UserRole.OWNER,
            password_hash="test-hash",
            password_salt="test-salt",
            workspace_id=workspace.id,
            email_verified=True,
        )
        conn.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,"
            "email_verified,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id) DO NOTHING",
            (
                owner.id,
                workspace.id,
                owner.email,
                owner.email.lower(),
                owner.role.value,
                owner.disabled,
                owner.email_verified,
                Jsonb(owner.model_dump(mode="json")),
            ),
        )
        PostgresTrustRepository().insert_capability_lease(conn, lease)
    return run_id, lease


def test_postgres_agent_run_capability_binding_is_durable_and_idempotent(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect, workspace=workspace, task_id=task_id, state=state, client=client
    )

    first = client.post(
        f"/agent/runs/{run_id}/capability-lease",
        json={"capability_lease_id": lease.id},
    )
    second = client.post(
        f"/agent/runs/{run_id}/capability-lease",
        json={"capability_lease_id": lease.id},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["run"]["capability_lease_id"] == lease.id
    assert second.json()["run"]["capability_lease_id"] == lease.id
    persisted = repositories.agent_runs.get(run_id)
    assert persisted is not None and persisted.capability_lease_id == lease.id
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_run_events WHERE run_id=%s AND event_type='CAPABILITY_LEASE_ATTACHED'",
                (run_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='agent_run.capability_lease_attached'",
                (run_id,),
            ).fetchone()[0]
            == 1
        )
    assert run_id not in store.agent_runs

    with connect() as conn:
        with conn.transaction():
            repositories.trust.revoke_capability_lease(
                conn,
                lease_id=lease.id,
                workspace_id=workspace.id,
                actor_user_id=workspace.owner_user_id,
                actor=f"{workspace.owner_user_id}@example.test",
                reason="Operator revoked the lease",
                now=now_utc(),
                trust_event_repository=repositories.trust_events,
            )
    revoked_retry = client.post(
        f"/agent/runs/{run_id}/capability-lease",
        json={"capability_lease_id": lease.id},
    )
    assert revoked_retry.status_code == 409


@pytest.mark.parametrize(
    "unsupported_step_type",
    [
        AgentRunStepType.APPLY_CHANGES,
        AgentRunStepType.RUN_VALIDATION,
        AgentRunStepType.CREATE_PULL_REQUEST,
    ],
)
def test_postgres_agent_run_rejects_unsupported_side_effect_before_claim_or_trust_mutation(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
    unsupported_step_type: AgentRunStepType,
) -> None:
    """Unsupported PostgreSQL side effects fail closed before execution or trust writes."""
    import app.agent_run_routes as agent_run_routes

    def unexpected_execute(*_args, **_kwargs):
        raise AssertionError("PostgreSQL side-effect rejection reached AgentRun execution")

    monkeypatch.setattr(agent_run_routes, "execute_next_step", unexpected_execute)
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        state=state,
        client=client,
    )
    attached = client.post(
        f"/agent/runs/{run_id}/capability-lease",
        json={"capability_lease_id": lease.id},
    )
    assert attached.status_code == 200, attached.text

    run = repositories.agent_runs.get(run_id)
    assert run is not None
    steps = repositories.agent_runs.list_steps_for_run(run_id)
    step = steps[0]
    # Put the durable fixture at an externally-effectful step that has no
    # dedicated PostgreSQL executor. The route must reject it before acquiring
    # a worker lease or touching trust state. CREATE_BRANCH has its own guarded
    # PostgreSQL path and is covered separately.
    run.current_step_index = step.sequence
    run.updated_at = now_utc()
    step.step_type = unsupported_step_type
    step.status = AgentRunStepStatus.READY
    step.updated_at = run.updated_at
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET updated_at=%s,payload=%s "
            "WHERE run_id=%s AND workspace_id=%s",
            (run.updated_at, Jsonb(run.model_dump(mode="json")), run_id, workspace.id),
        )
        conn.execute(
            "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
            "WHERE step_id=%s AND run_id=%s AND workspace_id=%s",
            (
                step.status.value,
                step.updated_at,
                Jsonb(step.model_dump(mode="json")),
                step.id,
                run_id,
                workspace.id,
            ),
        )

    before_run = repositories.agent_runs.get(run_id)
    before_steps = repositories.agent_runs.list_steps_for_run(run_id)
    with connect() as conn:
        trust_counts_before = conn.execute(
            "SELECT (SELECT count(*) FROM action_consumptions WHERE workspace_id=%s), "
            "(SELECT count(*) FROM trust_events WHERE workspace_id=%s), "
            "(SELECT count(*) FROM policy_decisions WHERE workspace_id=%s)",
            (workspace.id, workspace.id, workspace.id),
        ).fetchone()
        run_events_before = conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id=%s", (run_id,)
        ).fetchone()[0]
        audits_before = conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s", (run_id,)
        ).fetchone()[0]

    rejected = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-side-effect-guard", "timeout_seconds": 10},
    )

    assert rejected.status_code == 409
    assert "PostgreSQL" in rejected.json()["detail"]
    assert repositories.agent_runs.get(run_id) == before_run
    assert repositories.agent_runs.list_steps_for_run(run_id) == before_steps
    with connect() as conn:
        assert conn.execute(
            "SELECT (SELECT count(*) FROM action_consumptions WHERE workspace_id=%s), "
            "(SELECT count(*) FROM trust_events WHERE workspace_id=%s), "
            "(SELECT count(*) FROM policy_decisions WHERE workspace_id=%s)",
            (workspace.id, workspace.id, workspace.id),
        ).fetchone() == trust_counts_before
        assert conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id=%s", (run_id,)
        ).fetchone()[0] == run_events_before
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s", (run_id,)
        ).fetchone()[0] == audits_before


@pytest.mark.parametrize("bundle_state", ["missing", "mismatched"])
def test_postgres_create_branch_fails_closed_without_lifespan_owned_repository_bundle(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
    bundle_state: str,
) -> None:
    """CREATE_BRANCH must not execute through partial or mismatched app bindings."""
    import app.agent_run_routes as agent_run_routes

    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        state=state,
        client=client,
    )
    attached = client.post(
        f"/agent/runs/{run_id}/capability-lease",
        json={"capability_lease_id": lease.id},
    )
    assert attached.status_code == 200, attached.text

    run = repositories.agent_runs.get(run_id)
    assert run is not None
    step = repositories.agent_runs.list_steps_for_run(run_id)[0]
    run.current_step_index = step.sequence
    run.updated_at = now_utc()
    step.step_type = AgentRunStepType.CREATE_BRANCH
    step.status = AgentRunStepStatus.READY
    step.updated_at = run.updated_at
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET updated_at=%s,payload=%s "
            "WHERE run_id=%s AND workspace_id=%s",
            (run.updated_at, Jsonb(run.model_dump(mode="json")), run_id, workspace.id),
        )
        conn.execute(
            "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
            "WHERE step_id=%s AND run_id=%s AND workspace_id=%s",
            (
                step.status.value,
                step.updated_at,
                Jsonb(step.model_dump(mode="json")),
                step.id,
                run_id,
                workspace.id,
            ),
        )

    before_run = repositories.agent_runs.get(run_id)
    before_steps = repositories.agent_runs.list_steps_for_run(run_id)
    with connect() as conn:
        intents_before = conn.execute(
            "SELECT count(*) FROM agent_run_branch_intents WHERE run_id=%s", (run_id,)
        ).fetchone()[0]

    def unexpected_execution(*_args, **_kwargs):
        raise AssertionError("Missing repository authority reached AgentRun execution")

    def unexpected_provider(*_args, **_kwargs):
        raise AssertionError("Missing repository authority initialized a provider client")

    monkeypatch.setattr(agent_run_routes, "execute_next_step", unexpected_execution)
    monkeypatch.setattr(
        "app.postgres_agent_run_branch_route._default_client", unexpected_provider
    )
    monkeypatch.setattr(
        main.app.state, "postgres_repositories", repositories, raising=False
    )
    if bundle_state == "mismatched":
        monkeypatch.setattr(
            main.app.state, "postgres_runtime", SimpleNamespace(repositories=object()), raising=False
        )
    else:
        monkeypatch.setattr(main.app.state, "postgres_runtime", None, raising=False)

    rejected = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-branch-authority-guard", "timeout_seconds": 10},
    )

    assert rejected.status_code == 503, rejected.text
    assert "lifespan-owned repository bundle" in rejected.json()["detail"]
    assert repositories.agent_runs.get(run_id) == before_run
    assert repositories.agent_runs.list_steps_for_run(run_id) == before_steps
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_run_branch_intents WHERE run_id=%s", (run_id,)
        ).fetchone()[0] == intents_before


def test_postgres_create_branch_route_commits_verified_branch_and_task_records_atomically(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP route uses one lifespan-owned PostgreSQL bundle end to end."""
    from app.agent_run_models import AgentRunStatus
    from app.agent_worker_github_branch import GitHubBranchObservation
    from app.postgres_agent_run_branch import BranchIntentStatus
    from app.postgres_agent_run_branch_executor import PostgresCreateBranchCoordinator
    from app.postgres_runtime import PostgresRuntime

    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)

    # The capability lease, task branch, and approved payload must all bind the
    # same target ref before the durable run is created.
    with connect() as conn:
        approval_row = conn.execute(
            "SELECT payload FROM approval_requests WHERE approval_id=("
            "SELECT approval_request_id FROM agent_tasks WHERE task_id=%s) AND workspace_id=%s",
            (task_id, workspace.id),
        ).fetchone()
        approval = ApprovalRequest.model_validate(approval_row[0])
        approval.target_branch = "feature/approved-branch"
        approval.approved_payload_hash = compute_approval_payload_hash(approval)
        conn.execute(
            "UPDATE approval_requests SET payload=%s WHERE approval_id=%s AND workspace_id=%s",
            (Jsonb(approval.model_dump(mode="json")), approval.id, workspace.id),
        )

    run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        state=state,
        client=client,
    )
    attached = client.post(
        f"/agent/runs/{run_id}/capability-lease",
        json={"capability_lease_id": lease.id},
    )
    assert attached.status_code == 200, attached.text

    # Position a valid durable run at its final branch step. Earlier validation
    # steps are already complete, so this single route invocation can reach a
    # terminal run without pretending the remaining phases were executed.
    run = repositories.agent_runs.get(run_id)
    assert run is not None
    steps = repositories.agent_runs.list_steps_for_run(run_id)
    branch_step = steps[-1]
    run.status = AgentRunStatus.RUNNING
    run.current_step_index = branch_step.sequence
    run.current_step_id = branch_step.id
    run.updated_at = now_utc()
    for step in steps[:-1]:
        step.status = AgentRunStepStatus.SUCCEEDED
        step.updated_at = run.updated_at
    branch_step.step_type = AgentRunStepType.CREATE_BRANCH
    branch_step.status = AgentRunStepStatus.READY
    branch_step.updated_at = run.updated_at
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET status=%s,current_step_id=%s,updated_at=%s,payload=%s "
            "WHERE run_id=%s AND workspace_id=%s",
            (
                run.status.value,
                run.current_step_id,
                run.updated_at,
                Jsonb(run.model_dump(mode="json")),
                run_id,
                workspace.id,
            ),
        )
        for step in steps:
            conn.execute(
                "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
                "WHERE step_id=%s AND run_id=%s AND workspace_id=%s",
                (
                    step.status.value,
                    step.updated_at,
                    Jsonb(step.model_dump(mode="json")),
                    step.id,
                    run_id,
                    workspace.id,
                ),
            )

    # Use a real runtime object around the fixture's schema-scoped connector,
    # then bind every route-visible repository to that exact bundle identity.
    runtime = PostgresRuntime(
        _dsn="postgresql://test-runtime/agent-run-routes",
        profile="test",
        _connector=connect,
    )
    runtime_repositories = runtime.repositories
    assert runtime_repositories.agent_run_branch_intents is not repositories.agent_run_branch_intents
    monkeypatch.setattr(main.app.state, "postgres_runtime", runtime, raising=False)
    monkeypatch.setattr(
        main.app.state, "postgres_repositories", runtime_repositories, raising=False
    )
    for state_name, repository_name in {
        "agent_run_repository": "agent_runs",
        "agent_task_repository": "agent_tasks",
        "approval_decision_repository": "approval_decisions",
        "trust_repository": "trust",
        "trust_event_repository": "trust_events",
        "agent_run_branch_intent_repository": "agent_run_branch_intents",
    }.items():
        monkeypatch.setattr(
            main.app.state,
            state_name,
            getattr(runtime_repositories, repository_name),
            raising=False,
        )

    source_sha = "a" * 40

    class FakeGitHubProvider:
        def __init__(self) -> None:
            self.post_calls: list[tuple[str, str, str]] = []
            self.observations: list[tuple[str, str]] = []

        def observe_branch(self, repository: str, branch: str) -> GitHubBranchObservation:
            self.observations.append((repository, branch))
            if branch == "main":
                return GitHubBranchObservation(
                    repository=repository,
                    branch=branch,
                    found=True,
                    sha=source_sha,
                    html_url=f"https://github.com/{repository}/tree/main",
                )
            if self.post_calls:
                return GitHubBranchObservation(
                    repository=repository,
                    branch=branch,
                    found=True,
                    sha=source_sha,
                    html_url=f"https://github.com/{repository}/tree/{branch}",
                )
            return GitHubBranchObservation(
                repository=repository,
                branch=branch,
                found=False,
            )

        def create_branch(self, repository: str, branch: str, pinned_sha: str) -> None:
            self.post_calls.append((repository, branch, pinned_sha))

    provider = FakeGitHubProvider()
    monkeypatch.setattr(
        "app.postgres_agent_run_branch_route._default_client", lambda: provider
    )
    branch_errors: list[str] = []
    original_execute = PostgresCreateBranchCoordinator.execute

    def capture_branch_error(coordinator, branch_request):
        try:
            return original_execute(coordinator, branch_request)
        except Exception as error:
            branch_errors.append(f"{type(error).__name__}: {error}")
            raise

    monkeypatch.setattr(PostgresCreateBranchCoordinator, "execute", capture_branch_error)
    run_ids_before = set(store.agent_runs)
    step_ids_before = set(store.agent_run_steps)
    task_snapshot_before = store.agent_tasks.get(task_id)
    task_event_ids_before = set(store.agent_task_events)
    audit_ids_before = {event.id for event in store.audit_events}
    approval_snapshot_before = store.approval_requests.get(approval.id)

    executed = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-branch-route-test", "timeout_seconds": 10},
    )

    assert executed.status_code == 200, f"{executed.text}; branch_errors={branch_errors!r}"
    final_run = runtime_repositories.agent_runs.get(run_id)
    final_steps = runtime_repositories.agent_runs.list_steps_for_run(run_id)
    assert final_run is not None
    completed_step = next(step for step in final_steps if step.id == branch_step.id)
    run_events = runtime_repositories.agent_runs.list_events_for_run(run_id)
    assert final_run.status == AgentRunStatus.SUCCEEDED, (
        f"run_status={final_run.status.value}; step_status={completed_step.status.value}; "
        f"step_reason={completed_step.reason!r}; evidence={completed_step.evidence!r}; "
        f"branch_errors={branch_errors!r}; "
        f"events={[event.model_dump(mode='json') for event in run_events]!r}"
    )
    assert completed_step.status == AgentRunStepStatus.SUCCEEDED
    assert completed_step.output_reference == (
        "https://github.com/owner/approved-repository/tree/feature/approved-branch"
    )

    intent = runtime_repositories.agent_run_branch_intents.get_for_step(
        workspace_id=workspace.id,
        run_id=run_id,
        step_id=branch_step.id,
        idempotency_key=branch_step.idempotency_key,
    )
    assert intent is not None and intent.status == BranchIntentStatus.COMPLETED
    assert intent.source_sha == source_sha
    assert provider.post_calls == [
        ("owner/approved-repository", "feature/approved-branch", source_sha)
    ]
    assert provider.observations.count(("owner/approved-repository", "main")) == 2
    assert provider.observations.count(
        ("owner/approved-repository", "feature/approved-branch")
    ) == 2

    task = runtime_repositories.agent_tasks.get(task_id)
    assert task is not None and task.status == AgentTaskStatus.APPROVED
    task_events = runtime_repositories.agent_tasks.list_events(
        task_id, workspace_id=workspace.id
    )
    branch_events = [
        event
        for event in task_events
        if event.metadata.get("branch_intent_id") == intent.id
    ]
    assert len(branch_events) == 1
    assert branch_events[0].event_type == AgentTaskEventType.NOTE
    assert branch_events[0].actor == "postgres-branch-route-test"
    assert branch_events[0].metadata["source_sha"] == source_sha

    success_events = [
        event
        for event in run_events
        if event.step_id == branch_step.id
        and event.event_type == AgentRunEventType.STEP_SUCCEEDED
    ]
    assert len(success_events) == 1
    assert success_events[0].metadata["branch_intent_id"] == intent.id
    with connect() as conn:
        task_audits = conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_type='agent_task' "
            "AND entity_id=%s AND event_type='agent_task.branch_created'",
            (workspace.id, task_id),
        ).fetchone()[0]
        run_audits = conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_type='agent_run' "
            "AND entity_id=%s AND event_type='agent_run.step_completed'",
            (workspace.id, run_id),
        ).fetchone()[0]
        persisted_task_status = conn.execute(
            "SELECT status FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
            (task_id, workspace.id),
        ).fetchone()[0]
    assert task_audits == 1
    assert run_audits == 1
    assert persisted_task_status == AgentTaskStatus.APPROVED.value

    # The shared supervisor still publishes AgentRun compatibility snapshots
    # into process-local maps. Task, approval, task-event, and audit state must
    # remain PostgreSQL-authoritative and unchanged in those SQLite maps.
    assert run_id in store.agent_runs
    assert store.agent_runs[run_id].status == final_run.status
    assert store.agent_runs[run_id].updated_at == final_run.updated_at
    assert set(store.agent_runs) == run_ids_before | {run_id}
    assert set(store.agent_run_steps) == step_ids_before | {step.id for step in steps}
    assert store.agent_tasks.get(task_id) == task_snapshot_before
    assert set(store.agent_task_events) == task_event_ids_before
    projected_audits = [
        event for event in store.audit_events if event.id not in audit_ids_before
    ]
    assert len(projected_audits) == 1
    assert projected_audits[0].event_type == "agent_run.step_started"
    assert projected_audits[0].actor == "postgres-branch-route-test"
    assert len(store.audit_events) == len(audit_ids_before) + 1
    assert store.approval_requests.get(approval.id) == approval_snapshot_before


@pytest.mark.parametrize(
    ("rollback_completion", "process_claim_race"),
    [(False, False), (True, False), (False, True)],
)
def test_postgres_apply_changes_route_is_atomic_and_recovers_without_second_write(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
    rollback_completion: bool,
    process_claim_race: bool,
) -> None:
    """Exercise durable approval, one GitHub mutation, and atomic PG outcome/recovery."""
    import base64
    from app.agent_run_models import AgentRunStatus
    from app.postgres_agent_run_branch import AgentRunBranchIntent, BranchIntentStatus
    from app.postgres_runtime import PostgresRuntime

    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    path = "src/approved.txt"
    content = "durably approved file content\n"
    source_sha = "a" * 40
    commit_sha = "b" * 40
    blob_sha = "c" * 40

    # Bind the owner-approved payload to one create operation before issuing
    # its capability lease and creating the AgentRun.
    with connect() as conn:
        approval_row = conn.execute(
            "SELECT payload FROM approval_requests WHERE approval_id=("
            "SELECT approval_request_id FROM agent_tasks WHERE task_id=%s) AND workspace_id=%s",
            (task_id, workspace.id),
        ).fetchone()
        approval = ApprovalRequest.model_validate(approval_row[0])
        approval.target_branch = "feature/approved-branch"
        approval.files = [FileChange(
            path=path, change_type="create", diff="+approved\n", new_content=content,
        )]
        approval.approved_payload_hash = compute_approval_payload_hash(approval)
        conn.execute(
            "UPDATE approval_requests SET payload=%s WHERE approval_id=%s AND workspace_id=%s",
            (Jsonb(approval.model_dump(mode="json")), approval.id, workspace.id),
        )

    run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect, workspace=workspace, task_id=task_id, state=state, client=client,
        lease_actions=[CapabilityActionType.CREATE_BRANCH, CapabilityActionType.MODIFY_FILES],
        allowed_path_prefixes=["src"],
    )
    attached = client.post(
        f"/agent/runs/{run_id}/capability-lease",
        json={"capability_lease_id": lease.id},
    )
    assert attached.status_code == 200, attached.text

    run = repositories.agent_runs.get(run_id)
    assert run is not None
    steps = repositories.agent_runs.list_steps_for_run(run_id)
    branch_step = next(item for item in steps if item.step_type == AgentRunStepType.CREATE_BRANCH)
    apply_step = next(item for item in steps if item.step_type == AgentRunStepType.APPLY_CHANGES)
    run.status = AgentRunStatus.RUNNING
    run.current_step_id = apply_step.id
    run.current_step_index = apply_step.sequence
    run.updated_at = now_utc()
    for item in steps:
        item.updated_at = run.updated_at
        if item.sequence < apply_step.sequence:
            item.status = AgentRunStepStatus.SUCCEEDED
        elif item.id == apply_step.id:
            item.status = AgentRunStepStatus.READY
    branch_intent = AgentRunBranchIntent(
        id=f"branch_intent_{uuid.uuid4().hex}", workspace_id=workspace.id,
        run_id=run_id, task_id=task_id, approval_id=approval.id, step_id=branch_step.id,
        action_id=f"branch_action_{uuid.uuid4().hex}", lease_id=lease.id,
        idempotency_key=branch_step.idempotency_key,
        repository="owner/approved-repository", source_branch="main",
        target_ref="feature/approved-branch", source_sha=source_sha,
        lease_token_hash="d" * 64, initial_lease_token_hash="d" * 64,
        run_revision=run.updated_at, step_revision=branch_step.updated_at,
        status=BranchIntentStatus.COMPLETED,
        branch_url="https://github.com/owner/approved-repository/tree/feature/approved-branch",
        reason="Approved branch fixture", created_at=run.updated_at, updated_at=run.updated_at,
    )
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET status=%s,current_step_id=%s,updated_at=%s,payload=%s "
            "WHERE run_id=%s AND workspace_id=%s",
            (run.status.value, run.current_step_id, run.updated_at,
             Jsonb(run.model_dump(mode="json")), run_id, workspace.id),
        )
        for item in steps:
            conn.execute(
                "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
                "WHERE step_id=%s AND run_id=%s AND workspace_id=%s",
                (item.status.value, item.updated_at, Jsonb(item.model_dump(mode="json")),
                 item.id, run_id, workspace.id),
            )
        conn.execute(
            "INSERT INTO agent_run_branch_intents("
            "intent_id,workspace_id,run_id,task_id,approval_id,step_id,action_id,lease_id,"
            "idempotency_key,repository,source_branch,target_ref,source_sha,lease_token_hash,"
            "initial_lease_token_hash,run_revision,step_revision,status,branch_url,reason,created_at,updated_at,payload"
            ") VALUES(" + ",".join(["%s"] * 22) + ",%s)",
            (
                branch_intent.id, branch_intent.workspace_id, branch_intent.run_id,
                branch_intent.task_id, branch_intent.approval_id, branch_intent.step_id,
                branch_intent.action_id, branch_intent.lease_id, branch_intent.idempotency_key,
                branch_intent.repository, branch_intent.source_branch, branch_intent.target_ref,
                branch_intent.source_sha, branch_intent.lease_token_hash,
                branch_intent.initial_lease_token_hash, branch_intent.run_revision,
                branch_intent.step_revision, branch_intent.status.value, branch_intent.branch_url,
                branch_intent.reason, branch_intent.created_at, branch_intent.updated_at,
                Jsonb(branch_intent.model_dump(mode="json")),
            ),
        )

    runtime = PostgresRuntime(
        _dsn="postgresql://test-runtime/agent-run-routes", profile="test", _connector=connect,
    )
    runtime_repositories = runtime.repositories
    monkeypatch.setattr(main.app.state, "postgres_runtime", runtime, raising=False)
    monkeypatch.setattr(main.app.state, "postgres_repositories", runtime_repositories, raising=False)
    for state_name, repository_name in {
        "agent_run_repository": "agent_runs",
        "agent_task_repository": "agent_tasks",
        "approval_decision_repository": "approval_decisions",
        "trust_repository": "trust",
        "trust_event_repository": "trust_events",
        "agent_run_branch_intent_repository": "agent_run_branch_intents",
    }.items():
        monkeypatch.setattr(main.app.state, state_name, getattr(runtime_repositories, repository_name), raising=False)

    monkeypatch.setenv("AIXION_POSTGRES_APPLY_CHANGES_ENABLED", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    http_calls: list[tuple[str, dict]] = []
    remote_written = False
    intent_marker = ""

    class FakeResponse:
        def __init__(self, status_code: int, payload: object) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url: str, **kwargs):
        nonlocal remote_written
        http_calls.append(("GET", {"url": url, **kwargs}))
        if "/git/ref/heads/" in url:
            return FakeResponse(200, {"object": {"sha": source_sha}})
        if "/contents/" in url:
            requested_ref = kwargs.get("params", {}).get("ref")
            if not remote_written or requested_ref == source_sha:
                return FakeResponse(404, {})
            return FakeResponse(200, {
                "type": "file", "sha": blob_sha,
                "content": base64.b64encode(content.encode()).decode(),
            })
        if url.endswith("/commits"):
            return FakeResponse(200, [{
                "sha": commit_sha,
                "commit": {"message": f"approved [aixion-apply-intent:{intent_marker}]"},
            }])
        raise AssertionError(f"Unexpected GitHub GET: {url}")

    def fake_post(url: str, **kwargs):
        nonlocal remote_written, intent_marker
        http_calls.append(("POST", {"url": url, **kwargs}))
        assert url == "https://api.github.com/graphql"
        mutation_input = kwargs["json"]["variables"]["input"]
        intent_marker = mutation_input["clientMutationId"]
        assert mutation_input["expectedHeadOid"] == source_sha
        assert mutation_input["fileChanges"]["additions"] == [{
            "path": path, "contents": base64.b64encode(content.encode()).decode(),
        }]
        remote_written = True
        return FakeResponse(200, {"data": {"createCommitOnBranch": {
            "commit": {"oid": commit_sha,
                       "message": f"approved [aixion-apply-intent:{intent_marker}]"},
            "ref": {"target": {"oid": commit_sha}},
        }}})

    monkeypatch.setattr("app.postgres_agent_run_apply_changes_route.httpx.get", fake_get)
    monkeypatch.setattr("app.postgres_agent_run_apply_changes_route.httpx.post", fake_post)

    process_claim_results: list[tuple[str, int, str | None, str | None]] = []
    process_claim_context: dict[str, object] = {}
    original_claim = runtime_repositories.agent_run_apply_change_intents.claim_write_once

    def race_process_claims(*, intent, expected_run, expected_step, worker_lease_token, now):
        # This hook runs only after the route has durably recorded its approved
        # intent, action consumption, and current worker lease. The two spawned
        # workers use independent connections and repository instances.
        with connect() as conn:
            schema = conn.execute("SELECT current_schema()").fetchone()[0]
        dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
        assert dsn and schema
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        workers = [
            context.Process(
                target=_claim_apply_change_intent_in_process,
                args=(
                    dsn,
                    schema,
                    intent.model_dump(mode="json"),
                    expected_run.model_dump(mode="json"),
                    expected_step.model_dump(mode="json"),
                    worker_lease_token,
                    barrier,
                    results,
                ),
            )
            for _ in range(2)
        ]
        try:
            for worker in workers:
                worker.start()
            received = []
            for _ in workers:
                try:
                    received.append(results.get(timeout=30))
                except queue_module.Empty:
                    pytest.fail("Timed out waiting for APPLY_CHANGES claim processes")
            for worker in workers:
                worker.join(timeout=10)
                if worker.is_alive():
                    pytest.fail("APPLY_CHANGES claim process did not exit before timeout")
            assert [worker.exitcode for worker in workers] == [0, 0]
            assert len({result[1] for result in received}) == 2
            assert sorted(result[0] for result in received) == ["claimed", "conflict"]
            assert all(result[2] == ApplyChangeIntentStatus.POST_CLAIMED.value for result in received)
            assert all(result[3] is None for result in received if result[0] == "claimed")
            assert all(result[3] for result in received if result[0] == "conflict")
            process_claim_results.extend(received)
            process_claim_context.update(
                {
                    "intent": intent,
                    "expected_run": expected_run,
                    "expected_step": expected_step,
                    "worker_lease_token": worker_lease_token,
                }
            )
            durable = runtime_repositories.agent_run_apply_change_intents.get_for_step(
                workspace_id=intent.workspace_id,
                run_id=intent.run_id,
                step_id=intent.step_id,
                idempotency_key=intent.idempotency_key,
            )
            assert durable is not None
            assert durable.status == ApplyChangeIntentStatus.POST_CLAIMED
            # Stop the route before its provider write: the two-process race
            # itself owns the durable one-shot claim in this test variant.
            raise RuntimeError("Cross-process claim race completed; provider write suppressed")
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=5)
            results.close()
            results.join_thread()

    if process_claim_race:
        monkeypatch.setattr(
            runtime_repositories.agent_run_apply_change_intents,
            "claim_write_once",
            race_process_claims,
        )
    original_commit = runtime_repositories.agent_runs.commit_step_completion
    if rollback_completion:
        first_apply_commit = True

        def fail_first_apply_commit(**kwargs):
            nonlocal first_apply_commit
            if kwargs.get("apply_change_intent_id") and first_apply_commit:
                first_apply_commit = False
                raise RuntimeError("simulated outcome transaction rollback")
            return original_commit(**kwargs)

        monkeypatch.setattr(runtime_repositories.agent_runs, "commit_step_completion", fail_first_apply_commit)

    task_snapshot_before = store.agent_tasks.get(task_id)
    task_event_ids_before = set(store.agent_task_events)
    approval_snapshot_before = store.approval_requests.get(approval.id)
    first = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-apply-route-test", "timeout_seconds": 10},
    )
    assert first.status_code == 200, first.text

    if process_claim_race:
        assert len(process_claim_results) == 2
        assert sorted(result[0] for result in process_claim_results) == ["claimed", "conflict"]
        persisted = runtime_repositories.agent_run_apply_change_intents.get_for_step(
            workspace_id=workspace.id,
            run_id=run_id,
            step_id=apply_step.id,
            idempotency_key=apply_step.idempotency_key,
        )
        assert persisted is not None
        assert persisted.status == ApplyChangeIntentStatus.POST_CLAIMED
        assert not any(method == "POST" for method, _call in http_calls)
        with pytest.raises(PostgresTrustRepositoryConflict):
            original_claim(
                intent=process_claim_context["intent"],
                expected_run=process_claim_context["expected_run"],
                expected_step=process_claim_context["expected_step"],
                worker_lease_token=process_claim_context["worker_lease_token"],
                now=now_utc(),
            )
        assert runtime_repositories.agent_run_apply_change_intents.get_for_step(
            workspace_id=workspace.id,
            run_id=run_id,
            step_id=apply_step.id,
            idempotency_key=apply_step.idempotency_key,
        ).status == ApplyChangeIntentStatus.POST_CLAIMED
        assert not any(method == "POST" for method, _call in http_calls)
        return

    intents = runtime_repositories.agent_run_apply_change_intents
    persisted = intents.get_for_step(
        workspace_id=workspace.id, run_id=run_id, step_id=apply_step.id,
        idempotency_key=apply_step.idempotency_key,
    )
    assert persisted is not None
    if rollback_completion:
        assert persisted.status == ApplyChangeIntentStatus.REMOTE_VERIFIED
        # A process restart loses the original runtime/repository objects. Build
        # a fresh lifespan-style bundle over the same durable schema before
        # retrying; recovery must discover the remote commit with GETs only.
        runtime_before_restart = runtime
        runtime_after_restart = PostgresRuntime(
            _dsn="postgresql://test-runtime/agent-run-routes-restarted",
            profile="test",
            _connector=connect,
        )
        restarted_repositories = runtime_after_restart.repositories
        assert restarted_repositories is not runtime_repositories
        monkeypatch.setattr(
            main.app.state, "postgres_runtime", runtime_after_restart, raising=False
        )
        monkeypatch.setattr(
            main.app.state, "postgres_repositories", restarted_repositories, raising=False
        )
        for state_name, repository_name in {
            "agent_run_repository": "agent_runs",
            "agent_task_repository": "agent_tasks",
            "approval_decision_repository": "approval_decisions",
            "trust_repository": "trust",
            "trust_event_repository": "trust_events",
            "agent_run_branch_intent_repository": "agent_run_branch_intents",
        }.items():
            monkeypatch.setattr(
                main.app.state,
                state_name,
                getattr(restarted_repositories, repository_name),
                raising=False,
            )
        assert main.app.state.postgres_runtime is not runtime_before_restart
        assert restarted_repositories.agent_runs.get(run_id) is not None
        restarted_intents = restarted_repositories.agent_run_apply_change_intents
        restarted_intent = restarted_intents.get_for_step(
            workspace_id=workspace.id,
            run_id=run_id,
            step_id=apply_step.id,
            idempotency_key=apply_step.idempotency_key,
        )
        assert restarted_intent is not None
        assert restarted_intent.status == ApplyChangeIntentStatus.REMOTE_VERIFIED
        retry = client.post(
            f"/agent/runs/{run_id}/retry",
            json={"step_id": apply_step.id, "reason": "Recover verified outcome"},
        )
        assert retry.status_code == 200, retry.text
        calls_before_recovery = len(http_calls)
        recovered = client.post(
            f"/agent/runs/{run_id}/execute-next",
            json={"worker_id": "postgres-apply-recovery-test", "timeout_seconds": 10},
        )
        assert recovered.status_code == 200, recovered.text
        recovery_calls = http_calls[calls_before_recovery:]
        assert recovery_calls
        assert all(method == "GET" for method, _call in recovery_calls)
        runtime_repositories = restarted_repositories
        intents = restarted_intents
    assert sum(method == "POST" for method, _call in http_calls) == 1
    assert intent_marker == persisted.id

    final_run = runtime_repositories.agent_runs.get(run_id)
    final_steps = runtime_repositories.agent_runs.list_steps_for_run(run_id)
    completed_step = next(item for item in final_steps if item.id == apply_step.id)
    run_events = runtime_repositories.agent_runs.list_events_for_run(run_id)
    assert final_run is not None and final_run.status == AgentRunStatus.RUNNING
    assert completed_step.status == AgentRunStepStatus.SUCCEEDED
    assert completed_step.output_reference == commit_sha
    completed_intent = intents.get_for_step(
        workspace_id=workspace.id, run_id=run_id, step_id=apply_step.id,
        idempotency_key=apply_step.idempotency_key,
    )
    assert completed_intent is not None
    assert completed_intent.status == ApplyChangeIntentStatus.COMPLETED
    assert completed_intent.commit_sha == commit_sha
    assert sum(
        event.step_id == apply_step.id and event.event_type == AgentRunEventType.STEP_SUCCEEDED
        for event in run_events
    ) == 1
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_type='agent_run' "
            "AND entity_id=%s AND event_type='agent_run.step_completed' "
            "AND details->>'apply_change_intent_id'=%s",
            (workspace.id, run_id, persisted.id),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM action_consumptions WHERE workspace_id=%s",
            (workspace.id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT status FROM agent_run_apply_change_intents WHERE intent_id=%s",
            (persisted.id,),
        ).fetchone()[0] == ApplyChangeIntentStatus.COMPLETED.value
    assert store.agent_tasks.get(task_id) == task_snapshot_before
    assert set(store.agent_task_events) == task_event_ids_before
    assert store.approval_requests.get(approval.id) == approval_snapshot_before


def test_postgres_credential_revocation_route_uses_durable_workspace_state(
    postgres_agent_run_route_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect, workspace, _project, other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    _run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        state=state,
        client=client,
    )
    grant = CredentialGrant(
        lease_id=lease.id,
        workspace_id=workspace.id,
        grant_type=CredentialGrantType.INTERNAL_CAPABILITY_TOKEN,
        audience="runner",
        subject="agent:codex",
        scope=["run:execute"],
        expires_at=now_utc() + timedelta(minutes=5),
        token_hash=uuid.uuid4().hex,
    )
    with connect() as conn:
        with conn.transaction():
            repositories.trust.insert_credential_grant(conn, grant)

    runtime = SimpleNamespace(connect=connect)
    for name, value in {
        "postgres_runtime": runtime,
        "postgres_repositories": repositories,
        "trust_repository": repositories.trust,
        "trust_event_repository": repositories.trust_events,
    }.items():
        monkeypatch.setattr(main.app.state, name, value, raising=False)
    monkeypatch.setitem(
        main.app.dependency_overrides,
        require_owner,
        lambda: state["current_user"]["value"],
    )
    sqlite_grants_before = dict(store.credential_grants)
    sqlite_events_before = dict(store.trust_events)

    response = client.post(
        f"/trust/credentials/{grant.id}/revoke",
        json={"reason": "Owner revoked the credential"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["revoked"] is True
    assert response.json()["metadata"]["revoked_by_user_id"] == state["users"][0].id
    with connect() as conn:
        persisted = repositories.trust.get_credential_grant(conn, grant.id, workspace.id)
        assert persisted is not None and persisted.revoked
        assert (
            conn.execute(
                "SELECT count(*) FROM trust_events WHERE entity_id=%s AND event_type=%s",
                (grant.id, TrustEventType.CREDENTIAL_GRANT_REVOKED.value),
            ).fetchone()[0]
            == 1
        )
    assert store.credential_grants == sqlite_grants_before
    assert store.trust_events == sqlite_events_before

    state["current_user"]["value"] = state["users"][1]
    hidden = client.post(
        f"/trust/credentials/{grant.id}/revoke",
        json={"reason": "Other workspace revoke attempt"},
    )
    assert hidden.status_code == 404
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM trust_events WHERE entity_id=%s AND event_type=%s",
                (grant.id, TrustEventType.CREDENTIAL_GRANT_REVOKED.value),
            ).fetchone()[0]
            == 1
        )

    state["current_user"]["value"] = state["users"][0]
    monkeypatch.setattr(main.app.state, "trust_event_repository", object())
    partial_runtime = client.post(
        f"/trust/credentials/{grant.id}/revoke",
        json={"reason": "Retry with partial persistence"},
    )
    assert partial_runtime.status_code == 409
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM trust_events WHERE entity_id=%s AND event_type=%s",
                (grant.id, TrustEventType.CREDENTIAL_GRANT_REVOKED.value),
            ).fetchone()[0]
            == 1
        )


def test_postgres_credential_issue_introspect_and_revoke_are_durable(
    postgres_agent_run_route_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    _run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        state=state,
        client=client,
    )
    runtime = SimpleNamespace(connect=connect)
    for name, value in {
        "postgres_runtime": runtime,
        "postgres_repositories": repositories,
        "trust_repository": repositories.trust,
        "trust_event_repository": repositories.trust_events,
    }.items():
        monkeypatch.setattr(main.app.state, name, value, raising=False)
    monkeypatch.setitem(
        main.app.dependency_overrides,
        require_owner,
        lambda: state["current_user"]["value"],
    )
    sqlite_grants_before = dict(store.credential_grants)
    sqlite_events_before = dict(store.trust_events)

    issued = client.post(
        "/trust/credentials",
        json={
            "lease_id": lease.id,
            "audience": "runner",
            "subject": "agent:codex",
            "scope": ["run:execute"],
            "expires_in_seconds": 240,
        },
    )
    assert issued.status_code == 200, issued.text
    issued_payload = issued.json()
    raw_token = issued_payload["bearer_token"]
    grant_id = issued_payload["grant"]["id"]
    assert raw_token and issued_payload["grant"]["revoked"] is False
    assert "bearer_token" not in issued_payload["grant"]
    with connect() as conn:
        row = conn.execute(
            "SELECT token_hash,payload FROM credential_grants WHERE grant_id=%s AND workspace_id=%s",
            (grant_id, workspace.id),
        ).fetchone()
        assert row is not None
        assert row[0] != raw_token
        assert row[1]["token_hash"] == row[0]
        assert "bearer_token" not in row[1]

    active = client.post(
        "/trust/credentials/introspect",
        json={
            "bearer_token": raw_token,
            "audience": "runner",
            "required_scope": ["run:execute"],
        },
    )
    assert active.status_code == 200, active.text
    assert active.json()["active"] is True
    insufficient_scope = client.post(
        "/trust/credentials/introspect",
        json={
            "bearer_token": raw_token,
            "audience": "runner",
            "required_scope": ["admin:all"],
        },
    )
    assert insufficient_scope.status_code == 200
    assert insufficient_scope.json()["active"] is False

    revoked = client.post(
        f"/trust/credentials/{grant_id}/revoke",
        json={"reason": "Owner revoked the credential"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked"] is True
    inactive = client.post(
        "/trust/credentials/introspect",
        json={
            "bearer_token": raw_token,
            "audience": "runner",
            "required_scope": ["run:execute"],
        },
    )
    assert inactive.status_code == 200
    assert inactive.json()["active"] is False
    assert inactive.json()["reason"] == "Credential grant was revoked."
    assert store.credential_grants == sqlite_grants_before
    assert store.trust_events == sqlite_events_before

    monkeypatch.setattr(main.app.state, "trust_event_repository", object())
    unavailable = client.post(
        "/trust/credentials/introspect",
        json={
            "bearer_token": raw_token,
            "audience": "runner",
            "required_scope": ["run:execute"],
        },
    )
    assert unavailable.status_code == 503
    assert store.credential_grants == sqlite_grants_before
    assert store.trust_events == sqlite_events_before


@pytest.mark.parametrize(
    (
        "status",
        "approved_payload_hash",
        "expires_at",
        "valid_signature",
        "lease_branch",
        "lease_max_retries",
    ),
    [
        (CapabilityLeaseStatus.REVOKED, None, None, True, "feature/approved-branch", 3),
        (
            CapabilityLeaseStatus.EXPIRED,
            None,
            now_utc() - timedelta(seconds=1),
            True,
            "feature/approved-branch",
            3,
        ),
        (
            CapabilityLeaseStatus.ACTIVE,
            "sha256:" + "f" * 64,
            None,
            True,
            "feature/approved-branch",
            3,
        ),
        (CapabilityLeaseStatus.ACTIVE, None, None, False, "feature/approved-branch", 3),
        (CapabilityLeaseStatus.ACTIVE, None, None, True, "feature/other-branch", 3),
        (CapabilityLeaseStatus.ACTIVE, None, None, True, "feature/approved-branch", 1),
    ],
)
def test_postgres_agent_run_capability_binding_rejects_invalid_leases(
    postgres_agent_run_route_runtime,
    status,
    approved_payload_hash,
    expires_at,
    valid_signature,
    lease_branch,
    lease_max_retries,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        state=state,
        client=client,
        status=status,
        approved_payload_hash=approved_payload_hash,
        expires_at=expires_at,
        valid_signature=valid_signature,
        lease_branch=lease_branch,
        lease_max_retries=lease_max_retries,
    )

    response = client.post(
        f"/agent/runs/{run_id}/capability-lease",
        json={"capability_lease_id": lease.id},
    )

    assert response.status_code == 409
    assert repositories.agent_runs.get(run_id).capability_lease_id is None
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_run_events WHERE run_id=%s AND event_type='CAPABILITY_LEASE_ATTACHED'",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='agent_run.capability_lease_attached'",
                (run_id,),
            ).fetchone()[0]
            == 0
        )


def test_postgres_run_validation_completion_requires_intent_and_event_together(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    created = client.post("/agent/runs", json={"task_id": task_id})
    assert created.status_code == 200, created.text
    run = repositories.agent_runs.get(created.json()["run"]["id"])
    assert run is not None
    run, _lease_event, _audit = repositories.agent_runs.acquire_lease(
        run_id=run.id, worker_id="validation-contract-test", lease_seconds=300, now=now_utc()
    )
    with pytest.raises(ValueError, match="requires its intent and task event"):
        repositories.agent_runs.commit_authoritative_task_step(
            expected_run=run,
            expected_steps=repositories.agent_runs.list_steps_for_run(run.id),
            worker_id="validation-contract-test",
            lease_token=run.lease_token or "",
            step_type=AgentRunStepType.VERIFY_RESULT,
            validation_intent_id="intent-without-event",
            build_records=lambda *_args: pytest.fail("callback must not run"),
        )


def _seed_postgres_validation_completion_records(
    *, connect, workspace, task_id, state, repositories, client,
    include_claim: bool = True, include_consumption: bool = True,
    result_exit_code: int = 0, claim_status: str = "RESULT_RECORDED",
):
    """Seed an exact RUNNING validation checkpoint, intent, claim, action, and consumption."""
    from app.agent_run_supervisor import _build_postgres_agent_run_step_completion_records
    from app.trust_crypto import sha256_hex

    command = "python -m pytest"
    bundle_revision = "1" * 40
    bundle_git_tree_sha = "2" * 40
    with connect() as conn:
        task_row = conn.execute(
            "SELECT payload FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
            (task_id, workspace.id),
        ).fetchone()
        task = AgentTask.model_validate(task_row[0])
        task.repository = "owner/approved-repository"
        task.branch_preference = "feature/approved-branch"
        conn.execute(
            "UPDATE agent_tasks SET payload=%s WHERE task_id=%s AND workspace_id=%s",
            (Jsonb(task.model_dump(mode="json")), task_id, workspace.id),
        )
        approval_row = conn.execute(
            "SELECT payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
            (task.approval_request_id, workspace.id),
        ).fetchone()
        approval = ApprovalRequest.model_validate(approval_row[0])
        approval.test_plan = [command]
        approval.approved_payload_hash = compute_approval_payload_hash(approval)
        conn.execute(
            "UPDATE approval_requests SET payload=%s WHERE approval_id=%s AND workspace_id=%s",
            (Jsonb(approval.model_dump(mode="json")), approval.id, workspace.id),
        )

    run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        state=state,
        client=client,
        lease_actions=[CapabilityActionType.RUN_COMMAND],
        allowed_commands=[command],
    )
    attached = client.post(
        f"/agent/runs/{run_id}/capability-lease", json={"capability_lease_id": lease.id}
    )
    assert attached.status_code == 200, attached.text
    worker = "postgres-run-validation-worker"
    run, _lease_event, _lease_audit = repositories.agent_runs.acquire_lease(
        run_id=run_id, worker_id=worker, lease_seconds=300, now=now_utc()
    )
    assert run.lease_token and run.lease_expires_at
    steps = repositories.agent_runs.list_steps_for_run(run_id)
    validation_step = next(step for step in steps if step.step_type == AgentRunStepType.RUN_VALIDATION)
    now = now_utc()
    run.status = AgentRunStatus.RUNNING
    run.started_at = now
    run.current_step_id = validation_step.id
    run.current_step_index = validation_step.sequence
    run.updated_at = now
    for step in steps:
        step.updated_at = now
        if step.sequence < validation_step.sequence:
            step.status = AgentRunStepStatus.SUCCEEDED
            step.started_at = now
            step.completed_at = now
        elif step.id == validation_step.id:
            step.status = AgentRunStepStatus.RUNNING
            step.attempt_count = 1
            step.lease_owner = worker
            step.lease_token = run.lease_token
            step.lease_expires_at = run.lease_expires_at
            step.started_at = now
        else:
            step.status = AgentRunStepStatus.PENDING
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET status=%s,current_step_id=%s,updated_at=%s,payload=%s "
            "WHERE run_id=%s AND workspace_id=%s",
            (run.status.value, run.current_step_id, now, Jsonb(run.model_dump(mode="json")), run.id, workspace.id),
        )
        for step in steps:
            conn.execute(
                "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
                "WHERE step_id=%s AND workspace_id=%s",
                (step.status.value, step.updated_at, Jsonb(step.model_dump(mode="json")), step.id, workspace.id),
            )
    run = repositories.agent_runs.get(run_id)
    assert run is not None and run.lease_token
    steps = repositories.agent_runs.list_steps_for_run(run_id)
    validation_step = next(step for step in steps if step.id == run.current_step_id)
    intent_id = f"validation_{uuid.uuid4().hex}"
    command_hash = sha256_hex({"command": command})
    approval_hash = compute_approval_payload_hash(approval)
    test_plan_hash = sha256_hex([command])
    lease_token_hash = sha256_hex({"lease_token": run.lease_token})
    action = ProposedAction(
        lease_id=lease.id,
        provider=task.provider,
        agent_id=task.external_agent_id,
        run_id=run.id,
        task_id=task.id,
        project_id=task.project_id,
        workspace_id=workspace.id,
        action_type=CapabilityActionType.RUN_COMMAND,
        repository=task.repository,
        branch=lease.scope.branch,
        command=command,
        estimated_runtime_seconds=30,
        metadata={
            "step_id": validation_step.id,
            "step_type": AgentRunStepType.RUN_VALIDATION.value,
            "idempotency_key": validation_step.idempotency_key,
            "approval_request_id": approval.id,
            "approved_payload_hash": approval.approved_payload_hash,
            "command_source": "approval.test_plan",
            "command_index": 0,
            "command_sha256": command_hash,
            "bundle_revision": bundle_revision,
            "bundle_git_tree_sha": bundle_git_tree_sha,
        },
    )
    intent_time = now_utc()
    intent_payload = {
        "id": intent_id,
        "workspace_id": workspace.id,
        "run_id": run.id,
        "task_id": task.id,
        "approval_id": approval.id,
        "step_id": validation_step.id,
        "lease_id": lease.id,
        "idempotency_key": validation_step.idempotency_key,
        "approval_hash": approval_hash,
        "test_plan_hash": test_plan_hash,
        "lease_token_hash": lease_token_hash,
        "bundle_revision": bundle_revision,
        "bundle_git_tree_sha": bundle_git_tree_sha,
        "run_revision": run.updated_at.isoformat(),
        "step_revision": validation_step.updated_at.isoformat(),
        "status": "EXECUTION_CLAIMED",
        "reason": "",
        "created_at": intent_time.isoformat(),
        "updated_at": intent_time.isoformat(),
    }
    action_consumption = ActionConsumption(
        actual_runtime_seconds=30,
        retry_consumed=validation_step.attempt_count > 1,
        evidence={
            "workspace_id": workspace.id,
            "run_id": run.id,
            "task_id": task.id,
            "approval_id": approval.id,
            "step_id": validation_step.id,
            "action_id": action.id,
            "lease_id": lease.id,
            "intent_id": intent_id,
            "command_index": 0,
            "command_sha256": command_hash,
            "runtime_seconds_reserved": 30,
            "authorized_attempt_count": validation_step.attempt_count,
            "authorized_retry_consumed": validation_step.attempt_count > 1,
        },
    )
    consumed_at = intent_time
    claimed_at = consumed_at + timedelta(milliseconds=1)
    claim_updated_at = claimed_at + timedelta(milliseconds=1)
    claim_payload = {
        "intent_id": intent_id,
        "workspace_id": workspace.id,
        "action_id": action.id,
        "command_index": 0,
        "command_hash": command_hash,
        "status": "RESULT_RECORDED",
        "exit_code": result_exit_code,
        "timed_out": False,
        "stdout_sha256": "a" * 64,
        "stdout_bytes": 0,
        "stderr_sha256": "b" * 64,
        "stderr_bytes": 0,
        "reason": "",
        "claimed_at": claimed_at.isoformat(),
        "updated_at": claim_updated_at.isoformat(),
    }
    with connect() as conn:
        PostgresTrustRepository().insert_proposed_action(conn, action)
        if include_consumption:
            envelope = {
                "action_id": action.id,
                "workspace_id": workspace.id,
                "lease_id": lease.id,
                "consumed_at": consumed_at.isoformat(),
                "consumption": action_consumption.model_dump(mode="json"),
            }
            conn.execute(
                "INSERT INTO action_consumptions(action_id,workspace_id,lease_id,consumed_at,payload) "
                "VALUES(%s,%s,%s,%s,%s)",
                (action.id, workspace.id, lease.id, consumed_at, Jsonb(envelope)),
            )
        conn.execute(
            "INSERT INTO agent_run_validation_intents(intent_id,workspace_id,run_id,task_id,approval_id,step_id,"
            "lease_id,idempotency_key,approval_hash,test_plan_hash,lease_token_hash,bundle_revision,"
            "bundle_git_tree_sha,run_revision,step_revision,status,reason,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'EXECUTION_CLAIMED','',%s,%s,%s)",
            (
                intent_id, workspace.id, run.id, task.id, approval.id, validation_step.id, lease.id,
                validation_step.idempotency_key, approval_hash, test_plan_hash, lease_token_hash,
                bundle_revision, bundle_git_tree_sha,
                run.updated_at, validation_step.updated_at, intent_time, intent_time, Jsonb(intent_payload),
            ),
        )
        if include_claim and claim_status == "RESULT_RECORDED":
            conn.execute(
                "INSERT INTO agent_run_validation_command_claims(intent_id,workspace_id,action_id,command_index,"
                "command_hash,status,exit_code,timed_out,stdout_sha256,stdout_bytes,stderr_sha256,stderr_bytes,"
                "reason,claimed_at,updated_at,payload) "
                "VALUES(%s,%s,%s,0,%s,'RESULT_RECORDED',%s,FALSE,%s,0,%s,0,'',%s,%s,%s)",
                (
                    intent_id, workspace.id, action.id, command_hash, result_exit_code, "a" * 64,
                    "b" * 64, claimed_at, claim_updated_at, Jsonb(claim_payload),
                ),
            )
        elif include_claim:
            ambiguous_payload = dict(claim_payload)
            ambiguous_payload.update(
                status="CLAIMED", exit_code=None, timed_out=None,
                stdout_sha256=None, stdout_bytes=None, stderr_sha256=None, stderr_bytes=None,
            )
            conn.execute(
                "INSERT INTO agent_run_validation_command_claims(intent_id,workspace_id,action_id,command_index,"
                "command_hash,status,exit_code,timed_out,stdout_sha256,stdout_bytes,stderr_sha256,stderr_bytes,"
                "reason,claimed_at,updated_at,payload) "
                "VALUES(%s,%s,%s,0,%s,'CLAIMED',NULL,NULL,NULL,NULL,NULL,NULL,'',%s,%s,%s)",
                (
                    intent_id, workspace.id, action.id, command_hash, claimed_at,
                    claim_updated_at, Jsonb(ambiguous_payload),
                ),
            )
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=(
            AgentTaskEventType.TESTS_PASSED if result_exit_code == 0 else AgentTaskEventType.TESTS_FAILED
        ),
        status=AgentTaskStatus.APPROVED if result_exit_code == 0 else AgentTaskStatus.FAILED,
        actor=worker,
        created_at=now_utc(),
        metadata={
            "run_id": run.id,
            "step_id": validation_step.id,
            "intent_id": intent_id,
            "attempt_count": validation_step.attempt_count,
            "approval_hash": approval_hash,
            "test_plan_hash": test_plan_hash,
            "lease_token_hash": lease_token_hash,
            "bundle_revision": bundle_revision,
            "bundle_git_tree_sha": bundle_git_tree_sha,
            "command_results": [{
                "command_index": 0,
                "command_hash": command_hash,
                "exit_code": result_exit_code,
                "timed_out": False,
                "stdout_sha256": "a" * 64,
                "stdout_bytes": 0,
                "stderr_sha256": "b" * 64,
                "stderr_bytes": 0,
            }],
        },
    )

    def build_records(_task, _approval, _task_events, durable_run, durable_steps):
        updated_run, updated_steps, events, audit = _build_postgres_agent_run_step_completion_records(
            run=durable_run,
            steps=durable_steps,
            step_type=AgentRunStepType.RUN_VALIDATION,
            result=AgentRunStepExecutionResult(
                success=result_exit_code == 0,
                decision=(
                    VerificationDecision.PASS
                    if result_exit_code == 0 else VerificationDecision.FAIL_PERMANENT
                ),
                reason=(
                    "Validation commands passed."
                    if result_exit_code == 0 else "The first approved validation command failed."
                ),
                evidence={
                    "validation_intent_id": intent_id,
                    "test_plan_hash": test_plan_hash,
                    "bundle_revision": bundle_revision,
                    "bundle_git_tree_sha": bundle_git_tree_sha,
                },
            ),
            worker_id=worker,
            lease_token=durable_run.lease_token or "",
            already_running=True,
        )
        audit_details = {
            **audit.details,
            "validation_intent_id": intent_id,
            "approval_hash": approval_hash,
            "test_plan_hash": test_plan_hash,
            "lease_token_hash": lease_token_hash,
            "bundle_revision": bundle_revision,
            "bundle_git_tree_sha": bundle_git_tree_sha,
        }
        events = [
            item.model_copy(update={
                "metadata": {
                    **item.metadata,
                    "bundle_revision": bundle_revision,
                    "bundle_git_tree_sha": bundle_git_tree_sha,
                }
            }) if item.step_id == validation_step.id else item
            for item in events
        ]
        return updated_run, updated_steps, events, audit.model_copy(update={"details": audit_details})

    return run, steps, worker, intent_id, event, build_records


def test_postgres_run_validation_completion_commits_success_atomically(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = postgres_agent_run_route_runtime
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    values = _seed_postgres_validation_completion_records(
        connect=connect, workspace=workspace, task_id=task_id, state=state,
        repositories=repositories, client=TestClient(main.app),
    )
    run, steps, worker, intent_id, task_event, build_records = values
    updated_run, updated_steps, completed_events, completion_audit = repositories.agent_runs.commit_authoritative_task_step(
        expected_run=run,
        expected_steps=steps,
        worker_id=worker,
        lease_token=run.lease_token or "",
        step_type=AgentRunStepType.RUN_VALIDATION,
        validation_intent_id=intent_id,
        validation_task_event=task_event,
        build_records=build_records,
    )
    assert updated_run.lease_token is None
    completed_step = next(step for step in updated_steps if step.step_type == AgentRunStepType.RUN_VALIDATION)
    assert completed_step.status == AgentRunStepStatus.SUCCEEDED
    assert completed_step.evidence["bundle_revision"] == task_event.metadata["bundle_revision"]
    assert completed_step.evidence["bundle_git_tree_sha"] == task_event.metadata["bundle_git_tree_sha"]
    step_events = [event for event in completed_events if event.step_id == completed_step.id]
    assert step_events
    assert all(
        event.metadata["bundle_revision"] == task_event.metadata["bundle_revision"]
        and event.metadata["bundle_git_tree_sha"] == task_event.metadata["bundle_git_tree_sha"]
        for event in step_events
    )
    assert completion_audit.details["bundle_revision"] == task_event.metadata["bundle_revision"]
    assert completion_audit.details["bundle_git_tree_sha"] == task_event.metadata["bundle_git_tree_sha"]
    assert repositories.agent_tasks.get(task_id).status == AgentTaskStatus.APPROVED
    with connect() as conn:
        persisted_task_event = conn.execute(
            "SELECT event_type,payload FROM agent_task_events WHERE event_id=%s AND task_id=%s "
            "AND workspace_id=%s",
            (task_event.id, task_id, workspace.id),
        ).fetchone()
        assert persisted_task_event is not None, (task_event.id, task_id, workspace.id, conn.execute(
            "SELECT event_id,task_id,workspace_id,event_type FROM agent_task_events WHERE task_id=%s",
            (task_id,),
        ).fetchall())
        assert persisted_task_event[0] == AgentTaskEventType.TESTS_PASSED.value
        assert persisted_task_event[1] == task_event.model_dump(mode="json")
        assert conn.execute(
            "SELECT status FROM agent_run_validation_intents WHERE intent_id=%s", (intent_id,)
        ).fetchone()[0] == "COMPLETED"
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type='agent_task' AND entity_id=%s "
            "AND event_type='agent_worker.validation_run_completed'", (task_id,)
        ).fetchone()[0] == 1
        task_audit_details = conn.execute(
            "SELECT details FROM audit_events WHERE entity_type='agent_task' AND entity_id=%s "
            "AND event_type='agent_worker.validation_run_completed'",
            (task_id,),
        ).fetchone()[0]
        assert task_audit_details["bundle_revision"] == task_event.metadata["bundle_revision"]
        assert task_audit_details["bundle_git_tree_sha"] == task_event.metadata["bundle_git_tree_sha"]


def test_postgres_run_validation_completion_commits_first_deterministic_failure(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = postgres_agent_run_route_runtime
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    run, steps, worker, intent_id, task_event, build_records = _seed_postgres_validation_completion_records(
        connect=connect, workspace=workspace, task_id=task_id, state=state,
        repositories=repositories, client=TestClient(main.app), result_exit_code=2,
    )
    updated_run, updated_steps, _events, _audit = repositories.agent_runs.commit_authoritative_task_step(
        expected_run=run, expected_steps=steps, worker_id=worker,
        lease_token=run.lease_token or "", step_type=AgentRunStepType.RUN_VALIDATION,
        validation_intent_id=intent_id, validation_task_event=task_event,
        build_records=build_records,
    )
    completed_step = next(step for step in updated_steps if step.step_type == AgentRunStepType.RUN_VALIDATION)
    assert completed_step.status == AgentRunStepStatus.FAILED
    assert updated_run.status == AgentRunStatus.FAILED
    assert repositories.agent_tasks.get(task_id).status == AgentTaskStatus.FAILED
    with connect() as conn:
        assert conn.execute(
            "SELECT status,reason,payload->>'reason' FROM agent_run_validation_intents WHERE intent_id=%s",
            (intent_id,),
        ).fetchone() == (
            "COMPLETED",
            "Approved validation command failed or timed out; later commands were not executed.",
            "Approved validation command failed or timed out; later commands were not executed.",
        )
        assert conn.execute(
            "SELECT event_type,payload->>'status' FROM agent_task_events WHERE event_id=%s",
            (task_event.id,),
        ).fetchone() == (AgentTaskEventType.TESTS_FAILED.value, AgentTaskStatus.FAILED.value)


@pytest.mark.parametrize("missing", ["claim", "consumption"])
def test_postgres_run_validation_completion_rejects_missing_claim_or_consumption(
    postgres_agent_run_route_runtime, missing: str,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = postgres_agent_run_route_runtime
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    values = _seed_postgres_validation_completion_records(
        connect=connect, workspace=workspace, task_id=task_id, state=state,
        repositories=repositories, client=TestClient(main.app),
        include_claim=missing != "claim", include_consumption=missing != "consumption",
    )
    run, steps, worker, intent_id, task_event, build_records = values
    before_run = repositories.agent_runs.get(run.id)
    before_steps = repositories.agent_runs.list_steps_for_run(run.id)
    with pytest.raises(AgentRunRepositoryConflict):
        repositories.agent_runs.commit_authoritative_task_step(
            expected_run=run, expected_steps=steps, worker_id=worker,
            lease_token=run.lease_token or "", step_type=AgentRunStepType.RUN_VALIDATION,
            validation_intent_id=intent_id, validation_task_event=task_event,
            build_records=build_records,
        )
    assert repositories.agent_runs.get(run.id) == before_run
    assert repositories.agent_runs.list_steps_for_run(run.id) == before_steps
    with connect() as conn:
        assert conn.execute(
            "SELECT status FROM agent_run_validation_intents WHERE intent_id=%s", (intent_id,)
        ).fetchone()[0] == "EXECUTION_CLAIMED"
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE event_id=%s", (task_event.id,)
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "tamper",
    [
        "claim_hash", "consumption_evidence", "task_event", "task_event_bundle",
        "action_metadata_bundle", "step_evidence_bundle", "run_event_bundle", "audit_bundle",
    ],
)
def test_postgres_run_validation_completion_rejects_mismatched_evidence(
    postgres_agent_run_route_runtime, tamper: str,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = postgres_agent_run_route_runtime
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    run, steps, worker, intent_id, task_event, build_records = _seed_postgres_validation_completion_records(
        connect=connect, workspace=workspace, task_id=task_id, state=state,
        repositories=repositories, client=TestClient(main.app),
    )
    if tamper == "claim_hash":
        with connect() as conn:
            payload = conn.execute(
                "SELECT payload FROM agent_run_validation_command_claims WHERE intent_id=%s",
                (intent_id,),
            ).fetchone()[0]
            payload["command_hash"] = "c" * 64
            conn.execute(
                "UPDATE agent_run_validation_command_claims SET command_hash=%s,payload=%s "
                "WHERE intent_id=%s AND command_index=0",
                ("c" * 64, Jsonb(payload), intent_id),
            )
    elif tamper == "consumption_evidence":
        with connect() as conn:
            action_id = conn.execute(
                "SELECT action_id FROM agent_run_validation_command_claims WHERE intent_id=%s",
                (intent_id,),
            ).fetchone()[0]
            envelope = conn.execute(
                "SELECT payload FROM action_consumptions WHERE action_id=%s", (action_id,)
            ).fetchone()[0]
            envelope["consumption"]["evidence"]["command_sha256"] = "d" * 64
            conn.execute(
                "UPDATE action_consumptions SET payload=%s WHERE action_id=%s",
                (Jsonb(envelope), action_id),
            )
    elif tamper == "task_event":
        metadata = dict(task_event.metadata)
        metadata["test_plan_hash"] = "e" * 64
        task_event = task_event.model_copy(update={"metadata": metadata})
    elif tamper == "task_event_bundle":
        metadata = dict(task_event.metadata)
        metadata["bundle_revision"] = "3" * 40
        task_event = task_event.model_copy(update={"metadata": metadata})
    elif tamper == "action_metadata_bundle":
        with connect() as conn:
            action_id = conn.execute(
                "SELECT action_id FROM agent_run_validation_command_claims WHERE intent_id=%s",
                (intent_id,),
            ).fetchone()[0]
            action_payload = conn.execute(
                "SELECT payload FROM proposed_actions WHERE action_id=%s", (action_id,)
            ).fetchone()[0]
            action_payload["metadata"]["bundle_git_tree_sha"] = "4" * 40
            conn.execute(
                "UPDATE proposed_actions SET payload=%s WHERE action_id=%s",
                (Jsonb(action_payload), action_id),
            )
    else:
        original_build_records = build_records

        def tampered_build_records(*args):
            updated_run, updated_steps, events, audit = original_build_records(*args)
            if tamper == "step_evidence_bundle":
                step = next(item for item in updated_steps if item.step_type == AgentRunStepType.RUN_VALIDATION)
                step.evidence["bundle_revision"] = "5" * 40
                step.evidence_hash = canonical_evidence_hash(step.evidence)
            elif tamper == "run_event_bundle":
                events = [
                    item.model_copy(update={
                        "metadata": {**item.metadata, "bundle_git_tree_sha": "6" * 40}
                    }) if item.step_id == run.current_step_id else item
                    for item in events
                ]
            elif tamper == "audit_bundle":
                details = dict(audit.details)
                details["bundle_revision"] = "7" * 40
                audit = audit.model_copy(update={"details": details})
            return updated_run, updated_steps, events, audit

        build_records = tampered_build_records

    before_run = repositories.agent_runs.get(run.id)
    before_steps = repositories.agent_runs.list_steps_for_run(run.id)
    with pytest.raises(AgentRunRepositoryConflict):
        repositories.agent_runs.commit_authoritative_task_step(
            expected_run=run, expected_steps=steps, worker_id=worker,
            lease_token=run.lease_token or "", step_type=AgentRunStepType.RUN_VALIDATION,
            validation_intent_id=intent_id, validation_task_event=task_event,
            build_records=build_records,
        )
    assert repositories.agent_runs.get(run.id) == before_run
    assert repositories.agent_runs.list_steps_for_run(run.id) == before_steps
    with connect() as conn:
        assert conn.execute(
            "SELECT status FROM agent_run_validation_intents WHERE intent_id=%s", (intent_id,)
        ).fetchone()[0] == "EXECUTION_CLAIMED"
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE event_id=%s", (task_event.id,)
        ).fetchone()[0] == 0


@pytest.mark.parametrize("claim_state", ["ambiguous", "extra"])
def test_postgres_run_validation_completion_rejects_ambiguous_or_extra_claims(
    postgres_agent_run_route_runtime, claim_state: str,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = postgres_agent_run_route_runtime
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    run, steps, worker, intent_id, task_event, build_records = _seed_postgres_validation_completion_records(
        connect=connect, workspace=workspace, task_id=task_id, state=state,
        repositories=repositories, client=TestClient(main.app),
        claim_status="CLAIMED" if claim_state == "ambiguous" else "RESULT_RECORDED",
    )
    with connect() as conn:
        row = conn.execute(
            "SELECT workspace_id,action_id,command_hash,claimed_at,updated_at,payload "
            "FROM agent_run_validation_command_claims WHERE intent_id=%s AND command_index=0",
            (intent_id,),
        ).fetchone()
        if claim_state == "extra":
            extra_payload = dict(row[5])
            extra_payload["command_index"] = 1
            extra_payload["command_hash"] = "f" * 64
            conn.execute(
                "INSERT INTO agent_run_validation_command_claims(intent_id,workspace_id,action_id,command_index,"
                "command_hash,status,exit_code,timed_out,stdout_sha256,stdout_bytes,stderr_sha256,stderr_bytes,"
                "reason,claimed_at,updated_at,payload) "
                "VALUES(%s,%s,%s,1,%s,'RESULT_RECORDED',0,FALSE,%s,0,%s,0,'',%s,%s,%s)",
                (
                    intent_id, row[0], row[1], "f" * 64, "a" * 64, "b" * 64,
                    row[3], row[4], Jsonb(extra_payload),
                ),
            )
    with pytest.raises(AgentRunRepositoryConflict):
        repositories.agent_runs.commit_authoritative_task_step(
            expected_run=run, expected_steps=steps, worker_id=worker,
            lease_token=run.lease_token or "", step_type=AgentRunStepType.RUN_VALIDATION,
            validation_intent_id=intent_id, validation_task_event=task_event,
            build_records=build_records,
        )
    assert repositories.agent_runs.get(run.id) == run
    with connect() as conn:
        assert conn.execute(
            "SELECT status FROM agent_run_validation_intents WHERE intent_id=%s", (intent_id,)
        ).fetchone()[0] == "EXECUTION_CLAIMED"
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE event_id=%s", (task_event.id,)
        ).fetchone()[0] == 0


def test_postgres_run_validation_completion_rolls_back_all_writes_on_task_event_conflict(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = postgres_agent_run_route_runtime
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    run, steps, worker, intent_id, task_event, build_records = _seed_postgres_validation_completion_records(
        connect=connect, workspace=workspace, task_id=task_id, state=state,
        repositories=repositories, client=TestClient(main.app),
    )
    conflicting_event = task_event.model_copy(update={
        "event_type": AgentTaskEventType.NOTE,
        "status": None,
        "metadata": {"preexisting": True},
    })
    with connect() as conn:
        conn.execute(
            "INSERT INTO agent_task_events(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                conflicting_event.id, conflicting_event.task_id, workspace.id,
                conflicting_event.event_type.value, conflicting_event.actor,
                conflicting_event.created_at, Jsonb(conflicting_event.model_dump(mode="json")),
            ),
        )
    before_run = repositories.agent_runs.get(run.id)
    before_steps = repositories.agent_runs.list_steps_for_run(run.id)
    with pytest.raises(psycopg.errors.UniqueViolation):
        repositories.agent_runs.commit_authoritative_task_step(
            expected_run=run, expected_steps=steps, worker_id=worker,
            lease_token=run.lease_token or "", step_type=AgentRunStepType.RUN_VALIDATION,
            validation_intent_id=intent_id, validation_task_event=task_event,
            build_records=build_records,
        )
    assert repositories.agent_runs.get(run.id) == before_run
    assert repositories.agent_runs.list_steps_for_run(run.id) == before_steps
    assert repositories.agent_tasks.get(task_id).status == AgentTaskStatus.APPROVED
    with connect() as conn:
        assert conn.execute(
            "SELECT status FROM agent_run_validation_intents WHERE intent_id=%s", (intent_id,)
        ).fetchone()[0] == "EXECUTION_CLAIMED"
        task_events = conn.execute(
            "SELECT event_type,payload FROM agent_task_events WHERE event_id=%s", (task_event.id,)
        ).fetchall()
        assert len(task_events) == 1
        assert task_events[0][0] == AgentTaskEventType.NOTE.value
        assert task_events[0][1]["metadata"] == {"preexisting": True}
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type='agent_run' AND entity_id=%s "
            "AND event_type='agent_run.validation_completed'", (run.id,),
        ).fetchone()[0] == 0


def test_postgres_apply_change_intent_repository_persists_observation_cas(
    postgres_agent_run_route_runtime,
) -> None:
    """Intent indexed state and GET-only recovery transitions are PostgreSQL durable."""
    import hashlib

    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect, workspace=workspace, task_id=task_id, state=state, client=client,
    )
    attached = client.post(
        f"/agent/runs/{run_id}/capability-lease",
        json={"capability_lease_id": lease.id},
    )
    assert attached.status_code == 200, attached.text
    run = repositories.agent_runs.get(run_id)
    assert run is not None
    step = repositories.agent_runs.list_steps_for_run(run_id)[2]
    now = now_utc()
    content = "persisted intent content\n"
    intent = AgentRunApplyChangeIntent(
        id=f"intent_{uuid.uuid4().hex}", workspace_id=workspace.id,
        run_id=run_id, task_id=task_id, approval_id=run.approval_request_id or "",
        step_id=step.id, action_id=f"action_{uuid.uuid4().hex}", lease_id=lease.id,
        idempotency_key=step.idempotency_key, repository="owner/approved-repository",
        branch="feature/approved-branch", path="src/intent.txt", change_type="create",
        before_sha=None, desired_content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        lease_token_hash="a" * 64, run_revision=run.updated_at, step_revision=step.updated_at,
        status=ApplyChangeIntentStatus.POST_CLAIMED, commit_sha=None,
        reason="one-shot claim persisted", created_at=now, updated_at=now,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO agent_run_apply_change_intents("
            "intent_id,workspace_id,run_id,task_id,approval_id,step_id,action_id,lease_id,"
            "idempotency_key,repository,branch,path,change_type,before_sha,desired_content_sha256,"
            "lease_token_hash,run_revision,step_revision,status,commit_sha,reason,created_at,updated_at,payload"
            ") VALUES(" + ",".join(["%s"] * 23) + ",%s)",
            (
                intent.id, intent.workspace_id, intent.run_id, intent.task_id, intent.approval_id,
                intent.step_id, intent.action_id, intent.lease_id, intent.idempotency_key,
                intent.repository, intent.branch, intent.path, intent.change_type, intent.before_sha,
                intent.desired_content_sha256, intent.lease_token_hash, intent.run_revision,
                intent.step_revision, intent.status.value, intent.commit_sha, intent.reason,
                intent.created_at, intent.updated_at, Jsonb(intent.model_dump(mode="json")),
            ),
        )

    repository = repositories.agent_run_apply_change_intents
    fetched = repository.get_for_step(
        workspace_id=workspace.id, run_id=run_id, step_id=step.id,
        idempotency_key=step.idempotency_key,
    )
    assert fetched == intent
    recovered = repository.record_observation(
        intent=fetched, expected_status=ApplyChangeIntentStatus.POST_CLAIMED,
        status=ApplyChangeIntentStatus.RECONCILIATION_REQUIRED, commit_sha=None,
        reason="remote result not attributable", now=now_utc(),
    )
    assert repository.get_for_step(
        workspace_id=workspace.id, run_id=run_id, step_id=step.id,
        idempotency_key=step.idempotency_key,
    ) == recovered
    with pytest.raises(ValueError, match="compare-and-swap"):
        repository.record_observation(
            intent=fetched, expected_status=ApplyChangeIntentStatus.POST_CLAIMED,
            status=ApplyChangeIntentStatus.RECONCILIATION_REQUIRED, commit_sha=None,
            reason="stale retry", now=now_utc(),
        )


def test_postgres_apply_change_claim_rejects_cancelled_run_without_consuming_intent(
    postgres_agent_run_route_runtime,
) -> None:
    """A cancellation observed under the DB claim locks leaves the intent unclaimed."""
    import hashlib

    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect, workspace=workspace, task_id=task_id, state=state, client=client,
    )
    attached = client.post(
        f"/agent/runs/{run_id}/capability-lease",
        json={"capability_lease_id": lease.id},
    )
    assert attached.status_code == 200, attached.text
    run = repositories.agent_runs.get(run_id)
    assert run is not None
    steps = repositories.agent_runs.list_steps_for_run(run_id)
    step = steps[2]
    now = now_utc()
    run.status = AgentRunStatus.RUNNING
    run.current_step_index = step.sequence
    run.current_step_id = step.id
    run.lease_owner = "apply-claim-test"
    run.lease_token = "apply-worker-token"
    run.lease_expires_at = now + timedelta(minutes=3)
    run.cancel_requested = True
    run.updated_at = now
    step.status = AgentRunStepStatus.RUNNING
    step.lease_token = run.lease_token
    step.updated_at = now
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET status=%s,current_step_id=%s,lease_owner=%s,lease_token=%s,"
            "lease_expires_at=%s,updated_at=%s,payload=%s WHERE run_id=%s AND workspace_id=%s",
            (
                run.status.value, run.current_step_id, run.lease_owner, run.lease_token,
                run.lease_expires_at, run.updated_at, Jsonb(run.model_dump(mode="json")),
                run_id, workspace.id,
            ),
        )
        conn.execute(
            "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
            "WHERE step_id=%s AND run_id=%s AND workspace_id=%s",
            (
                step.status.value, step.updated_at, Jsonb(step.model_dump(mode="json")),
                step.id, run_id, workspace.id,
            ),
        )
    content = "cancelled write must not run\n"
    intent = AgentRunApplyChangeIntent(
        id=f"intent_{uuid.uuid4().hex}", workspace_id=workspace.id,
        run_id=run_id, task_id=task_id, approval_id=run.approval_request_id or "",
        step_id=step.id, action_id=f"action_{uuid.uuid4().hex}", lease_id=lease.id,
        idempotency_key=step.idempotency_key, repository="owner/approved-repository",
        branch="feature/approved-branch", path="src/cancelled.txt", change_type="create",
        before_sha=None, desired_content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        lease_token_hash=hashlib.sha256(b'{"lease_token":"apply-worker-token"}').hexdigest(),
        run_revision=run.updated_at, step_revision=step.updated_at,
        status=ApplyChangeIntentStatus.INTENT, created_at=now, updated_at=now,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO agent_run_apply_change_intents("
            "intent_id,workspace_id,run_id,task_id,approval_id,step_id,action_id,lease_id,"
            "idempotency_key,repository,branch,path,change_type,before_sha,desired_content_sha256,"
            "lease_token_hash,run_revision,step_revision,status,commit_sha,reason,created_at,updated_at,payload"
            ") VALUES(" + ",".join(["%s"] * 23) + ",%s)",
            (
                intent.id, intent.workspace_id, intent.run_id, intent.task_id, intent.approval_id,
                intent.step_id, intent.action_id, intent.lease_id, intent.idempotency_key,
                intent.repository, intent.branch, intent.path, intent.change_type, intent.before_sha,
                intent.desired_content_sha256, intent.lease_token_hash, intent.run_revision,
                intent.step_revision, intent.status.value, intent.commit_sha, intent.reason,
                intent.created_at, intent.updated_at, Jsonb(intent.model_dump(mode="json")),
            ),
        )
    repository = repositories.agent_run_apply_change_intents
    persisted = repository.get_for_step(
        workspace_id=workspace.id, run_id=run_id, step_id=step.id,
        idempotency_key=step.idempotency_key,
    )
    assert persisted == intent
    with pytest.raises(
        PostgresTrustRepositoryConflict,
        match="authority changed before one-shot write",
    ):
        repository.claim_write_once(
            intent=intent, expected_run=run, expected_step=step,
            worker_lease_token=run.lease_token, now=now,
        )
    assert repository.get_for_step(
        workspace_id=workspace.id, run_id=run_id, step_id=step.id,
        idempotency_key=step.idempotency_key,
    ) == intent


def test_postgres_agent_run_capability_binding_hides_other_workspace_lease(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    client = TestClient(main.app)
    run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect,
        workspace=workspace,
        task_id=state["task_ids_by_workspace"][workspace.id],
        state=state,
        client=client,
    )
    state["current_user"]["value"] = state["users"][1]
    foreign_created = client.post(
        "/agent/runs",
        json={"task_id": state["task_ids_by_workspace"][other_workspace.id]},
    )
    assert foreign_created.status_code == 200, foreign_created.text

    response = client.post(
        f"/agent/runs/{foreign_created.json()['run']['id']}/capability-lease",
        json={"capability_lease_id": lease.id},
    )

    assert response.status_code == 404
    assert state["repositories"].agent_runs.get(run_id).capability_lease_id is None


def test_postgres_agent_run_capability_binding_serializes_concurrent_same_lease_requests(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repository = state["repositories"].agent_runs
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id, first = _create_run_with_postgres_capability_lease(
        connect=connect, workspace=workspace, task_id=task_id, state=state, client=client
    )
    barrier = Barrier(2)

    def attach() -> str | None:
        barrier.wait()
        try:
            return repository.attach_capability_lease(
                run_id=run_id,
                workspace_id=workspace.id,
                lease_id=first.id,
                actor="owner@example.test",
                now=now_utc(),
            ).capability_lease_id
        except AgentRunRepositoryConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: attach(), (1, 2)))

    assert results == [first.id, first.id]
    attached = repository.get(run_id)
    assert attached is not None and attached.capability_lease_id == first.id
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_run_events WHERE run_id=%s "
                "AND event_type='CAPABILITY_LEASE_ATTACHED'",
                (run_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='agent_run.capability_lease_attached'",
                (run_id,),
            ).fetchone()[0]
            == 1
        )

    with pytest.raises(AgentRunRepositoryConflict, match="cannot be replaced"):
        repository.attach_capability_lease(
            run_id=run_id,
            workspace_id=workspace.id,
            lease_id="different-lease-id",
            actor="owner@example.test",
            now=now_utc(),
        )


def test_postgres_agent_run_capability_binding_rolls_back_if_audit_insert_fails(
    postgres_agent_run_route_runtime,
) -> None:
    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repository = state["repositories"].agent_runs
    task_id = state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect, workspace=workspace, task_id=task_id, state=state, client=client
    )
    with connect() as conn:
        conn.execute(
            "CREATE FUNCTION reject_capability_binding_audit() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "IF NEW.event_type='agent_run.capability_lease_attached' THEN "
            "RAISE EXCEPTION 'injected capability audit insert failure'; "
            "END IF; RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER reject_capability_binding_audit "
            "BEFORE INSERT ON audit_events FOR EACH ROW "
            "EXECUTE FUNCTION reject_capability_binding_audit()"
        )

    with pytest.raises(psycopg.Error, match="injected capability audit insert failure"):
        repository.attach_capability_lease(
            run_id=run_id,
            workspace_id=workspace.id,
            lease_id=lease.id,
            actor="owner@example.test",
            now=now_utc(),
        )

    persisted = repository.get(run_id)
    assert persisted is not None and persisted.capability_lease_id is None
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_run_events WHERE run_id=%s "
                "AND event_type='CAPABILITY_LEASE_ATTACHED'",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='agent_run.capability_lease_attached'",
                (run_id,),
            ).fetchone()[0]
            == 0
        )

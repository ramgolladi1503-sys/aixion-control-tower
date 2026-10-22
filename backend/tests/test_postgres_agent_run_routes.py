from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
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
    AgentRunStepStatus,
    AgentRunStepType,
    VerificationDecision,
)
from app.agent_run_repository import AgentRunRepositoryConflict
from app.agent_run_verifier import canonical_evidence_hash
from app.agent_run_routes import MaintainerDependency, MetricsDependency, ReviewerDependency
from app.agent_worker_validation_runner import (
    CommandExecutionResult,
    run_agent_worker_validation_commands,
)
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
from app.store import store
from app.trust_crypto import sign_payload
from app.trust_models import (
    CapabilityActionType,
    CapabilityLease,
    CapabilityLeaseMode,
    CapabilityLeaseStatus,
    CapabilityScope,
    CredentialGrant,
    CredentialGrantType,
    TrustEventType,
)
from app.postgres_trust_repository import PostgresTrustRepository
from app.trust_policy import lease_receipt_payload


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

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
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


def _seed_postgres_verify_result_ready(
    *,
    connect: Callable[[], Connection],
    workspace: Workspace,
    task_id: str,
    repositories: ApplicationRepositories,
    client: TestClient,
    include_evidence: bool = True,
) -> str:
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
    if include_evidence:
        validation = run_agent_worker_validation_commands(
            task_id=task_id,
            worker_id=f"mission-control:{run_id}:validation",
            executor=lambda command: CommandExecutionResult(command=command, exit_code=0),
            task_repository=repositories.agent_tasks,
            approval_repository=repositories.approval_decisions,
        )
        assert validation.success is True, validation.reason
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
    assert rejected.status_code == 409
    assert "VALIDATE_SCOPE, VERIFY_RESULT, and SEAL_EVIDENCE steps" in rejected.json()["detail"]
    assert repositories.agent_runs.get(run_id) == durable_run
    assert repositories.agent_runs.list_steps_for_run(run_id) == durable_steps


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
            == 1
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
            == 2
        )


def test_postgres_agent_run_seals_durable_evidence_and_rejects_replay(
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
            == 2
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
    [("command_count", True), ("exit_code", False)],
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
    if evidence_field == "command_count":
        event.metadata[evidence_field] = invalid_value
    else:
        event.metadata["results"][0][evidence_field] = invalid_value
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
    with connect() as conn:
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

    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-result-verifier", "timeout_seconds": 10},
    )

    assert response.status_code == 409
    assert "completion could not be committed" in response.json()["detail"]
    durable_run = repositories.agent_runs.get(run_id)
    assert durable_run is not None and durable_run.lease_token is not None
    verify_step = next(
        step
        for step in repositories.agent_runs.list_steps_for_run(run_id)
        if step.step_type == AgentRunStepType.VERIFY_RESULT
    )
    assert verify_step.status == AgentRunStepStatus.RUNNING
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
            == 0
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
            allowed_actions=[CapabilityActionType.CREATE_BRANCH],
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


def test_postgres_agent_run_rejects_side_effect_step_before_claim_or_trust_mutation(
    postgres_agent_run_route_runtime,
) -> None:
    """PostgreSQL must not execute a side effect without durable run/trust coupling."""
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
    # Put the durable fixture at the first externally-effectful step. The route
    # must reject it before acquiring a worker lease or touching trust state.
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
    assert "VALIDATE_SCOPE, VERIFY_RESULT, and SEAL_EVIDENCE" in rejected.json()["detail"]
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

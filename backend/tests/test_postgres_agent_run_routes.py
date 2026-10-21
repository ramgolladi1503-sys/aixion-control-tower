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

from app import main
from app.agent_run_models import AgentRunStatus
from app.agent_run_repository import AgentRunRepositoryConflict
from app.agent_run_routes import MaintainerDependency, ReviewerDependency
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.approval_integrity import compute_approval_payload_hash
from app.application_repositories import ApplicationRepositories
from app.auth import require_maintainer, require_reviewer
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

        settings = SimpleNamespace(auth_enabled=False, is_production=False)
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
            "SELECT event_type FROM agent_run_events "
            "WHERE workspace_id=%s AND run_id=%s",
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
                owner.id, workspace.id, owner.email, owner.email.lower(), owner.role.value,
                owner.disabled, owner.email_verified, Jsonb(owner.model_dump(mode="json")),
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
        assert conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id=%s AND event_type='CAPABILITY_LEASE_ATTACHED'",
            (run_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='agent_run.capability_lease_attached'",
            (run_id,),
        ).fetchone()[0] == 1
    assert run_id not in store.agent_runs


@pytest.mark.parametrize(
    (
        "status", "approved_payload_hash", "expires_at", "valid_signature",
        "lease_branch", "lease_max_retries",
    ),
    [
        (CapabilityLeaseStatus.REVOKED, None, None, True, "feature/approved-branch", 3),
        (CapabilityLeaseStatus.EXPIRED, None, now_utc() - timedelta(seconds=1), True, "feature/approved-branch", 3),
        (CapabilityLeaseStatus.ACTIVE, "sha256:" + "f" * 64, None, True, "feature/approved-branch", 3),
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
        assert conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id=%s AND event_type='CAPABILITY_LEASE_ATTACHED'",
            (run_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='agent_run.capability_lease_attached'",
            (run_id,),
        ).fetchone()[0] == 0


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
        assert conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id=%s "
            "AND event_type='CAPABILITY_LEASE_ATTACHED'",
            (run_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='agent_run.capability_lease_attached'",
            (run_id,),
        ).fetchone()[0] == 1

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
        assert conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id=%s "
            "AND event_type='CAPABILITY_LEASE_ATTACHED'",
            (run_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='agent_run.capability_lease_attached'",
            (run_id,),
        ).fetchone()[0] == 0

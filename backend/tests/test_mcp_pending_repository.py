from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.approval_integrity import compute_approval_payload_hash
from app.application_repositories import ApplicationRepositories
from app.database_migrations import run_migrations
from app.mcp_pending_repository import (
    MCPPendingRepositoryScopeError,
    PostgresMCPPendingRepository,
    SQLiteMCPPendingRepository,
)
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    FileChange,
    MCPPendingRequest,
    MCPChildServer,
    MCPToolDefinition,
    MCPPendingStatus,
    Notification,
    Project,
    RiskAssessment,
    RiskLevel,
    Workspace,
    now_utc,
)
from app.postgres_database import POSTGRES_MIGRATIONS, run_postgres_migrations


SERVER_REVISION = datetime.fromisoformat("2026-10-23T00:00:00+00:00")


def _records() -> tuple[ApprovalRequest, MCPPendingRequest, AuditEvent, Notification]:
    call_snapshot = {
        "server_name": "server",
        "tool_name": "write_file",
        "arguments": {"path": "a.txt", "text": "reviewed"},
        "session_id": None,
        "requested_by": "test-client",
        "mutating": True,
        "target_server_id": "mcp-server-pending-test",
        "target_server_revision": SERVER_REVISION.isoformat(),
    }
    approval = ApprovalRequest(
        id="approval-mcp-pending-test",
        project_id="project-mcp-pending-test",
        workspace_id="workspace-mcp-pending-test",
        title="Approve MCP write_file",
        summary="Review exact mutating MCP call",
        agent_name="mcp-gateway",
        target_branch="mcp-gateway/write_file",
        source_provider="OTHER",
        source_agent_name="mcp-gateway",
        files=[FileChange(
            path="mcp://server/write_file",
            change_type="mcp_tool_call",
            diff="MCP mutating tool call requires approval: " + str(call_snapshot),
            new_content=json.dumps(call_snapshot, sort_keys=True, separators=(",", ":")),
        )],
        risk=RiskAssessment(level=RiskLevel.HIGH, reasons=["Mutating MCP call"]),
        status=ApprovalStatus.REQUESTED,
        verified_source=True,
    )
    pending = MCPPendingRequest(
        id="mcp-pending-test",
        project_id=approval.project_id,
        workspace_id=approval.workspace_id,
        approval_request_id=approval.id,
        server_name="server",
        tool_name="write_file",
        arguments={"path": "a.txt", "text": "reviewed"},
        requested_by="test-client",
    )
    event = AuditEvent(
        id="audit-mcp-pending-create",
        event_type="mcp.approval_requested",
        actor="test-client",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        details={
            "mcp_pending_request_id": pending.id,
            "server_name": pending.server_name,
            "tool_name": pending.tool_name,
            "mutating": True,
        },
    )
    notification = Notification(
        id="notification-mcp-pending-test",
        title="MCP approval needed",
        body="Review a mutating MCP request",
        entity_type="approval_request",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
    )
    return approval, pending, event, notification


def _sqlite_repository(path: Path) -> SQLiteMCPPendingRepository:
    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    with connect() as connection:
        run_migrations(connection)
    return SQLiteMCPPendingRepository(connect)


def _seed_sqlite_project(path: Path) -> None:
    workspace = Workspace(
        id="workspace-mcp-pending-test",
        name="MCP pending test",
        owner_user_id="owner-mcp-pending-test",
    )
    project = Project(
        id="project-mcp-pending-test",
        name="MCP pending project",
        workspace_id=workspace.id,
    )
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            [
                ("workspace", workspace.id, workspace.model_dump_json()),
                ("project", project.id, project.model_dump_json()),
            ],
        )


def _create_sqlite(repository: SQLiteMCPPendingRepository, path: Path):
    _seed_sqlite_project(path)
    records = _records()
    repository.create_request(
        approval=records[0], pending=records[1], audit_event=records[2], notification=records[3]
    )
    return records


def _approved(approval: ApprovalRequest) -> ApprovalRequest:
    candidate = approval.model_copy(update={
        "status": ApprovalStatus.APPROVED,
        "approved_payload_hash_version": 2,
        "approved_at": now_utc(),
        "approved_by_user_id": "reviewer-mcp-test",
    })
    return candidate.model_copy(update={"approved_payload_hash": compute_approval_payload_hash(candidate)})


def _transition_audit(pending: MCPPendingRequest, status: MCPPendingStatus) -> AuditEvent:
    return AuditEvent(
        event_type="mcp.forwarded",
        actor="mcp-gateway",
        entity_id=pending.approval_request_id,
        workspace_id=pending.workspace_id,
        details={"mcp_pending_request_id": pending.id, "new_status": status},
    )


def test_sqlite_request_creation_is_atomic_and_workspace_scoped(tmp_path: Path) -> None:
    path = tmp_path / "mcp-pending.sqlite3"
    repository = _sqlite_repository(path)
    approval, pending, _, _ = _create_sqlite(repository, path)

    assert repository.get(pending.id) == pending
    assert repository.get_for_approval(approval.id) == pending
    assert repository.list_for_workspace(approval.workspace_id, project_id=approval.project_id) == [pending]
    with sqlite3.connect(path) as connection:
        types = connection.execute(
            "SELECT entity_type,count(*) FROM kv_store WHERE entity_type IN "
            "('approval_request','mcp_pending_request','audit_event','notification') "
            "GROUP BY entity_type ORDER BY entity_type"
        ).fetchall()
    assert types == [
        ("approval_request", 1),
        ("audit_event", 1),
        ("mcp_pending_request", 1),
        ("notification", 1),
    ]


def test_sqlite_audit_failure_rolls_back_approval_pending_and_notification(tmp_path: Path) -> None:
    path = tmp_path / "mcp-pending-rollback.sqlite3"
    repository = _sqlite_repository(path)
    approval, pending, event, notification = _create_sqlite(repository, path)
    approval2 = approval.model_copy(update={"id": "approval-mcp-pending-second"})
    pending2 = pending.model_copy(update={
        "id": "mcp-pending-second",
        "approval_request_id": approval2.id,
    })
    event2 = event.model_copy(update={
        "id": "audit-mcp-pending-second",
        "entity_id": approval2.id,
        "details": {**event.details, "mcp_pending_request_id": pending2.id},
    })
    notification2 = notification.model_copy(update={
        "id": "notification-mcp-pending-second",
        "entity_id": approval2.id,
    })

    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_mcp_pending_test_audit BEFORE INSERT ON kv_store "
            "WHEN NEW.entity_type='audit_event' AND NEW.entity_id='audit-mcp-pending-second' "
            "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected audit failure"):
        repository.create_request(
            approval=approval2,
            pending=pending2,
            audit_event=event2,
            notification=notification2,
        )
    assert repository.get(pending2.id) is None
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_id IN (?,?)",
            (approval2.id, notification2.id),
        ).fetchone()[0] == 0


def test_application_repository_bundle_exposes_mcp_pending_repository() -> None:
    repositories = ApplicationRepositories.from_connector(
        lambda: pytest.fail("bundle construction must not open a database connection")
    )

    assert isinstance(repositories.mcp_pending_requests, PostgresMCPPendingRepository)


def test_sqlite_forward_claim_requires_exact_current_v2_approval_and_finishes_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mcp-claim.sqlite3"
    repository = _sqlite_repository(path)
    approval, pending, _, _ = _create_sqlite(repository, path)
    now = now_utc()

    assert repository.claim_forwarding(
        pending_id=pending.id,
        owner="worker-one",
        now=now,
        lease_expires_at=now + timedelta(seconds=30),
    ) is None
    with sqlite3.connect(path) as connection:
        approved = _approved(approval)
        connection.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type=? AND entity_id=?",
            (approved.model_dump_json(), "approval_request", approval.id),
        )

    claimed = repository.claim_forwarding(
        pending_id=pending.id,
        owner="worker-one",
        now=now,
        lease_expires_at=now + timedelta(seconds=30),
    )
    assert claimed is not None
    assert claimed.status == MCPPendingStatus.FORWARDING
    assert claimed.attempts == 1
    updated = claimed.model_copy(update={
        "status": MCPPendingStatus.FORWARDED,
        "lease_owner": None,
        "lease_expires_at": None,
        "updated_at": now + timedelta(seconds=1),
    })
    repository.finish_forwarding(
        expected=claimed,
        updated=updated,
        audit_event=_transition_audit(updated, MCPPendingStatus.FORWARDED),
    )
    assert repository.get(pending.id) == updated


def test_sqlite_expired_forwarding_lease_fails_closed_as_unknown_outcome(tmp_path: Path) -> None:
    path = tmp_path / "mcp-unknown.sqlite3"
    repository = _sqlite_repository(path)
    approval, pending, _, _ = _create_sqlite(repository, path)
    now = now_utc()
    with sqlite3.connect(path) as connection:
        approved = _approved(approval)
        connection.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type=? AND entity_id=?",
            (approved.model_dump_json(), "approval_request", approval.id),
        )
    claimed = repository.claim_forwarding(
        pending_id=pending.id,
        owner="worker-one",
        now=now,
        lease_expires_at=now + timedelta(seconds=10),
    )
    assert claimed is not None

    assert repository.claim_forwarding(
        pending_id=pending.id,
        owner="worker-two",
        now=now + timedelta(seconds=11),
        lease_expires_at=now + timedelta(seconds=41),
    ) is None
    recovered = repository.get(pending.id)
    assert recovered is not None
    assert recovered.status == MCPPendingStatus.DEAD_LETTER
    assert recovered.last_error == "FORWARD_OUTCOME_UNKNOWN"
    assert recovered.lease_owner is None
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type='audit_event' "
            "AND json_extract(payload,'$.event_type')='mcp.forward_outcome_unknown'"
        ).fetchone()[0] == 1


@pytest.fixture
def postgres_mcp_pending_repository() -> Iterator[
    tuple[PostgresMCPPendingRepository, Callable[[], Connection]]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is not configured")
    schema = "mcp_pending_" + uuid.uuid4().hex

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        run_postgres_migrations(connect)
        assert "pg_0012_workspace_scoped_mcp_pending_requests" in {
            migration.migration_id for migration in POSTGRES_MIGRATIONS
        }
        yield PostgresMCPPendingRepository(connect), connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _seed_postgres_project(connect: Callable[[], Connection]) -> None:
    workspace = Workspace(
        id="workspace-mcp-pending-test",
        name="MCP pending test",
        owner_user_id="owner-mcp-pending-test",
    )
    project = Project(
        id="project-mcp-pending-test",
        name="MCP pending project",
        workspace_id=workspace.id,
    )
    server = MCPChildServer(
        id="mcp-server-pending-test",
        project_id=project.id,
        workspace_id=workspace.id,
        name="server",
        enabled=True,
        tools=[MCPToolDefinition(name="write_file", mutating=True)],
        created_at=SERVER_REVISION,
        updated_at=SERVER_REVISION,
    )
    with connect() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        connection.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
        connection.execute(
            "INSERT INTO mcp_child_servers(server_id,project_id,workspace_id,name,enabled,"
            "created_at,updated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (server.id, server.project_id, server.workspace_id, server.name, server.enabled,
             server.created_at, server.updated_at, Jsonb(server.model_dump(mode="json"))),
        )


def _postgres_create(repository: PostgresMCPPendingRepository, connect: Callable[[], Connection]):
    _seed_postgres_project(connect)
    records = _records()
    repository.create_request(
        approval=records[0], pending=records[1], audit_event=records[2], notification=records[3]
    )
    return records


def _target_server_binding(connect: Callable[[], Connection]) -> tuple[str, datetime]:
    with connect() as connection:
        row = connection.execute(
            "SELECT server_id,updated_at FROM mcp_child_servers WHERE name=%s",
            ("server",),
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def test_postgres_approval_pending_notification_and_audit_share_transaction(
    postgres_mcp_pending_repository,
) -> None:
    repository, connect = postgres_mcp_pending_repository
    approval, pending, _, _ = _postgres_create(repository, connect)

    assert repository.get(pending.id) == pending
    assert repository.get_for_approval(approval.id) == pending
    assert repository.list_for_workspace(approval.workspace_id) == [pending]
    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM mcp_pending_requests").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 1


def test_postgres_audit_failure_rolls_back_approval_pending_and_notification(
    postgres_mcp_pending_repository,
) -> None:
    repository, connect = postgres_mcp_pending_repository
    approval, pending, event, notification = _postgres_create(repository, connect)
    approval2 = approval.model_copy(update={"id": "approval-mcp-pending-second"})
    pending2 = pending.model_copy(update={
        "id": "mcp-pending-second",
        "approval_request_id": approval2.id,
    })
    event2 = event.model_copy(update={
        "entity_id": approval2.id,
        "details": {**event.details, "mcp_pending_request_id": pending2.id},
    })
    notification2 = notification.model_copy(update={
        "id": "notification-mcp-pending-second",
        "entity_id": approval2.id,
    })

    with pytest.raises(UniqueViolation):
        repository.create_request(
            approval=approval2,
            pending=pending2,
            audit_event=event2,
            notification=notification2,
        )
    assert repository.get(pending2.id) is None
    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM mcp_pending_requests").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 1


def test_postgres_approved_request_claim_and_expired_lease_fail_closed(
    postgres_mcp_pending_repository,
) -> None:
    repository, connect = postgres_mcp_pending_repository
    approval, pending, _, _ = _postgres_create(repository, connect)
    approved = _approved(approval)
    with connect() as connection:
        connection.execute(
            "UPDATE approval_requests SET status=%s,payload=%s WHERE approval_id=%s",
            (approved.status.value, Jsonb(approved.model_dump(mode="json")), approval.id),
        )
    now = now_utc()
    server_id, server_revision = _target_server_binding(connect)
    claimed = repository.claim_forwarding(
        pending_id=pending.id,
        approval_id=approval.id,
        workspace_id=approval.workspace_id,
        project_id=approval.project_id,
        server_name=pending.server_name,
        expected_server_id=server_id,
        expected_server_revision=server_revision,
        owner="worker-one",
        now=now,
        lease_expires_at=now + timedelta(seconds=10),
    )
    assert claimed is not None
    assert claimed.status == MCPPendingStatus.FORWARDING

    assert repository.claim_forwarding(
        pending_id=pending.id,
        approval_id=approval.id,
        workspace_id=approval.workspace_id,
        project_id=approval.project_id,
        server_name=pending.server_name,
        expected_server_id=server_id,
        expected_server_revision=server_revision,
        owner="worker-two",
        now=now + timedelta(seconds=11),
        lease_expires_at=now + timedelta(seconds=41),
    ) is None
    recovered = repository.get(pending.id)
    assert recovered is not None
    assert recovered.status == MCPPendingStatus.DEAD_LETTER
    assert recovered.last_error == "FORWARD_OUTCOME_UNKNOWN"
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE event_type='mcp.forward_outcome_unknown'"
        ).fetchone()[0] == 1


def test_postgres_mismatched_project_scope_is_rejected_without_rows(
    postgres_mcp_pending_repository,
) -> None:
    repository, connect = postgres_mcp_pending_repository
    _seed_postgres_project(connect)
    approval, pending, event, notification = _records()
    wrong_approval = approval.model_copy(update={"workspace_id": "workspace-other"})
    wrong_pending = pending.model_copy(update={"workspace_id": "workspace-other"})
    wrong_event = event.model_copy(update={"workspace_id": "workspace-other"})
    wrong_notification = notification.model_copy(update={"workspace_id": "workspace-other"})

    with pytest.raises(MCPPendingRepositoryScopeError, match="project workspace"):
        repository.create_request(
            approval=wrong_approval,
            pending=wrong_pending,
            audit_event=wrong_event,
            notification=wrong_notification,
        )
    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM mcp_pending_requests").fetchone()[0] == 0


def test_postgres_forward_claim_is_workspace_and_child_server_scoped(
    postgres_mcp_pending_repository,
) -> None:
    repository, connect = postgres_mcp_pending_repository
    approval, pending, _, _ = _postgres_create(repository, connect)
    approved = _approved(approval)
    with connect() as connection:
        connection.execute(
            "UPDATE approval_requests SET status=%s,payload=%s WHERE approval_id=%s",
            (approved.status.value, Jsonb(approved.model_dump(mode="json")), approval.id),
        )
    now = now_utc()
    server_id, server_revision = _target_server_binding(connect)
    common = {
        "pending_id": pending.id,
        "approval_id": approval.id,
        "workspace_id": approval.workspace_id,
        "project_id": approval.project_id,
        "server_name": pending.server_name,
        "expected_server_id": server_id,
        "expected_server_revision": server_revision,
        "owner": "worker-one",
        "now": now,
        "lease_expires_at": now + timedelta(seconds=30),
    }
    assert repository.claim_forwarding(**{**common, "workspace_id": "workspace-other"}) is None
    assert repository.claim_forwarding(**{**common, "project_id": "project-other"}) is None
    assert repository.claim_forwarding(**{**common, "server_name": "other-server"}) is None
    assert repository.claim_forwarding(
        **{**common, "expected_server_id": "different-server-id"}
    ) is None
    assert repository.claim_forwarding(
        **{**common, "expected_server_revision": server_revision + timedelta(seconds=1)}
    ) is None
    current = repository.get(pending.id)
    assert current is not None
    assert current.status == MCPPendingStatus.WAITING_FOR_APPROVAL
    assert current.attempts == 0


def test_postgres_forward_claim_rejects_stale_child_config_revision_without_attempt(
    postgres_mcp_pending_repository,
) -> None:
    repository, connect = postgres_mcp_pending_repository
    approval, pending, _, _ = _postgres_create(repository, connect)
    approved = _approved(approval)
    with connect() as connection:
        connection.execute(
            "UPDATE approval_requests SET status=%s,payload=%s WHERE approval_id=%s",
            (approved.status.value, Jsonb(approved.model_dump(mode="json")), approval.id),
        )
        row = connection.execute(
            "SELECT payload FROM mcp_child_servers WHERE name=%s",
            (pending.server_name,),
        ).fetchone()
        child = MCPChildServer.model_validate(row[0])
        stale = child.model_copy(update={
            "updated_at": child.updated_at + timedelta(seconds=1),
        })
        connection.execute(
            "UPDATE mcp_child_servers SET updated_at=%s,payload=%s WHERE server_id=%s",
            (stale.updated_at, Jsonb(stale.model_dump(mode="json")), child.id),
        )
    server_id, approved_revision = _target_server_binding(connect)
    # The database now returns the changed revision; use the revision bound in
    # the approval snapshot (the fixture's original revision) for the claim.
    assert approved_revision != SERVER_REVISION
    now = now_utc()
    claimed = repository.claim_forwarding(
        pending_id=pending.id,
        approval_id=approval.id,
        workspace_id=approval.workspace_id,
        project_id=approval.project_id,
        server_name=pending.server_name,
        expected_server_id=server_id,
        expected_server_revision=SERVER_REVISION,
        owner="worker-one",
        now=now,
        lease_expires_at=now + timedelta(seconds=30),
    )
    assert claimed is None
    current = repository.get(pending.id)
    assert current is not None
    assert current.status == MCPPendingStatus.WAITING_FOR_APPROVAL
    assert current.attempts == 0


def test_postgres_forward_claim_rejects_disabled_child_and_tool_mismatch_without_attempt(
    postgres_mcp_pending_repository,
) -> None:
    repository, connect = postgres_mcp_pending_repository
    approval, pending, _, _ = _postgres_create(repository, connect)
    approved = _approved(approval)
    with connect() as connection:
        connection.execute(
            "UPDATE approval_requests SET status=%s,payload=%s WHERE approval_id=%s",
            (approved.status.value, Jsonb(approved.model_dump(mode="json")), approval.id),
        )
    server_id, server_revision = _target_server_binding(connect)
    now = now_utc()
    common = {
        "pending_id": pending.id,
        "approval_id": approval.id,
        "workspace_id": approval.workspace_id,
        "project_id": approval.project_id,
        "server_name": pending.server_name,
        "expected_server_id": server_id,
        "expected_server_revision": server_revision,
        "owner": "worker-one",
        "now": now,
        "lease_expires_at": now + timedelta(seconds=30),
    }
    with connect() as connection:
        row = connection.execute(
            "SELECT payload FROM mcp_child_servers WHERE server_id=%s", (server_id,)
        ).fetchone()
        child_payload = dict(row[0])
        child_payload["enabled"] = False
        connection.execute(
            "UPDATE mcp_child_servers SET enabled=FALSE,payload=%s WHERE server_id=%s",
            (Jsonb(child_payload), server_id),
        )
    assert repository.claim_forwarding(**common) is None
    with connect() as connection:
        row = connection.execute(
            "SELECT payload FROM mcp_child_servers WHERE server_id=%s", (server_id,)
        ).fetchone()
        child_payload = dict(row[0])
        child_payload["enabled"] = True
        child_payload["tools"] = [
            {"name": "write_file", "description": "", "input_schema": {"type": "object", "properties": {}}, "mutating": False}
        ]
        connection.execute(
            "UPDATE mcp_child_servers SET enabled=TRUE,payload=%s WHERE server_id=%s",
            (Jsonb(child_payload), server_id),
        )
    assert repository.claim_forwarding(**common) is None
    current = repository.get(pending.id)
    assert current is not None
    assert current.status == MCPPendingStatus.WAITING_FOR_APPROVAL
    assert current.attempts == 0


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("server_name", "different-server"),
        ("tool_name", "different-tool"),
        ("arguments", {"path": "different.txt", "text": "reviewed"}),
        ("session_id", "different-session"),
        ("requested_by", "different-client"),
        ("target_server_id", "different-server-id"),
        ("target_server_revision", "2026-10-23T00:00:01+00:00"),
    ],
)
def test_postgres_forward_claim_rejects_validly_rehashed_but_mismatched_mcp_action(
    postgres_mcp_pending_repository,
    field: str,
    replacement: object,
) -> None:
    repository, connect = postgres_mcp_pending_repository
    approval, pending, _, _ = _postgres_create(repository, connect)
    action = json.loads(approval.files[0].new_content)
    action[field] = replacement
    tampered_file = approval.files[0].model_copy(update={
        "new_content": json.dumps(action, sort_keys=True, separators=(",", ":")),
        "diff": "MCP mutating tool call requires approval: " + str(action),
    })
    candidate = approval.model_copy(update={
        "files": [tampered_file],
        "status": ApprovalStatus.APPROVED,
        "approved_payload_hash_version": 2,
        "approved_at": now_utc(),
        "approved_by_user_id": "reviewer-mcp-test",
    })
    approved = candidate.model_copy(update={
        "approved_payload_hash": compute_approval_payload_hash(candidate),
    })
    with connect() as connection:
        connection.execute(
            "UPDATE approval_requests SET status=%s,payload=%s WHERE approval_id=%s",
            (approved.status.value, Jsonb(approved.model_dump(mode="json")), approval.id),
        )
    now = now_utc()
    server_id, server_revision = _target_server_binding(connect)
    assert repository.claim_forwarding(
        pending_id=pending.id,
        approval_id=approval.id,
        workspace_id=approval.workspace_id,
        project_id=approval.project_id,
        server_name=pending.server_name,
        expected_server_id=server_id,
        expected_server_revision=server_revision,
        owner="worker-one",
        now=now,
        lease_expires_at=now + timedelta(seconds=30),
    ) is None
    assert repository.get(pending.id).status == MCPPendingStatus.WAITING_FOR_APPROVAL

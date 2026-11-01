from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Protocol

from psycopg import Connection
from psycopg.types.json import Jsonb

from .models import ApprovalRequest, AuditEvent, Project, TestRun


class TestRunRepositoryError(RuntimeError):
    """A test-run record could not be read or written safely."""


class TestRunRepositoryScopeError(TestRunRepositoryError):
    """A test run is missing or outside the requested workspace."""


class TestRunRepository(Protocol):
    def create(
        self, *, test_run: TestRun, workspace_id: str | None, audit_event: AuditEvent
    ) -> None: ...

    def list_for_workspace(self, workspace_id: str | None = None) -> list[TestRun]: ...


def _validate_audit(test_run: TestRun, workspace_id: str | None, audit: AuditEvent) -> None:
    expected = {
        "approval_request_id": test_run.approval_request_id,
        "status": test_run.status,
        "command": test_run.command,
    }
    if (
        audit.event_type != "test_run.recorded"
        or audit.entity_id != test_run.id
        or audit.workspace_id != workspace_id
        or any(audit.details.get(key) != value for key, value in expected.items())
    ):
        raise ValueError("Test-run audit must match the record identity, scope, and details")


def _decode(row: tuple[object, ...]) -> TestRun:
    try:
        item = TestRun.model_validate_json(row[1]) if isinstance(row[1], str) else TestRun.model_validate(row[1])
    except Exception as error:
        raise TestRunRepositoryError("Stored test-run payload is invalid") from error
    if item.id != row[0]:
        raise TestRunRepositoryError("Stored test-run identity does not match its row")
    if len(row) > 2 and item.approval_request_id != row[2]:
        raise TestRunRepositoryError("Stored test-run approval does not match its row")
    return item


class SQLiteTestRunRepository:
    """Transactional test-run persistence for the local SQLite profile."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def create(
        self, *, test_run: TestRun, workspace_id: str | None, audit_event: AuditEvent
    ) -> None:
        _validate_audit(test_run, workspace_id, audit_event)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            approval_row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type='approval_request' AND entity_id=?",
                (test_run.approval_request_id,),
            ).fetchone()
            if approval_row is None:
                raise TestRunRepositoryScopeError("Test-run approval does not exist")
            approval = ApprovalRequest.model_validate_json(approval_row[0])
            if approval.id != test_run.approval_request_id:
                raise TestRunRepositoryScopeError("Test-run approval identity is inconsistent")
            if approval.project_id:
                project_row = conn.execute(
                    "SELECT payload FROM kv_store WHERE entity_type='project' AND entity_id=?",
                    (approval.project_id,),
                ).fetchone()
                if project_row is None:
                    raise TestRunRepositoryScopeError("Test-run approval project does not exist")
                project = Project.model_validate_json(project_row[0])
                if (
                    project.id != approval.project_id
                    or (
                        approval.workspace_id is not None
                        and project.workspace_id != approval.workspace_id
                    )
                    or (workspace_id is not None and project.workspace_id != workspace_id)
                ):
                    raise TestRunRepositoryScopeError("Test-run project is outside its workspace")
            conn.executemany(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                [
                    ("test_run", test_run.id, test_run.model_dump_json()),
                    ("audit_event", audit_event.id, audit_event.model_dump_json()),
                ],
            )

    def list_for_workspace(self, workspace_id: str | None = None) -> list[TestRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t.entity_id,t.payload,json_extract(a.payload,'$.id') "
                "FROM kv_store t JOIN kv_store a ON a.entity_type='approval_request' "
                "AND a.entity_id=json_extract(t.payload,'$.approval_request_id') "
                "LEFT JOIN kv_store p ON p.entity_type='project' "
                "AND p.entity_id=json_extract(a.payload,'$.project_id') "
                "WHERE t.entity_type='test_run' "
                "AND (? IS NULL OR COALESCE(json_extract(a.payload,'$.workspace_id'),"
                "json_extract(p.payload,'$.workspace_id'))=?) ORDER BY t.entity_id",
                (workspace_id, workspace_id),
            ).fetchall()
        return [_decode(row) for row in rows]


class PostgresTestRunRepository:
    """Workspace-scoped test-run persistence with an atomic audit record."""

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def create(
        self, *, test_run: TestRun, workspace_id: str | None, audit_event: AuditEvent
    ) -> None:
        _validate_audit(test_run, workspace_id, audit_event)
        if not workspace_id:
            raise TestRunRepositoryScopeError("PostgreSQL test runs require a workspace")
        with self._connect() as conn:
            approval = conn.execute(
                "SELECT project_id,workspace_id FROM approval_requests "
                "WHERE approval_id=%s FOR KEY SHARE",
                (test_run.approval_request_id,),
            ).fetchone()
            if approval is None or approval[1] != workspace_id:
                raise TestRunRepositoryScopeError("Test-run approval is missing or outside its workspace")
            conn.execute(
                "INSERT INTO test_runs(test_run_id,approval_id,project_id,workspace_id,created_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (test_run.id, test_run.approval_request_id, approval[0], workspace_id,
                 test_run.created_at, Jsonb(test_run.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                "actor,details,created_at,payload) VALUES(%s,%s,NULL,'test_run',%s,%s,%s,%s,%s,%s)",
                (audit_event.id, workspace_id, test_run.id, audit_event.event_type, audit_event.actor,
                 Jsonb(audit_event.details), audit_event.created_at,
                 Jsonb(audit_event.model_dump(mode="json"))),
            )

    def list_for_workspace(self, workspace_id: str | None = None) -> list[TestRun]:
        if not workspace_id:
            raise TestRunRepositoryScopeError("PostgreSQL test-run listing requires a workspace")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT test_run_id,payload,approval_id,project_id,workspace_id FROM test_runs "
                "WHERE workspace_id=%s ORDER BY created_at DESC,test_run_id DESC",
                (workspace_id,),
            ).fetchall()
        items: list[TestRun] = []
        for row in rows:
            item = _decode(row[:3])
            if row[4] != workspace_id:
                raise TestRunRepositoryScopeError("Test-run row escaped its workspace query")
            items.append(item)
        return items

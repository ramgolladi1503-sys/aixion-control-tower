from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app import main
from app.approval_recovery import reconcile_linked_approval_tasks

pytest_plugins = ("test_postgres_agent_task_read_routes",)


def _approved_task_without_run(connect, project) -> tuple[str, str]:
    """Create a real approved PostgreSQL task, then model a legacy missing run."""
    client = TestClient(main.app, raise_server_exceptions=False)
    task_response = client.post(
        "/agent/tasks",
        json={
            "provider": "MANUAL",
            "project_id": project.id,
            "title": "Recovery boundary task",
            "goal": "Verify durable approval recovery boundaries",
            "requested_action": "CREATE_WORK_ORDER",
            "requires_approval": True,
        },
    )
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()
    approval_response = client.post(
        f"/agent/tasks/{task['id']}/approval",
        json={
            "project_id": project.id,
            "title": "Recovery boundary approval",
            "summary": "Approval recovery test fixture.",
            "target_branch": "feature/approval-recovery-boundary-test",
            "files": [
                {
                    "path": "src/recovery-boundary.txt",
                    "change_type": "modified",
                    "diff": "-before\n+after",
                }
            ],
        },
    )
    assert approval_response.status_code == 200, approval_response.text
    approval_id = approval_response.json()["id"]
    decision = client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "approve"}
    )
    assert decision.status_code == 200, decision.text

    # Preserve the committed approval and task decision. Remove only the run
    # artifacts so the tests exercise the documented recovery path for legacy
    # rows where the approval decision survived but the first run did not.
    with connect() as conn:
        conn.execute("DELETE FROM agent_run_events WHERE task_id=%s", (task["id"],))
        conn.execute("DELETE FROM agent_run_steps WHERE task_id=%s", (task["id"],))
        conn.execute("DELETE FROM agent_runs WHERE task_id=%s", (task["id"],))
    return task["id"], approval_id


def _reconcile() -> int:
    return reconcile_linked_approval_tasks(
        approval_repository=main.app.state.approval_decision_repository,
        task_repository=main.app.state.agent_task_repository,
        run_repository=main.app.state.agent_run_repository,
    )


def _scheduled_audit_count(connect, task_id: str, run_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type='agent_task' "
            "AND entity_id=%s AND event_type='agent_task.run_scheduled' "
            "AND details->>'agent_run_id'=%s",
            (task_id, run_id),
        ).fetchone()[0]


def test_postgres_recovery_retries_missing_scheduled_audit_after_run_commit(
    postgres_agent_task_route_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect, _workspace, project = postgres_agent_task_route_runtime
    task_id, _approval_id = _approved_task_without_run(connect, project)
    run_repository = main.app.state.agent_run_repository
    original_create = run_repository.create
    def persist_run_without_scheduled_audit(**kwargs):
        scheduled_audit = kwargs.pop("scheduled_audit")
        # Model an interrupted legacy write between durable run creation and
        # the separately repairable scheduled-audit append.
        original_create(**kwargs, scheduled_audit=None)
        assert scheduled_audit is not None

    monkeypatch.setattr(run_repository, "create", persist_run_without_scheduled_audit)
    assert _reconcile() == 1
    runs = run_repository.list_for_task(task_id)
    assert len(runs) == 1
    run = runs[0]
    assert _scheduled_audit_count(connect, task_id, run.id) == 0

    original_record_audit = run_repository.record_run_scheduled_audit
    attempts = 0

    def fail_first_audit_append(event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated scheduled-audit persistence failure")
        return original_record_audit(event)

    monkeypatch.setattr(run_repository, "record_run_scheduled_audit", fail_first_audit_append)
    assert _reconcile() == 0
    assert run_repository.list_for_task(task_id) == runs
    assert _scheduled_audit_count(connect, task_id, run.id) == 0

    assert _reconcile() == 1
    assert run_repository.list_for_task(task_id) == runs
    assert _scheduled_audit_count(connect, task_id, run.id) == 1
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s AND event_type='APPROVED'",
            (task_id,),
        ).fetchone()[0] == 1
    assert _reconcile() == 0
    assert run_repository.list_for_task(task_id) == runs
    assert _scheduled_audit_count(connect, task_id, run.id) == 1


def test_postgres_concurrent_recovery_schedules_one_run_and_one_audit(
    postgres_agent_task_route_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect, _workspace, project = postgres_agent_task_route_runtime
    task_id, _approval_id = _approved_task_without_run(connect, project)
    run_repository = main.app.state.agent_run_repository
    original_list_for_task = run_repository.list_for_task
    both_read_empty = Barrier(2, timeout=10)

    def synchronize_empty_run_reads(task_id_arg: str):
        rows = original_list_for_task(task_id_arg)
        if task_id_arg == task_id and not rows:
            both_read_empty.wait()
        return rows

    monkeypatch.setattr(run_repository, "list_for_task", synchronize_empty_run_reads)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _index: _reconcile(), range(2)))

    runs = run_repository.list_for_task(task_id)
    assert len(runs) == 1
    run = runs[0]
    assert _scheduled_audit_count(connect, task_id, run.id) == 1
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s AND event_type='APPROVED'",
            (task_id,),
        ).fetchone()[0] == 1
    # A later scheduler pass must converge cleanly even if one concurrent pass
    # lost the repository-level race and reported a retryable failure.
    assert _reconcile() == 0
    assert run_repository.list_for_task(task_id) == runs
    assert _scheduled_audit_count(connect, task_id, run.id) == 1

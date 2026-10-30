from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.agent_run_models import AgentRunFaultConfig
from scripts.import_approval_slice_to_postgres import (
    ImportBlocked,
    _insert_payload,
    _prepare_agent_run_faults,
    build_plan,
)
from test_postgres_import import _snapshot


@pytest.fixture(autouse=True)
def _email_outbox_test_key(monkeypatch):
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", "11" * 32)


def _fault(*, fault_id="fault-1", run_id="fault-run", enabled=False, remaining_uses=2):
    return {
        "id": fault_id,
        "run_id": run_id,
        "step_type": None,
        "fault_kind": "WORKER_CRASH",
        "enabled": enabled,
        "remaining_uses": remaining_uses,
        "message": "diagnostic config",
        "created_by": "test",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


@pytest.mark.parametrize(
    ("enabled", "remaining_uses"),
    [(False, 3), (True, 0)],
)
def test_inactive_agent_run_faults_are_imported_and_counted(
    tmp_path, enabled, remaining_uses
):
    source = tmp_path / "inactive_fault.sqlite3"
    fault = _fault(enabled=enabled, remaining_uses=remaining_uses)
    _snapshot(source, include_omitted=False, agent_run_faults=[fault])

    plan, selected, _ = build_plan(source)

    assert selected["agent_run_fault"][fault["id"]] == AgentRunFaultConfig.model_validate(
        fault
    ).model_dump(mode="json")
    assert plan["planned_counts"]["agent_run_fault"] == 1
    assert plan["omitted_counts"].get("agent_run_fault", 0) == 0
    assert plan["agent_run_fault_injection"]["inactive_import_count"] == 1
    assert plan["scope"].endswith("_faults_legacy_audit_global_auth_quarantine_v15")


def test_fault_import_binds_workspace_to_exact_agent_run_parent():
    fault = _fault(run_id="run-1")
    parent = {
        "id": "run-1",
        "task_id": "task-1",
        "workspace_id": "workspace-parent",
        "project_id": "project-1",
        "status": "SUCCEEDED",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    prepared = _prepare_agent_run_faults({"fault-1": fault}, {"run-1": parent})

    class Connection:
        def __init__(self, run):
            self.run = run
            self.calls = []

        def execute(self, query, params):
            self.calls.append((query, params))
            if query.startswith("SELECT workspace_id FROM agent_runs"):
                assert params == ("run-1",)
                return self
            assert "INSERT INTO agent_run_faults" in query
            return self

        def fetchone(self):
            return (self.run["workspace_id"],)

    conn = Connection(parent)
    _insert_payload(conn, "agent_run_fault", "fault-1", prepared["fault-1"])

    insert = next(params for query, params in conn.calls if "INSERT INTO agent_run_faults" in query)
    assert insert[0:3] == ("fault-1", "workspace-parent", "run-1")


@pytest.mark.parametrize(
    ("fault", "runs", "message"),
    [
        (_fault(run_id="missing-run"), {}, "missing AgentRun parent"),
        (
            _fault(run_id="run-1"),
            {
                "run-1": {
                    "id": "different-run",
                    "task_id": "task-1",
                    "workspace_id": "workspace-parent",
                    "project_id": "project-1",
                    "status": "SUCCEEDED",
                }
            },
            "mismatched or unscoped AgentRun parent",
        ),
        (
            {**_fault(run_id="run-1"), "workspace_id": "workspace-other"},
            {
                "run-1": {
                    "id": "run-1",
                    "task_id": "task-1",
                    "workspace_id": "workspace-parent",
                    "project_id": "project-1",
                    "status": "SUCCEEDED",
                }
            },
            "workspace does not match",
        ),
    ],
)
def test_fault_import_rejects_missing_or_mismatched_parent(fault, runs, message):
    with pytest.raises(ImportBlocked, match=message):
        _prepare_agent_run_faults({"fault-1": fault}, runs)


def test_active_agent_run_fault_still_blocks_plan(tmp_path):
    source = tmp_path / "active_fault.sqlite3"
    _snapshot(
        source,
        include_omitted=False,
        agent_run_faults=[_fault(enabled=True, remaining_uses=1)],
    )

    with pytest.raises(ImportBlocked, match="active AgentRun fault injection settings"):
        build_plan(source)

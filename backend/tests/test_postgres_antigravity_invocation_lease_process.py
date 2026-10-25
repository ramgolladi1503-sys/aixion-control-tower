from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.models import ApprovalStatus
from test_postgres_approval_route_flow import (
    _start_postgres_approval_http_api,
    _stop_postgres_approval_http_api,
)

pytest_plugins = ("test_postgres_approval_route_flow",)

REPOSITORY_ROOT = Path(__file__).parents[2]
HOOK_PATH = (
    REPOSITORY_ROOT
    / "integrations"
    / "antigravity"
    / "aixion_pre_tool_hook.py"
)


def _start_hook_process(payload: dict, environment: dict[str, str]) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, str(HOOK_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload))
    process.stdin.close()
    # Popen.communicate() flushes stdin when it is still present, even if the
    # pipe was explicitly closed above. The hook is intentionally left running
    # while the test inspects its persisted approval, so retain stdout/stderr
    # capture but detach the closed stdin handle.
    process.stdin = None
    return process


def _wait_for_approval(connect, workspace_id: str, agent_id: str, deadline: float):
    while time.monotonic() < deadline:
        with connect() as connection:
            row = connection.execute(
                "SELECT approval_id,source_invocation_id,status "
                "FROM approval_requests WHERE workspace_id=%s AND source_agent_id=%s "
                "ORDER BY created_at DESC LIMIT 1",
                (workspace_id, agent_id),
            ).fetchone()
        if row is not None:
            return row
        time.sleep(0.05)
    pytest.fail("Antigravity hook did not persist its approval before the deadline")


def test_postgres_expired_invocation_lease_blocks_resumed_antigravity_hook_process(
    postgres_approval_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, workspace, project, reviewer, _repositories, agent, agent_token, schema = (
        postgres_approval_route_runtime
    )

    # Use the smallest supported lease so the real process-loss window remains bounded.
    monkeypatch.setenv("AIXION_EXTERNAL_AGENT_INVOCATION_LEASE_TTL_SECONDS", "30")
    api = _start_postgres_approval_http_api(
        dsn=dsn,
        schema=schema,
        reviewer=reviewer,
    )
    hook_environment = os.environ.copy()
    hook_environment.update(
        {
            "AIXION_API_BASE_URL": api[2],
            "AIXION_AGENT_ID": agent.id,
            "AIXION_AGENT_TOKEN": agent_token,
            "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
            "AIXION_PROJECT_ID": project.id,
            "AIXION_APPROVAL_TIMEOUT_SECONDS": "90",
        }
    )
    payload = {
        "conversationId": f"session-pg-stale-lease-{uuid.uuid4().hex}",
        "stepIdx": 1,
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "echo must-not-resume-after-lease-expiry"},
        },
    }
    hook: subprocess.Popen | None = None
    resumed_hook: subprocess.Popen | None = None
    try:
        hook = _start_hook_process(payload, hook_environment)
        approval_id, invocation_id, status = _wait_for_approval(
            connect,
            workspace.id,
            agent.id,
            time.monotonic() + 15,
        )
        assert status == ApprovalStatus.PENDING_REVIEW

        with connect() as connection:
            initial_lease = connection.execute(
                "SELECT workspace_id,source_agent_id,source_invocation_id,expires_at "
                "FROM approval_invocation_leases WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()
        assert initial_lease is not None
        assert initial_lease[:3] == (workspace.id, agent.id, invocation_id)
        expiry = initial_lease[3]
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        # Simulate a host crash: the pending invocation process is killed with no cleanup.
        hook.kill()
        hook.wait(timeout=5)
        assert hook.returncode is not None and hook.returncode != 0
        hook = None

        lease_deadline = time.monotonic() + 35
        while datetime.now(timezone.utc) <= expiry and time.monotonic() < lease_deadline:
            time.sleep(0.05)
        assert datetime.now(timezone.utc) > expiry, "test failed to wait for server lease expiry"

        with httpx.Client(timeout=5) as client:
            decision = client.post(
                f"{api[2]}/approvals/{approval_id}/decision",
                json={
                    "decision": "approve",
                    "reason": "Decision after the original connector process died",
                },
            )
        assert decision.status_code == 409, decision.text
        assert "changed before this decision committed" in decision.json()["detail"]

        # Relaunch with the byte-for-byte same provider payload and therefore the same
        # invocation identity. The approved decision must not outlive its lease.
        resumed_hook = _start_hook_process(payload, hook_environment)
        stdout, stderr = resumed_hook.communicate(timeout=30)
        assert resumed_hook.returncode == 0, stderr
        outcome = json.loads(stdout)
        assert outcome["decision"] == "deny"
        assert outcome["reason"] != "Approved from Aixion Control Tower"

        with connect() as connection:
            request_rows = connection.execute(
                "SELECT approval_id,status,source_invocation_id FROM approval_requests "
                "WHERE workspace_id=%s AND source_agent_id=%s AND source_invocation_id=%s",
                (workspace.id, agent.id, invocation_id),
            ).fetchall()
            assert request_rows == [
                (approval_id, ApprovalStatus.PENDING_REVIEW, invocation_id)
            ]
            lease_row = connection.execute(
                "SELECT workspace_id,source_agent_id,source_invocation_id,expires_at "
                "FROM approval_invocation_leases WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()
            assert lease_row is not None
            assert lease_row[:3] == (workspace.id, agent.id, invocation_id)
            assert lease_row[3] == expiry
            assert connection.execute(
                "SELECT count(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.created_by_agent'",
                (approval_id,),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.decision'",
                (approval_id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.delivery_claimed'",
                (approval_id,),
            ).fetchone()[0] == 0
    finally:
        for process in (hook, resumed_hook):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        if api[0].is_alive():
            _stop_postgres_approval_http_api(api)

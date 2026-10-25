from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from app.agent_auth import revoke_external_agent_token
from app.models import ApprovalStatus
from test_postgres_approval_route_flow import (
    _start_postgres_approval_http_api,
    _stop_postgres_approval_http_api,
)

pytest_plugins = ("test_postgres_approval_route_flow",)

REPOSITORY_ROOT = Path(__file__).parents[2]
ANTIGRAVITY_HOOK_PATH = (
    REPOSITORY_ROOT / "integrations" / "antigravity" / "aixion_pre_tool_hook.py"
)


def _start_hook(payload: dict, environment: dict[str, str]) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, str(ANTIGRAVITY_HOOK_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        cwd=REPOSITORY_ROOT,
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload))
    process.stdin.close()
    process.stdin = None
    return process


def test_postgres_revocation_while_hook_waits_denies_without_delivery_claim(
    postgres_approval_route_runtime,
) -> None:
    """A live hook must observe durable revocation through the real API process."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, workspace, project, _reviewer, repositories, agent, token, schema = (
        postgres_approval_route_runtime
    )
    api = _start_postgres_approval_http_api(
        dsn=dsn,
        schema=schema,
        reviewer=_reviewer,
    )
    payload = {
        "conversationId": f"session-postgres-revoke-{uuid.uuid4().hex}",
        "stepIdx": 1,
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "echo revoked-connector-must-not-resume"},
        },
    }
    environment = os.environ.copy()
    environment.update(
        {
            "AIXION_API_BASE_URL": api[2],
            "AIXION_AGENT_ID": agent.id,
            "AIXION_AGENT_TOKEN": token,
            "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
            "AIXION_PROJECT_ID": project.id,
            "AIXION_APPROVAL_TIMEOUT_SECONDS": "120",
        }
    )
    hook: subprocess.Popen[str] | None = None
    try:
        hook = _start_hook(payload, environment)
        deadline = time.monotonic() + 15
        approval_id: str | None = None
        invocation_id: str | None = None
        while time.monotonic() < deadline:
            with connect() as connection:
                row = connection.execute(
                    "SELECT approval_id,source_invocation_id,status FROM approval_requests "
                    "WHERE workspace_id=%s AND source_agent_id=%s "
                    "AND payload->>'source_session_id'=%s ORDER BY created_at DESC LIMIT 1",
                    (workspace.id, agent.id, payload["conversationId"]),
                ).fetchone()
            if row is not None:
                approval_id, invocation_id, status = row
                assert status == ApprovalStatus.PENDING_REVIEW
                break
            assert hook.poll() is None, "Antigravity hook exited before creating its approval"
            time.sleep(0.05)
        assert approval_id is not None and invocation_id is not None
        assert hook.poll() is None, "Hook must remain blocked before revocation"

        current = repositories.external_agent_auth.get(agent.id)
        assert current is not None
        current_agent, current_credential = current
        revoke_external_agent_token(
            current_agent,
            credential=current_credential,
            repository=repositories.external_agent_auth,
            actor="test:postgres-revoke-pending-hook",
        )

        stdout, stderr = hook.communicate(timeout=10)
        assert hook.returncode == 0, stderr
        output_lines = [line for line in stdout.splitlines() if line.strip()]
        assert len(output_lines) == 1
        decision = json.loads(output_lines[0])
        assert decision["decision"] == "deny"

        revoked_agent = repositories.external_agent_auth.get(agent.id)
        assert revoked_agent is not None
        assert revoked_agent[0].secret_hash is None
        assert revoked_agent[1].token_revoked_at is not None
        with connect() as connection:
            persisted = connection.execute(
                "SELECT status,source_invocation_id FROM approval_requests "
                "WHERE approval_id=%s AND workspace_id=%s",
                (approval_id, workspace.id),
            ).fetchone()
            assert persisted == (ApprovalStatus.PENDING_REVIEW, invocation_id)
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.delivery_claimed'",
                (approval_id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='agent.token_revoked'",
                (agent.id,),
            ).fetchone()[0] == 1
    finally:
        if hook is not None and hook.poll() is None:
            hook.kill()
            hook.communicate(timeout=5)
        if api[0].is_alive():
            _stop_postgres_approval_http_api(api)

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

from app.agent_auth import revoke_external_agent_token
from app.agent_credential_models import AgentCredentialRecord
from app.models import AgentProvider, AuditEvent, ApprovalStatus, new_id
from integrations.codex.aixion_permission_request_hook import _invocation_identity
from test_postgres_approval_route_flow import (
    _start_postgres_approval_http_api,
    _stop_postgres_approval_http_api,
)

pytest_plugins = ("test_postgres_approval_route_flow",)

REPOSITORY_ROOT = Path(__file__).parents[2]
HOOK_PATH = REPOSITORY_ROOT / "integrations" / "codex" / "aixion_permission_request_hook.py"
SETUP_PATH = REPOSITORY_ROOT / "integrations" / "codex" / "setup_workspace.py"
SETUP_SPEC = importlib.util.spec_from_file_location("codex_revocation_setup", SETUP_PATH)
assert SETUP_SPEC and SETUP_SPEC.loader
setup_workspace = importlib.util.module_from_spec(SETUP_SPEC)
SETUP_SPEC.loader.exec_module(setup_workspace)


def _codex_connector(postgres_approval_route_runtime):
    workspace, project, reviewer, repositories, fixture_agent = (
        postgres_approval_route_runtime[1],
        postgres_approval_route_runtime[2],
        postgres_approval_route_runtime[3],
        postgres_approval_route_runtime[4],
        postgres_approval_route_runtime[5],
    )
    token = f"postgres-codex-restart-revoke-{uuid.uuid4().hex}"
    agent = fixture_agent.model_copy(
        update={
            "id": new_id("agent"),
            "provider": AgentProvider.CODEX,
            "display_name": "Codex PostgreSQL restart revocation test",
            "secret_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        }
    )
    repositories.external_agent_auth.create(
        agent=agent,
        credential=AgentCredentialRecord(agent_id=agent.id),
        audit_event=AuditEvent(
            event_type="agent.registered",
            entity_id=agent.id,
            workspace_id=workspace.id,
            actor=f"user:{reviewer.id}",
            details={
                "provider": agent.provider,
                "display_name": agent.display_name,
                "auth_type": agent.auth_type,
                "allowed_project_ids": agent.allowed_project_ids,
                "allowed_repositories": agent.allowed_repositories,
                "allowed_actions": agent.allowed_actions,
            },
        ),
    )
    return workspace, project, reviewer, repositories, agent, token


def test_postgres_codex_pending_hook_denies_revocation_across_api_restart(
    tmp_path: Path,
    postgres_approval_route_runtime,
) -> None:
    """A restarted API must enforce a PostgreSQL connector revocation on the live hook."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, _workspace, _project, _reviewer, _repositories, _agent, _token, schema = (
        postgres_approval_route_runtime
    )
    workspace, project, reviewer, repositories, agent, token = _codex_connector(
        postgres_approval_route_runtime
    )
    server = _start_postgres_approval_http_api(
        dsn=dsn,
        schema=schema,
        reviewer=reviewer,
    )
    hook_process: subprocess.Popen[str] | None = None
    home = tmp_path / "codex restart revocation home"
    workspace_path = tmp_path / "codex workspace with spaces"
    home.mkdir(mode=0o700)
    workspace_path.mkdir(mode=0o700)

    try:
        config_path, hooks_path = setup_workspace.install_workspace(
            workspace_path,
            api_base_url=server[2],
            agent_id=agent.id,
            project_id=project.id,
            home=home,
            hook_script=HOOK_PATH,
            python=sys.executable,
        )
        command = json.loads(hooks_path.read_text(encoding="utf-8"))["hooks"][
            "PermissionRequest"
        ][0]["hooks"][0]["command"]
        argv = shlex.split(command)
        assert argv == [
            sys.executable,
            str(HOOK_PATH.resolve()),
            "--config",
            str(config_path),
        ]
        event = {
            "hook_event_name": "PermissionRequest",
            "session_id": f"codex-restart-revoke-session-{uuid.uuid4().hex}",
            "turn_id": f"codex-restart-revoke-turn-{uuid.uuid4().hex}",
            "tool_use_id": f"codex-restart-revoke-call-{uuid.uuid4().hex}",
            "cwd": str(workspace_path),
            "permission_mode": "default",
            "tool_name": "Bash",
            "tool_input": {
                "command": "printf 'must remain denied after connector revocation'",
                "description": "Isolated PostgreSQL API restart revocation regression",
            },
        }
        invocation_identity = _invocation_identity(
            event,
            tool_name="Bash",
            tool_input=event["tool_input"],
        )
        assert invocation_identity is not None
        _source_session_id, source_invocation_id = invocation_identity
        child_environment = {
            key: value
            for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "WINDIR")
            if (value := os.environ.get(key))
        }
        child_environment.update(
            {
                "HOME": str(home),
                "AIXION_AGENT_TOKEN": token,
                "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
                "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "120",
            }
        )
        hook_process = subprocess.Popen(
            argv,
            cwd=workspace_path,
            env=child_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert hook_process.stdin is not None
        hook_process.stdin.write(json.dumps(event))
        hook_process.stdin.close()
        hook_process.stdin = None

        approval_row: tuple[str, str, str] | None = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if hook_process.poll() is not None:
                stdout, stderr = hook_process.communicate()
                raise AssertionError(
                    "Codex hook exited before the pending approval was durable: "
                    f"exit={hook_process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
                )
            with connect() as connection:
                rows = connection.execute(
                    "SELECT approval_id,source_invocation_id,status FROM approval_requests "
                    "WHERE workspace_id=%s AND source_agent_id=%s "
                    "AND payload->>'source_provider'=%s AND source_invocation_id=%s "
                    "ORDER BY created_at DESC",
                    (workspace.id, agent.id, AgentProvider.CODEX.value, source_invocation_id),
                ).fetchall()
            assert len(rows) <= 1, "Codex hook created duplicate PostgreSQL approval rows"
            if rows:
                approval_row = rows[0]
                break
            time.sleep(0.05)
        if approval_row is None:
            if hook_process.poll() is None:
                hook_process.kill()
            stdout, stderr = hook_process.communicate(timeout=5)
            raise AssertionError(
                "Codex hook created no PostgreSQL approval: "
                f"exit={hook_process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
            )
        approval_id, invocation_id, approval_status = approval_row
        assert approval_status == ApprovalStatus.PENDING_REVIEW.value
        assert hook_process.poll() is None, "Codex hook must wait for review"

        # Make the hook observe a real transport disconnect while the same
        # invocation remains pending, then revoke in PostgreSQL before restart.
        original_port = int(server[2].rsplit(":", 1)[1])
        _stop_postgres_approval_http_api(server)
        server = None
        current = repositories.external_agent_auth.get(agent.id)
        assert current is not None
        current_agent, current_credential = current
        revoke_external_agent_token(
            current_agent,
            credential=current_credential,
            repository=repositories.external_agent_auth,
            actor="test:codex-restart-pending-hook-revocation",
        )
        server = _start_postgres_approval_http_api(
            dsn=dsn,
            schema=schema,
            reviewer=reviewer,
            port=original_port,
        )

        stdout, stderr = hook_process.communicate(timeout=15)
        assert token not in stdout
        assert token not in stderr
        assert hook_process.returncode == 0, stderr
        result = json.loads(stdout)
        assert result["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
        assert result["hookSpecificOutput"]["decision"]["behavior"] == "deny"

        revoked_agent = repositories.external_agent_auth.get(agent.id)
        assert revoked_agent is not None
        assert revoked_agent[0].secret_hash is None
        assert revoked_agent[1].token_revoked_at is not None
        with connect() as connection:
            approval_after = connection.execute(
                "SELECT status,source_invocation_id FROM approval_requests "
                "WHERE approval_id=%s AND workspace_id=%s AND source_agent_id=%s",
                (approval_id, workspace.id, agent.id),
            ).fetchone()
            assert approval_after == (ApprovalStatus.PENDING_REVIEW.value, invocation_id)
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
        if hook_process is not None and hook_process.poll() is None:
            hook_process.kill()
            hook_process.communicate(timeout=5)
        if server is not None and server[0].is_alive():
            _stop_postgres_approval_http_api(server)

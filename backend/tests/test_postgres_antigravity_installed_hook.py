from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from integrations.antigravity import setup_workspace
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


@pytest.mark.parametrize(
    ("decision", "expected_hook_decision", "expected_status", "expected_claims"),
    [
        ("approve", "allow", "APPROVED", 1),
        ("deny", "deny", "DENIED", 0),
    ],
)
def test_installed_antigravity_command_uses_postgres_for_phone_decision(
    tmp_path: Path,
    postgres_approval_route_runtime,
    decision: str,
    expected_hook_decision: str,
    expected_status: str,
    expected_claims: int,
) -> None:
    """Run installer output through a real PostgreSQL-backed approval API."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, workspace_record, project, reviewer, _repositories, agent, agent_token, schema = (
        postgres_approval_route_runtime
    )

    server = _start_postgres_approval_http_api(
        dsn=dsn,
        schema=schema,
        reviewer=reviewer,
    )
    process: subprocess.Popen[str] | None = None
    try:
        api_base_url = server[2]
        home = tmp_path / "isolated home"
        workspace = tmp_path / "workspace with spaces"
        workspace.mkdir(mode=0o700)

        config_path, hooks_path = setup_workspace.install_workspace(
            workspace,
            api_base_url=api_base_url,
            agent_id=agent.id,
            project_id=project.id,
            home=home,
            hook_script=HOOK_PATH,
            python=sys.executable,
        )
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        command = hooks[setup_workspace.HOOK_NAME]["PreToolUse"][0]["hooks"][0][
            "command"
        ]
        assert shlex.split(command) == [
            sys.executable,
            str(HOOK_PATH.resolve()),
            "--config",
            str(config_path),
        ]

        session_id = f"postgres-installed-hook-{decision}-{time.time_ns()}"
        payload = {
            "conversationId": session_id,
            "stepIdx": 17,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "echo installed-hook-postgres-round-trip"},
            },
        }
        child_environment = {
            key: value
            for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "WINDIR")
            if (value := os.environ.get(key))
        }
        child_environment.update(
            {
                "HOME": str(home),
                "AIXION_AGENT_TOKEN": agent_token,
                "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
                "AIXION_APPROVAL_TIMEOUT_SECONDS": "20",
            }
        )

        process = subprocess.Popen(
            command,
            shell=True,
            executable="/bin/sh",
            cwd=workspace,
            env=child_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload))
        process.stdin.close()
        process.stdin = None

        approval_row: tuple[str, str] | None = None
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    "Installed Antigravity hook exited before creating its "
                    f"PostgreSQL approval (exit={process.returncode}, "
                    f"stdout={stdout!r}, stderr={stderr!r})"
                )
            with connect() as connection:
                approval_row = connection.execute(
                    "SELECT approval_id,source_invocation_id FROM approval_requests "
                    "WHERE workspace_id=%s AND source_agent_id=%s "
                    "AND payload->>'source_session_id'=%s",
                    (workspace_record.id, agent.id, session_id),
                ).fetchone()
            if approval_row is not None:
                break
            time.sleep(0.05)
        assert approval_row is not None, (
            "Installed command did not create a PostgreSQL approval before timeout"
        )

        approval_id, invocation_id = approval_row
        assert invocation_id
        with httpx.Client(timeout=10) as client:
            response = client.post(
                f"{api_base_url}/approvals/{approval_id}/decision",
                json={
                    "decision": decision,
                    "reason": "installed Antigravity PostgreSQL integration test",
                },
            )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == expected_status

        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            pytest.fail(
                "Installed Antigravity hook did not finish after the decision "
                f"(stdout={stdout!r}, stderr={stderr!r})"
            )
        assert process.returncode == 0, stderr
        assert json.loads(stdout)["decision"] == expected_hook_decision

        with connect() as connection:
            assert connection.execute(
                "SELECT approval_id,status,source_invocation_id FROM approval_requests "
                "WHERE approval_id=%s AND workspace_id=%s AND source_agent_id=%s",
                (approval_id, workspace_record.id, agent.id),
            ).fetchall() == [(approval_id, expected_status, invocation_id)]

            def audit_count(event_type: str) -> int:
                return connection.execute(
                    "SELECT COUNT(*) FROM audit_events "
                    "WHERE entity_id=%s AND event_type=%s",
                    (approval_id, event_type),
                ).fetchone()[0]

            assert audit_count("approval.created_by_agent") == 1
            assert audit_count("approval.decision") == 1
            assert audit_count("approval.delivery_claimed") == expected_claims
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()[0] == expected_claims
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        _stop_postgres_approval_http_api(server)

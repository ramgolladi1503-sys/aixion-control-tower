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

import httpx
import pytest

from app.agent_credential_models import AgentCredentialRecord
from app.models import AgentProvider, AuditEvent, ApprovalStatus, new_id
from test_postgres_approval_route_flow import (
    _start_postgres_approval_http_api,
    _stop_postgres_approval_http_api,
)

pytest_plugins = ("test_postgres_approval_route_flow",)

REPOSITORY_ROOT = Path(__file__).parents[2]
HOOK_PATH = (
    REPOSITORY_ROOT
    / "integrations"
    / "codex"
    / "aixion_permission_request_hook.py"
)
SETUP_PATH = REPOSITORY_ROOT / "integrations" / "codex" / "setup_workspace.py"
SETUP_SPEC = importlib.util.spec_from_file_location("codex_installed_hook_setup", SETUP_PATH)
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
    token = f"postgres-installed-codex-token-{uuid.uuid4().hex}"
    agent = fixture_agent.model_copy(
        update={
            "id": new_id("agent"),
            "provider": AgentProvider.CODEX,
            "display_name": "Installed Codex PostgreSQL test",
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


@pytest.mark.parametrize(
    ("decision", "expected_behavior", "expected_status", "expected_claims"),
    [
        ("approve", "allow", ApprovalStatus.APPROVED.value, 1),
        ("deny", "deny", ApprovalStatus.DENIED.value, 0),
    ],
)
def test_installed_codex_permission_request_uses_postgres_for_exact_decision_and_replay(
    tmp_path: Path,
    postgres_approval_route_runtime,
    decision: str,
    expected_behavior: str,
    expected_status: str,
    expected_claims: int,
) -> None:
    """Exercise the installer-generated command against the isolated PG API."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, workspace, project, reviewer, _repositories, _fixture_agent, _token, schema = (
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
    process: subprocess.Popen[str] | None = None
    replay_process: subprocess.Popen[str] | None = None
    try:
        api_base_url = server[2]
        home = tmp_path / "isolated Codex home"
        workspace_path = tmp_path / "workspace with spaces"
        home.mkdir(mode=0o700)
        workspace_path.mkdir(mode=0o700)
        config_path, hooks_path = setup_workspace.install_workspace(
            workspace_path,
            api_base_url=api_base_url,
            agent_id=agent.id,
            project_id=project.id,
            home=home,
            hook_script=HOOK_PATH,
            python=sys.executable,
        )
        registered_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        command = registered_hooks["hooks"]["PermissionRequest"][0]["hooks"][0][
            "command"
        ]
        assert shlex.split(command) == [
            sys.executable,
            str(HOOK_PATH.resolve()),
            "--config",
            str(config_path),
        ]

        session_id = f"codex-installed-session-{uuid.uuid4().hex}"
        event = {
            "hook_event_name": "PermissionRequest",
            "session_id": session_id,
            "turn_id": f"turn-{uuid.uuid4().hex}",
            # Codex's current public schema does not list tool_use_id. This
            # optional field is the adapter's documented restart-stable path
            # when a host build supplies a stable ID; absence remains callback-scoped.
            "tool_use_id": f"call-{uuid.uuid4().hex}",
            "cwd": str(workspace_path),
            "permission_mode": "default",
            "tool_name": "Bash",
            "tool_input": {
                "command": "printf 'aixion codex approval test'",
                "description": "Run the isolated integration-test command",
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
                "AIXION_AGENT_TOKEN": token,
                "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
                "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "20",
            }
        )

        def start_installed_hook() -> subprocess.Popen[str]:
            child = subprocess.Popen(
                command,
                shell=True,
                executable="/bin/sh",
                cwd=workspace_path,
                env=child_environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert child.stdin is not None
            child.stdin.write(json.dumps(event))
            child.stdin.close()
            child.stdin = None
            return child

        process = start_installed_hook()
        approval_row: tuple[str, str, str] | None = None
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    "Installed Codex hook exited before creating its PostgreSQL "
                    f"approval (exit={process.returncode}, stdout={stdout!r}, "
                    f"stderr={stderr!r})"
                )
            with connect() as connection:
                approval_row = connection.execute(
                    "SELECT approval_id,source_invocation_id,status "
                    "FROM approval_requests WHERE workspace_id=%s "
                    "AND source_agent_id=%s AND payload->>'source_provider'=%s "
                    "AND payload->>'source_session_id' IS NOT NULL",
                    (workspace.id, agent.id, AgentProvider.CODEX.value),
                ).fetchone()
            if approval_row is not None:
                break
            time.sleep(0.05)
        assert approval_row is not None, (
            "Installed Codex command did not create a PostgreSQL approval before timeout"
        )

        approval_id, invocation_id, status = approval_row
        assert invocation_id
        assert status == ApprovalStatus.PENDING_REVIEW.value
        with connect() as connection:
            approval_count = connection.execute(
                "SELECT COUNT(*) FROM approval_requests WHERE workspace_id=%s "
                "AND source_agent_id=%s",
                (workspace.id, agent.id),
            ).fetchone()[0]
        assert approval_count == 1

        with httpx.Client(timeout=10) as client:
            response = client.post(
                f"{api_base_url}/approvals/{approval_id}/decision",
                json={
                    "decision": decision,
                    "reason": "installed Codex PostgreSQL integration test",
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
                "Installed Codex hook did not finish after its decision "
                f"(stdout={stdout!r}, stderr={stderr!r})"
            )
        assert process.returncode == 0, stderr
        hook_result = json.loads(stdout)
        assert hook_result["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
        assert hook_result["hookSpecificOutput"]["decision"]["behavior"] == (
            expected_behavior
        )

        # A same-event replay with the stable optional call identity must not
        # create a second request or claim an already-consumed approval again.
        replay_process = start_installed_hook()
        try:
            replay_stdout, replay_stderr = replay_process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            replay_process.kill()
            replay_stdout, replay_stderr = replay_process.communicate()
            pytest.fail(
                "Installed Codex replay did not finish "
                f"(stdout={replay_stdout!r}, stderr={replay_stderr!r})"
            )
        assert replay_process.returncode == 0, replay_stderr
        replay_result = json.loads(replay_stdout)
        assert replay_result["hookSpecificOutput"]["decision"]["behavior"] == "deny"

        with connect() as connection:
            row = connection.execute(
                "SELECT approval_id,status,source_invocation_id "
                "FROM approval_requests WHERE approval_id=%s AND workspace_id=%s "
                "AND source_agent_id=%s",
                (approval_id, workspace.id, agent.id),
            ).fetchone()
            assert row == (approval_id, expected_status, invocation_id)
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_requests WHERE workspace_id=%s "
                "AND source_agent_id=%s",
                (workspace.id, agent.id),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()[0] == expected_claims
            assert connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.delivery_claimed'",
                (approval_id,),
            ).fetchone()[0] == expected_claims
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_failures WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()[0] == 0
    finally:
        for child in (process, replay_process):
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        _stop_postgres_approval_http_api(server)


def test_installed_codex_callback_without_documented_call_id_cannot_reuse_approval(
    tmp_path: Path,
    postgres_approval_route_runtime,
) -> None:
    """A documented-field-only callback gets a fresh pending request after restart."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, workspace, project, reviewer, _repositories, _fixture_agent, _token, schema = (
        postgres_approval_route_runtime
    )
    workspace, project, reviewer, _repositories, agent, token = _codex_connector(
        postgres_approval_route_runtime
    )
    server = _start_postgres_approval_http_api(
        dsn=dsn,
        schema=schema,
        reviewer=reviewer,
    )
    lost_process: subprocess.Popen[str] | None = None
    replacement_process: subprocess.Popen[str] | None = None
    try:
        api_base_url = server[2]
        home = tmp_path / "callback-scoped Codex home"
        workspace_path = tmp_path / "callback-scoped workspace"
        home.mkdir(mode=0o700)
        workspace_path.mkdir(mode=0o700)
        config_path, hooks_path = setup_workspace.install_workspace(
            workspace_path,
            api_base_url=api_base_url,
            agent_id=agent.id,
            project_id=project.id,
            home=home,
            hook_script=HOOK_PATH,
            python=sys.executable,
        )
        command = json.loads(hooks_path.read_text(encoding="utf-8"))["hooks"][
            "PermissionRequest"
        ][0]["hooks"][0]["command"]
        assert shlex.split(command) == [
            sys.executable,
            str(HOOK_PATH.resolve()),
            "--config",
            str(config_path),
        ]
        event = {
            "hook_event_name": "PermissionRequest",
            "session_id": f"documented-codex-session-{uuid.uuid4().hex}",
            "turn_id": f"documented-codex-turn-{uuid.uuid4().hex}",
            "cwd": str(workspace_path),
            "permission_mode": "default",
            "tool_name": "Bash",
            "tool_input": {
                "command": "printf 'callback scoped request'",
                "description": "Run the isolated integration-test command",
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
                "AIXION_AGENT_TOKEN": token,
                "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
                # The first request must remain actionable while CI starts the
                # reviewer call; only the replacement callback should expire fast.
                "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "30",
            }
        )

        def start_callback() -> subprocess.Popen[str]:
            child = subprocess.Popen(
                command,
                shell=True,
                executable="/bin/sh",
                cwd=workspace_path,
                env=child_environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert child.stdin is not None
            child.stdin.write(json.dumps(event))
            child.stdin.close()
            child.stdin = None
            return child

        def approvals() -> list[tuple[str, str, str]]:
            with connect() as connection:
                return connection.execute(
                    "SELECT approval_id,source_invocation_id,status "
                    "FROM approval_requests WHERE workspace_id=%s "
                    "AND source_agent_id=%s AND payload->>'source_provider'=%s "
                    "ORDER BY created_at",
                    (workspace.id, agent.id, AgentProvider.CODEX.value),
                ).fetchall()

        lost_process = start_callback()
        deadline = time.monotonic() + 12
        initial_rows: list[tuple[str, str, str]] = []
        while time.monotonic() < deadline:
            initial_rows = approvals()
            if initial_rows:
                break
            assert lost_process.poll() is None, (
                "Initial installed Codex callback exited before creating its approval"
            )
            time.sleep(0.05)
        assert len(initial_rows) == 1, initial_rows
        old_approval_id, old_invocation_id, old_status = initial_rows[0]
        assert old_invocation_id
        assert old_status in {
            ApprovalStatus.PENDING_REVIEW.value,
            ApprovalStatus.REQUESTED.value,
        }
        lost_process.kill()
        lost_process.communicate(timeout=5)
        lost_process = None

        with httpx.Client(timeout=10) as client:
            decision = client.post(
                f"{api_base_url}/approvals/{old_approval_id}/decision",
                json={"decision": "approve", "reason": "Prior callback lost."},
            )
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == ApprovalStatus.APPROVED.value

        child_environment["AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS"] = "5"
        replacement_process = start_callback()
        deadline = time.monotonic() + 12
        rows = initial_rows
        while time.monotonic() < deadline:
            rows = approvals()
            if len(rows) > 1:
                break
            assert replacement_process.poll() is None, (
                "Replacement callback exited before creating its own approval"
            )
            time.sleep(0.05)
        assert len(rows) == 2, rows
        old_row = next(row for row in rows if row[0] == old_approval_id)
        new_row = next(row for row in rows if row[0] != old_approval_id)
        assert old_row[1] == old_invocation_id
        assert new_row[1]
        assert new_row[0] != old_approval_id
        assert new_row[1] != old_invocation_id
        assert new_row[2] in {
            ApprovalStatus.PENDING_REVIEW.value,
            ApprovalStatus.REQUESTED.value,
            ApprovalStatus.EXPIRED.value,
        }

        try:
            stdout, stderr = replacement_process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            replacement_process.kill()
            stdout, stderr = replacement_process.communicate()
            pytest.fail(
                "Replacement callback did not expire and deny its unapproved request "
                f"(stdout={stdout!r}, stderr={stderr!r})"
            )
        assert replacement_process.returncode == 0, stderr
        result = json.loads(stdout)
        assert result["hookSpecificOutput"]["decision"]["behavior"] == "deny"

        final_rows = approvals()
        assert len(final_rows) == 2
        assert next(row for row in final_rows if row[0] == old_approval_id)[2] == (
            ApprovalStatus.APPROVED.value
        )
        assert next(row for row in final_rows if row[0] != old_approval_id)[2] == (
            ApprovalStatus.EXPIRED.value
        )
        with connect() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims "
                "WHERE approval_id IN (%s, %s)",
                (old_approval_id, new_row[0]),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id IN (%s, %s) "
                "AND event_type='approval.delivery_claimed'",
                (old_approval_id, new_row[0]),
            ).fetchone()[0] == 0
    finally:
        for child in (lost_process, replacement_process):
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        _stop_postgres_approval_http_api(server)

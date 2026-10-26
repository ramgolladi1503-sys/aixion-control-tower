from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from integrations.antigravity import setup_workspace
from app import agent_routes, main
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


def _reviewed_decision_payload(
    client: httpx.Client,
    *,
    base_url: str,
    approval_id: str,
    decision: str,
    reason: str,
) -> dict[str, str]:
    reviewed = client.get(f"{base_url}/approvals/{approval_id}")
    assert reviewed.status_code == 200, reviewed.text
    fingerprint = reviewed.json().get("review_fingerprint")
    assert isinstance(fingerprint, str) and fingerprint
    return {
        "decision": decision,
        "reason": reason,
        "review_fingerprint": fingerprint,
    }


@pytest.mark.parametrize(
    ("decision", "expected_hook_decision", "expected_status", "expected_claims"),
    [
        ("approve", "auto_approve", "APPROVED", 1),
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
        with connect() as connection:
            pending_approval = connection.execute(
                "SELECT status,source_invocation_id FROM approval_requests "
                "WHERE approval_id=%s AND workspace_id=%s AND source_agent_id=%s",
                (approval_id, workspace_record.id, agent.id),
            ).fetchone()
            creation_audits = connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.created_by_agent'",
                (approval_id,),
            ).fetchone()[0]
            notifications_before_decision = connection.execute(
                "SELECT notification_id,status,attempts,next_attempt_at,lease_expires_at,payload "
                "FROM notifications WHERE approval_id=%s ORDER BY notification_id",
                (approval_id,),
            ).fetchall()
        assert pending_approval == ("PENDING_REVIEW", invocation_id)
        assert process.poll() is None, (
            "Installed Antigravity hook must keep the exact invocation blocked "
            "until the phone decision arrives"
        )
        assert creation_audits == 1
        assert len(notifications_before_decision) == 1

        with httpx.Client(timeout=10) as client:
            response = client.post(
                f"{api_base_url}/approvals/{approval_id}/decision",
                json=_reviewed_decision_payload(
                    client,
                    base_url=api_base_url,
                    approval_id=approval_id,
                    decision=decision,
                    reason="installed Antigravity PostgreSQL integration test",
                ),
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
            notifications_after_decision = connection.execute(
                "SELECT notification_id,status,attempts,next_attempt_at,lease_expires_at,payload "
                "FROM notifications WHERE approval_id=%s ORDER BY notification_id",
                (approval_id,),
            ).fetchall()
            assert len(notifications_after_decision) == len(notifications_before_decision) + 1
            notifications_by_id = {
                row[0]: row for row in notifications_after_decision
            }
            assert all(
                notifications_by_id.get(row[0]) == row
                for row in notifications_before_decision
            )
            decision_notification_title = (
                f"Approval {expected_status.lower()}: Antigravity approval: run_command"
            )
            assert connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE approval_id=%s "
                "AND payload->>'title'=%s AND payload->>'body'=%s",
                (
                    approval_id,
                    decision_notification_title,
                    "installed Antigravity PostgreSQL integration test",
                ),
            ).fetchone()[0] == 1
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        _stop_postgres_approval_http_api(server)


def test_installed_antigravity_command_expires_without_phone_decision(
    tmp_path: Path,
    postgres_approval_route_runtime,
) -> None:
    """A missing phone decision expires durably and denies the exact invocation."""
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
        home = tmp_path / "timeout installed hook home"
        workspace = tmp_path / "timeout installed hook workspace"
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

        session_id = f"postgres-installed-hook-timeout-{time.time_ns()}"
        payload = {
            "conversationId": session_id,
            "stepIdx": 29,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "echo installed-hook-postgres-timeout"},
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
                "AIXION_APPROVAL_TIMEOUT_SECONDS": "1",
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

        approval_row: tuple[str, str, str] | None = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    "Installed Antigravity hook exited before creating its "
                    f"PostgreSQL approval (exit={process.returncode}, "
                    f"stdout={stdout!r}, stderr={stderr!r})"
                )
            with connect() as connection:
                rows = connection.execute(
                    "SELECT approval_id,source_invocation_id,status "
                    "FROM approval_requests WHERE workspace_id=%s "
                    "AND source_agent_id=%s AND payload->>'source_session_id'=%s",
                    (workspace_record.id, agent.id, session_id),
                ).fetchall()
            assert len(rows) <= 1, "installed hook created duplicate approval rows"
            if rows:
                approval_row = rows[0]
                break
            time.sleep(0.05)

        assert approval_row is not None, (
            "Installed command did not create a PostgreSQL approval before timeout"
        )
        approval_id, invocation_id, initial_status = approval_row
        assert invocation_id
        assert initial_status == "PENDING_REVIEW"
        assert process.poll() is None, "installed hook should wait for the phone decision"

        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            pytest.fail(
                "Installed Antigravity hook did not exit after its approval timeout "
                f"(stdout={stdout!r}, stderr={stderr!r})"
            )
        assert process.returncode == 0, stderr
        assert json.loads(stdout) == {
            "decision": "deny",
            "reason": "Approval timed out; the request expired and the tool call was denied",
        }

        with connect() as connection:
            persisted_approval = connection.execute(
                "SELECT approval_id,status,workspace_id,source_agent_id,source_invocation_id "
                "FROM approval_requests WHERE approval_id=%s AND workspace_id=%s "
                "AND source_agent_id=%s AND source_invocation_id=%s "
                "AND payload->>'source_session_id'=%s",
                (approval_id, workspace_record.id, agent.id, invocation_id, session_id),
            ).fetchone()
            expired_audits = connection.execute(
                "SELECT COUNT(*) FROM audit_events "
                "WHERE approval_id=%s AND event_type='approval.expired'",
                (approval_id,),
            ).fetchone()[0]
            claimed_audits = connection.execute(
                "SELECT COUNT(*) FROM audit_events "
                "WHERE approval_id=%s AND event_type='approval.delivery_claimed'",
                (approval_id,),
            ).fetchone()[0]
            delivery_claims = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()[0]

        assert persisted_approval == (
            approval_id,
            "EXPIRED",
            workspace_record.id,
            agent.id,
            invocation_id,
        )
        assert expired_audits == 1
        assert claimed_audits == 0
        assert delivery_claims == 0
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        _stop_postgres_approval_http_api(server)


def test_installed_antigravity_command_keeps_live_invocation_across_postgres_api_restart(
    tmp_path: Path,
    postgres_approval_route_runtime,
) -> None:
    """The installed hook process resumes only its exact live PG invocation after API restart."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, workspace_record, project, reviewer, _repositories, agent, agent_token, schema = (
        postgres_approval_route_runtime
    )
    servers: list[tuple[object, object, str]] = []
    process: subprocess.Popen[str] | None = None

    def start_server(*, port: int | None = None):
        server = _start_postgres_approval_http_api(
            dsn=dsn,
            schema=schema,
            reviewer=reviewer,
            port=port,
        )
        servers.append(server)
        return server

    try:
        first_server = start_server()
        home = tmp_path / "installed hook restart home"
        workspace = tmp_path / "installed hook restart workspace"
        workspace.mkdir(mode=0o700)
        config_path, hooks_path = setup_workspace.install_workspace(
            workspace,
            api_base_url=first_server[2],
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

        session_id = f"installed-postgres-restart-{time.time_ns()}"
        payload = {
            "conversationId": session_id,
            "stepIdx": 29,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "echo installed-hook-api-restart"},
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
                "AIXION_APPROVAL_TIMEOUT_SECONDS": "120",
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
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    "Installed Antigravity hook exited before PostgreSQL approval "
                    f"(exit={process.returncode}, stdout={stdout!r}, stderr={stderr!r})"
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
        assert approval_row is not None, "Installed hook did not create its PostgreSQL request"
        approval_id, invocation_id = approval_row
        assert process.poll() is None, "Installed hook must remain blocked for this invocation"

        with connect() as connection:
            assert connection.execute(
                "SELECT workspace_id,source_agent_id,source_invocation_id,expires_at "
                "FROM approval_invocation_leases WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()[:3] == (workspace_record.id, agent.id, invocation_id)

        # Reap only the API process. The installed hook and its exact invocation
        # stay alive while the new server binds to the same loopback endpoint.
        _stop_postgres_approval_http_api(first_server)
        # Keep the endpoint unavailable past the hook's two-second poll
        # interval. This forces its live decision read to observe connection
        # failure, rather than merely restarting the API between polls.
        time.sleep(3)
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                "Hook exited while the API was unavailable "
                f"(exit={process.returncode}, stdout={stdout!r}, stderr={stderr!r})"
            )
        restart_port_value = httpx.URL(first_server[2]).port
        assert restart_port_value is not None
        restart_port = int(restart_port_value)
        restarted_server = start_server(port=restart_port)
        assert restarted_server[2] == first_server[2]
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                "Hook exited before the reviewer decision after API restart "
                f"(exit={process.returncode}, stdout={stdout!r}, stderr={stderr!r})"
            )

        with httpx.Client(timeout=10) as reviewer_client:
            decision = reviewer_client.post(
                f"{restarted_server[2]}/approvals/{approval_id}/decision",
                json=_reviewed_decision_payload(
                    reviewer_client,
                    base_url=restarted_server[2],
                    approval_id=approval_id,
                    decision="approve",
                    reason="approve after API process restart",
                ),
            )
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == "APPROVED"

        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        assert json.loads(stdout) == {
            "decision": "auto_approve",
            "reason": "Approved from Aixion Control Tower",
        }

        with connect() as connection:
            assert connection.execute(
                "SELECT approval_id,status,source_invocation_id FROM approval_requests "
                "WHERE approval_id=%s AND workspace_id=%s AND source_agent_id=%s",
                (approval_id, workspace_record.id, agent.id),
            ).fetchall() == [(approval_id, "APPROVED", invocation_id)]
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()[0] == 1
            for event_type in (
                "approval.created_by_agent",
                "approval.decision",
                "approval.delivery_claimed",
            ):
                assert connection.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s AND event_type=%s",
                    (approval_id, event_type),
                ).fetchone()[0] == 1
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for server in reversed(servers):
            if server[0].is_alive():
                _stop_postgres_approval_http_api(server)


def test_installed_antigravity_hook_relaunch_recovers_exact_postgres_invocation_once(
    tmp_path: Path,
    postgres_approval_route_runtime,
) -> None:
    """A fresh hook process reattaches to one exact durable invocation in PostgreSQL."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, workspace_record, project, reviewer, _repositories, agent, agent_token, schema = (
        postgres_approval_route_runtime
    )
    server = _start_postgres_approval_http_api(
        dsn=dsn,
        schema=schema,
        reviewer=reviewer,
    )
    first_process: subprocess.Popen[str] | None = None
    recovered_process: subprocess.Popen[str] | None = None

    try:
        home = tmp_path / "installed hook relaunch home"
        workspace = tmp_path / "installed hook relaunch workspace"
        workspace.mkdir(mode=0o700)
        config_path, hooks_path = setup_workspace.install_workspace(
            workspace,
            api_base_url=server[2],
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
        argv = shlex.split(command)
        assert argv == [
            sys.executable,
            str(HOOK_PATH.resolve()),
            "--config",
            str(config_path),
        ]

        session_id = f"installed-postgres-hook-relaunch-{time.time_ns()}"
        payload = {
            "conversationId": session_id,
            "stepIdx": 53,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "echo exact-postgres-hook-relaunch"},
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
                "AIXION_APPROVAL_TIMEOUT_SECONDS": "60",
            }
        )

        def launch_hook() -> subprocess.Popen[str]:
            process = subprocess.Popen(
                argv,
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
            return process

        def find_approval() -> tuple[str, str, str] | None:
            with connect() as connection:
                rows = connection.execute(
                    "SELECT approval_id,source_invocation_id,status "
                    "FROM approval_requests WHERE workspace_id=%s "
                    "AND source_agent_id=%s AND payload->>'source_session_id'=%s",
                    (workspace_record.id, agent.id, session_id),
                ).fetchall()
            assert len(rows) <= 1, "same callback created duplicate PostgreSQL approvals"
            return rows[0] if rows else None

        # Stop the original host hook after it has durably created its pending
        # request, as happens when the provider relaunches after a host restart.
        first_process = launch_hook()
        deadline = time.monotonic() + 15
        first_row: tuple[str, str, str] | None = None
        while time.monotonic() < deadline:
            if first_process.poll() is not None:
                stdout, stderr = first_process.communicate()
                pytest.fail(
                    "Installed Antigravity hook exited before PostgreSQL approval "
                    f"(exit={first_process.returncode}, stdout={stdout!r}, stderr={stderr!r})"
                )
            first_row = find_approval()
            if first_row is not None:
                break
            time.sleep(0.05)
        assert first_row is not None, "Installed hook did not persist its PostgreSQL request"
        approval_id, invocation_id, status = first_row
        assert status == "PENDING_REVIEW"
        assert invocation_id
        with connect() as connection:
            lease = connection.execute(
                "SELECT workspace_id,source_agent_id,source_invocation_id,expires_at, "
                "expires_at > CURRENT_TIMESTAMP "
                "FROM approval_invocation_leases WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()
        assert lease is not None
        assert lease[:3] == (workspace_record.id, agent.id, invocation_id)
        assert lease[4] is True, "test must relaunch while the invocation lease is live"
        first_process.kill()
        first_process.communicate(timeout=5)
        first_process = None

        # A new interpreter receives the exact same provider callback while
        # the durable lease is live. It must attach, not create or re-key it.
        recovered_process = launch_hook()
        deadline = time.monotonic() + 15
        recovered_row: tuple[str, str, str] | None = None
        while time.monotonic() < deadline:
            if recovered_process.poll() is not None:
                stdout, stderr = recovered_process.communicate()
                pytest.fail(
                    "Relaunched Antigravity hook exited before reviewer decision "
                    f"(exit={recovered_process.returncode}, stdout={stdout!r}, stderr={stderr!r})"
                )
            recovered_row = find_approval()
            if recovered_row is not None:
                break
            time.sleep(0.05)
        assert recovered_row == (approval_id, invocation_id, "PENDING_REVIEW")
        assert recovered_process.poll() is None

        with httpx.Client(timeout=10) as reviewer_client:
            decision = reviewer_client.post(
                f"{server[2]}/approvals/{approval_id}/decision",
                json=_reviewed_decision_payload(
                    reviewer_client,
                    base_url=server[2],
                    approval_id=approval_id,
                    decision="approve",
                    reason="approve after hook process relaunch",
                ),
            )
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == "APPROVED"

        stdout, stderr = recovered_process.communicate(timeout=20)
        assert recovered_process.returncode == 0, stderr
        assert json.loads(stdout) == {
            "decision": "auto_approve",
            "reason": "Approved from Aixion Control Tower",
        }
        recovered_process = None

        # Replaying a consumed invocation cannot authorize the same tool call a
        # second time; the durable claim remains unique for this identity.
        replay = subprocess.run(
            argv,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=workspace,
            env=child_environment,
            timeout=20,
            check=False,
        )
        assert replay.returncode == 0, replay.stderr
        replay_output = json.loads(replay.stdout)
        assert replay_output["decision"] == "deny"

        with connect() as connection:
            assert connection.execute(
                "SELECT approval_id,status,source_invocation_id FROM approval_requests "
                "WHERE workspace_id=%s AND source_agent_id=%s AND source_invocation_id=%s",
                (workspace_record.id, agent.id, invocation_id),
            ).fetchall() == [(approval_id, "APPROVED", invocation_id)]
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()[0] == 1
            for event_type in (
                "approval.created_by_agent",
                "approval.decision",
                "approval.delivery_claimed",
            ):
                assert connection.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s AND event_type=%s",
                    (approval_id, event_type),
                ).fetchone()[0] == 1
    finally:
        for process in (first_process, recovered_process):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        _stop_postgres_approval_http_api(server)


def test_installed_antigravity_expired_invocation_lease_rejects_reviewer_decision(
    tmp_path: Path,
    postgres_approval_route_runtime,
) -> None:
    """An expired connector lease fences review without decision side effects."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, workspace_record, project, reviewer, _repositories, agent, agent_token, schema = (
        postgres_approval_route_runtime
    )
    server = _start_postgres_approval_http_api(
        dsn=dsn,
        schema=schema,
        reviewer=reviewer,
    )
    hook_process: subprocess.Popen[str] | None = None

    try:
        home = tmp_path / "expired installed hook home"
        workspace = tmp_path / "expired installed hook workspace"
        workspace.mkdir(mode=0o700)
        config_path, hooks_path = setup_workspace.install_workspace(
            workspace,
            api_base_url=server[2],
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
        argv = shlex.split(command)
        assert argv == [
            sys.executable,
            str(HOOK_PATH.resolve()),
            "--config",
            str(config_path),
        ]

        session_id = f"installed-postgres-expired-lease-{time.time_ns()}"
        payload = {
            "conversationId": session_id,
            "stepIdx": 61,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "echo expired-invocation-lease"},
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
                "AIXION_APPROVAL_TIMEOUT_SECONDS": "60",
            }
        )
        hook_process = subprocess.Popen(
            argv,
            cwd=workspace,
            env=child_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert hook_process.stdin is not None
        hook_process.stdin.write(json.dumps(payload))
        hook_process.stdin.close()
        hook_process.stdin = None

        approval_row: tuple[str, str, str] | None = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if hook_process.poll() is not None:
                stdout, stderr = hook_process.communicate()
                pytest.fail(
                    "Installed hook exited before pending approval was durable "
                    f"(exit={hook_process.returncode}, stdout={stdout!r}, stderr={stderr!r})"
                )
            with connect() as connection:
                rows = connection.execute(
                    "SELECT approval_id,source_invocation_id,status FROM approval_requests "
                    "WHERE workspace_id=%s AND source_agent_id=%s "
                    "AND payload->>'source_session_id'=%s",
                    (workspace_record.id, agent.id, session_id),
                ).fetchall()
            assert len(rows) <= 1, "installed hook created duplicate approval rows"
            if rows:
                approval_row = rows[0]
                break
            time.sleep(0.05)

        assert approval_row is not None, "Installed hook did not create its PostgreSQL approval"
        approval_id, invocation_id, status = approval_row
        assert status == "PENDING_REVIEW"
        assert invocation_id
        assert hook_process.poll() is None, "hook should be blocked awaiting review"

        # Stop the test-owned connector process, then expire only its lease.
        # Keep the approval itself pending and within its approval deadline.
        hook_process.kill()
        hook_process.communicate(timeout=5)
        hook_process = None
        with connect() as connection:
            approval_before = connection.execute(
                "SELECT status,payload FROM approval_requests WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()
            audit_before = connection.execute(
                "SELECT event_id,event_type,workspace_id,actor,details,payload "
                "FROM audit_events WHERE approval_id=%s ORDER BY event_id",
                (approval_id,),
            ).fetchall()
            notifications_before = connection.execute(
                "SELECT notification_id,status,payload FROM notifications "
                "WHERE approval_id=%s ORDER BY notification_id",
                (approval_id,),
            ).fetchall()
            update = connection.execute(
                "UPDATE approval_invocation_leases "
                "SET heartbeat_at=%s,expires_at=%s "
                "WHERE approval_id=%s AND workspace_id=%s AND source_agent_id=%s "
                "AND source_invocation_id=%s",
                (
                    "1999-12-31 23:59:59+00",
                    "2000-01-01 00:00:00+00",
                    approval_id,
                    workspace_record.id,
                    agent.id,
                    invocation_id,
                ),
            )
            assert update.rowcount == 1, "expected to expire exactly this test invocation lease"
            lease = connection.execute(
                "SELECT workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at, "
                "expires_at < CURRENT_TIMESTAMP "
                "FROM approval_invocation_leases WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()
            assert lease is not None
            assert lease[:3] == (workspace_record.id, agent.id, invocation_id)
            assert lease[5] is True, "test fixture must expire the invocation lease"

        # The durable reviewer endpoint must reject the decision because this
        # native invocation is no longer live, without committing any decision,
        # notification, or delivery-claim side effects.
        with httpx.Client(timeout=10) as reviewer_client:
            decision = reviewer_client.post(
                f"{server[2]}/approvals/{approval_id}/decision",
                json=_reviewed_decision_payload(
                    reviewer_client,
                    base_url=server[2],
                    approval_id=approval_id,
                    decision="approve",
                    reason="attempt after invocation lease expiry",
                ),
            )
        assert decision.status_code == 409, decision.text

        # A provider restart may replay the same callback. The durable pending
        # approval must not let a new hook process reclaim an invocation whose
        # liveness lease has expired.
        hook_process = subprocess.Popen(
            argv,
            cwd=workspace,
            env=child_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert hook_process.stdin is not None
        hook_process.stdin.write(json.dumps(payload))
        hook_process.stdin.close()
        hook_process.stdin = None
        stdout, stderr = hook_process.communicate(timeout=25)
        assert hook_process.returncode == 0, stderr
        assert json.loads(stdout)["decision"] == "deny"
        hook_process = None

        with connect() as connection:
            approval_after = connection.execute(
                "SELECT status,payload FROM approval_requests WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()
            audit_after = connection.execute(
                "SELECT event_id,event_type,workspace_id,actor,details,payload "
                "FROM audit_events WHERE approval_id=%s ORDER BY event_id",
                (approval_id,),
            ).fetchall()
            notifications_after = connection.execute(
                "SELECT notification_id,status,payload FROM notifications "
                "WHERE approval_id=%s ORDER BY notification_id",
                (approval_id,),
            ).fetchall()
            claims = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()[0]

        assert approval_before == ("PENDING_REVIEW", approval_after[1])
        assert approval_after[0] == "PENDING_REVIEW"
        assert audit_after == audit_before
        assert not any(row[1] == "approval.decision" for row in audit_after)
        assert notifications_after == notifications_before
        assert claims == 0

        # A connector process may restart with a callback that has a fresh
        # provider invocation identity. It must not inherit the expired
        # invocation lease or consume any decision attached to the old call.
        # The new callback is allowed to create its own independent pending
        # approval, which can only be claimed after its own reviewer decision.
        fresh_session_id = f"{session_id}-fresh-invocation"
        fresh_payload = {
            **payload,
            "conversationId": fresh_session_id,
            "stepIdx": payload["stepIdx"] + 1,
        }
        hook_process = subprocess.Popen(
            argv,
            cwd=workspace,
            env=child_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert hook_process.stdin is not None
        hook_process.stdin.write(json.dumps(fresh_payload))
        hook_process.stdin.close()
        hook_process.stdin = None

        fresh_row: tuple[str, str, str] | None = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if hook_process.poll() is not None:
                stdout, stderr = hook_process.communicate()
                pytest.fail(
                    "Fresh connector invocation exited before creating its own "
                    f"PostgreSQL approval (exit={hook_process.returncode}, "
                    f"stdout={stdout!r}, stderr={stderr!r})"
                )
            with connect() as connection:
                rows = connection.execute(
                    "SELECT approval_id,source_invocation_id,status FROM approval_requests "
                    "WHERE workspace_id=%s AND source_agent_id=%s "
                    "AND payload->>'source_session_id'=%s",
                    (workspace_record.id, agent.id, fresh_session_id),
                ).fetchall()
            assert len(rows) <= 1, "fresh invocation created duplicate approval rows"
            if rows:
                fresh_row = rows[0]
                break
            time.sleep(0.05)

        assert fresh_row is not None, "fresh connector invocation created no durable approval"
        fresh_approval_id, fresh_invocation_id, fresh_status = fresh_row
        assert fresh_approval_id != approval_id
        assert fresh_invocation_id != invocation_id
        assert fresh_status == "PENDING_REVIEW"
        with connect() as connection:
            old_claims_before_fresh_decision = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()[0]
            fresh_lease = connection.execute(
                "SELECT workspace_id,source_agent_id,source_invocation_id,expires_at "
                "FROM approval_invocation_leases WHERE approval_id=%s",
                (fresh_approval_id,),
            ).fetchone()
        assert old_claims_before_fresh_decision == 0
        assert fresh_lease is not None
        assert fresh_lease[:3] == (workspace_record.id, agent.id, fresh_invocation_id)

        with httpx.Client(timeout=10) as reviewer_client:
            fresh_decision = reviewer_client.post(
                f"{server[2]}/approvals/{fresh_approval_id}/decision",
                json=_reviewed_decision_payload(
                    reviewer_client,
                    base_url=server[2],
                    approval_id=fresh_approval_id,
                    decision="approve",
                    reason="approve fresh invocation only",
                ),
            )
        assert fresh_decision.status_code == 200, fresh_decision.text
        fresh_stdout, fresh_stderr = hook_process.communicate(timeout=20)
        assert hook_process.returncode == 0, fresh_stderr
        assert json.loads(fresh_stdout)["decision"] == "auto_approve"
        hook_process = None

        with connect() as connection:
            old_final = connection.execute(
                "SELECT status,source_invocation_id FROM approval_requests WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()
            old_claims = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()[0]
            fresh_claims = connection.execute(
                "SELECT source_agent_id,source_invocation_id FROM approval_delivery_claims "
                "WHERE approval_id=%s",
                (fresh_approval_id,),
            ).fetchall()
            fresh_decision_audits = connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE approval_id=%s "
                "AND event_type='approval.decision'",
                (fresh_approval_id,),
            ).fetchone()[0]
        assert old_final == ("PENDING_REVIEW", invocation_id)
        assert old_claims == 0
        assert fresh_claims == [(agent.id, fresh_invocation_id)]
        assert fresh_decision_audits == 1
    finally:
        if hook_process is not None and hook_process.poll() is None:
            hook_process.terminate()
            try:
                hook_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                hook_process.kill()
                hook_process.wait(timeout=5)
        _stop_postgres_approval_http_api(server)


def test_installed_antigravity_hook_denies_after_postgres_credential_revocation(
    tmp_path: Path,
    postgres_approval_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revoked installer credential cannot deliver an approval decision."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, workspace_record, project, reviewer, _repositories, agent, agent_token, schema = (
        postgres_approval_route_runtime
    )
    server = _start_postgres_approval_http_api(
        dsn=dsn,
        schema=schema,
        reviewer=reviewer,
    )
    hook_process: subprocess.Popen[str] | None = None

    try:
        home = tmp_path / "revoked installed hook home"
        workspace = tmp_path / "revoked installed hook workspace"
        workspace.mkdir(mode=0o700)
        config_path, hooks_path = setup_workspace.install_workspace(
            workspace,
            api_base_url=server[2],
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
        argv = shlex.split(command)
        assert argv == [
            sys.executable,
            str(HOOK_PATH.resolve()),
            "--config",
            str(config_path),
        ]

        session_id = f"installed-postgres-revoked-credential-{time.time_ns()}"
        payload = {
            "conversationId": session_id,
            "stepIdx": 73,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "echo revoked-credential-fails-closed"},
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
                "AIXION_APPROVAL_TIMEOUT_SECONDS": "60",
            }
        )
        hook_process = subprocess.Popen(
            argv,
            cwd=workspace,
            env=child_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert hook_process.stdin is not None
        hook_process.stdin.write(json.dumps(payload))
        hook_process.stdin.close()
        hook_process.stdin = None

        approval_row: tuple[str, str, str] | None = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if hook_process.poll() is not None:
                stdout, stderr = hook_process.communicate()
                pytest.fail(
                    "Installed hook exited before creating its PostgreSQL approval "
                    f"(exit={hook_process.returncode}, stdout={stdout!r}, stderr={stderr!r})"
                )
            with connect() as connection:
                rows = connection.execute(
                    "SELECT approval_id,source_invocation_id,status FROM approval_requests "
                    "WHERE workspace_id=%s AND source_agent_id=%s "
                    "AND payload->>'source_session_id'=%s",
                    (workspace_record.id, agent.id, session_id),
                ).fetchall()
            assert len(rows) <= 1, "installed hook created duplicate approval rows"
            if rows:
                approval_row = rows[0]
                break
            time.sleep(0.05)

        assert approval_row is not None, "Installed hook did not create a PostgreSQL approval"
        approval_id, invocation_id, status = approval_row
        assert invocation_id
        assert status == "PENDING_REVIEW"
        assert hook_process.poll() is None, "installed hook should be waiting for review"

        # Exercise the actual owner-only HTTP route against the pending hook.
        # Revocation must commit in one request while the hook is still polling;
        # a 409 would leave the connector active and is a product-level failure.
        monkeypatch.setitem(
            main.app.dependency_overrides,
            agent_routes.OwnerDependency.dependency,
            lambda: reviewer,
        )
        owner_client = TestClient(main.app)
        try:
            response = owner_client.post(f"/agents/{agent.id}/token/revoke")
        finally:
            owner_client.close()
        assert response.status_code == 200, (
            "owner token-revoke route must commit during active hook polling; "
            f"got HTTP {response.status_code}: {response.text}"
        )

        stdout, stderr = hook_process.communicate(timeout=15)
        assert agent_token not in stdout
        assert agent_token not in stderr
        assert hook_process.returncode == 0, stderr
        assert json.loads(stdout)["decision"] == "deny"

        with connect() as connection:
            approval_after = connection.execute(
                "SELECT status,source_invocation_id FROM approval_requests "
                "WHERE approval_id=%s AND workspace_id=%s AND source_agent_id=%s",
                (approval_id, workspace_record.id, agent.id),
            ).fetchone()
            revoked = connection.execute(
                "SELECT token_revoked_at FROM external_agent_credentials "
                "WHERE agent_id=%s AND workspace_id=%s",
                (agent.id, workspace_record.id),
            ).fetchone()
            audit_events = connection.execute(
                "SELECT event_type FROM audit_events "
                "WHERE (entity_id=%s AND event_type='agent.token_revoked') "
                "OR (approval_id=%s AND event_type='approval.delivery_claimed')",
                (agent.id, approval_id),
            ).fetchall()
            claims = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()[0]

        assert approval_after == ("PENDING_REVIEW", invocation_id)
        assert revoked is not None and revoked[0] is not None
        assert [event[0] for event in audit_events] == ["agent.token_revoked"]
        assert claims == 0
    finally:
        if hook_process is not None and hook_process.poll() is None:
            hook_process.terminate()
            try:
                hook_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                hook_process.kill()
                hook_process.wait(timeout=5)
        _stop_postgres_approval_http_api(server)


def test_installed_antigravity_hook_denies_when_postgres_consume_response_is_lost(
    tmp_path: Path,
    postgres_approval_route_runtime,
) -> None:
    """A committed consume with a dropped response cannot authorize on replay."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, workspace_record, project, reviewer, _repositories, agent, agent_token, schema = (
        postgres_approval_route_runtime
    )
    api_server = _start_postgres_approval_http_api(
        dsn=dsn,
        schema=schema,
        reviewer=reviewer,
    )
    api_base_url = api_server[2]
    dropped_consume_response = threading.Event()
    proxy_errors: list[str] = []
    proxy_lock = threading.Lock()
    consume_drop_state = {"dropped": False}

    class ApprovalResponseDropProxy(BaseHTTPRequestHandler):
        """Forward API traffic, then sever one successful consume response."""

        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._forward()

        def do_POST(self) -> None:
            self._forward()

        def _forward(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in {"host", "content-length", "connection"}
            }
            try:
                with httpx.Client(timeout=10) as client:
                    upstream_response = client.request(
                        self.command,
                        f"{api_base_url}{self.path}",
                        headers=headers,
                        content=body,
                    )
                is_consume = self.command == "POST" and self.path.endswith("/consume")
                should_drop = False
                if is_consume and upstream_response.status_code == 200:
                    with proxy_lock:
                        if not consume_drop_state["dropped"]:
                            consume_drop_state["dropped"] = True
                            should_drop = True
                if should_drop:
                    approval_id = self.path.split("/")[-2]
                    # The upstream response is returned only after the API
                    # route has completed. Verify the claim and its audit are
                    # visible from a separate PostgreSQL connection before
                    # dropping the downstream response. This makes the fault
                    # point deterministic and proves commit-before-drop.
                    with connect() as connection:
                        claim = connection.execute(
                            "SELECT approval_id,workspace_id,source_agent_id "
                            "FROM approval_delivery_claims WHERE approval_id=%s",
                            (approval_id,),
                        ).fetchone()
                        claim_audits = connection.execute(
                            "SELECT COUNT(*) FROM audit_events WHERE approval_id=%s "
                            "AND event_type='approval.delivery_claimed'",
                            (approval_id,),
                        ).fetchone()[0]
                    if (
                        upstream_response.json() != {"consumed": True}
                        or claim != (approval_id, workspace_record.id, agent.id)
                        or claim_audits != 1
                    ):
                        proxy_errors.append(
                            "upstream consume response arrived without one durable "
                            f"claim and audit: body={upstream_response.text!r}, "
                            f"claim={claim!r}, audits={claim_audits}"
                        )
                        self.send_response(502)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    dropped_consume_response.set()
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return

                response_body = upstream_response.content
                self.send_response(upstream_response.status_code)
                for name, value in upstream_response.headers.items():
                    if name.lower() not in {
                        "content-length",
                        "connection",
                        "transfer-encoding",
                        "content-encoding",
                    }:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                if response_body:
                    self.wfile.write(response_body)
            except (httpx.HTTPError, OSError, ValueError) as exc:
                proxy_errors.append(f"proxy forwarding failed: {exc!r}")
                if not self.wfile.closed:
                    try:
                        self.send_response(502)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    except OSError:
                        pass

        def log_message(self, _format: str, *_args: object) -> None:
            return

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ApprovalResponseDropProxy)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    proxy_base_url = f"http://127.0.0.1:{proxy.server_address[1]}"
    process: subprocess.Popen[str] | None = None

    try:
        home = tmp_path / "lost consume response home"
        workspace = tmp_path / "lost consume response workspace"
        workspace.mkdir(mode=0o700)
        config_path, hooks_path = setup_workspace.install_workspace(
            workspace,
            api_base_url=proxy_base_url,
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
        argv = shlex.split(command)
        assert argv == [
            sys.executable,
            str(HOOK_PATH.resolve()),
            "--config",
            str(config_path),
        ]

        session_id = f"installed-postgres-lost-consume-{time.time_ns()}"
        payload = {
            "conversationId": session_id,
            "stepIdx": 83,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "echo lost-consume-response-must-deny"},
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
                "AIXION_APPROVAL_TIMEOUT_SECONDS": "60",
            }
        )

        process = subprocess.Popen(
            argv,
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

        approval_row: tuple[str, str, str] | None = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    "Installed hook exited before creating its PostgreSQL request "
                    f"(exit={process.returncode}, stdout={stdout!r}, stderr={stderr!r})"
                )
            with connect() as connection:
                rows = connection.execute(
                    "SELECT approval_id,source_invocation_id,status FROM approval_requests "
                    "WHERE workspace_id=%s AND source_agent_id=%s "
                    "AND payload->>'source_session_id'=%s",
                    (workspace_record.id, agent.id, session_id),
                ).fetchall()
            assert len(rows) <= 1, "installed hook created duplicate PostgreSQL approvals"
            if rows:
                approval_row = rows[0]
                break
            time.sleep(0.05)

        assert approval_row is not None, "Installed hook created no PostgreSQL approval"
        approval_id, invocation_id, status = approval_row
        assert invocation_id
        assert status == "PENDING_REVIEW"
        assert process.poll() is None, "hook must remain blocked pending reviewer decision"

        with httpx.Client(timeout=10) as reviewer_client:
            decision = reviewer_client.post(
                f"{api_base_url}/approvals/{approval_id}/decision",
                json=_reviewed_decision_payload(
                    reviewer_client,
                    base_url=api_base_url,
                    approval_id=approval_id,
                    decision="approve",
                    reason="lost consume response test",
                ),
            )
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == "APPROVED"

        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        assert dropped_consume_response.is_set(), (
            "proxy did not observe a committed consume response before dropping it; "
            f"proxy errors={proxy_errors!r}"
        )
        assert not proxy_errors, proxy_errors
        assert json.loads(stdout)["decision"] == "deny"
        assert "ambiguous" in json.loads(stdout)["reason"].lower()

        # Replaying the exact provider callback must not turn the durable claim
        # into a second allow. The proxy drops only the first successful consume
        # response; a replay receives the real API's already-claimed conflict.
        replay = subprocess.run(
            argv,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=workspace,
            env=child_environment,
            timeout=20,
            check=False,
        )
        assert replay.returncode == 0, replay.stderr
        replay_result = json.loads(replay.stdout)
        assert replay_result["decision"] == "deny"

        with connect() as connection:
            approval_after = connection.execute(
                "SELECT status,source_invocation_id FROM approval_requests "
                "WHERE approval_id=%s AND workspace_id=%s AND source_agent_id=%s",
                (approval_id, workspace_record.id, agent.id),
            ).fetchone()
            claimed_rows = connection.execute(
                "SELECT approval_id,workspace_id,source_agent_id,source_invocation_id "
                "FROM approval_delivery_claims WHERE approval_id=%s",
                (approval_id,),
            ).fetchall()
            audit_counts = connection.execute(
                "SELECT event_type,COUNT(*) FROM audit_events WHERE approval_id=%s "
                "GROUP BY event_type ORDER BY event_type",
                (approval_id,),
            ).fetchall()
        assert approval_after == ("APPROVED", invocation_id)
        assert claimed_rows == [(approval_id, workspace_record.id, agent.id, invocation_id)]
        assert dict(audit_counts) == {
            "approval.created_by_agent": 1,
            "approval.decision": 1,
            "approval.delivery_claimed": 1,
            "approval.delivery_failed": 1,
        }
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=5)
        _stop_postgres_approval_http_api(api_server)

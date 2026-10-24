from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import psycopg
import pytest
from psycopg import sql

from app.models import AgentProvider, ApprovalStatus, AuthUser, UserRole

pytest_plugins = ("test_postgres_approval_route_flow",)

ANTIGRAVITY_HOOK = (
    Path(__file__).parents[2]
    / "integrations"
    / "antigravity"
    / "aixion_pre_tool_hook.py"
)


def _serve_with_lifespan_owned_postgres(
    dsn: str,
    schema: str,
    reviewer_id: str,
    reviewer_email: str,
    workspace_id: str,
    requested_port: int | None,
    status_queue: multiprocessing.queues.Queue,
    stop_event: multiprocessing.synchronize.Event,
) -> None:
    """Run Uvicorn with the real lifespan and a schema-scoped PG connector."""
    import uvicorn

    from app import main
    from app.postgres_runtime import PostgresRuntime

    def connect(_dsn: str, **_kwargs: object):
        connection = psycopg.connect(dsn)
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        return connection

    settings = main.get_settings()
    # Exercise production's PostgreSQL selection/binding path in an isolated
    # test profile. Required runtime prerequisites are enabled below by the
    # parent test process; no external services or production DB are used.
    from dataclasses import replace

    settings = replace(
        settings,
        auth_enabled=True,
        postgres_runtime_probe_enabled=True,
        postgres_auth_rate_limits_enabled=True,
        postgres_auth_enabled=True,
        email_dispatch_enabled=True,
        email_smtp_host="127.0.0.1",
        email_from="test@example.invalid",
        email_outbox_key_configured=True,
    )
    main.get_settings = lambda: settings

    def runtime_from_environment():
        return PostgresRuntime(
            _dsn=dsn,
            profile="test",
            settings=settings,
            _connector=connect,
        )

    main.PostgresRuntime.from_environment = staticmethod(runtime_from_environment)

    reviewer = AuthUser(
        id=reviewer_id,
        email=reviewer_email,
        display_name="PostgreSQL lifecycle restart reviewer",
        role=UserRole.OWNER,
        workspace_id=workspace_id,
    )
    main.app.dependency_overrides[main.ReviewerDependency.dependency] = lambda: reviewer
    main._workspace_id_for_route = lambda user, _request: user.workspace_id

    listener: socket.socket | None = None
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", requested_port or 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                main.app,
                host="127.0.0.1",
                port=port,
                lifespan="on",
                log_level="warning",
                access_log=False,
            )
        )

        async def run() -> None:
            server_task = asyncio.create_task(server.serve(sockets=[listener]))
            deadline = asyncio.get_running_loop().time() + 30
            while (
                not server.started
                and not server_task.done()
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.02)
            if not server.started:
                status_queue.put(("error", "Uvicorn lifespan startup did not complete"))
                server.should_exit = True
                await server_task
                return

            runtime = main.app.state.postgres_runtime
            repositories = main.app.state.postgres_repositories
            if runtime is None or repositories is not runtime.repositories:
                status_queue.put(("error", "PostgreSQL runtime bundle identity mismatch"))
                server.should_exit = True
                await server_task
                return
            if main.app.state.approval_decision_repository is not repositories.approval_decisions:
                status_queue.put(("error", "Approval route repository is not runtime-owned"))
                server.should_exit = True
                await server_task
                return

            status_queue.put(("ready", port))
            await asyncio.to_thread(stop_event.wait)
            server.should_exit = True
            await server_task

        asyncio.run(run())
    except BaseException as error:
        status_queue.put(("error", f"{type(error).__name__}: {error}"))
    finally:
        if listener is not None:
            listener.close()


def _start_server(
    *, dsn: str, schema: str, reviewer: AuthUser, port: int | None = None
) -> tuple[multiprocessing.Process, multiprocessing.synchronize.Event, str]:
    context = multiprocessing.get_context("spawn")
    status_queue = context.Queue()
    stop_event = context.Event()
    process = context.Process(
        target=_serve_with_lifespan_owned_postgres,
        args=(
            dsn,
            schema,
            reviewer.id,
            reviewer.email,
            reviewer.workspace_id or "",
            port,
            status_queue,
            stop_event,
        ),
    )
    process.start()
    try:
        status, value = status_queue.get(timeout=45)
    except Exception:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        raise
    finally:
        status_queue.close()
    if status != "ready":
        process.join(timeout=5)
        pytest.fail(f"PostgreSQL lifespan server failed startup: {value}")
    return process, stop_event, f"http://127.0.0.1:{value}"


def _stop_server(
    handle: tuple[multiprocessing.Process, multiprocessing.synchronize.Event, str],
) -> None:
    process, stop_event, _url = handle
    if process.is_alive():
        stop_event.set()
        process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    assert process.exitcode == 0


def test_lifespan_owned_postgres_approval_survives_api_restart_and_resumes_once(
    postgres_approval_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove real lifespan repository selection, API restart, and hook recovery."""
    connect, workspace, project, reviewer, _repositories, agent, token, schema = (
        postgres_approval_route_runtime
    )
    dsn = os.environ["AIXION_TEST_POSTGRES_DSN"]
    monkeypatch.setenv("AIXION_POSTGRES_DSN", dsn)
    monkeypatch.setenv("AIXION_PROFILE", "test")
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED", "true")
    monkeypatch.setenv("AIXION_EMAIL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("AIXION_EMAIL_FROM", "test@example.invalid")
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", "a" * 64)
    monkeypatch.setenv("AIXION_AUTH_RATE_LIMIT_HMAC_KEY", "b" * 64)

    servers: list[tuple[multiprocessing.Process, multiprocessing.synchronize.Event, str]] = []
    hook: subprocess.Popen[str] | None = None
    session_id = f"lifespan-restart-{uuid.uuid4().hex}"
    payload = {
        "conversationId": session_id,
        "stepIdx": 71,
        "toolCall": {"name": "run_command", "args": {"CommandLine": "echo restart-e2e"}},
    }

    try:
        first = _start_server(dsn=dsn, schema=schema, reviewer=reviewer)
        servers.append(first)
        child_env = os.environ.copy()
        child_env.update(
            {
                "AIXION_API_BASE_URL": first[2],
                "AIXION_AGENT_ID": agent.id,
                "AIXION_AGENT_TOKEN": token,
                "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
                "AIXION_PROJECT_ID": project.id,
                "AIXION_APPROVAL_TIMEOUT_SECONDS": "120",
            }
        )
        hook = subprocess.Popen(
            [sys.executable, str(ANTIGRAVITY_HOOK)],
            cwd=Path(__file__).parents[2],
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert hook.stdin is not None
        hook.stdin.write(json.dumps(payload))
        hook.stdin.close()
        hook.stdin = None

        approval_row: tuple[str, str, str] | None = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if hook.poll() is not None:
                stdout, stderr = hook.communicate()
                pytest.fail(
                    f"Hook exited before pending review: {hook.returncode}, {stdout!r}, {stderr!r}"
                )
            with connect() as connection:
                approval_row = connection.execute(
                    "SELECT approval_id,status,source_invocation_id FROM approval_requests "
                    "WHERE workspace_id=%s AND source_agent_id=%s "
                    "AND payload->>'source_provider'=%s "
                    "AND payload->>'source_session_id'=%s",
                    (workspace.id, agent.id, AgentProvider.ANTIGRAVITY.value, session_id),
                ).fetchone()
            if approval_row:
                break
            time.sleep(0.05)
        assert approval_row is not None
        approval_id, status, invocation_id = approval_row
        assert status == ApprovalStatus.PENDING_REVIEW

        # Stop and recreate the API process; the blocked hook stays alive.
        _stop_server(first)
        original_port = int(urlparse(first[2]).port or 0)
        second = _start_server(
            dsn=dsn, schema=schema, reviewer=reviewer, port=original_port
        )
        servers.append(second)
        assert second[2] == first[2]
        assert hook.poll() is None
        with httpx.Client(timeout=15) as client:
            decision = client.post(
                f"{second[2]}/approvals/{approval_id}/decision",
                json={"decision": "approve", "reason": "restart recovery E2E"},
            )
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == ApprovalStatus.APPROVED

        stdout, stderr = hook.communicate(timeout=30)
        assert hook.returncode == 0, stderr
        assert json.loads(stdout) == {
            "decision": "auto_approve",
            "reason": "Approved from Aixion Control Tower",
        }

        with connect() as connection:
            assert connection.execute(
                "SELECT approval_id,status,source_invocation_id FROM approval_requests "
                "WHERE approval_id=%s AND workspace_id=%s AND source_agent_id=%s",
                (approval_id, workspace.id, agent.id),
            ).fetchall() == [(approval_id, ApprovalStatus.APPROVED, invocation_id)]
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
        if hook is not None and hook.poll() is None:
            hook.kill()
            hook.communicate(timeout=5)
        for server in reversed(servers):
            if server[0].is_alive():
                _stop_server(server)

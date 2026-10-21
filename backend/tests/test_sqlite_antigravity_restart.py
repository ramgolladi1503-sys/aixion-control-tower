from __future__ import annotations

import asyncio
import multiprocessing
import os
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

import httpx
import pytest

HOOK_PATH = (
    Path(__file__).parents[2]
    / "integrations"
    / "antigravity"
    / "aixion_pre_tool_hook.py"
)


def _serve_sqlite_approval_api(
    status_queue: multiprocessing.queues.Queue,
    stop_event: multiprocessing.synchronize.Event,
) -> None:
    """Serve the real app with a process-local, test-owned SQLite database."""
    import uvicorn

    from app.main import app
    from app.models import Workspace
    from app.store import store

    if not store.workspaces:
        workspace = Workspace(name="SQLite restart test", owner_user_id="dev_user")
        store.workspaces[workspace.id] = workspace
        store.persist()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        lifespan="on",
        log_level="critical",
        access_log=False,
    )
    server = uvicorn.Server(config)

    async def serve_until_stopped() -> None:
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        startup_deadline = asyncio.get_running_loop().time() + 20
        while (
            not server.started
            and not server_task.done()
            and asyncio.get_running_loop().time() < startup_deadline
        ):
            await asyncio.sleep(0.02)
        if not server.started:
            status_queue.put(("error", "Uvicorn did not complete lifespan startup"))
            server.should_exit = True
            await server_task
            return
        status_queue.put(("ready", port))
        await asyncio.to_thread(stop_event.wait)
        server.should_exit = True
        await server_task

    try:
        asyncio.run(serve_until_stopped())
    except BaseException as error:
        status_queue.put(("error", f"{type(error).__name__}: {error}"))
    finally:
        listener.close()


def _sanitized_test_environment(db_path: Path) -> dict[str, str]:
    """Provide the spawned API only the runtime variables needed for this test."""
    environment: dict[str, str] = {}
    for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "WINDIR"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    environment.update(
        {
            "PYTHONPATH": str(Path(__file__).parents[1]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "AIXION_PROFILE": "test",
            "AIXION_AUTH_ENABLED": "false",
            "AIXION_DB_PATH": str(db_path),
            "AIXION_OUTBOX_DISPATCH_MODE": "scheduled",
        }
    )
    return environment


def _start_sqlite_approval_api(
    db_path: Path,
) -> tuple[multiprocessing.Process, multiprocessing.synchronize.Event, str]:
    """Start a real API subprocess and wait for its health endpoint."""
    process_context = multiprocessing.get_context("spawn")
    status_queue = process_context.Queue()
    stop_event = process_context.Event()
    old_environment = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(_sanitized_test_environment(db_path))
        process = process_context.Process(
            target=_serve_sqlite_approval_api,
            args=(status_queue, stop_event),
        )
        process.start()
    finally:
        os.environ.clear()
        os.environ.update(old_environment)

    try:
        status, value = status_queue.get(timeout=30)
        status_queue.close()
        if status != "ready":
            pytest.fail(f"SQLite approval API failed to start: {value}")
        api_base = f"http://127.0.0.1:{value}"
        with httpx.Client(timeout=1) as client:
            for _ in range(100):
                if not process.is_alive():
                    pytest.fail(
                        f"SQLite approval API exited during startup: {process.exitcode}"
                    )
                try:
                    response = client.get(f"{api_base}/health")
                    if response.status_code == 200 and response.json().get("status") == "ok":
                        return process, stop_event, api_base
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
        pytest.fail("SQLite API reported startup but health stayed unavailable")
    except BaseException:
        stop_event.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        raise


def _stop_sqlite_approval_api(
    handle: tuple[multiprocessing.Process, multiprocessing.synchronize.Event, str],
) -> None:
    """Gracefully stop and reap only the subprocess created by this test."""
    process, stop_event, _api_base = handle
    stop_event.set()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    assert not process.is_alive(), "test-owned SQLite API subprocess was not reaped"
    assert process.exitcode == 0


def _hook_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "antigravity_sqlite_restart_hook", HOOK_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_antigravity_invocation_recovers_once_after_sqlite_api_process_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "isolated-approval-restart.sqlite3"
    db_path.parent.chmod(0o700)
    servers: list[
        tuple[multiprocessing.Process, multiprocessing.synchronize.Event, str]
    ] = []
    hook = _hook_module()

    def start_server() -> tuple[multiprocessing.Process, multiprocessing.synchronize.Event, str]:
        handle = _start_sqlite_approval_api(db_path)
        servers.append(handle)
        return handle

    project_id: str
    agent_id: str
    agent_token: str
    invocation_payload = {
        "conversationId": "sqlite-server-restart-session",
        "stepIdx": 8,
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "echo sqlite-restart-safe"},
        },
    }

    try:
        first_server = start_server()
        with httpx.Client(timeout=10) as client:
            project_response = client.post(
                f"{first_server[2]}/projects",
                json={"name": "SQLite restart approval project"},
            )
            assert project_response.status_code == 200, project_response.text
            project_id = project_response.json()["id"]
            agent_response = client.post(
                f"{first_server[2]}/agents",
                json={
                    "provider": "ANTIGRAVITY",
                    "display_name": "SQLite restart test hook",
                    "auth_type": "API_KEY",
                    "allowed_project_ids": [project_id],
                    "allowed_actions": [
                        "CREATE_APPROVAL",
                        "READ_APPROVAL_DECISION",
                        "CONSUME_APPROVAL_DECISION",
                        "REPORT_APPROVAL_DELIVERY_FAILURE",
                        "EXPIRE_OWN_APPROVAL",
                    ],
                    "enabled": True,
                },
            )
            assert agent_response.status_code == 200, agent_response.text
            agent_body = agent_response.json()
            agent_id = agent_body["agent"]["id"]
            agent_token = agent_body["agent_token"]

        environment = {
            "AIXION_API_BASE_URL": first_server[2],
            "AIXION_AGENT_ID": agent_id,
            "AIXION_AGENT_TOKEN": agent_token,
            "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
            "AIXION_PROJECT_ID": project_id,
            "AIXION_APPROVAL_TIMEOUT_SECONDS": "10",
        }
        real_request = hook._request
        created: dict[str, dict[str, Any]] = {}

        def lose_committed_create_response(
            method: str,
            url: str,
            request_agent_id: str,
            request_token: str,
            body: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            result = real_request(method, url, request_agent_id, request_token, body)
            if method == "POST" and urlparse(url).path == "/agents/approvals":
                created["approval"] = result
                _stop_sqlite_approval_api(first_server)
                raise hook.AmbiguousRequestError(
                    "simulated test-only dropped response after SQLite commit"
                )
            return result

        with patch.object(hook, "_request", side_effect=lose_committed_create_response):
            with pytest.raises(hook.AmbiguousRequestError):
                hook.run(invocation_payload, environment)

        first_approval = created["approval"]
        approval_id = first_approval["id"]
        invocation_id = first_approval["source_invocation_id"]
        assert first_approval["status"] == "PENDING_REVIEW"
        assert first_approval["source_provider"] == "ANTIGRAVITY"

        second_server = start_server()
        environment["AIXION_API_BASE_URL"] = second_server[2]
        with httpx.Client(timeout=10) as client:
            decision = client.post(
                f"{second_server[2]}/approvals/{approval_id}/decision",
                json={"decision": "approve", "reason": "test after SQLite restart"},
            )
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == "APPROVED"

        _stop_sqlite_approval_api(second_server)
        third_server = start_server()
        environment["AIXION_API_BASE_URL"] = third_server[2]
        recovered = hook.run(invocation_payload, environment)
        assert recovered == {
            "decision": "allow",
            "reason": "Approved from Aixion Control Tower",
        }

        with sqlite3.connect(db_path) as connection:
            approvals = connection.execute(
                "SELECT entity_id,json_extract(payload,'$.status'),"
                "json_extract(payload,'$.source_invocation_id') FROM kv_store "
                "WHERE entity_type='approval_request' "
                "AND json_extract(payload,'$.source_agent_id')=? "
                "AND json_extract(payload,'$.source_invocation_id')=?",
                (agent_id, invocation_id),
            ).fetchall()
            assert approvals == [(approval_id, "APPROVED", invocation_id)]
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
                (approval_id,),
            ).fetchone()[0] == 1
            for event_type in (
                "approval.created_by_agent",
                "approval.decision",
                "approval.delivery_claimed",
            ):
                assert connection.execute(
                    "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' "
                    "AND json_extract(payload,'$.event_type')=? "
                    "AND json_extract(payload,'$.entity_id')=?",
                    (event_type, approval_id),
                ).fetchone()[0] == 1
    finally:
        for server in reversed(servers):
            if server[0].is_alive():
                _stop_sqlite_approval_api(server)

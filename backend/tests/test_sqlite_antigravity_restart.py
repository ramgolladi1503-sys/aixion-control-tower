from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import shlex
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

import httpx
import pytest
from integrations.antigravity import setup_workspace

HOOK_PATH = (
    Path(__file__).parents[2]
    / "integrations"
    / "antigravity"
    / "aixion_pre_tool_hook.py"
)


def _serve_sqlite_approval_api(
    status_queue: multiprocessing.queues.Queue,
    stop_event: multiprocessing.synchronize.Event,
    requested_port: int = 0,
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
    listener.bind(("127.0.0.1", requested_port))
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
    *,
    port: int = 0,
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
            args=(status_queue, stop_event, port),
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


def _launch_hook_process(payload: dict[str, Any], environment: dict[str, str]) -> subprocess.Popen[str]:
    """Launch a fresh hook interpreter with one provider payload on stdin."""
    read_fd, write_fd = os.pipe()
    try:
        process = subprocess.Popen(
            [sys.executable, str(HOOK_PATH)],
            stdin=read_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
    finally:
        os.close(read_fd)
    try:
        os.write(write_fd, json.dumps(payload).encode("utf-8"))
    finally:
        os.close(write_fd)
    return process


def _read_antigravity_approvals(
    db_path: Path, *, agent_id: str, session_id: str
) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type='approval_request' "
            "AND json_extract(payload,'$.source_agent_id')=? "
            "AND json_extract(payload,'$.source_session_id')=?",
            (agent_id, session_id),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _wait_for_antigravity_approvals(
    db_path: Path, *, agent_id: str, session_id: str, count: int
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        approvals = _read_antigravity_approvals(
            db_path, agent_id=agent_id, session_id=session_id
        )
        if len(approvals) >= count:
            return approvals
        time.sleep(0.05)
    pytest.fail(f"hook did not persist {count} approval request(s) before the deadline")


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
        create_bodies: list[dict[str, Any]] = []

        def lose_committed_create_response(
            method: str,
            url: str,
            request_agent_id: str,
            request_token: str,
            body: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            if method == "POST" and urlparse(url).path == "/agents/approvals":
                assert body is not None
                create_bodies.append(body.copy())
            result = real_request(method, url, request_agent_id, request_token, body)
            if method == "POST" and urlparse(url).path == "/agents/approvals":
                prior = created.get("approval")
                if prior is not None:
                    assert result["id"] == prior["id"]
                created["approval"] = result
                _stop_sqlite_approval_api(first_server)
                raise hook.AmbiguousRequestError(
                    "simulated test-only dropped response after SQLite commit"
                )
            return result

        with patch.object(hook, "_request", side_effect=lose_committed_create_response):
            first_attempt = hook.run(invocation_payload, environment)

        assert first_attempt["decision"] == "deny"
        assert "remained ambiguous" in first_attempt["reason"]
        assert len(create_bodies) == hook.MAX_CREATE_ATTEMPTS == 2
        assert create_bodies[0] == create_bodies[1]

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


@pytest.mark.parametrize(
    ("decision_value", "expected_decision", "expected_claims"),
    [
        ("approve", "allow", 1),
        ("deny", "deny", 0),
    ],
)
def test_blocked_antigravity_invocation_survives_sqlite_api_restart(
    tmp_path: Path,
    decision_value: str,
    expected_decision: str,
    expected_claims: int,
) -> None:
    """A waiting real hook reconnects to the same durable request after restart."""
    import threading

    db_path = tmp_path / f"blocked-hook-{decision_value}-restart.sqlite3"
    db_path.parent.chmod(0o700)
    servers: list[
        tuple[multiprocessing.Process, multiprocessing.synchronize.Event, str]
    ] = []
    hook = _hook_module()
    waiting_on_pending_decision = threading.Event()
    hook_result: dict[str, Any] = {}
    hook_error: dict[str, BaseException] = {}
    hook_thread: threading.Thread | None = None

    def start_server(port: int = 0):
        handle = _start_sqlite_approval_api(db_path, port=port)
        servers.append(handle)
        return handle

    try:
        first_server = start_server()
        with httpx.Client(timeout=10) as client:
            project_response = client.post(
                f"{first_server[2]}/projects",
                json={"name": "SQLite in-flight restart project"},
            )
            assert project_response.status_code == 200, project_response.text
            project_id = project_response.json()["id"]
            agent_response = client.post(
                f"{first_server[2]}/agents",
                json={
                    "provider": "ANTIGRAVITY",
                    "display_name": "SQLite in-flight restart hook",
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

        invocation_payload = {
            "conversationId": f"sqlite-inflight-{decision_value}",
            "stepIdx": 3,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "echo restart-in-flight"},
            },
        }
        environment = {
            "AIXION_API_BASE_URL": first_server[2],
            "AIXION_AGENT_ID": agent_body["agent"]["id"],
            "AIXION_AGENT_TOKEN": agent_body["agent_token"],
            "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
            "AIXION_PROJECT_ID": project_id,
            "AIXION_APPROVAL_TIMEOUT_SECONDS": "20",
        }
        real_request = hook._request
        approval_identity: dict[str, str] = {}

        def observe_real_request(
            method: str,
            url: str,
            request_agent_id: str,
            request_token: str,
            body: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            response = real_request(
                method, url, request_agent_id, request_token, body, **kwargs
            )
            path = urlparse(url).path
            if method == "POST" and path == "/agents/approvals":
                approval_identity["id"] = str(response["id"])
                approval_identity["invocation_id"] = str(
                    response["source_invocation_id"]
                )
            elif (
                method == "GET"
                and path.endswith("/decision")
                and response.get("status") == "PENDING_REVIEW"
            ):
                waiting_on_pending_decision.set()
            return response

        def run_hook() -> None:
            try:
                hook_result["value"] = hook.run(
                    invocation_payload, environment
                )
            except BaseException as error:
                hook_error["error"] = error

        with patch.object(hook, "_request", side_effect=observe_real_request):
            hook_thread = threading.Thread(target=run_hook, daemon=True)
            hook_thread.start()
            assert waiting_on_pending_decision.wait(timeout=10), (
                "hook never reached a real pending-decision poll"
            )
            assert approval_identity.get("id"), "hook did not create an approval"

            first_port = int(urlparse(first_server[2]).port or 0)
            _stop_sqlite_approval_api(first_server)
            time.sleep(0.7)
            second_server = start_server(first_port)
            assert second_server[2] == first_server[2]

            with httpx.Client(timeout=10) as client:
                decision_response = client.post(
                    f"{second_server[2]}/approvals/{approval_identity['id']}/decision",
                    json={
                        "decision": decision_value,
                        "reason": "test decision after API process restart",
                    },
                )
            assert decision_response.status_code == 200, decision_response.text
            hook_thread.join(timeout=15)

        assert hook_thread is not None and not hook_thread.is_alive(), (
            "hook did not finish within the bounded test deadline"
        )
        assert not hook_error, f"hook raised unexpectedly: {hook_error}"
        assert hook_result.get("value", {}).get("decision") == expected_decision

        with sqlite3.connect(db_path) as connection:
            approvals = connection.execute(
                "SELECT entity_id,json_extract(payload,'$.status'),"
                "json_extract(payload,'$.source_invocation_id') FROM kv_store "
                "WHERE entity_type='approval_request' "
                "AND json_extract(payload,'$.source_agent_id')=? "
                "AND json_extract(payload,'$.source_invocation_id')=?",
                (
                    agent_body["agent"]["id"],
                    approval_identity["invocation_id"],
                ),
            ).fetchall()
            assert len(approvals) == 1
            assert approvals[0][0] == approval_identity["id"]
            expected_status = "APPROVED" if decision_value == "approve" else "DENIED"
            assert approvals[0][1] == expected_status
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
                (approval_identity["id"],),
            ).fetchone()[0] == expected_claims
            for event_type in ("approval.created_by_agent", "approval.decision"):
                assert connection.execute(
                    "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' "
                    "AND json_extract(payload,'$.event_type')=? "
                    "AND json_extract(payload,'$.entity_id')=?",
                    (event_type, approval_identity["id"]),
                ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' "
                "AND json_extract(payload,'$.event_type')='approval.delivery_claimed' "
                "AND json_extract(payload,'$.entity_id')=?",
                (approval_identity["id"],),
            ).fetchone()[0] == expected_claims
    finally:
        if hook_thread is not None and hook_thread.is_alive():
            hook_thread.join(timeout=5)
        for server in reversed(servers):
            if server[0].is_alive():
                _stop_sqlite_approval_api(server)


@pytest.mark.parametrize(
    ("decision_value", "expected_decision", "expected_claims"),
    [("approve", "allow", 1), ("deny", "deny", 0)],
)
def test_installed_antigravity_command_runs_same_invocation_approval_flow(
    tmp_path: Path,
    decision_value: str,
    expected_decision: str,
    expected_claims: int,
) -> None:
    """Run the exact installer-generated command, not an imported hook function."""
    from concurrent.futures import ThreadPoolExecutor

    db_path = tmp_path / f"installed-hook-{decision_value}.sqlite3"
    db_path.parent.chmod(0o700)
    servers: list[
        tuple[multiprocessing.Process, multiprocessing.synchronize.Event, str]
    ] = []

    def start_server(port: int = 0):
        handle = _start_sqlite_approval_api(db_path, port=port)
        servers.append(handle)
        return handle

    try:
        server = start_server()
        with httpx.Client(timeout=10) as client:
            project_response = client.post(
                f"{server[2]}/projects",
                json={"name": "Installed Antigravity hook project"},
            )
            assert project_response.status_code == 200, project_response.text
            project_id = project_response.json()["id"]
            agent_response = client.post(
                f"{server[2]}/agents",
                json={
                    "provider": "ANTIGRAVITY",
                    "display_name": "Installed Antigravity integration test",
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

        home = tmp_path / "isolated home"
        workspace = tmp_path / "workspace with spaces"
        home.mkdir(mode=0o700)
        workspace.mkdir(mode=0o700)
        config_path, hooks_path = setup_workspace.install_workspace(
            workspace,
            api_base_url=server[2],
            agent_id=agent_body["agent"]["id"],
            project_id=project_id,
            home=home,
            hook_script=HOOK_PATH,
            python=sys.executable,
        )
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        command = hooks[setup_workspace.HOOK_NAME]["PreToolUse"][0]["hooks"][0][
            "command"
        ]
        command_args = shlex.split(command)
        assert command_args == [
            sys.executable,
            str(HOOK_PATH.resolve()),
            "--config",
            str(config_path),
        ]

        # Antigravity PreToolUse fields as exercised by the documented hook fixture.
        invocation_payload = {
            "conversationId": f"installed-hook-{decision_value}",
            "stepIdx": 17,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "echo installed-hook-round-trip"},
            },
        }
        session_id = invocation_payload["conversationId"]
        expected_invocation_id = "antigravity:v1:" + hashlib.sha256(
            json.dumps(
                [
                    session_id,
                    invocation_payload["stepIdx"],
                    invocation_payload["toolCall"]["name"],
                    invocation_payload["toolCall"]["args"],
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        environment = _sanitized_test_environment(db_path)
        environment.update(
            {
                "HOME": str(home),
                "AIXION_AGENT_TOKEN": agent_body["agent_token"],
                "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
                "AIXION_APPROVAL_TIMEOUT_SECONDS": "20",
            }
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(
                subprocess.run,
                command,
                shell=True,
                executable="/bin/sh",
                input=json.dumps(invocation_payload),
                text=True,
                capture_output=True,
                timeout=25,
                cwd=workspace,
                env=environment,
                check=False,
            )
            approvals = _wait_for_antigravity_approvals(
                db_path,
                agent_id=agent_body["agent"]["id"],
                session_id=session_id,
                count=1,
            )
            assert approvals[0]["source_invocation_id"] == expected_invocation_id
            approval_id = approvals[0]["id"]
            original_port = int(urlparse(server[2]).port or 0)
            _stop_sqlite_approval_api(server)
            time.sleep(0.7)
            server = start_server(original_port)
            assert server[2] == f"http://127.0.0.1:{original_port}"
            with httpx.Client(timeout=10) as client:
                decision_response = client.post(
                    f"{server[2]}/approvals/{approval_id}/decision",
                    json={
                        "decision": decision_value,
                        "reason": "installer-to-hook integration test",
                    },
                )
            assert decision_response.status_code == 200, decision_response.text
            assert decision_response.json()["status"] == (
                "APPROVED" if decision_value == "approve" else "DENIED"
            )
            first_result = first.result(timeout=25)
            assert first_result.returncode == 0, first_result.stderr
            first_output = json.loads(first_result.stdout)
            assert first_output["decision"] == expected_decision

            # Re-running the same installed callback must not create another approval
            # or claim an already claimed allow decision a second time.
            replay_result = subprocess.run(
                command,
                shell=True,
                executable="/bin/sh",
                input=json.dumps(invocation_payload),
                text=True,
                capture_output=True,
                timeout=25,
                cwd=workspace,
                env=environment,
                check=False,
            )
            assert replay_result.returncode == 0, replay_result.stderr
            replay_output = json.loads(replay_result.stdout)
            assert replay_output["decision"] == "deny"

        with sqlite3.connect(db_path) as connection:
            persisted = connection.execute(
                "SELECT entity_id,json_extract(payload,'$.status'),"
                "json_extract(payload,'$.source_invocation_id') FROM kv_store "
                "WHERE entity_type='approval_request' "
                "AND json_extract(payload,'$.source_agent_id')=? "
                "AND json_extract(payload,'$.source_invocation_id')=?",
                (agent_body["agent"]["id"], expected_invocation_id),
            ).fetchall()
            assert persisted == [
                (
                    approval_id,
                    "APPROVED" if decision_value == "approve" else "DENIED",
                    expected_invocation_id,
                )
            ]
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
                (approval_id,),
            ).fetchone()[0] == expected_claims
            for event_type, expected_count in (
                ("approval.created_by_agent", 1),
                ("approval.decision", 1),
                ("approval.delivery_claimed", expected_claims),
            ):
                assert connection.execute(
                    "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' "
                    "AND json_extract(payload,'$.event_type')=? "
                    "AND json_extract(payload,'$.entity_id')=?",
                    (event_type, approval_id),
                ).fetchone()[0] == expected_count
    finally:
        for server in reversed(servers):
            if server[0].is_alive():
                _stop_sqlite_approval_api(server)


def test_antigravity_hook_process_relaunch_recovers_exact_approved_invocation(
    tmp_path: Path,
) -> None:
    """A new hook process can recover an exact request; changed/replayed calls fail closed."""
    db_path = tmp_path / "hook-process-relaunch.sqlite3"
    db_path.parent.chmod(0o700)
    server = _start_sqlite_approval_api(db_path)
    waiting_process: subprocess.Popen[str] | None = None
    changed_process: subprocess.Popen[str] | None = None

    session_id = "hook-process-relaunch-session"
    invocation_payload = {
        "conversationId": session_id,
        "stepIdx": 12,
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "echo relaunch-exact-invocation"},
        },
    }

    try:
        with httpx.Client(timeout=10) as client:
            project_response = client.post(
                f"{server[2]}/projects", json={"name": "Hook process relaunch project"}
            )
            assert project_response.status_code == 200, project_response.text
            project_id = project_response.json()["id"]
            agent_response = client.post(
                f"{server[2]}/agents",
                json={
                    "provider": "ANTIGRAVITY",
                    "display_name": "Hook process relaunch test",
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

        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "AIXION_API_BASE_URL": server[2],
            "AIXION_AGENT_ID": agent_body["agent"]["id"],
            "AIXION_AGENT_TOKEN": agent_body["agent_token"],
            "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
            "AIXION_PROJECT_ID": project_id,
            "AIXION_APPROVAL_TIMEOUT_SECONDS": "30",
        }

        # The first connector process reaches a durable pending request, then
        # terminates before receiving a decision, as can happen on the host.
        waiting_process = _launch_hook_process(invocation_payload, environment)
        first_approvals = _wait_for_antigravity_approvals(
            db_path,
            agent_id=agent_body["agent"]["id"],
            session_id=session_id,
            count=1,
        )
        assert len(first_approvals) == 1
        approval_id = first_approvals[0]["id"]
        invocation_id = first_approvals[0]["source_invocation_id"]
        assert first_approvals[0]["status"] == "PENDING_REVIEW"
        waiting_process.kill()
        waiting_process.communicate(timeout=5)
        waiting_process = None

        with httpx.Client(timeout=10) as client:
            decision_response = client.post(
                f"{server[2]}/approvals/{approval_id}/decision",
                json={"decision": "approve", "reason": "approve during hook restart"},
            )
        assert decision_response.status_code == 200, decision_response.text
        assert decision_response.json()["status"] == "APPROVED"

        # Relaunch with the exact provider payload. A fresh interpreter must
        # resolve the same durable approval and claim its delivery once.
        recovered = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=json.dumps(invocation_payload),
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
            check=False,
        )
        assert recovered.returncode == 0, recovered.stderr
        assert json.loads(recovered.stdout) == {
            "decision": "allow",
            "reason": "Approved from Aixion Control Tower",
        }

        # Re-delivery after the at-most-once claim cannot authorize the tool a
        # second time, even though the invocation identity matches exactly.
        replayed = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=json.dumps(invocation_payload),
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
            check=False,
        )
        assert replayed.returncode == 0, replayed.stderr
        replayed_result = json.loads(replayed.stdout)
        assert replayed_result["decision"] == "deny"
        assert "could not be claimed safely" in replayed_result["reason"]

        # A changed argument has a different deterministic invocation key and
        # remains pending; the earlier approval cannot authorize it.
        changed_payload = json.loads(json.dumps(invocation_payload))
        changed_payload["toolCall"]["args"]["CommandLine"] = "echo changed-invocation"
        changed_process = _launch_hook_process(changed_payload, environment)
        approvals = _wait_for_antigravity_approvals(
            db_path,
            agent_id=agent_body["agent"]["id"],
            session_id=session_id,
            count=2,
        )
        changed_approval = next(
            item for item in approvals if item["source_invocation_id"] != invocation_id
        )
        assert changed_approval["id"] != approval_id
        assert changed_approval["status"] == "PENDING_REVIEW"
        changed_process.kill()
        changed_process.communicate(timeout=5)
        changed_process = None

        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
                (approval_id,),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
                (changed_approval["id"],),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' "
                "AND json_extract(payload,'$.event_type')='approval.created_by_agent' "
                "AND json_extract(payload,'$.entity_id')=?",
                (approval_id,),
            ).fetchone()[0] == 1
    finally:
        for process in (waiting_process, changed_process):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        _stop_sqlite_approval_api(server)


def test_hook_process_crash_after_consume_commit_fails_closed_on_relaunch(
    tmp_path: Path,
) -> None:
    """A fresh hook cannot replay a claim committed before the first process died."""
    db_path = tmp_path / "hook-crash-after-consume.sqlite3"
    db_path.parent.chmod(0o700)
    server = _start_sqlite_approval_api(db_path)
    consume_committed = threading.Event()
    release_consume_response = threading.Event()
    consume_handler_completed = threading.Event()
    proxy_errors: list[str] = []

    class DropConsumeResponseHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._forward()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._forward()

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _forward(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower().startswith("x-") or name.lower() == "content-type"
            }
            try:
                response = httpx.request(
                    self.command,
                    f"{server[2]}{self.path}",
                    headers=headers,
                    content=body,
                    timeout=10,
                )
                if self.command == "POST" and self.path.endswith("/consume"):
                    if response.status_code != 200:
                        proxy_errors.append(
                            f"consume returned {response.status_code}: {response.text}"
                        )
                    consume_committed.set()
                    if not release_consume_response.wait(timeout=15):
                        proxy_errors.append("test did not release the committed consume response")
                        return
                self.send_response(response.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)
            except (BrokenPipeError, ConnectionResetError):
                # Expected: the test kills the host hook while its response is held.
                return
            except Exception as error:  # surfaced in the test thread below
                proxy_errors.append(f"{type(error).__name__}: {error}")
            finally:
                if self.command == "POST" and self.path.endswith("/consume"):
                    consume_handler_completed.set()

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), DropConsumeResponseHandler)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    proxy_base = f"http://127.0.0.1:{proxy.server_address[1]}"
    hook_process: subprocess.Popen[str] | None = None
    try:
        with httpx.Client(timeout=10) as client:
            project_response = client.post(
                f"{server[2]}/projects", json={"name": "Crash replay project"}
            )
            assert project_response.status_code == 200, project_response.text
            project_id = project_response.json()["id"]
            agent_response = client.post(
                f"{server[2]}/agents",
                json={
                    "provider": "ANTIGRAVITY",
                    "display_name": "Crash after consume test hook",
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

        session_id = "hook-crash-after-consume-session"
        invocation_payload = {
            "conversationId": session_id,
            "stepIdx": 19,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "echo crash-after-claim"},
            },
        }
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "AIXION_API_BASE_URL": proxy_base,
            "AIXION_AGENT_ID": agent_body["agent"]["id"],
            "AIXION_AGENT_TOKEN": agent_body["agent_token"],
            "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
            "AIXION_PROJECT_ID": project_id,
            "AIXION_APPROVAL_TIMEOUT_SECONDS": "30",
        }
        hook_process = _launch_hook_process(invocation_payload, environment)
        approvals = _wait_for_antigravity_approvals(
            db_path,
            agent_id=agent_body["agent"]["id"],
            session_id=session_id,
            count=1,
        )
        approval = approvals[0]
        assert approval["status"] == "PENDING_REVIEW"
        with httpx.Client(timeout=10) as client:
            decision = client.post(
                f"{server[2]}/approvals/{approval['id']}/decision",
                json={"decision": "approve", "reason": "test crash window"},
            )
        assert decision.status_code == 200, decision.text

        assert consume_committed.wait(timeout=10), "consume was not committed by the API"
        assert hook_process.poll() is None, "hook exited before the consume response was released"
        hook_process.kill()
        hook_process.communicate(timeout=5)
        hook_process = None
        release_consume_response.set()
        assert consume_handler_completed.wait(timeout=10), (
            "proxy did not finish handling the committed consume response"
        )

        # Relaunching the exact invocation after the host crash must observe the
        # existing unique claim and deny, even though the request is approved.
        replay_environment = dict(environment)
        replay_environment["AIXION_API_BASE_URL"] = server[2]
        replay = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=json.dumps(invocation_payload),
            capture_output=True,
            text=True,
            env=replay_environment,
            timeout=20,
            check=False,
        )
        assert replay.returncode == 0, replay.stderr
        replay_result = json.loads(replay.stdout)
        assert replay_result["decision"] == "deny"
        assert "could not be claimed safely" in replay_result["reason"]
        assert proxy_errors == []

        with sqlite3.connect(db_path) as connection:
            claims = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
                (approval["id"],),
            ).fetchone()[0]
            failures = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_failures WHERE approval_id=?",
                (approval["id"],),
            ).fetchone()[0]
            events = [
                json.loads(row[0])["event_type"]
                for row in connection.execute(
                    "SELECT payload FROM kv_store WHERE entity_type='audit_event' "
                    "AND json_extract(payload,'$.entity_id')=?",
                    (approval["id"],),
                ).fetchall()
            ]
            durable_approval = connection.execute(
                "SELECT json_extract(payload,'$.status') FROM kv_store "
                "WHERE entity_type='approval_request' AND entity_id=?",
                (approval["id"],),
            ).fetchone()
        assert claims == 1
        # The process died before it could report its ambiguous response. The
        # durable claim and audit trail are the evidence for this crash window.
        assert failures == 0
        assert durable_approval == ("APPROVED",)
        assert events.count("approval.created_by_agent") == 1
        assert events.count("approval.decision") == 1
        assert events.count("approval.delivery_claimed") == 1
    finally:
        release_consume_response.set()
        if hook_process is not None and hook_process.poll() is None:
            hook_process.kill()
            hook_process.communicate(timeout=5)
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=5)
        _stop_sqlite_approval_api(server)

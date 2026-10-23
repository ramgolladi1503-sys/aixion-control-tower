from __future__ import annotations

import json
import os
import shlex
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

BACKEND_ROOT = Path(__file__).parents[1]
HOOK_PATH = (
    BACKEND_ROOT.parent
    / "integrations"
    / "codex"
    / "aixion_permission_request_hook.py"
)


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_api(db_path: Path, port: int) -> subprocess.Popen:
    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith("AIXION_POSTGRES_"):
            environment.pop(key)
    environment.update(
        {
            "AIXION_PROFILE": "test",
            "AIXION_AUTH_ENABLED": "false",
            "AIXION_DB_PATH": str(db_path),
        }
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_api(process: subprocess.Popen, base_url: str) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"Local API exited with code {process.returncode}")
        try:
            response = httpx.get(f"{base_url}/health", timeout=0.5)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("Local API did not become healthy before timeout")


def _stop_api(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_real_codex_permission_hook_allows_only_after_durable_phone_decision(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "codex-process-flow.sqlite3"
    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    api_process = _start_api(db_path, port)
    hook_process: subprocess.Popen | None = None

    try:
        _wait_for_api(api_process, base_url)
        with httpx.Client(base_url=base_url, timeout=5) as api:
            registration = api.post(
                "/auth/register",
                json={"email": "codex-process-owner@example.com", "password": "Codex-process-password-123!", "display_name": "Codex process owner"},
            )
            assert registration.status_code == 200, registration.text
            code = registration.json().get("dev_verification_code")
            assert code
            verified = api.post(
                "/auth/verify-email",
                json={"email": "codex-process-owner@example.com", "code": code},
            )
            assert verified.status_code == 200, verified.text
            project_response = api.post(
                "/projects", json={"name": "Codex process approval project"}
            )
            assert project_response.status_code == 200, project_response.text
            project_id = project_response.json()["id"]

            registered = api.post(
                "/agents",
                json={
                    "provider": "CODEX",
                    "display_name": "Codex local process integration",
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
            assert registered.status_code == 200, registered.text
            agent_id = registered.json()["agent"]["id"]
            agent_token = registered.json()["agent_token"]

        hook_environment = os.environ.copy()
        hook_environment.update(
            {
                "AIXION_API_BASE_URL": base_url,
                "AIXION_AGENT_ID": agent_id,
                "AIXION_AGENT_TOKEN": agent_token,
                "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
                "AIXION_PROJECT_ID": project_id,
                "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "60",
            }
        )
        for key in list(hook_environment):
            if key.startswith("AIXION_POSTGRES_"):
                hook_environment.pop(key)

        permission_request = {
            "hook_event_name": "PermissionRequest",
            "session_id": "codex-session-process-flow",
            "turn_id": "codex-turn-process-flow",
            "tool_use_id": "codex-tool-process-flow",
            "tool_name": "Bash",
            "tool_input": {"command": "echo codex-process-flow"},
        }
        hook_process = subprocess.Popen(
            [sys.executable, str(HOOK_PATH)],
            cwd=BACKEND_ROOT.parent,
            env=hook_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert hook_process.stdin is not None
        hook_process.stdin.write(json.dumps(permission_request) + "\n")
        hook_process.stdin.close()
        # communicate() flushes stdin before collecting output. Clear the
        # reference after sending EOF so it only drains stdout/stderr.
        hook_process.stdin = None

        approval: dict | None = None
        deadline = time.monotonic() + 15
        with httpx.Client(base_url=base_url, timeout=5) as api:
            while time.monotonic() < deadline:
                response = api.get("/approvals")
                assert response.status_code == 200, response.text
                approval = next(
                    (
                        item
                        for item in response.json()
                        if item.get("source_provider") == "CODEX"
                        and item.get("source_agent_id") == agent_id
                        and item.get("title") == "Codex permission: Bash"
                    ),
                    None,
                )
                if approval is not None:
                    break
                assert hook_process.poll() is None, (
                    "Codex hook exited before a pending approval was created"
                )
                time.sleep(0.1)

            assert approval is not None, "Codex hook did not create an approval"
            assert approval["status"] in {"REQUESTED", "PENDING_REVIEW"}
            assert approval["source_session_id"].startswith("codex:v1:session:")
            assert approval["source_invocation_id"]
            assert hook_process.poll() is None, (
                "Codex hook returned before a reviewer decision was recorded"
            )

            decision = api.post(
                f"/approvals/{approval['id']}/decision",
                json={"decision": "approve"},
            )
            assert decision.status_code == 200, decision.text
            assert decision.json()["status"] == "APPROVED"

        stdout, stderr = hook_process.communicate(timeout=20)
        assert hook_process.returncode == 0, stderr
        output = json.loads(stdout.strip().splitlines()[-1])
        assert output == {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        }

        with sqlite3.connect(db_path) as connection:
            claim_count = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
                (approval["id"],),
            ).fetchone()[0]
        assert claim_count == 1
    finally:
        if hook_process is not None and hook_process.poll() is None:
            hook_process.kill()
            hook_process.wait(timeout=5)
        _stop_api(api_process)


def test_real_codex_permission_hook_denies_without_claim_or_tool_side_effect(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "codex-process-deny.sqlite3"
    marker_path = tmp_path / "codex-denied-tool-ran"
    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    api_process = _start_api(db_path, port)
    hook_process: subprocess.Popen | None = None

    try:
        _wait_for_api(api_process, base_url)
        with httpx.Client(base_url=base_url, timeout=5) as api:
            registration = api.post(
                "/auth/register",
                json={
                    "email": "codex-process-deny@example.com",
                    "password": "Saffron-River-42!Granite",
                    "display_name": "Codex deny process owner",
                },
            )
            assert registration.status_code == 200, registration.text
            code = registration.json().get("dev_verification_code")
            assert code
            verified = api.post(
                "/auth/verify-email",
                json={"email": "codex-process-deny@example.com", "code": code},
            )
            assert verified.status_code == 200, verified.text
            project_response = api.post(
                "/projects", json={"name": "Codex deny process project"}
            )
            assert project_response.status_code == 200, project_response.text
            project_id = project_response.json()["id"]

            registered = api.post(
                "/agents",
                json={
                    "provider": "CODEX",
                    "display_name": "Codex deny process integration",
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
            assert registered.status_code == 200, registered.text
            agent_id = registered.json()["agent"]["id"]
            agent_token = registered.json()["agent_token"]

        hook_environment = os.environ.copy()
        hook_environment.update(
            {
                "AIXION_API_BASE_URL": base_url,
                "AIXION_AGENT_ID": agent_id,
                "AIXION_AGENT_TOKEN": agent_token,
                "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
                "AIXION_PROJECT_ID": project_id,
                "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "60",
            }
        )
        for key in list(hook_environment):
            if key.startswith("AIXION_POSTGRES_"):
                hook_environment.pop(key)

        command = f"touch {shlex.quote(str(marker_path))}"
        permission_request = {
            "hook_event_name": "PermissionRequest",
            "session_id": "codex-session-process-deny",
            "turn_id": "codex-turn-process-deny",
            "tool_use_id": "codex-tool-process-deny",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        hook_process = subprocess.Popen(
            [sys.executable, str(HOOK_PATH)],
            cwd=BACKEND_ROOT.parent,
            env=hook_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert hook_process.stdin is not None
        hook_process.stdin.write(json.dumps(permission_request) + "\n")
        hook_process.stdin.close()
        hook_process.stdin = None

        approval: dict | None = None
        deadline = time.monotonic() + 15
        with httpx.Client(base_url=base_url, timeout=5) as api:
            while time.monotonic() < deadline:
                response = api.get("/approvals")
                assert response.status_code == 200, response.text
                approval = next(
                    (
                        item
                        for item in response.json()
                        if item.get("source_provider") == "CODEX"
                        and item.get("source_agent_id") == agent_id
                        and item.get("title") == "Codex permission: Bash"
                    ),
                    None,
                )
                if approval is not None:
                    break
                assert hook_process.poll() is None, (
                    "Codex hook exited before a pending approval was created"
                )
                time.sleep(0.1)

            assert approval is not None, "Codex hook did not create an approval"
            assert approval["status"] in {"REQUESTED", "PENDING_REVIEW"}
            assert approval["source_session_id"].startswith("codex:v1:session:")
            assert approval["source_invocation_id"]
            assert hook_process.poll() is None, (
                "Codex hook returned before a reviewer decision was recorded"
            )

            decision = api.post(
                f"/approvals/{approval['id']}/decision",
                json={"decision": "reject"},
            )
            assert decision.status_code == 200, decision.text
            assert decision.json()["status"] == "DENIED"

        stdout, stderr = hook_process.communicate(timeout=20)
        assert hook_process.returncode == 0, stderr
        output = json.loads(stdout.strip().splitlines()[-1])
        assert output == {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {
                    "behavior": "deny",
                    "message": "Control Tower decision: DENIED",
                },
            }
        }

        # Model the host's documented behavior: only run the requested tool
        # when the synchronous permission hook returns allow.
        if output["hookSpecificOutput"]["decision"]["behavior"] == "allow":
            subprocess.run(command, shell=True, check=True, timeout=5)
        assert not marker_path.exists()

        with sqlite3.connect(db_path) as connection:
            claim_count = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
                (approval["id"],),
            ).fetchone()[0]
        assert claim_count == 0
    finally:
        if hook_process is not None and hook_process.poll() is None:
            hook_process.kill()
            hook_process.wait(timeout=5)
        _stop_api(api_process)

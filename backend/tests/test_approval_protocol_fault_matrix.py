from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


HOOK_PATH = (
    Path(__file__).parents[2]
    / "integrations"
    / "antigravity"
    / "aixion_pre_tool_hook.py"
)


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_isolated_api(db_path: Path, port: int) -> subprocess.Popen:
    environment = os.environ.copy()
    environment.update(
        {
            "AIXION_PROFILE": "test",
            "AIXION_AUTH_ENABLED": "false",
            "AIXION_DB_PATH": str(db_path),
            "AIXION_POSTGRES_RUNTIME_PROBE_ENABLED": "false",
            "AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED": "false",
            "AIXION_POSTGRES_AUTH_ENABLED": "false",
        }
    )
    for key in tuple(environment):
        if key.startswith("AIXION_POSTGRES_"):
            environment.pop(key, None)
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
        cwd=Path(__file__).parents[1],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_api(process: subprocess.Popen, base_url: str) -> None:
    import httpx

    deadline = time.monotonic() + 15
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


def test_hook_wait_deadline_expires_pending_request_and_denies(tmp_path: Path) -> None:
    """A short configured host deadline must expire server state and deny the tool."""
    import httpx

    db_path = tmp_path / "approval-timeout.sqlite3"
    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    api_process = _start_isolated_api(db_path, port)
    try:
        _wait_for_api(api_process, base_url)
        with httpx.Client(base_url=base_url, timeout=5) as api:
            registration = api.post(
                "/auth/register",
                json={"email": "antigravity-timeout@example.com", "password": "Timeout-test-password-123!", "display_name": "Timeout owner"},
            )
            assert registration.status_code == 200, registration.text
            code = registration.json().get("dev_verification_code")
            assert code
            verified = api.post(
                "/auth/verify-email",
                json={"email": "antigravity-timeout@example.com", "code": code},
            )
            assert verified.status_code == 200, verified.text
            project_response = api.post(
                "/projects", json={"name": "Antigravity timeout project"}
            )
            assert project_response.status_code == 200, project_response.text
            project_id = project_response.json()["id"]
            registered = api.post(
                "/agents",
                json={
                    "provider": "ANTIGRAVITY",
                    "display_name": "Antigravity timeout integration",
                    "auth_type": "API_KEY",
                    "allowed_project_ids": [project_id],
                    "allowed_actions": [
                        "CREATE_APPROVAL",
                        "READ_APPROVAL_DECISION",
                        "CONSUME_APPROVAL_DECISION",
                        "EXPIRE_OWN_APPROVAL",
                    ],
                    "enabled": True,
                },
            )
            assert registered.status_code == 200, registered.text
            agent_id = registered.json()["agent"]["id"]
            agent_token = registered.json()["agent_token"]

        hook_environment = os.environ.copy()
        for key in tuple(hook_environment):
            if key.startswith("AIXION_POSTGRES_"):
                hook_environment.pop(key, None)
        hook_environment.update(
            {
                "AIXION_API_BASE_URL": base_url,
                "AIXION_AGENT_ID": agent_id,
                "AIXION_AGENT_TOKEN": agent_token,
                "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
                "AIXION_PROJECT_ID": project_id,
                "AIXION_APPROVAL_TIMEOUT_SECONDS": "1",
            }
        )
        payload = {
            "conversationId": "session-short-timeout",
            "stepIdx": 7,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "echo must-not-run"},
            },
        }
        started_at = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=json.dumps(payload) + "\n",
            text=True,
            capture_output=True,
            env=hook_environment,
            timeout=20,
            check=False,
        )
        elapsed_seconds = time.monotonic() - started_at
        assert result.returncode == 0, result.stderr
        decision = json.loads(result.stdout.strip().splitlines()[-1])
        assert decision["decision"] == "deny"
        assert "timed out" in decision["reason"].lower()
        # The hook polls on a fixed cadence, so allow one polling interval of
        # scheduling overhead while still bounding the host wait to < 5 seconds.
        assert 1 <= elapsed_seconds < 5

        with httpx.Client(base_url=base_url, timeout=5) as api:
            approvals = api.get("/approvals")
            assert approvals.status_code == 200, approvals.text
            approval = next(
                item
                for item in approvals.json()
                if item.get("source_session_id") == payload["conversationId"]
            )
            assert approval["status"] == "EXPIRED"

        with sqlite3.connect(db_path) as connection:
            claim_count = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
                (approval["id"],),
            ).fetchone()[0]
        assert claim_count == 0
    finally:
        _stop_api(api_process)

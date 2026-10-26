from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import httpx


BACKEND_ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
HOOK_PATH = (
    REPOSITORY_ROOT / "integrations" / "codex" / "aixion_permission_request_hook.py"
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
    deadline = time.monotonic() + 30
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


def _run_hook(event: dict, environment: dict[str, str]) -> dict:
    completed = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        input=json.dumps(event) + "\n",
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, completed.stderr
    return json.loads(lines[-1])


def _review_fingerprint(api: httpx.Client, approval_id: str) -> str:
    preflight = api.get(f"/approvals/{approval_id}")
    assert preflight.status_code == 200, preflight.text
    reviewed = preflight.json()
    assert reviewed["id"] == approval_id
    fingerprint = reviewed.get("review_fingerprint")
    assert isinstance(fingerprint, str) and fingerprint.startswith("sha256:review-v1:")
    return fingerprint


def test_codex_hook_real_http_decision_consumes_once_and_replay_denies(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "codex-hook-http-integration.sqlite3"
    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    api_process = _start_api(db_path, port)
    first_hook: subprocess.Popen | None = None

    try:
        _wait_for_api(api_process, base_url)
        with httpx.Client(base_url=base_url, timeout=5) as api:
            registration = api.post(
                "/auth/register",
                json={
                    "email": "codex-http-replay@example.com",
                    "password": "Quartz-Maple-915!River",
                    "display_name": "Codex HTTP replay owner",
                },
            )
            assert registration.status_code == 200, registration.text
            code = registration.json().get("dev_verification_code")
            assert code
            verified = api.post(
                "/auth/verify-email",
                json={"email": "codex-http-replay@example.com", "code": code},
            )
            assert verified.status_code == 200, verified.text

            project_response = api.post(
                "/projects", json={"name": "Codex HTTP replay project"}
            )
            assert project_response.status_code == 200, project_response.text
            project_id = project_response.json()["id"]

            registered = api.post(
                "/agents",
                json={
                    "provider": "CODEX",
                    "display_name": "Codex HTTP loopback integration",
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

        environment = os.environ.copy()
        for key in list(environment):
            if key.startswith("AIXION_POSTGRES_"):
                environment.pop(key)
        environment.update(
            {
                "AIXION_API_BASE_URL": base_url,
                "AIXION_AGENT_ID": agent_id,
                "AIXION_AGENT_TOKEN": agent_token,
                "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
                "AIXION_PROJECT_ID": project_id,
                "AIXION_CODEX_WORKSPACE_PATH": str(REPOSITORY_ROOT),
                "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "30",
            }
        )
        event = {
            "hook_event_name": "PermissionRequest",
            "cwd": str(REPOSITORY_ROOT),
            "permission_mode": "default",
            "session_id": "codex-http-replay-session",
            "turn_id": "codex-http-replay-turn",
            "tool_use_id": "codex-http-replay-tool-use",
            "tool_name": "Bash",
            "tool_input": {
                "command": "printf 'exact payload'",
                "workdir": "/tmp/codex-http-replay-workspace",
            },
        }

        first_hook = subprocess.Popen(
            [sys.executable, str(HOOK_PATH)],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert first_hook.stdin is not None
        first_hook.stdin.write(json.dumps(event) + "\n")
        first_hook.stdin.close()
        first_hook.stdin = None

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
                assert first_hook.poll() is None, (
                    "Codex hook exited before creating the pending approval"
                )
                time.sleep(0.1)

            assert approval is not None, "Codex hook did not create its approval"
            assert approval["status"] in {"REQUESTED", "PENDING_REVIEW"}
            assert approval["files"][0]["diff"] == json.dumps(
                event["tool_input"], sort_keys=True, separators=(",", ":")
            )
            review_fingerprint = _review_fingerprint(api, approval["id"])
            decision = api.post(
                f"/approvals/{approval['id']}/decision",
                json={
                    "decision": "approve",
                    "review_fingerprint": review_fingerprint,
                },
            )
            assert decision.status_code == 200, decision.text
            assert decision.json()["status"] == "APPROVED"

        assert first_hook is not None
        first_stdout, first_stderr = first_hook.communicate(timeout=20)
        assert first_hook.returncode == 0, first_stderr
        first_result = json.loads(first_stdout.strip().splitlines()[-1])
        assert first_result["hookSpecificOutput"]["decision"]["behavior"] == "allow"

        replay_result = _run_hook(event, environment)
        replay_decision = replay_result["hookSpecificOutput"]["decision"]
        assert replay_decision["behavior"] == "deny"

        with httpx.Client(base_url=base_url, timeout=5) as api:
            final_rows = api.get("/approvals")
            assert final_rows.status_code == 200, final_rows.text
            matching = [
                item
                for item in final_rows.json()
                if item.get("source_provider") == "CODEX"
                and item.get("source_agent_id") == agent_id
                and item.get("source_invocation_id")
                == approval["source_invocation_id"]
            ]
            assert len(matching) == 1
            assert matching[0]["id"] == approval["id"]

        with sqlite3.connect(db_path) as connection:
            claim_count = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
                (approval["id"],),
            ).fetchone()[0]
        assert claim_count == 1
    finally:
        if first_hook is not None and first_hook.poll() is None:
            first_hook.terminate()
            try:
                first_hook.wait(timeout=5)
            except subprocess.TimeoutExpired:
                first_hook.kill()
                first_hook.wait(timeout=5)
        _stop_api(api_process)

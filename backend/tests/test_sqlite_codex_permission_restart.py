from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
BACKEND_ROOT = Path(__file__).parents[1]
HOOK_PATH = (
    REPOSITORY_ROOT
    / "integrations"
    / "codex"
    / "aixion_permission_request_hook.py"
)


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _api_environment(db_path: Path) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "WINDIR")
        if os.environ.get(key)
    }
    environment.update(
        {
            "PYTHONPATH": str(BACKEND_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "AIXION_PROFILE": "test",
            "AIXION_AUTH_ENABLED": "false",
            "AIXION_DB_PATH": str(db_path),
            "AIXION_OUTBOX_DISPATCH_MODE": "scheduled",
        }
    )
    return environment


def _start_api(db_path: Path, port: int) -> tuple[subprocess.Popen[bytes], str]:
    base_url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
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
        env=_api_environment(db_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    try:
        with httpx.Client(timeout=0.5) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(
                        f"Local API exited during startup with {process.returncode}"
                    )
                try:
                    response = client.get(f"{base_url}/health")
                    if response.status_code == 200 and response.json().get("status") == "ok":
                        return process, base_url
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
        raise AssertionError("Local API did not become healthy before timeout")
    except BaseException:
        _stop_api(process)
        raise


def _stop_api(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _create_codex_agent(base_url: str) -> tuple[str, str, str]:
    with httpx.Client(base_url=base_url, timeout=5) as api:
        registration = api.post(
            "/auth/register",
            json={
                "email": "codex-restart-owner@example.com",
                "password": "Codex-restart-password-123!",
                "display_name": "Codex restart owner",
            },
        )
        assert registration.status_code == 200, registration.text
        code = registration.json().get("dev_verification_code")
        assert code
        verification = api.post(
            "/auth/verify-email",
            json={"email": "codex-restart-owner@example.com", "code": code},
        )
        assert verification.status_code == 200, verification.text
        project_response = api.post(
            "/projects", json={"name": "Codex SQLite restart project"}
        )
        assert project_response.status_code == 200, project_response.text
        project_id = project_response.json()["id"]
        registered = api.post(
            "/agents",
            json={
                "provider": "CODEX",
                "display_name": "Codex SQLite restart test",
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
        body = registered.json()
    return project_id, body["agent"]["id"], body["agent_token"]


def _hook_environment(base_url: str, project_id: str, agent_id: str, token: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "AIXION_API_BASE_URL": base_url,
        "AIXION_AGENT_ID": agent_id,
        "AIXION_AGENT_TOKEN": token,
        "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
        "AIXION_PROJECT_ID": project_id,
        "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "30",
    }


def _launch_hook(event: dict[str, Any], environment: dict[str, str]) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, str(HOOK_PATH)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(event) + "\n")
    process.stdin.close()
    # communicate() should only drain the output pipes after EOF was sent.
    process.stdin = None
    return process


def _finish_hook(process: subprocess.Popen[str]) -> tuple[dict[str, Any], str]:
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 0, stderr
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, "Codex hook emitted no decision"
    output = json.loads(lines[-1])
    assert output.get("hookSpecificOutput", {}).get("hookEventName") == "PermissionRequest"
    return output["hookSpecificOutput"]["decision"], stderr


def _assert_terminal_diagnostic(stderr: str, *, approval_id: str, decision: str) -> None:
    lines = [line for line in stderr.splitlines() if line.strip()]
    assert len(lines) == 1
    diagnostic = json.loads(lines[0])
    assert diagnostic["event"] == "terminal_decision"
    assert diagnostic["provider"] == "CODEX"
    assert diagnostic["decision"] == decision
    assert diagnostic["approval_id"] == approval_id


def _read_approvals(db_path: Path, agent_id: str) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path, timeout=5) as connection:
        rows = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type='approval_request' "
            "AND json_extract(payload,'$.source_provider')='CODEX' "
            "AND json_extract(payload,'$.source_agent_id')=?",
            (agent_id,),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _wait_for_approval(
    db_path: Path,
    *,
    agent_id: str,
    process: subprocess.Popen[str],
    exclude_ids: set[str] | None = None,
) -> dict[str, Any]:
    excluded = exclude_ids or set()
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        approvals = [
            approval
            for approval in _read_approvals(db_path, agent_id)
            if approval.get("id") not in excluded
        ]
        if approvals:
            return max(approvals, key=lambda approval: approval.get("created_at", ""))
        assert process.poll() is None, "Codex hook exited before creating a pending approval"
        time.sleep(0.05)
    pytest.fail("Codex hook did not persist a pending approval before the deadline")


def _decide(base_url: str, approval_id: str, decision: str) -> dict[str, Any]:
    with httpx.Client(base_url=base_url, timeout=5) as api:
        response = api.post(
            f"/approvals/{approval_id}/decision",
            json={"decision": decision, "reason": "SQLite subprocess integration test"},
        )
    assert response.status_code == 200, response.text
    return response.json()


def _claim_count(db_path: Path, approval_id: str) -> int:
    with sqlite3.connect(db_path, timeout=5) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
    return int(row[0])


def _permission_event(*, tool_use_id: str | None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "hook_event_name": "PermissionRequest",
        "session_id": "codex-sqlite-restart-session",
        "turn_id": "codex-sqlite-restart-turn",
        "tool_name": "Bash",
        "tool_input": {"command": "echo durable-codex-approval"},
    }
    if tool_use_id is not None:
        event["tool_use_id"] = tool_use_id
    return event


def test_identified_codex_invocation_recovers_after_api_restart_and_consumes_once(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "codex-hook-restart.sqlite3"
    db_path.parent.chmod(0o700)
    port = _free_loopback_port()
    api_process: subprocess.Popen[bytes] | None = None
    hook_process: subprocess.Popen[str] | None = None
    replay_process: subprocess.Popen[str] | None = None

    try:
        api_process, base_url = _start_api(db_path, port)
        project_id, agent_id, token = _create_codex_agent(base_url)
        environment = _hook_environment(base_url, project_id, agent_id, token)
        event = _permission_event(tool_use_id="codex-tool-use-restart-19")

        hook_process = _launch_hook(event, environment)
        approval = _wait_for_approval(
            db_path, agent_id=agent_id, process=hook_process
        )
        assert approval["status"] in {"PENDING_REVIEW", "REQUESTED"}
        assert approval["source_provider"] == "CODEX"
        assert approval["source_invocation_id"]
        assert hook_process.poll() is None, "hook returned before a reviewer decision"

        _stop_api(api_process)
        api_process = None
        api_process, restarted_base_url = _start_api(db_path, port)
        assert restarted_base_url == base_url

        decision = _decide(base_url, approval["id"], "approve")
        assert decision["status"] == "APPROVED"
        hook_decision, stderr = _finish_hook(hook_process)
        hook_process = None
        _assert_terminal_diagnostic(stderr, approval_id=approval["id"], decision="allow")
        assert hook_decision == {"behavior": "allow"}
        assert _claim_count(db_path, approval["id"]) == 1

        # A restarted hook with the same provider invocation cannot deliver a
        # second allow: the existing one-time decision claim is authoritative.
        replay_process = _launch_hook(event, environment)
        replay_decision, replay_stderr = _finish_hook(replay_process)
        replay_process = None
        _assert_terminal_diagnostic(replay_stderr, approval_id=approval["id"], decision="deny")
        assert replay_decision["behavior"] == "deny"
        assert _claim_count(db_path, approval["id"]) == 1
        invocation_approvals = [
            item
            for item in _read_approvals(db_path, agent_id)
            if item.get("source_invocation_id") == approval["source_invocation_id"]
        ]
        assert [item["id"] for item in invocation_approvals] == [approval["id"]]
    finally:
        if hook_process is not None and hook_process.poll() is None:
            hook_process.kill()
            hook_process.communicate(timeout=5)
        if replay_process is not None and replay_process.poll() is None:
            replay_process.kill()
            replay_process.communicate(timeout=5)
        if api_process is not None:
            _stop_api(api_process)


def test_codex_callback_without_tool_use_id_gets_new_identity_after_relaunch(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "codex-callback-identity.sqlite3"
    db_path.parent.chmod(0o700)
    port = _free_loopback_port()
    api_process: subprocess.Popen[bytes] | None = None
    first_process: subprocess.Popen[str] | None = None
    second_process: subprocess.Popen[str] | None = None

    try:
        api_process, base_url = _start_api(db_path, port)
        project_id, agent_id, token = _create_codex_agent(base_url)
        environment = _hook_environment(base_url, project_id, agent_id, token)
        event = _permission_event(tool_use_id=None)

        first_process = _launch_hook(event, environment)
        first_approval = _wait_for_approval(
            db_path, agent_id=agent_id, process=first_process
        )
        assert first_process.poll() is None
        first_decision = _decide(base_url, first_approval["id"], "approve")
        assert first_decision["status"] == "APPROVED"
        first_output, first_stderr = _finish_hook(first_process)
        first_process = None
        _assert_terminal_diagnostic(first_stderr, approval_id=first_approval["id"], decision="allow")
        assert first_output == {"behavior": "allow"}
        assert _claim_count(db_path, first_approval["id"]) == 1

        second_process = _launch_hook(event, environment)
        second_approval = _wait_for_approval(
            db_path,
            agent_id=agent_id,
            process=second_process,
            exclude_ids={first_approval["id"]},
        )
        assert second_process.poll() is None, "new callback inherited an earlier approval"
        assert second_approval["id"] != first_approval["id"]
        assert second_approval["source_invocation_id"] != first_approval["source_invocation_id"]
        assert second_approval["status"] in {"PENDING_REVIEW", "REQUESTED"}

        second_decision = _decide(base_url, second_approval["id"], "reject")
        assert second_decision["status"] in {"DENIED", "REJECTED"}
        second_output, second_stderr = _finish_hook(second_process)
        second_process = None
        _assert_terminal_diagnostic(second_stderr, approval_id=second_approval["id"], decision="deny")
        assert second_output["behavior"] == "deny"
        assert _claim_count(db_path, first_approval["id"]) == 1
        assert _claim_count(db_path, second_approval["id"]) == 0
    finally:
        for process in (first_process, second_process):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        if api_process is not None:
            _stop_api(api_process)

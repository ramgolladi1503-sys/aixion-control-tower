from __future__ import annotations

import json
import os
import sqlite3
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
HOOK_PATH = REPO_ROOT / "integrations" / "antigravity" / "aixion_pre_tool_hook.py"


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_api(tmp_path: Path) -> tuple[subprocess.Popen[str], str]:
    port = _free_loopback_port()
    db_path = tmp_path / "antigravity-revocation.sqlite3"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AIXION_POSTGRES_")
    }
    env.update(
        {
            "AIXION_PROFILE": "test",
            "AIXION_AUTH_ENABLED": "false",
            "AIXION_DB_PATH": str(db_path),
            "AIXION_POSTGRES_RUNTIME_PROBE_ENABLED": "false",
            "AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED": "false",
            "AIXION_POSTGRES_AUTH_ENABLED": "false",
            "PYTHONPATH": str(BACKEND_ROOT),
        }
    )
    env.pop("AIXION_POSTGRES_DSN", None)
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
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return process, f"http://127.0.0.1:{port}"


def _wait_for_api(process: subprocess.Popen[str], base_url: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"isolated API exited with status {process.returncode}")
        try:
            response = httpx.get(f"{base_url}/health", timeout=0.5)
            if response.is_success:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("isolated API did not become ready")


def _stop_api(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_antigravity_waiting_hook_fails_closed_after_credential_revocation(
    tmp_path: Path,
) -> None:
    """A live hook cannot claim delivery after its connector credential is revoked."""
    assert HOOK_PATH.is_file(), f"Antigravity hook not found: {HOOK_PATH}"
    api_process, base_url = _start_api(tmp_path)
    hook_process: subprocess.Popen[str] | None = None
    try:
        _wait_for_api(api_process, base_url)
        with httpx.Client(base_url=base_url, timeout=5) as client:
            seeded = client.post("/demo/seed")
            assert seeded.status_code == 200, seeded.text
            projects_response = client.get("/projects")
            assert projects_response.status_code == 200, projects_response.text
            projects = projects_response.json()
            assert projects
            project_id = projects[0]["id"]
            registered = client.post(
                "/agents",
                json={
                    "provider": "ANTIGRAVITY",
                    "display_name": "Antigravity revocation regression",
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
            assert registered.status_code == 200
            agent = registered.json()["agent"]
            credential = registered.json()["agent_token"]

        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AIXION_POSTGRES_")
        }
        env.update(
            {
                "AIXION_API_BASE_URL": base_url,
                "AIXION_AGENT_ID": agent["id"],
                "AIXION_AGENT_TOKEN": credential,
                "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
                "AIXION_PROJECT_ID": project_id,
                "AIXION_APPROVAL_TIMEOUT_SECONDS": "30",
                "PYTHONPATH": str(REPO_ROOT),
            }
        )
        hook_process = subprocess.Popen(
            [sys.executable, str(HOOK_PATH)],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        hook_input = {
            "conversationId": "session-credential-revocation",
            "stepIdx": 57,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "printf revocation-regression"},
            },
        }
        assert hook_process.stdin is not None
        hook_process.stdin.write(json.dumps(hook_input) + "\n")
        hook_process.stdin.close()

        deadline = time.monotonic() + 15
        approval = None
        while time.monotonic() < deadline:
            with httpx.Client(base_url=base_url, timeout=3) as client:
                response = client.get("/approvals")
            assert response.status_code == 200, response.text
            candidates = [
                item
                for item in response.json()
                if item.get("source_agent_id") == agent["id"]
                and item.get("source_provider") == "ANTIGRAVITY"
                and item.get("source_session_id") == "session-credential-revocation"
            ]
            if candidates:
                approval = candidates[0]
                break
            if hook_process.poll() is not None:
                stdout, stderr = hook_process.communicate()
                assert credential not in stdout
                assert credential not in stderr
                raise AssertionError("hook ended before its pending approval appeared")
            time.sleep(0.1)

        assert approval is not None, "hook did not create its pending approval"
        assert approval["status"] in {"REQUESTED", "PENDING_REVIEW"}
        assert hook_process.poll() is None, "hook should be waiting for the mobile decision"

        with httpx.Client(base_url=base_url, timeout=5) as client:
            revoked = client.post(f"/agents/{agent['id']}/token/revoke")
        assert revoked.status_code == 200

        stdout, stderr = hook_process.communicate(timeout=15)
        assert credential not in stdout
        assert credential not in stderr
        assert hook_process.returncode == 0, stderr
        result = json.loads(stdout.strip())
        assert result["decision"] == "deny"

        with sqlite3.connect(tmp_path / "antigravity-revocation.sqlite3") as db:
            db.row_factory = sqlite3.Row
            claim_count = db.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id = ?",
                (approval["id"],),
            ).fetchone()[0]
            audit_rows = db.execute(
                "SELECT payload FROM kv_store WHERE entity_type = 'audit_event'"
            ).fetchall()
            audit_events = [json.loads(row[0]) for row in audit_rows]
            claim_audit_count = sum(
                event.get("event_type") == "approval.delivery_claimed"
                and event.get("entity_id") == approval["id"]
                for event in audit_events
            )
            revocation_audit_count = sum(
                event.get("event_type") == "agent.token_revoked"
                and event.get("entity_id") == agent["id"]
                for event in audit_events
            )
        assert claim_count == 0
        assert claim_audit_count == 0
        assert revocation_audit_count == 1
    finally:
        if hook_process is not None and hook_process.poll() is None:
            hook_process.kill()
            try:
                hook_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        _stop_api(api_process)

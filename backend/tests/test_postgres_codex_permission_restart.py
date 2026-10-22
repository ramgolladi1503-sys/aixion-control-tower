from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from app.models import AuthUser, Project, User, UserRole, Workspace
from app.postgres_database import run_postgres_migrations

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


def _schema_connector(dsn: str, schema: str):
    def connect():
        connection = psycopg.connect(dsn)
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        return connection

    return connect


def _seed_workspace_project(connect) -> tuple[Workspace, Project, AuthUser]:
    user = User(
        id=f"codex_test_user_{uuid.uuid4().hex}",
        email=f"codex-{uuid.uuid4().hex}@example.test",
        display_name="Codex PostgreSQL restart test",
        role=UserRole.OWNER,
        password_hash="test-only-not-a-credential",
        password_salt="test-only-salt",
        email_verified=True,
    )
    workspace = Workspace(
        id=f"codex_test_workspace_{uuid.uuid4().hex}",
        name="Codex PostgreSQL restart test",
        owner_user_id=user.id,
    )
    user.workspace_id = workspace.id
    project = Project(
        id=f"codex_test_project_{uuid.uuid4().hex}",
        name="Codex PostgreSQL restart test",
        workspace_id=workspace.id,
        created_by_user_id=user.id,
    )
    with connect() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        connection.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,"
            "email_verified,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                user.id,
                workspace.id,
                user.email,
                user.email.lower(),
                user.role.value,
                user.disabled,
                user.email_verified,
                Jsonb(user.model_dump(mode="json")),
            ),
        )
        connection.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
    auth_user = AuthUser(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        workspace_id=workspace.id,
        email_verified=True,
    )
    return workspace, project, auth_user


def _api_environment(
    *, runtime_dsn: str, db_path: Path, bootstrap_dir: Path, user: AuthUser, schema: str
) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "WINDIR")
        if os.environ.get(key)
    }
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join((str(bootstrap_dir), str(BACKEND_ROOT))),
            "PYTHONDONTWRITEBYTECODE": "1",
            "AIXION_PROFILE": "test",
            "AIXION_AUTH_ENABLED": "true",
            "AIXION_DB_PATH": str(db_path),
            "AIXION_POSTGRES_DSN": runtime_dsn,
            "AIXION_POSTGRES_RUNTIME_PROBE_ENABLED": "true",
            "AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED": "true",
            "AIXION_POSTGRES_AUTH_ENABLED": "true",
            "CODEX_TEST_POSTGRES_SCHEMA": schema,
            "AIXION_AUTH_RATE_LIMIT_HMAC_KEY": "cd" * 32,
            "AIXION_AUTH_REGISTER_LIMIT_PER_IP": "100",
            "AIXION_AUTH_VERIFY_LIMIT_PER_IP": "100",
            "AIXION_AUTH_VERIFY_LIMIT_PER_EMAIL": "100",
            "AIXION_AUTH_LOGIN_LIMIT_PER_IP": "100",
            "AIXION_EMAIL_DISPATCH_ENABLED": "true",
            "AIXION_EMAIL_SMTP_HOST": "127.0.0.1",
            "AIXION_EMAIL_FROM": "noreply@example.test",
            "AIXION_EMAIL_OUTBOX_KEY": "ab" * 32,
            "AIXION_OUTBOX_DISPATCH_MODE": "scheduled",
            "AIXION_PUSH_DISPATCH_ENABLED": "false",
            "CODEX_TEST_USER_ID": user.id,
            "CODEX_TEST_USER_EMAIL": user.email,
            "CODEX_TEST_USER_NAME": user.display_name,
            "CODEX_TEST_WORKSPACE_ID": user.workspace_id or "",
        }
    )
    return environment


def _write_bootstrap(bootstrap_dir: Path) -> None:
    bootstrap_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    bootstrap = '''
import os

from app.auth import require_owner, require_reviewer
from app.main import app
from app.models import AuthUser, UserRole
from app.postgres_runtime import PostgresRuntime
from app.settings import get_settings
import psycopg
from psycopg import sql

test_user = AuthUser(
    id=os.environ["CODEX_TEST_USER_ID"],
    email=os.environ["CODEX_TEST_USER_EMAIL"],
    display_name=os.environ["CODEX_TEST_USER_NAME"],
    role=UserRole.OWNER,
    workspace_id=os.environ["CODEX_TEST_WORKSPACE_ID"],
    email_verified=True,
)
app.dependency_overrides[require_owner] = lambda: test_user
app.dependency_overrides[require_reviewer] = lambda: test_user

def isolated_runtime(cls, *, connector=None):
    settings = get_settings()
    dsn = os.environ["AIXION_POSTGRES_DSN"]
    schema = os.environ["CODEX_TEST_POSTGRES_SCHEMA"]

    def connect(_dsn, **kwargs):
        connection = psycopg.connect(_dsn, **kwargs)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    return cls(_dsn=dsn, profile=settings.profile, settings=settings, _connector=connect)

PostgresRuntime.from_environment = classmethod(isolated_runtime)
'''
    (bootstrap_dir / "codex_postgres_test_app.py").write_text(bootstrap)


def _start_api(
    *,
    runtime_dsn: str,
    db_path: Path,
    bootstrap_dir: Path,
    user: AuthUser,
    schema: str,
    port: int,
) -> tuple[subprocess.Popen[bytes], str]:
    base_url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "codex_postgres_test_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=BACKEND_ROOT,
        env=_api_environment(
            runtime_dsn=runtime_dsn,
            db_path=db_path,
            bootstrap_dir=bootstrap_dir,
            user=user,
                schema=schema,
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 25
    try:
        with httpx.Client(timeout=0.5) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    _, stderr = process.communicate()
                    raise AssertionError(
                        "Local PostgreSQL API exited during startup with "
                        f"{process.returncode}: {stderr.decode(errors='replace')}"
                    )
                try:
                    response = client.get(f"{base_url}/health")
                    if response.status_code == 200 and response.json().get("status") == "ok":
                        return process, base_url
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
        raise AssertionError("PostgreSQL API did not become healthy before timeout")
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


def _create_codex_agent(base_url: str, project_id: str) -> tuple[str, str]:
    with httpx.Client(base_url=base_url, timeout=5) as api:
        registered = api.post(
            "/agents",
            json={
                "provider": "CODEX",
                "display_name": "Codex PostgreSQL restart test",
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
    return body["agent"]["id"], body["agent_token"]


def _hook_environment(
    base_url: str, project_id: str, agent_id: str, token: str
) -> dict[str, str]:
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


def _read_approvals(connect, agent_id: str) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT payload FROM approval_requests WHERE payload->>'source_provider'='CODEX' "
            "AND source_agent_id=%s",
            (agent_id,),
        ).fetchall()
    return [row[0] for row in rows]


def _wait_for_approval(
    connect,
    *,
    agent_id: str,
    process: subprocess.Popen[str],
) -> dict[str, Any]:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        approvals = _read_approvals(connect, agent_id)
        if approvals:
            return max(approvals, key=lambda approval: approval.get("created_at", ""))
        assert process.poll() is None, "Codex hook exited before creating a pending approval"
        time.sleep(0.05)
    pytest.fail("Codex hook did not persist a pending approval before the deadline")


def _decide(base_url: str, approval_id: str) -> dict[str, Any]:
    with httpx.Client(base_url=base_url, timeout=5) as api:
        response = api.post(
            f"/approvals/{approval_id}/decision",
            json={"decision": "approve", "reason": "PostgreSQL subprocess integration test"},
        )
    assert response.status_code == 200, response.text
    return response.json()


def _claim_count(connect, approval_id: str) -> int:
    with connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
            (approval_id,),
        ).fetchone()
    return int(row[0])


def test_identified_codex_invocation_survives_postgres_api_restart_and_consumes_once(
    tmp_path: Path,
) -> None:
    base_dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not base_dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL Codex restart tests")

    schema = f"ct_codex_restart_{uuid.uuid4().hex}"
    schema_created = False
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        schema_created = True

    connect = _schema_connector(base_dsn, schema)
    runtime_dsn = base_dsn
    api_process: subprocess.Popen[bytes] | None = None
    hook_process: subprocess.Popen[str] | None = None
    replay_process: subprocess.Popen[str] | None = None

    try:
        run_postgres_migrations(connect)
        _, project, user = _seed_workspace_project(connect)
        bootstrap_dir = tmp_path / "api_bootstrap"
        _write_bootstrap(bootstrap_dir)
        (tmp_path / "sqlite-fallback").mkdir(mode=0o700)
        port = _free_loopback_port()
        api_process, base_url = _start_api(
            runtime_dsn=runtime_dsn,
            db_path=tmp_path / "sqlite-fallback" / "unused.sqlite3",
            bootstrap_dir=bootstrap_dir,
            user=user,
            schema=schema,
            port=port,
        )
        agent_id, token = _create_codex_agent(base_url, project.id)
        environment = _hook_environment(base_url, project.id, agent_id, token)
        event = {
            "hook_event_name": "PermissionRequest",
            "session_id": "codex-postgres-restart-session",
            "turn_id": "codex-postgres-restart-turn",
            "tool_use_id": "codex-postgres-tool-use-19",
            "tool_name": "Bash",
            "tool_input": {"command": "echo durable-codex-approval"},
        }

        hook_process = _launch_hook(event, environment)
        approval = _wait_for_approval(connect, agent_id=agent_id, process=hook_process)
        assert approval["status"] in {"PENDING_REVIEW", "REQUESTED"}
        assert approval["source_provider"] == "CODEX"
        assert approval["source_invocation_id"]
        assert hook_process.poll() is None, "hook returned before a reviewer decision"

        _stop_api(api_process)
        api_process = None
        api_process, restarted_base_url = _start_api(
            runtime_dsn=runtime_dsn,
            db_path=tmp_path / "sqlite-fallback" / "unused.sqlite3",
            bootstrap_dir=bootstrap_dir,
            user=user,
            schema=schema,
            port=port,
        )
        assert restarted_base_url == base_url

        decision = _decide(base_url, approval["id"])
        assert decision["status"] == "APPROVED"
        hook_decision, stderr = _finish_hook(hook_process)
        hook_process = None
        assert stderr == ""
        assert hook_decision == {"behavior": "allow"}
        assert _claim_count(connect, approval["id"]) == 1

        replay_process = _launch_hook(event, environment)
        replay_decision, replay_stderr = _finish_hook(replay_process)
        replay_process = None
        assert replay_stderr == ""
        assert replay_decision["behavior"] == "deny"
        assert _claim_count(connect, approval["id"]) == 1
        invocation_approvals = [
            item
            for item in _read_approvals(connect, agent_id)
            if item.get("source_invocation_id") == approval["source_invocation_id"]
        ]
        assert [item["id"] for item in invocation_approvals] == [approval["id"]]
    finally:
        for process in (hook_process, replay_process):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        if api_process is not None:
            _stop_api(api_process)
        if schema_created:
            with psycopg.connect(base_dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

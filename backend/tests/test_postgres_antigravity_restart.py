from __future__ import annotations

import hashlib
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
HOOK_PATH = REPOSITORY_ROOT / "integrations" / "antigravity" / "aixion_pre_tool_hook.py"


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _connect_factory(dsn: str, schema: str):
    def connect() -> psycopg.Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    return connect


def _seed_workspace(connect) -> tuple[Workspace, Project, AuthUser]:
    user = User(
        id=f"ag_restart_user_{uuid.uuid4().hex}",
        email=f"ag-restart-{uuid.uuid4().hex}@example.test",
        display_name="Antigravity PostgreSQL restart test",
        role=UserRole.OWNER,
        password_hash="test-only-not-a-credential",
        password_salt="test-only-salt",
        email_verified=True,
    )
    workspace = Workspace(
        id=f"ag_restart_workspace_{uuid.uuid4().hex}",
        name="Antigravity PostgreSQL restart test",
        owner_user_id=user.id,
    )
    user.workspace_id = workspace.id
    project = Project(
        id=f"ag_restart_project_{uuid.uuid4().hex}",
        name="Antigravity PostgreSQL restart test",
        workspace_id=workspace.id,
        created_by_user_id=user.id,
    )
    with connect() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        connection.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
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


def _write_bootstrap(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / "antigravity_postgres_test_app.py").write_text("""
import os
import psycopg
from psycopg import sql
from app.auth import require_owner, require_reviewer
from app.main import app
from app.models import AuthUser, UserRole
from app.postgres_runtime import PostgresRuntime
from app.settings import get_settings

test_user = AuthUser(
    id=os.environ["AG_TEST_USER_ID"], email=os.environ["AG_TEST_USER_EMAIL"],
    display_name="Antigravity PostgreSQL restart test", role=UserRole.OWNER,
    workspace_id=os.environ["AG_TEST_WORKSPACE_ID"], email_verified=True,
)
app.dependency_overrides[require_owner] = lambda: test_user
app.dependency_overrides[require_reviewer] = lambda: test_user

def isolated_runtime(cls, *, connector=None):
    settings = get_settings()
    dsn = os.environ["AIXION_POSTGRES_DSN"]
    schema = os.environ["AG_TEST_POSTGRES_SCHEMA"]
    def connect(_dsn, **kwargs):
        connection = psycopg.connect(_dsn, **kwargs)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection
    return cls(_dsn=dsn, profile=settings.profile, settings=settings, _connector=connect)

PostgresRuntime.from_environment = classmethod(isolated_runtime)
""")


def _api_environment(
    *, dsn: str, db_path: Path, bootstrap: Path, user: AuthUser, schema: str
) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "WINDIR")
        if os.environ.get(key)
    }
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join((str(bootstrap), str(BACKEND_ROOT))),
            "PYTHONDONTWRITEBYTECODE": "1",
            "AIXION_PROFILE": "test",
            "AIXION_AUTH_ENABLED": "true",
            "AIXION_DB_PATH": str(db_path),
            "AIXION_POSTGRES_DSN": dsn,
            "AIXION_POSTGRES_RUNTIME_PROBE_ENABLED": "true",
            "AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED": "true",
            "AIXION_POSTGRES_AUTH_ENABLED": "true",
            "AG_TEST_POSTGRES_SCHEMA": schema,
            "AIXION_AUTH_RATE_LIMIT_HMAC_KEY": "cd" * 32,
            "AIXION_AUTH_REGISTER_LIMIT_PER_IP": "100",
            "AIXION_AUTH_VERIFY_LIMIT_PER_IP": "100",
            "AIXION_AUTH_VERIFY_LIMIT_PER_EMAIL": "100",
            "AIXION_AUTH_LOGIN_LIMIT_PER_IP": "100",
            "AIXION_EMAIL_DISPATCH_ENABLED": "true",
            "AIXION_EMAIL_SMTP_HOST": "127.0.0.1",
            "AIXION_EMAIL_FROM": "noreply@example.test",
            "AIXION_EMAIL_OUTBOX_KEY": "ab" * 32,
            "AIXION_PUSH_DISPATCH_ENABLED": "false",
            "AIXION_OUTBOX_DISPATCH_MODE": "scheduled",
            "AG_TEST_USER_ID": user.id,
            "AG_TEST_USER_EMAIL": user.email,
            "AG_TEST_WORKSPACE_ID": user.workspace_id or "",
        }
    )
    return environment


def _start_api(
    *, dsn: str, db_path: Path, bootstrap: Path, user: AuthUser, schema: str, port: int
) -> tuple[subprocess.Popen[bytes], str]:
    base_url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "antigravity_postgres_test_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=BACKEND_ROOT,
        env=_api_environment(
            dsn=dsn, db_path=db_path, bootstrap=bootstrap, user=user, schema=schema
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 25
    last_health: str = "no response"
    try:
        with httpx.Client(timeout=0.5) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    _, stderr = process.communicate()
                    raise AssertionError(
                        f"PostgreSQL API exited at startup: {stderr.decode(errors='replace')}"
                    )
                try:
                    response = client.get(f"{base_url}/health")
                    last_health = f"HTTP {response.status_code}: {response.text[:500]}"
                    if response.status_code == 200 and response.json().get("status") == "ok":
                        return process, base_url
                except httpx.HTTPError as error:
                    last_health = f"{type(error).__name__}: {error}"
                time.sleep(0.05)
        _stop_api(process)
        _, stderr = process.communicate(timeout=5)
        raise AssertionError(
            "PostgreSQL API did not become healthy before timeout: "
            f"{stderr.decode(errors='replace')}; last health: {last_health}"
        )
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


def _hook_environment(base_url: str, project_id: str, agent_id: str, token: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "AIXION_API_BASE_URL": base_url,
        "AIXION_AGENT_ID": agent_id,
        "AIXION_AGENT_TOKEN": token,
        "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
        "AIXION_PROJECT_ID": project_id,
        "AIXION_APPROVAL_TIMEOUT_SECONDS": "45",
    }


def _launch_hook(payload: dict[str, Any], environment: dict[str, str]) -> subprocess.Popen[str]:
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
    process.stdin.write(__import__("json").dumps(payload) + "\n")
    process.stdin.close()
    process.stdin = None
    return process


def _wait_for_approval(
    connect, *, agent_id: str, invocation_id: str, process: subprocess.Popen[str]
) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with connect() as connection:
            row = connection.execute(
                "SELECT payload FROM approval_requests WHERE source_agent_id=%s "
                "AND payload->>'source_provider'='ANTIGRAVITY' "
                "AND payload->>'source_invocation_id'=%s",
                (agent_id, invocation_id),
            ).fetchone()
        if row:
            return row[0]
        assert process.poll() is None, "Antigravity hook exited before persisting pending approval"
        time.sleep(0.05)
    pytest.fail("Antigravity hook did not persist its pending approval")


def _finish_hook(process: subprocess.Popen[str]) -> tuple[dict[str, Any], str]:
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 0, stderr
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, "Antigravity hook emitted no decision"
    return __import__("json").loads(lines[-1]), stderr


def test_antigravity_pending_approval_survives_postgres_api_restart_and_claims_once(
    tmp_path: Path,
) -> None:
    base_dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not base_dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL Antigravity restart tests")

    schema = f"ct_ag_restart_{uuid.uuid4().hex}"
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    connect = _connect_factory(base_dsn, schema)
    api_process: subprocess.Popen[bytes] | None = None
    hook_process: subprocess.Popen[str] | None = None
    replay_process: subprocess.Popen[str] | None = None
    try:
        run_postgres_migrations(connect)
        workspace, project, user = _seed_workspace(connect)
        bootstrap = tmp_path / "bootstrap"
        _write_bootstrap(bootstrap)
        fallback = tmp_path / "sqlite-fallback"
        fallback.mkdir(mode=0o700)
        port = _free_loopback_port()
        api_process, base_url = _start_api(
            dsn=base_dsn,
            db_path=fallback / "unused.sqlite3",
            bootstrap=bootstrap,
            user=user,
            schema=schema,
            port=port,
        )
        with httpx.Client(base_url=base_url, timeout=5) as api:
            registered = api.post(
                "/agents",
                json={
                    "provider": "ANTIGRAVITY",
                    "display_name": "PostgreSQL restart integration",
                    "auth_type": "API_KEY",
                    "allowed_project_ids": [project.id],
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
        token = registered.json()["agent_token"]
        payload = {
            "conversationId": f"postgres-restart-{uuid.uuid4().hex}",
            "stepIdx": 8,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "echo postgres-restart-safe"},
            },
        }
        import json

        invocation_material = json.dumps(
            [
                payload["conversationId"],
                payload["stepIdx"],
                payload["toolCall"]["name"],
                payload["toolCall"]["args"],
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        invocation_id = "antigravity:v1:" + hashlib.sha256(invocation_material).hexdigest()
        environment = _hook_environment(base_url, project.id, agent_id, token)
        hook_process = _launch_hook(payload, environment)
        approval = _wait_for_approval(
            connect, agent_id=agent_id, invocation_id=invocation_id, process=hook_process
        )
        assert approval["status"] in {"PENDING_REVIEW", "REQUESTED"}
        assert approval["workspace_id"] == workspace.id
        assert approval["source_agent_id"] == agent_id
        assert approval["source_provider"] == "ANTIGRAVITY"
        assert approval["source_invocation_id"] == invocation_id
        assert hook_process.poll() is None
        with connect() as connection:
            pending_row = connection.execute(
                "SELECT workspace_id,source_agent_id,payload->>'source_invocation_id',status "
                "FROM approval_requests WHERE approval_id=%s",
                (approval["id"],),
            ).fetchone()
            assert pending_row == (workspace.id, agent_id, invocation_id, "PENDING_REVIEW")
            assert (
                connection.execute(
                    "SELECT count(*) FROM approval_delivery_claims WHERE approval_id=%s",
                    (approval["id"],),
                ).fetchone()[0]
                == 0
            )

        _stop_api(api_process)
        api_process = None
        api_process, restarted_url = _start_api(
            dsn=base_dsn,
            db_path=fallback / "unused.sqlite3",
            bootstrap=bootstrap,
            user=user,
            schema=schema,
            port=port,
        )
        assert restarted_url == base_url
        with httpx.Client(base_url=base_url, timeout=5) as reviewer:
            decision = reviewer.post(
                f"/approvals/{approval['id']}/decision",
                json={"decision": "approve", "reason": "PostgreSQL restart integration test"},
            )
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == "APPROVED"

        result, _diagnostic = _finish_hook(hook_process)
        hook_process = None
        assert result == {
            "decision": "auto_approve",
            "reason": "Approved from Aixion Control Tower",
        }
        with connect() as connection:
            claim = connection.execute(
                "SELECT workspace_id,source_agent_id,source_invocation_id "
                "FROM approval_delivery_claims WHERE approval_id=%s",
                (approval["id"],),
            ).fetchone()
            assert claim is not None
            assert claim == (workspace.id, agent_id, invocation_id)
            delivery_audit = connection.execute(
                "SELECT details FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.delivery_claimed'",
                (approval["id"],),
            ).fetchone()
            assert delivery_audit is not None
            assert delivery_audit[0]["source_provider"] == "ANTIGRAVITY"
            assert delivery_audit[0]["source_agent_id"] == agent_id
            assert delivery_audit[0]["source_invocation_id"] == invocation_id
            assert (
                delivery_audit[0]["approved_payload_hash"]
                == decision.json()["approved_payload_hash"]
            )
            assert delivery_audit[0]["delivery"] == "at_most_once"
            assert (
                connection.execute(
                    "SELECT count(*) FROM approval_delivery_claims WHERE approval_id=%s",
                    (approval["id"],),
                ).fetchone()[0]
                == 1
            )

        replay_process = _launch_hook(payload, environment)
        replay, _replay_diagnostic = _finish_hook(replay_process)
        replay_process = None
        assert replay["decision"] == "deny"
        with connect() as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM approval_delivery_claims WHERE approval_id=%s",
                    (approval["id"],),
                ).fetchone()[0]
                == 1
            )
    finally:
        for process in (hook_process, replay_process):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        if api_process is not None:
            _stop_api(api_process)
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )

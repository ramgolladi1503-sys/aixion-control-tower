from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
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
        "AIXION_CODEX_WORKSPACE_PATH": str(REPOSITORY_ROOT),
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


def _assert_terminal_diagnostic(stderr: str, *, approval_id: str, decision: str) -> None:
    lines = [line for line in stderr.splitlines() if line.strip()]
    assert len(lines) == 1
    diagnostic = json.loads(lines[0])
    assert diagnostic["event"] == "terminal_decision"
    assert diagnostic["provider"] == "CODEX"
    assert diagnostic["decision"] == decision
    assert diagnostic["approval_id"] == approval_id


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
    exclude_invocation_id: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        approvals = _read_approvals(connect, agent_id)
        matching = [
            approval
            for approval in approvals
            if approval.get("source_invocation_id") != exclude_invocation_id
        ]
        if matching:
            return max(matching, key=lambda approval: approval.get("created_at", ""))
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(
                "Codex hook exited before creating a pending approval: "
                f"rc={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
            )
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


def _create_audit_count(connect, approval_id: str) -> int:
    with connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='approval.created_by_agent'",
            (approval_id,),
        ).fetchone()
    return int(row[0])


def test_codex_hook_restart_reattaches_same_live_postgres_invocation_once(
    tmp_path: Path,
) -> None:
    """A replacement hook for the same provider call resumes the same approval."""
    base_dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not base_dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL Codex restart tests")

    schema = f"ct_codex_reattach_{uuid.uuid4().hex}"
    schema_created = False
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        schema_created = True

    connect = _schema_connector(base_dsn, schema)
    api_process: subprocess.Popen[bytes] | None = None
    interrupted_hook: subprocess.Popen[str] | None = None
    restarted_hook: subprocess.Popen[str] | None = None
    replay_hook: subprocess.Popen[str] | None = None
    try:
        run_postgres_migrations(connect)
        _, project, user = _seed_workspace_project(connect)
        bootstrap_dir = tmp_path / "api_bootstrap"
        _write_bootstrap(bootstrap_dir)
        fallback_dir = tmp_path / "sqlite-fallback"
        fallback_dir.mkdir(mode=0o700)
        port = _free_loopback_port()
        api_process, base_url = _start_api(
            runtime_dsn=base_dsn,
            db_path=fallback_dir / "unused.sqlite3",
            bootstrap_dir=bootstrap_dir,
            user=user,
            schema=schema,
            port=port,
        )
        agent_id, token = _create_codex_agent(base_url, project.id)
        environment = _hook_environment(base_url, project.id, agent_id, token)
        event = {
            "hook_event_name": "PermissionRequest",
            "cwd": str(REPOSITORY_ROOT),
            "permission_mode": "default",
            "session_id": "codex-postgres-hook-restart-session",
            "turn_id": "codex-postgres-hook-restart-turn",
            "tool_use_id": "codex-postgres-hook-restart-tool-use-stable",
            "tool_name": "Bash",
            "tool_input": {"command": "echo resume-same-postgres-approval"},
        }

        interrupted_hook = _launch_hook(event, environment)
        original_approval = _wait_for_approval(
            connect, agent_id=agent_id, process=interrupted_hook
        )
        assert original_approval["status"] in {"PENDING_REVIEW", "REQUESTED"}
        assert original_approval["source_provider"] == "CODEX"
        assert original_approval["source_invocation_id"]
        assert interrupted_hook.poll() is None
        original_create_audits = _create_audit_count(connect, original_approval["id"])
        assert original_create_audits == 1

        # The callback process is gone, but its durable liveness lease is still
        # valid. Do not extend, expire, delete, or otherwise rewrite the lease:
        # the same stable provider call is reattaching within the live window.
        interrupted_hook.kill()
        interrupted_hook.communicate(timeout=5)
        assert interrupted_hook.poll() is not None
        interrupted_hook = None
        with connect() as connection:
            lease = connection.execute(
                "SELECT approval_id,workspace_id,source_agent_id,source_invocation_id,expires_at "
                "FROM approval_invocation_leases WHERE approval_id=%s",
                (original_approval["id"],),
            ).fetchone()
        assert lease is not None
        assert lease[:4] == (
            original_approval["id"],
            user.workspace_id,
            agent_id,
            original_approval["source_invocation_id"],
        )
        assert lease[4] > datetime.now(timezone.utc)

        restarted_hook = _launch_hook(event, environment)
        reattached = _wait_for_approval(
            connect, agent_id=agent_id, process=restarted_hook
        )
        assert reattached["id"] == original_approval["id"]
        assert reattached["source_invocation_id"] == original_approval["source_invocation_id"]
        assert restarted_hook.poll() is None, "restarted hook returned before reviewer decision"
        approvals = [
            item
            for item in _read_approvals(connect, agent_id)
            if item.get("source_invocation_id") == original_approval["source_invocation_id"]
        ]
        assert [item["id"] for item in approvals] == [original_approval["id"]]
        assert _create_audit_count(connect, original_approval["id"]) == original_create_audits
        assert _claim_count(connect, original_approval["id"]) == 0

        decision = _decide(base_url, original_approval["id"])
        assert decision["status"] == "APPROVED"
        hook_decision, stderr = _finish_hook(restarted_hook)
        restarted_hook = None
        _assert_terminal_diagnostic(
            stderr, approval_id=original_approval["id"], decision="allow"
        )
        assert hook_decision == {"behavior": "allow"}
        assert _claim_count(connect, original_approval["id"]) == 1

        # Replaying an already-consumed provider call cannot deliver another
        # allow decision or create a second durable delivery claim.
        replay_hook = _launch_hook(event, environment)
        replay_decision, replay_stderr = _finish_hook(replay_hook)
        replay_hook = None
        _assert_terminal_diagnostic(
            replay_stderr, approval_id=original_approval["id"], decision="deny"
        )
        assert replay_decision["behavior"] == "deny"
        assert _claim_count(connect, original_approval["id"]) == 1
        assert _create_audit_count(connect, original_approval["id"]) == original_create_audits
        final_approvals = [
            item
            for item in _read_approvals(connect, agent_id)
            if item.get("source_invocation_id") == original_approval["source_invocation_id"]
        ]
        assert [item["id"] for item in final_approvals] == [original_approval["id"]]
    finally:
        for process in (interrupted_hook, restarted_hook, replay_hook):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        if api_process is not None:
            _stop_api(api_process)
        if schema_created:
            with psycopg.connect(base_dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


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
            "cwd": str(REPOSITORY_ROOT),
            "permission_mode": "default",
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
        _assert_terminal_diagnostic(stderr, approval_id=approval["id"], decision="allow")
        assert hook_decision == {"behavior": "allow"}
        assert _claim_count(connect, approval["id"]) == 1

        replay_process = _launch_hook(event, environment)
        replay_decision, replay_stderr = _finish_hook(replay_process)
        replay_process = None
        _assert_terminal_diagnostic(replay_stderr, approval_id=approval["id"], decision="deny")
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


def test_identified_codex_permission_callback_returns_reviewer_denial_without_claim(
    tmp_path: Path,
) -> None:
    """A denied PostgreSQL decision returns deny to the exact waiting callback."""
    base_dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not base_dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL Codex restart tests")

    schema = f"ct_codex_deny_{uuid.uuid4().hex}"
    schema_created = False
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        schema_created = True

    connect = _schema_connector(base_dsn, schema)
    api_process: subprocess.Popen[bytes] | None = None
    hook_process: subprocess.Popen[str] | None = None
    try:
        run_postgres_migrations(connect)
        _, project, user = _seed_workspace_project(connect)
        bootstrap_dir = tmp_path / "api_bootstrap"
        _write_bootstrap(bootstrap_dir)
        fallback_dir = tmp_path / "sqlite-fallback"
        fallback_dir.mkdir(mode=0o700)
        port = _free_loopback_port()
        api_process, base_url = _start_api(
            runtime_dsn=base_dsn,
            db_path=fallback_dir / "unused.sqlite3",
            bootstrap_dir=bootstrap_dir,
            user=user,
            schema=schema,
            port=port,
        )
        agent_id, token = _create_codex_agent(base_url, project.id)
        environment = _hook_environment(base_url, project.id, agent_id, token)
        callback_id = f"codex-postgres-denial-{uuid.uuid4().hex}"
        event = {
            "hook_event_name": "PermissionRequest",
            "cwd": str(REPOSITORY_ROOT),
            "permission_mode": "default",
            "session_id": "codex-postgres-denial-session",
            "turn_id": "codex-postgres-denial-turn",
            "tool_use_id": callback_id,
            "tool_name": "Bash",
            "tool_input": {"command": "echo must-stop-after-phone-denial"},
        }

        hook_process = _launch_hook(event, environment)
        approval = _wait_for_approval(connect, agent_id=agent_id, process=hook_process)
        assert approval["status"] in {"PENDING_REVIEW", "REQUESTED"}
        assert approval["source_provider"] == "CODEX"
        assert approval["source_agent_id"] == agent_id
        assert approval["source_invocation_id"]
        assert hook_process.poll() is None, "permission callback returned before reviewer decision"

        with connect() as connection:
            lease = connection.execute(
                "SELECT approval_id,workspace_id,source_agent_id,source_invocation_id,"
                "expires_at FROM approval_invocation_leases WHERE approval_id=%s",
                (approval["id"],),
            ).fetchone()
            assert lease is not None
            assert lease[:4] == (
                approval["id"],
                user.workspace_id,
                agent_id,
                approval["source_invocation_id"],
            )
            assert lease[4] > datetime.now(timezone.utc)
            assert connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.decision'",
                (approval["id"],),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval["id"],),
            ).fetchone()[0] == 0

        reason = "Denied by reviewer in PostgreSQL callback regression"
        with httpx.Client(base_url=base_url, timeout=5) as reviewer:
            response = reviewer.post(
                f"/approvals/{approval['id']}/decision",
                json={"decision": "deny", "reason": reason},
            )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "DENIED"

        hook_decision, stderr = _finish_hook(hook_process)
        hook_process = None
        _assert_terminal_diagnostic(stderr, approval_id=approval["id"], decision="deny")
        assert hook_decision == {
            "behavior": "deny",
            "message": "Control Tower decision: DENIED",
        }

        with connect() as connection:
            row = connection.execute(
                "SELECT status,payload->>'status',source_agent_id,source_invocation_id "
                "FROM approval_requests WHERE approval_id=%s",
                (approval["id"],),
            ).fetchone()
            assert row == (
                "DENIED",
                "DENIED",
                agent_id,
                approval["source_invocation_id"],
            )
            assert connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.decision' AND details->>'decision'='deny'",
                (approval["id"],),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.delivery_claimed'",
                (approval["id"],),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval["id"],),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE approval_id=%s "
                "AND payload->>'title' LIKE 'Approval denied:%%' "
                "AND payload->>'body'=%s",
                (approval["id"], reason),
            ).fetchone()[0] == 1
    finally:
        if hook_process is not None and hook_process.poll() is None:
            hook_process.kill()
            hook_process.communicate(timeout=5)
        if api_process is not None:
            _stop_api(api_process)
        if schema_created:
            with psycopg.connect(base_dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_codex_dead_hook_expired_lease_rejects_review_and_isolates_next_invocation(
    tmp_path: Path,
) -> None:
    """A dead native callback cannot be approved or lend its approval to a new callback."""
    base_dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not base_dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL Codex restart tests")

    schema = f"ct_codex_dead_hook_{uuid.uuid4().hex}"
    schema_created = False
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        schema_created = True

    connect = _schema_connector(base_dsn, schema)
    api_process: subprocess.Popen[bytes] | None = None
    dead_hook: subprocess.Popen[str] | None = None
    next_hook: subprocess.Popen[str] | None = None
    try:
        run_postgres_migrations(connect)
        _, project, user = _seed_workspace_project(connect)
        bootstrap_dir = tmp_path / "api_bootstrap"
        _write_bootstrap(bootstrap_dir)
        (tmp_path / "sqlite-fallback").mkdir(mode=0o700)
        port = _free_loopback_port()
        api_process, base_url = _start_api(
            runtime_dsn=base_dsn,
            db_path=tmp_path / "sqlite-fallback" / "unused.sqlite3",
            bootstrap_dir=bootstrap_dir,
            user=user,
            schema=schema,
            port=port,
        )
        agent_id, token = _create_codex_agent(base_url, project.id)
        environment = _hook_environment(base_url, project.id, agent_id, token)
        original_event = {
            "hook_event_name": "PermissionRequest",
            "cwd": str(REPOSITORY_ROOT),
            "permission_mode": "default",
            "session_id": "codex-dead-hook-session",
            "turn_id": "codex-dead-hook-turn",
            "tool_use_id": "codex-dead-hook-tool-use-1",
            "tool_name": "Bash",
            "tool_input": {"command": "echo must-not-resume-after-hook-crash"},
        }

        dead_hook = _launch_hook(original_event, environment)
        old_approval = _wait_for_approval(connect, agent_id=agent_id, process=dead_hook)
        assert old_approval["status"] in {"PENDING_REVIEW", "REQUESTED"}
        assert dead_hook.poll() is None

        # Simulate the native Codex callback process disappearing while its
        # approval is still pending. The API process deliberately stays alive.
        dead_hook.kill()
        dead_hook.communicate(timeout=5)
        assert dead_hook.poll() is not None
        dead_hook = None

        # Expire only this test-owned lease. Keep the approval's own deadline in
        # the future so the decision route must reject specifically on liveness.
        with connect() as connection:
            connection.execute(
                "UPDATE approval_invocation_leases SET heartbeat_at=now()-interval '2 minutes',"
                "expires_at=now()-interval '1 minute' WHERE approval_id=%s",
                (old_approval["id"],),
            )
            lease = connection.execute(
                "SELECT heartbeat_at,expires_at FROM approval_invocation_leases "
                "WHERE approval_id=%s",
                (old_approval["id"],),
            ).fetchone()
            assert lease is not None and lease[1] > lease[0]
            assert lease[1] < datetime.now(timezone.utc)

        with connect() as connection:
            before_audit_events = connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s",
                (old_approval["id"],),
            ).fetchone()[0]
            before_decision_events = connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.decision'",
                (old_approval["id"],),
            ).fetchone()[0]
            before_notifications = connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE approval_id=%s",
                (old_approval["id"],),
            ).fetchone()[0]
        assert before_decision_events == 0

        with httpx.Client(base_url=base_url, timeout=5) as reviewer:
            rejected = reviewer.post(
                f"/approvals/{old_approval['id']}/decision",
                json={"decision": "approve", "reason": "stale Codex callback regression"},
            )
        assert rejected.status_code == 409, rejected.text

        with connect() as connection:
            old_row = connection.execute(
                "SELECT status,payload->>'status',expires_at FROM approval_requests "
                "WHERE approval_id=%s",
                (old_approval["id"],),
            ).fetchone()
            assert old_row is not None
            assert old_row[0] == "PENDING_REVIEW"
            assert old_row[1] == "PENDING_REVIEW"
            assert old_row[2] > datetime.now(timezone.utc)
            assert connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s",
                (old_approval["id"],),
            ).fetchone()[0] == before_audit_events
            assert connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.decision'",
                (old_approval["id"],),
            ).fetchone()[0] == before_decision_events
            assert connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE approval_id=%s",
                (old_approval["id"],),
            ).fetchone()[0] == before_notifications
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (old_approval["id"],),
            ).fetchone()[0] == 0

        # A subsequent callback with a distinct provider invocation identity
        # must create its own approval. It can complete only that new approval;
        # the expired request remains pending and unclaimed.
        next_event = {
            **original_event,
            "tool_use_id": "codex-dead-hook-tool-use-2",
            "tool_input": {"command": "echo distinct-invocation"},
        }
        next_hook = _launch_hook(next_event, environment)
        next_approval = _wait_for_approval(
            connect,
            agent_id=agent_id,
            process=next_hook,
            exclude_invocation_id=old_approval["source_invocation_id"],
        )
        assert next_approval["id"] != old_approval["id"]
        assert next_approval["source_invocation_id"] != old_approval["source_invocation_id"]
        assert next_approval["status"] in {"PENDING_REVIEW", "REQUESTED"}

        next_decision = _decide(base_url, next_approval["id"])
        assert next_decision["status"] == "APPROVED"
        hook_decision, stderr = _finish_hook(next_hook)
        next_hook = None
        _assert_terminal_diagnostic(
            stderr, approval_id=next_approval["id"], decision="allow"
        )
        assert hook_decision == {"behavior": "allow"}
        assert _claim_count(connect, old_approval["id"]) == 0
        assert _claim_count(connect, next_approval["id"]) == 1

        old_after_new_invocation = next(
            item for item in _read_approvals(connect, agent_id) if item["id"] == old_approval["id"]
        )
        assert old_after_new_invocation["status"] == "PENDING_REVIEW"
        assert old_after_new_invocation["source_invocation_id"] != next_approval[
            "source_invocation_id"
        ]
    finally:
        for process in (dead_hook, next_hook):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        if api_process is not None:
            _stop_api(api_process)
        if schema_created:
            with psycopg.connect(base_dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_callback_without_provider_call_id_cannot_reuse_approval_after_hook_restart(
    tmp_path: Path,
) -> None:
    """A replacement callback gets a new identity and cannot inherit the old phone decision."""
    base_dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not base_dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL Codex restart tests")

    schema = f"ct_codex_callback_restart_{uuid.uuid4().hex}"
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    connect = _schema_connector(base_dsn, schema)
    api_process: subprocess.Popen[bytes] | None = None
    lost_hook: subprocess.Popen[str] | None = None
    replacement_hook: subprocess.Popen[str] | None = None
    try:
        run_postgres_migrations(connect)
        _, project, user = _seed_workspace_project(connect)
        bootstrap_dir = tmp_path / "api_bootstrap"
        _write_bootstrap(bootstrap_dir)
        fallback_dir = tmp_path / "sqlite-fallback"
        fallback_dir.mkdir(mode=0o700)
        port = _free_loopback_port()
        api_process, base_url = _start_api(
            runtime_dsn=base_dsn,
            db_path=fallback_dir / "unused.sqlite3",
            bootstrap_dir=bootstrap_dir,
            user=user,
            schema=schema,
            port=port,
        )
        agent_id, token = _create_codex_agent(base_url, project.id)
        environment = _hook_environment(base_url, project.id, agent_id, token)
        event = {
            "hook_event_name": "PermissionRequest",
            "cwd": str(REPOSITORY_ROOT),
            "permission_mode": "default",
            "session_id": "codex-callback-restart-session",
            "turn_id": "codex-callback-restart-turn",
            "tool_name": "Bash",
            "tool_input": {"command": "echo callback-scoped-approval"},
        }

        lost_hook = _launch_hook(event, environment)
        old_approval = _wait_for_approval(connect, agent_id=agent_id, process=lost_hook)
        assert old_approval["status"] in {"PENDING_REVIEW", "REQUESTED"}
        assert old_approval["source_invocation_id"]
        lost_hook.kill()
        lost_hook.communicate(timeout=5)
        lost_hook = None

        _decide(base_url, old_approval["id"])
        replacement_hook = _launch_hook(event, environment)
        deadline = time.monotonic() + 12
        new_approval = None
        while time.monotonic() < deadline:
            candidates = [
                item
                for item in _read_approvals(connect, agent_id)
                if item.get("source_invocation_id") != old_approval["source_invocation_id"]
            ]
            if candidates:
                new_approval = max(candidates, key=lambda item: item.get("created_at", ""))
                break
            assert replacement_hook.poll() is None, (
                "replacement Codex hook exited before creating its callback-scoped approval"
            )
            time.sleep(0.05)
        assert new_approval is not None, "replacement callback did not create a distinct approval"

        assert new_approval["id"] != old_approval["id"]
        assert new_approval["source_invocation_id"] != old_approval["source_invocation_id"]
        assert new_approval["status"] in {"PENDING_REVIEW", "REQUESTED"}
        assert replacement_hook.poll() is None, "replacement hook returned before its own decision"
        assert _claim_count(connect, old_approval["id"]) == 0
        assert _claim_count(connect, new_approval["id"]) == 0
    finally:
        for process in (lost_hook, replacement_hook):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        if api_process is not None:
            _stop_api(api_process)
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

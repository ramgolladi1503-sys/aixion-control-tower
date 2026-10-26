from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Iterator
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql

from app.application_repositories import ApplicationRepositories
from app.main import app
from app.models import Notification
from app.postgres_auth_repository import AuthenticatedSession, PostgresAuthRepository
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_auth_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[PostgresAuthRepository, Callable[[], Connection], str, str]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL auth route tests")

    schema = f"ct_auth_routes_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    prior_runtime = getattr(app.state, "postgres_runtime", None)
    try:
        run_postgres_migrations(connect)
        repositories = ApplicationRepositories.from_connector(connect)
        repository = repositories.auth
        runtime = SimpleNamespace(
            repositories=repositories,
            auth=repository,
            auth_rate_limits=repositories.auth_rate_limits,
            workspace_membership=repositories.workspace_membership,
            email_outbox=repositories.email_outbox,
        )
        monkeypatch.setenv("AIXION_PROFILE", "test")
        monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
        monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "true")
        monkeypatch.setenv("AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED", "true")
        monkeypatch.setenv("AIXION_POSTGRES_AUTH_ENABLED", "true")
        monkeypatch.setenv("AIXION_EMAIL_DISPATCH_ENABLED", "true")
        monkeypatch.setenv("AIXION_EMAIL_SMTP_HOST", "localhost")
        monkeypatch.setenv("AIXION_EMAIL_FROM", "noreply@example.com")
        monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", "ab" * 32)
        monkeypatch.setenv("AIXION_AUTH_RATE_LIMIT_HMAC_KEY", "cd" * 32)
        monkeypatch.setenv("AIXION_AUTH_REGISTER_LIMIT_PER_IP", "100")
        monkeypatch.setenv("AIXION_AUTH_VERIFY_LIMIT_PER_IP", "100")
        monkeypatch.setenv("AIXION_AUTH_VERIFY_LIMIT_PER_EMAIL", "100")
        monkeypatch.setenv("AIXION_AUTH_LOGIN_LIMIT_PER_IP", "100")
        monkeypatch.setattr(app.state, "postgres_runtime", runtime, raising=False)
        monkeypatch.setattr(app.state, "postgres_repositories", repositories, raising=False)
        monkeypatch.setattr(
            app.state,
            "notification_outbox_repository",
            repositories.notification_outbox,
            raising=False,
        )
        yield repository, connect, dsn, schema
    finally:
        monkeypatch.setattr(app.state, "postgres_runtime", prior_runtime, raising=False)
        monkeypatch.setattr(app.state, "postgres_repositories", None, raising=False)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _register_and_login(client: TestClient, email: str, password: str) -> tuple[dict, str]:
    registration = client.post(
        "/auth/register",
        json={"email": email, "password": password, "display_name": email.split("@")[0]},
    )
    assert registration.status_code == 200, registration.text
    code = registration.json()["dev_verification_code"]
    assert code
    verification = client.post("/auth/verify-email", json={"email": email, "code": code})
    assert verification.status_code == 200, verification.text
    login = client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    return registration.json()["user"], login.json()["access_token"]


def test_postgres_auth_route_session_is_accepted_by_fresh_api_process(
    postgres_auth_runtime: tuple[PostgresAuthRepository, Callable[[], Connection], str, str],
) -> None:
    _, _, dsn, schema = postgres_auth_runtime
    client = TestClient(app)
    email = f"cross-process-{uuid.uuid4().hex}@example.com"
    user, token = _register_and_login(
        client,
        email,
        "Cross-Process-Local-Postgres-Password-842!",
    )

    child_environment = os.environ.copy()
    child_environment.update(
        {
            "AIXION_POSTGRES_DSN": dsn,
            "AIXION_TEST_POSTGRES_SCHEMA": schema,
            "AIXION_TEST_POSTGRES_TOKEN": token,
            "AIXION_TEST_POSTGRES_USER_ID": user["id"],
            "AIXION_PROFILE": "test",
            "AIXION_AUTH_ENABLED": "true",
            "AIXION_POSTGRES_RUNTIME_PROBE_ENABLED": "true",
            "AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED": "true",
            "AIXION_POSTGRES_AUTH_ENABLED": "true",
            "AIXION_EMAIL_DISPATCH_ENABLED": "true",
            "AIXION_EMAIL_SMTP_HOST": "localhost",
            "AIXION_EMAIL_FROM": "noreply@example.com",
            "AIXION_EMAIL_OUTBOX_KEY": "ab" * 32,
            "AIXION_AUTH_RATE_LIMIT_HMAC_KEY": "cd" * 32,
        }
    )
    child_code = """
import asyncio
import os
import psycopg
from psycopg import sql
from fastapi.testclient import TestClient
import app.main as main
from app.postgres_runtime import PostgresRuntime

dsn = os.environ["AIXION_POSTGRES_DSN"]
schema = os.environ["AIXION_TEST_POSTGRES_SCHEMA"]
def connect(connection_dsn=None, **kwargs):
    connection = psycopg.connect(connection_dsn or dsn, **kwargs)
    connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    return connection

runtime = PostgresRuntime.from_environment(connector=connect)
PostgresRuntime.from_environment = classmethod(lambda cls: runtime)
async def idle_worker(**kwargs):
    await asyncio.Event().wait()
main.notification_dispatch_loop = idle_worker
main.email_dispatch_loop = idle_worker

with TestClient(main.app) as client:
    response = client.get(
        "/auth/me",
        headers={"Authorization": "Bearer " + os.environ["AIXION_TEST_POSTGRES_TOKEN"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == os.environ["AIXION_TEST_POSTGRES_USER_ID"]
"""
    observed_in_child = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=child_environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert observed_in_child.returncode == 0, observed_in_child.stderr


def test_concurrent_postgres_account_registration_creates_one_workspace_and_one_verification_email(
    postgres_auth_runtime: tuple[PostgresAuthRepository, Callable[[], Connection], str, str],
) -> None:
    _repository, connect, _dsn, _schema = postgres_auth_runtime
    email = f"concurrent-register-{uuid.uuid4().hex}@example.com"
    password = "Concurrent-Register-Local-Postgres-Password-842!"
    before_users = set(store.users)
    before_workspaces = set(store.workspaces)

    def register_once(display_name: str) -> tuple[int, dict]:
        # Deliberately do not enter TestClient's lifespan: this fixture binds
        # an isolated app-state runtime directly and must not open a second
        # environment-selected PostgreSQL connection pool.
        client = TestClient(app)
        try:
            response = client.post(
                "/auth/register",
                json={"email": email, "password": password, "display_name": display_name},
            )
            return response.status_code, response.json()
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(register_once, ("First owner", "Concurrent owner")))

    assert sorted(status_code for status_code, _body in outcomes) == [200, 409]
    successful = next(body for status_code, body in outcomes if status_code == 200)
    with connect() as conn:
        rows = conn.execute(
            "SELECT user_id,workspace_id FROM users WHERE normalized_email=%s",
            (email.lower(),),
        ).fetchall()
        assert len(rows) == 1
        user_id, workspace_id = rows[0]
        assert user_id == successful["user"]["id"]
        assert workspace_id == successful["user"]["workspace_id"]
        assert conn.execute(
            "SELECT payload->>'owner_user_id' FROM workspaces WHERE workspace_id=%s", (workspace_id,)
        ).fetchone() == (user_id,)
        assert conn.execute(
            "SELECT count(*) FROM auth_email_outbox WHERE recipient=%s", (email.lower(),)
        ).fetchone()[0] == 1
    assert set(store.users) == before_users
    assert set(store.workspaces) == before_workspaces


def test_postgres_logout_revokes_only_current_session_with_durable_audit(
    postgres_auth_runtime: tuple[PostgresAuthRepository, Callable[[], Connection], str, str],
) -> None:
    repository, connect, _, _ = postgres_auth_runtime
    client = TestClient(app)
    email = f"logout-{uuid.uuid4().hex}@example.com"
    password = "Logout-Local-Postgres-Password-842!"
    user, current_token = _register_and_login(client, email, password)
    sibling_login = client.post("/auth/login", json={"email": email, "password": password})
    assert sibling_login.status_code == 200, sibling_login.text
    sibling_token = sibling_login.json()["access_token"]

    response = client.post(
        "/auth/logout",
        headers={"Authorization": f"Bearer {current_token}"},
    )

    assert response.status_code == 204
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {current_token}"}).status_code == 401
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {sibling_token}"}).status_code == 200
    current_hash = hashlib.sha256(current_token.encode()).hexdigest()
    sibling_hash = hashlib.sha256(sibling_token.encode()).hexdigest()
    assert repository.authenticate_session(current_hash) is None
    assert repository.authenticate_session(sibling_hash) is not None
    with connect() as conn:
        events = conn.execute(
            "SELECT event_type,actor,entity_id,workspace_id,details FROM audit_events "
            "WHERE workspace_id=%s AND event_type='auth.session_revoked'",
            (user["workspace_id"],),
        ).fetchall()
    assert len(events) == 1
    event_type, actor, session_id, workspace_id, details = events[0]
    assert event_type == "auth.session_revoked"
    assert actor == email
    assert workspace_id == user["workspace_id"]
    assert details["session_id"] == session_id
    assert details["reason"] == "user_logout"


def test_postgres_push_device_is_bound_to_the_registering_session_and_stops_on_logout(
    postgres_auth_runtime: tuple[PostgresAuthRepository, Callable[[], Connection], str, str],
) -> None:
    _, connect, _, _ = postgres_auth_runtime
    repositories = app.state.postgres_repositories
    client = TestClient(app)
    email = f"device-session-{uuid.uuid4().hex}@example.com"
    password = "Device-Session-Local-Postgres-Password-842!"
    user, first_token = _register_and_login(client, email, password)
    second_login = client.post("/auth/login", json={"email": email, "password": password})
    assert second_login.status_code == 200, second_login.text
    second_token = second_login.json()["access_token"]

    registered = client.post(
        "/notifications/devices",
        headers={"Authorization": f"Bearer {first_token}"},
        json={"token": f"fcm-session-{uuid.uuid4().hex}", "app_version": "1.0"},
    )
    assert registered.status_code == 200, registered.text
    device = registered.json()
    first_session_hash = hashlib.sha256(first_token.encode()).hexdigest()
    with connect() as conn:
        first_session_id = conn.execute(
            "SELECT session_id FROM sessions WHERE token_hash=%s",
            (first_session_hash,),
        ).fetchone()[0]
        stored_device = conn.execute(
            "SELECT session_id,payload->>'session_id' FROM device_registrations WHERE device_id=%s",
            (device["id"],),
        ).fetchone()
    assert stored_device == (first_session_id, first_session_id)

    target = Notification(
        title="Approval",
        body="Review requested",
        entity_type="approval_request",
        entity_id="approval-session-binding",
        workspace_id=user["workspace_id"],
        user_id=user["id"],
    )
    assert [item.id for item in repositories.notification_outbox.list_device_targets(target)] == [
        device["id"]
    ]

    logged_out = client.post(
        "/auth/logout",
        headers={"Authorization": f"Bearer {first_token}"},
    )
    assert logged_out.status_code == 204
    assert repositories.notification_outbox.list_device_targets(target) == []
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {second_token}"}).status_code == 200

    rebound = client.post(
        "/notifications/devices",
        headers={"Authorization": f"Bearer {second_token}"},
        json={"token": device["token"], "app_version": "1.0"},
    )
    assert rebound.status_code == 200, rebound.text
    second_session_hash = hashlib.sha256(second_token.encode()).hexdigest()
    with connect() as conn:
        second_session_id = conn.execute(
            "SELECT session_id FROM sessions WHERE token_hash=%s",
            (second_session_hash,),
        ).fetchone()[0]
        stored_device = conn.execute(
            "SELECT device_id,session_id,payload->>'session_id' "
            "FROM device_registrations WHERE token=%s",
            (device["token"],),
        ).fetchone()
        rebound_events = conn.execute(
            "SELECT event_type FROM audit_events WHERE entity_id=%s "
            "AND event_type='notification.device_session_rebound'",
            (device["id"],),
        ).fetchall()
    assert stored_device == (device["id"], second_session_id, second_session_id)
    assert len(rebound_events) == 1
    assert [item.id for item in repositories.notification_outbox.list_device_targets(target)] == [
        device["id"]
    ]


def test_postgres_auth_rejects_session_without_durable_membership(
    postgres_auth_runtime: tuple[PostgresAuthRepository, Callable[[], Connection], str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, _, _ = postgres_auth_runtime
    client = TestClient(app)
    email = f"membership-{uuid.uuid4().hex}@example.com"
    _user, token = _register_and_login(
        client,
        email,
        "Membership-Local-Postgres-Password-842!",
    )
    authenticate_session = repository.authenticate_session

    def return_principal_without_membership(token_hash: str):
        authenticated = authenticate_session(token_hash)
        assert authenticated is not None
        inconsistent_user = authenticated.user.model_copy(
            update={"id": "missing-durable-user"}
        )
        return AuthenticatedSession(
            user=inconsistent_user,
            session=authenticated.session,
        )

    monkeypatch.setattr(
        repository,
        "authenticate_session",
        return_principal_without_membership,
    )

    response = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Workspace membership is required"


def test_auth_routes_use_postgres_for_workspace_invite_session_and_audit(
    postgres_auth_runtime: tuple[PostgresAuthRepository, Callable[[], Connection], str, str],
) -> None:
    _, connect, _, _ = postgres_auth_runtime
    client = TestClient(app)
    owner_email = f"owner-{uuid.uuid4().hex}@example.com"
    owner_password = "Owner-Local-Postgres-Password-842!"
    owner, owner_token = _register_and_login(client, owner_email, owner_password)
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    me = client.get("/auth/me", headers=owner_headers)
    assert me.status_code == 200
    assert me.json()["id"] == owner["id"]
    assert me.json()["workspace_id"] == owner["workspace_id"]

    invite_email = f"reviewer-{uuid.uuid4().hex}@example.com"
    created_invite = client.post(
        "/auth/invites",
        headers=owner_headers,
        json={"email": invite_email, "role": "REVIEWER"},
    )
    assert created_invite.status_code == 200, created_invite.text
    invite_token = created_invite.json()["token"]
    listed_invites = client.get("/auth/invites", headers=owner_headers)
    assert listed_invites.status_code == 200
    assert listed_invites.json()[0]["email"] == invite_email

    invite_registration = client.post(
        "/auth/register",
        json={
            "email": invite_email,
            "password": "Reviewer-Local-Postgres-Password-842!",
            "display_name": "Reviewer",
            "invite_token": invite_token,
        },
    )
    assert invite_registration.status_code == 200, invite_registration.text
    reviewer_code = invite_registration.json()["dev_verification_code"]
    reviewer_verify = client.post(
        "/auth/verify-email", json={"email": invite_email, "code": reviewer_code}
    )
    assert reviewer_verify.status_code == 200, reviewer_verify.text

    users = client.get("/auth/users", headers=owner_headers)
    assert users.status_code == 200
    reviewer = next(item for item in users.json() if item["email"] == invite_email)
    assert reviewer["workspace_id"] == owner["workspace_id"]

    role_update = client.patch(
        f"/auth/users/{reviewer['id']}/role",
        headers=owner_headers,
        json={"role": "MAINTAINER"},
    )
    assert role_update.status_code == 200, role_update.text
    assert role_update.json()["role"] == "MAINTAINER"

    revoke_invite_email = f"revoked-{uuid.uuid4().hex}@example.com"
    pending_invite = client.post(
        "/auth/invites",
        headers=owner_headers,
        json={"email": revoke_invite_email},
    )
    assert pending_invite.status_code == 200, pending_invite.text
    revoked_invite = client.post(
        f"/auth/invites/{pending_invite.json()['id']}/revoke",
        headers=owner_headers,
    )
    assert revoked_invite.status_code == 200, revoked_invite.text
    assert revoked_invite.json()["status"] == "REVOKED"

    reviewer_login = client.post(
        "/auth/login",
        json={"email": invite_email, "password": "Reviewer-Local-Postgres-Password-842!"},
    )
    assert reviewer_login.status_code == 200, reviewer_login.text

    sessions = client.get("/auth/sessions", headers=owner_headers)
    assert sessions.status_code == 200
    reviewer_session = next(
        item for item in sessions.json() if item["user_email"] == invite_email
    )
    revoke = client.post(
        f"/auth/users/{reviewer['id']}/sessions/revoke", headers=owner_headers
    )
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["revoked_sessions_count"] == 1
    revoked_me = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {reviewer_login.json()['access_token']}"}
    )
    assert revoked_me.status_code == 401

    deletion_email = f"deletion-{uuid.uuid4().hex}@example.com"
    deleted_user, deletion_token = _register_and_login(
        client, deletion_email, "Deletion-Local-Postgres-Password-842!"
    )
    deletion = client.post(
        "/auth/account-deletion-request",
        headers={"Authorization": f"Bearer {deletion_token}"},
        json={"reason": "route integration verification"},
    )
    assert deletion.status_code == 200, deletion.text
    assert deletion.json()["user_id"] == deleted_user["id"]
    assert deletion.json()["active_sessions_revoked"] == 1
    disabled_me = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {deletion_token}"}
    )
    assert disabled_me.status_code == 401

    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM workspaces WHERE workspace_id=%s", (owner["workspace_id"],)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM auth_email_outbox WHERE recipient IN (%s,%s,%s)",
            (owner_email, invite_email, deletion_email),
        ).fetchone()[0] == 3
        for event_type, expected in (
            ("auth.invite_created", 2),
            ("auth.invite_accepted", 1),
            ("auth.invite_revoked", 1),
            ("auth.role_updated", 1),
            ("auth.sessions_revoked", 1),
        ):
            assert connection.execute(
                "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND event_type=%s",
                (owner["workspace_id"], event_type),
            ).fetchone()[0] == expected
        assert connection.execute(
            "SELECT revoked FROM sessions WHERE session_id=%s", (reviewer_session["id"],)
        ).fetchone()[0] is True
        assert connection.execute(
            "SELECT disabled FROM users WHERE user_id=%s", (deleted_user["id"],)
        ).fetchone()[0] is True
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s "
            "AND event_type='auth.account_deletion_requested'",
            (deleted_user["workspace_id"],),
        ).fetchone()[0] == 1

    # The enabled PostgreSQL route path must not project auth authority into
    # the compatibility store's process-local user/session maps.
    assert all(
        user.email not in {owner_email, invite_email, deletion_email}
        for user in store.users.values()
    )
    assert all(
        session.user_id not in {owner["id"], reviewer["id"], deleted_user["id"]}
        for session in store.sessions.values()
    )


def test_postgres_resend_rotates_verification_and_second_repository_sees_commit(
    postgres_auth_runtime: tuple[PostgresAuthRepository, Callable[[], Connection], str, str],
) -> None:
    repository, connect, _, _ = postgres_auth_runtime
    client = TestClient(app)
    email = f"resend-{uuid.uuid4().hex}@example.com"
    registration = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "Resend-Local-Postgres-Password-842!",
            "display_name": "Resend",
        },
    )
    assert registration.status_code == 200, registration.text
    original_code = registration.json()["dev_verification_code"]
    assert original_code

    # A fresh OS process and repository pool must observe the route-committed
    # account without relying on this process's compatibility maps.
    with connect() as connection:
        schema = connection.execute("SELECT current_schema()").fetchone()[0]
    child_environment = os.environ.copy()
    child_environment["AIXION_TEST_POSTGRES_SCHEMA"] = schema
    child_environment["AIXION_TEST_POSTGRES_EMAIL"] = email
    child_environment["AIXION_TEST_POSTGRES_USER_ID"] = registration.json()["user"]["id"]
    child_code = """
import os
import psycopg
from psycopg import sql
from app.postgres_auth_repository import PostgresAuthRepository

dsn = os.environ["AIXION_TEST_POSTGRES_DSN"]
schema = os.environ["AIXION_TEST_POSTGRES_SCHEMA"]
def connect():
    connection = psycopg.connect(dsn)
    connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    return connection

user = PostgresAuthRepository(connect).get_user_by_email(os.environ["AIXION_TEST_POSTGRES_EMAIL"])
assert user is not None
assert user.id == os.environ["AIXION_TEST_POSTGRES_USER_ID"]
assert user.email_verified is False
"""
    observed_in_child = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=child_environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert observed_in_child.returncode == 0, observed_in_child.stderr

    resend = client.post("/auth/resend-verification", json={"email": email})
    assert resend.status_code == 200, resend.text
    replacement_code = resend.json()["dev_verification_code"]
    assert replacement_code and replacement_code != original_code

    stale = client.post(
        "/auth/verify-email", json={"email": email, "code": original_code}
    )
    assert stale.status_code == 400
    verified = client.post(
        "/auth/verify-email", json={"email": email, "code": replacement_code}
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["verified"] is True
    verified_user = repository.get_user_by_email(email)
    assert verified_user is not None and verified_user.email_verified is True

    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM auth_email_outbox WHERE recipient=%s", (email,)
        ).fetchone()[0] == 2

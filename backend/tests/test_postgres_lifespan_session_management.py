from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app import auth, main
from app.auth import require_owner
from app.models import SessionToken, User, UserRole, now_utc
from app.settings import get_settings
from app.store import store

pytest_plugins = ("test_postgres_lifespan_approval_inbox",)


def _start_postgres_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = replace(
        get_settings(),
        profile="test",
        auth_enabled=True,
        postgres_auth_enabled=True,
        postgres_runtime_probe_enabled=False,
        postgres_auth_rate_limits_enabled=False,
        outbox_dispatch_mode="scheduled",
        push_dispatch_enabled=False,
        email_dispatch_enabled=False,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)


def test_lifespan_session_list_and_revoke_use_bound_postgres_without_sqlite_projection(
    postgres_lifespan_approval_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, _project, owner, _agent, _agent_token = (
        postgres_lifespan_approval_context
    )
    _start_postgres_lifespan(monkeypatch)
    target = User(
        id=f"user_{uuid.uuid4().hex}",
        email=f"reviewer-{uuid.uuid4().hex}@example.test",
        display_name="Session revocation target",
        role=UserRole.REVIEWER,
        password_hash="0" * 64,
        password_salt="0" * 32,
        workspace_id=workspace.id,
        email_verified=True,
    )
    raw_token = f"session-token-{uuid.uuid4().hex}"
    target_session = SessionToken(
        user_id=target.id,
        token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        expires_at=now_utc() + timedelta(hours=1),
    )
    with connect() as connection:
        connection.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                target.id,
                workspace.id,
                target.email,
                target.email.lower(),
                target.role.value,
                target.disabled,
                target.email_verified,
                Jsonb(target.model_dump(mode="json")),
            ),
        )
        connection.execute(
            "INSERT INTO sessions(session_id,user_id,token_hash,created_at,expires_at,revoked,payload) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                target_session.id,
                target_session.user_id,
                target_session.token_hash,
                target_session.created_at,
                target_session.expires_at,
                target_session.revoked,
                Jsonb(target_session.model_dump(mode="json")),
            ),
        )

    monkeypatch.setitem(
        main.app.dependency_overrides,
        require_owner,
        lambda: owner,
    )
    before_sessions = set(store.sessions)
    before_audits = {event.id for event in store.audit_events}
    with TestClient(main.app) as client:
        repositories = client.app.state.postgres_runtime.repositories
        assert client.app.state.postgres_repositories is repositories
        assert client.app.state.postgres_repositories.auth is repositories.auth
        listed = client.get("/auth/sessions")
        assert listed.status_code == 200, listed.text
        visible = next(item for item in listed.json() if item["id"] == target_session.id)
        assert visible["user_id"] == target.id
        assert visible["user_email"] == target.email
        assert visible["active"] is True

        revoked = client.post(f"/auth/users/{target.id}/sessions/revoke")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["revoked_sessions_count"] == 1
        assert repositories.auth.authenticate_session(target_session.token_hash) is None
        with connect() as connection:
            assert connection.execute(
                "SELECT revoked FROM sessions WHERE session_id=%s", (target_session.id,)
            ).fetchone() == (True,)
            audit = connection.execute(
                "SELECT workspace_id,entity_id,details FROM audit_events "
                "WHERE event_type='auth.sessions_revoked' AND entity_id=%s",
                (target.id,),
            ).fetchone()
        assert audit is not None
        assert audit[0] == workspace.id
        assert audit[1] == target.id
        assert audit[2]["revoked_sessions_count"] == 1

    assert set(store.sessions) == before_sessions
    assert {event.id for event in store.audit_events} == before_audits


def test_lifespan_rejects_foreign_postgres_session_repository_binding(
    postgres_lifespan_approval_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _start_postgres_lifespan(monkeypatch)
    monkeypatch.setattr(main.app.state, "project_repository", object(), raising=False)

    with pytest.raises(RuntimeError, match="foreign binding"):
        with TestClient(main.app):
            pytest.fail("mixed repository binding unexpectedly started")


from __future__ import annotations

import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Iterator
from datetime import timedelta
from threading import Barrier

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.models import Invite, InviteStatus, SessionToken, User, UserRole, Workspace, now_utc
from app.postgres_auth_repository import (
    PostgresAccountDeletionError,
    PostgresAuthConflictError,
    PostgresEmailVerificationError,
    PostgresAuthRepository,
    PostgresInviteConflictError,
    PostgresRoleManagementError,
    PostgresSessionAuthorizationError,
)
from app.postgres_database import run_postgres_migrations


@pytest.fixture
def auth_repository() -> Iterator[tuple[PostgresAuthRepository, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_auth_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        yield PostgresAuthRepository(connect), connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _insert_workspace(
    conn: Connection, workspace_id: str | None = None, *, owner_user_id: str = "owner"
) -> Workspace:
    workspace = Workspace(
        id=workspace_id or f"workspace_{uuid.uuid4().hex}",
        name="Auth repository workspace",
        owner_user_id=owner_user_id,
    )
    conn.execute(
        "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
        (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
    )
    return workspace


def _insert_user(
    conn: Connection,
    workspace: Workspace,
    user_id: str,
    *,
    role: UserRole = UserRole.REVIEWER,
    disabled: bool = False,
    email_verified: bool = True,
) -> User:
    user = User(
        id=user_id,
        email=f"{user_id}@example.test",
        display_name=user_id,
        role=role,
        password_hash="test-password-hash",
        password_salt="test-password-salt",
        workspace_id=workspace.id,
        disabled=disabled,
        email_verified=email_verified,
    )
    conn.execute(
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
    return user


def _insert_session(
    conn: Connection,
    user: User,
    *,
    token: str,
    expires_at=None,
    revoked: bool = False,
) -> SessionToken:
    session = SessionToken(
        id=f"session_{uuid.uuid4().hex}",
        user_id=user.id,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=expires_at or (now_utc() + timedelta(days=10)),
        revoked=revoked,
    )
    conn.execute(
        "INSERT INTO sessions(session_id,user_id,token_hash,created_at,expires_at,revoked,payload) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (
            session.id,
            session.user_id,
            session.token_hash,
            session.created_at,
            session.expires_at,
            session.revoked,
            Jsonb(session.model_dump(mode="json")),
        ),
    )
    return session


def _registration_user(
    *, email: str, role: UserRole, workspace_id: str, user_id: str | None = None
) -> User:
    return User(
        id=user_id or f"user_{uuid.uuid4().hex}",
        email=email,
        display_name="New Account",
        role=role,
        password_hash="b" * 64,
        password_salt="a" * 32,
        workspace_id=workspace_id,
        email_verification_hash="c" * 64,
        email_verification_salt="d" * 32,
        email_verification_expires_at=now_utc() + timedelta(minutes=30),
    )


def _insert_invite(conn: Connection, workspace: Workspace, owner: User, email: str) -> Invite:
    invite = Invite(
        email=email,
        role=UserRole.REVIEWER,
        token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        expires_at=now_utc() + timedelta(days=2),
        created_by_user_id=owner.id,
        workspace_id=workspace.id,
    )
    conn.execute(
        "INSERT INTO invites(invite_id,workspace_id,email,normalized_email,role,token_hash,status,expires_at,"
        "created_by_user_id,accepted_by_user_id,created_at,updated_at,payload) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            invite.id,
            workspace.id,
            invite.email,
            invite.email.lower(),
            invite.role.value,
            invite.token_hash,
            invite.status.value,
            invite.expires_at,
            invite.created_by_user_id,
            invite.accepted_by_user_id,
            invite.created_at,
            invite.updated_at,
            Jsonb(invite.model_dump(mode="json")),
        ),
    )
    return invite


def test_user_lookup_normalizes_email_and_validates_persisted_identity(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace = _insert_workspace(conn)
        user = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)

    assert repository.get_user_by_email("  OWNER@EXAMPLE.TEST ") == user
    assert repository.get_user_by_email("missing@example.test") is None


def test_session_creation_and_authentication_use_durable_active_account(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace = _insert_workspace(conn)
        user = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)
    session = SessionToken(
        user_id=user.id,
        token_hash=hashlib.sha256(b"opaque-bearer-token").hexdigest(),
        expires_at=now_utc() + timedelta(days=30),
    )

    created_user = repository.create_session(session)
    authenticated = repository.authenticate_session(session.token_hash)

    assert created_user == user
    assert authenticated is not None
    assert authenticated.user == user
    assert authenticated.session == session


@pytest.mark.parametrize(
    ("disabled", "email_verified"),
    [(True, True), (False, False)],
)
def test_session_creation_fails_closed_for_inactive_accounts(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
    disabled: bool,
    email_verified: bool,
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace = _insert_workspace(conn)
        user = _insert_user(
            conn, workspace, "reviewer", disabled=disabled, email_verified=email_verified
        )
    session = SessionToken(
        user_id=user.id,
        token_hash=hashlib.sha256(b"inactive-account-token").hexdigest(),
        expires_at=now_utc() + timedelta(days=1),
    )

    with pytest.raises(PostgresSessionAuthorizationError):
        repository.create_session(session)
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_session_authentication_rejects_revoked_expired_and_unverified_sessions(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    current = now_utc()
    with connect() as conn:
        workspace = _insert_workspace(conn)
        verified = _insert_user(conn, workspace, "verified")
        unverified = _insert_user(conn, workspace, "unverified", email_verified=False)
        revoked = _insert_session(conn, verified, token="revoked", revoked=True)
        expired = _insert_session(
            conn, verified, token="expired", expires_at=current - timedelta(seconds=1)
        )
        unverified_session = _insert_session(conn, unverified, token="unverified")

    assert repository.authenticate_session(revoked.token_hash, now=current) is None
    assert repository.authenticate_session(expired.token_hash, now=current) is None
    assert repository.authenticate_session(unverified_session.token_hash, now=current) is None
    assert repository.authenticate_session("not-a-sha256-token-hash", now=current) is None


def test_owner_session_listing_and_revocation_are_workspace_scoped_and_audited(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    current = now_utc()
    with connect() as conn:
        workspace = _insert_workspace(conn)
        owner = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)
        target = _insert_user(conn, workspace, "reviewer")
        _insert_session(conn, owner, token="owner-session")
        active = _insert_session(conn, target, token="active-session")
        _insert_session(
            conn,
            target,
            token="expired-session",
            expires_at=current - timedelta(seconds=1),
        )
    sessions = repository.list_sessions(owner.id)
    result = repository.revoke_user_sessions(
        actor_user_id=owner.id, target_user_id=target.id, now=current
    )

    session_ids = {session.id for session, _ in sessions}
    assert active.id in session_ids
    assert len(sessions) == 3
    assert result.target == target
    assert result.revoked_count == 1
    assert result.audit_event.workspace_id == workspace.id
    assert result.audit_event.details["revoked_sessions_count"] == 1
    with connect() as conn:
        row = conn.execute(
            "SELECT revoked,payload FROM sessions WHERE session_id=%s", (active.id,)
        ).fetchone()
        event_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_type='auth.sessions_revoked' "
            "AND entity_id=%s",
            (target.id,),
        ).fetchone()[0]
    assert row[0] is True
    assert SessionToken.model_validate(row[1]).revoked is True
    assert event_count == 1


def test_session_revocation_rejects_self_cross_workspace_and_non_owner(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace_a = _insert_workspace(conn)
        owner_a = _insert_user(conn, workspace_a, "owner-a", role=UserRole.OWNER)
        unverified_owner = _insert_user(
            conn, workspace_a, "unverified-owner", role=UserRole.OWNER, email_verified=False
        )
        reviewer_a = _insert_user(conn, workspace_a, "reviewer-a")
        workspace_b = _insert_workspace(conn, owner_user_id="owner-b")
        owner_b = _insert_user(conn, workspace_b, "owner-b", role=UserRole.OWNER)

    with pytest.raises(PostgresSessionAuthorizationError, match="own sessions"):
        repository.revoke_user_sessions(actor_user_id=owner_a.id, target_user_id=owner_a.id)
    with pytest.raises(PostgresSessionAuthorizationError, match="workspace"):
        repository.revoke_user_sessions(actor_user_id=owner_a.id, target_user_id=owner_b.id)
    with pytest.raises(PostgresSessionAuthorizationError, match="owner"):
        repository.list_sessions(reviewer_a.id)
    with pytest.raises(PostgresSessionAuthorizationError, match="active workspace owner"):
        repository.list_sessions(unverified_owner.id)
    with pytest.raises(PostgresSessionAuthorizationError, match="outside the actor workspace"):
        repository.revoke_user_sessions(
            actor_user_id=unverified_owner.id, target_user_id=reviewer_a.id
        )


def test_session_revocation_rolls_back_if_audit_insert_fails(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace = _insert_workspace(conn)
        owner = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)
        target = _insert_user(conn, workspace, "reviewer")
        session = _insert_session(conn, target, token="rollback-session")
        conn.execute(
            "CREATE FUNCTION reject_session_revoke_audit() RETURNS trigger AS $$ "
            "BEGIN IF NEW.event_type='auth.sessions_revoked' THEN "
            "RAISE EXCEPTION 'audit unavailable'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        conn.execute(
            "CREATE TRIGGER reject_session_revoke_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_session_revoke_audit()"
        )

    with pytest.raises(psycopg.Error, match="audit unavailable"):
        repository.revoke_user_sessions(actor_user_id=owner.id, target_user_id=target.id)

    with connect() as conn:
        row = conn.execute(
            "SELECT revoked,payload FROM sessions WHERE session_id=%s", (session.id,)
        ).fetchone()
        event_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_type='auth.sessions_revoked'"
        ).fetchone()[0]
    assert row[0] is False
    assert SessionToken.model_validate(row[1]).revoked is False
    assert event_count == 0


def test_create_session_rejects_malformed_token_hash(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, _ = auth_repository
    session = SessionToken(user_id="user", token_hash="plaintext", expires_at=now_utc() + timedelta(days=1))
    with pytest.raises(ValueError, match="SHA-256 digest"):
        repository.create_session(session)


def test_owner_bootstrap_atomically_creates_workspace_account_and_verification_email(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    user_id = f"owner_{uuid.uuid4().hex}"
    user = _registration_user(
        email="Owner@Example.Test", role=UserRole.OWNER, workspace_id=f"workspace_{uuid.uuid4().hex}", user_id=user_id
    )
    workspace = Workspace(id=user.workspace_id, name="Owner workspace", owner_user_id=user.id)

    created = repository.bootstrap_owner(
        user=user,
        workspace=workspace,
        outbox_id=f"email_{uuid.uuid4().hex}",
        encrypted_code=b"x" * 29,
        email_created_at=now_utc(),
    )

    assert created == user
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 1
        row = conn.execute("SELECT recipient,status,encrypted_code FROM auth_email_outbox").fetchone()
    assert row == ("Owner@Example.Test", "PENDING", b"x" * 29)
    other = _registration_user(
        email="other@example.test", role=UserRole.OWNER, workspace_id=f"workspace_{uuid.uuid4().hex}"
    )
    repository.bootstrap_owner(
        user=other,
        workspace=Workspace(id=other.workspace_id, name="Other", owner_user_id=other.id),
        outbox_id=f"email_{uuid.uuid4().hex}",
        encrypted_code=b"y" * 29,
        email_created_at=now_utc(),
    )
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 2
        assert conn.execute("SELECT count(DISTINCT workspace_id) FROM users").fetchone()[0] == 2


def test_concurrent_owner_bootstrap_creates_separate_account_workspaces(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository

    def attempt(index: int) -> str:
        user = _registration_user(
            email=f"owner-{index}@example.test",
            role=UserRole.OWNER,
            workspace_id=f"workspace_{uuid.uuid4().hex}",
        )
        workspace = Workspace(id=user.workspace_id, name=f"Workspace {index}", owner_user_id=user.id)
        try:
            repository.bootstrap_owner(
                user=user,
                workspace=workspace,
                outbox_id=f"email_{uuid.uuid4().hex}",
                encrypted_code=bytes([index + 1]) * 29,
                email_created_at=now_utc(),
            )
            return "created"
        except PostgresAuthConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (0, 1)))
    assert sorted(results) == ["created", "created"]
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 2
        assert conn.execute("SELECT count(DISTINCT workspace_id) FROM users").fetchone()[0] == 2


def test_concurrent_owner_bootstrap_same_email_returns_one_conflict_without_partial_state(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    barrier = Barrier(2)

    def attempt(index: int) -> str:
        user = _registration_user(
            email="same-owner@example.test",
            role=UserRole.OWNER,
            workspace_id=f"workspace_{uuid.uuid4().hex}",
        )
        workspace = Workspace(id=user.workspace_id, name=f"Workspace {index}", owner_user_id=user.id)
        barrier.wait(timeout=5)
        try:
            repository.bootstrap_owner(
                user=user,
                workspace=workspace,
                outbox_id=f"email_{uuid.uuid4().hex}",
                encrypted_code=bytes([index + 1]) * 29,
                email_created_at=now_utc(),
            )
            return "created"
        except PostgresAuthConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (0, 1)))
    assert sorted(results) == ["conflict", "created"]
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM users WHERE normalized_email='same-owner@example.test'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM auth_email_outbox").fetchone()[0] == 1


def test_invite_registration_maps_existing_account_unique_conflict_and_rolls_back(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace = _insert_workspace(conn)
        owner = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)
        _insert_user(conn, workspace, "existing-reviewer")
        invite = _insert_invite(conn, workspace, owner, "existing-reviewer@example.test")
    attempted_user = _registration_user(
        email="existing-reviewer@example.test",
        role=UserRole.REVIEWER,
        workspace_id=workspace.id,
    )

    with pytest.raises(PostgresAuthConflictError, match="existing account state"):
        repository.register_with_invite(
            user=attempted_user,
            token_hash=invite.token_hash,
            outbox_id=f"email_{uuid.uuid4().hex}",
            encrypted_code=b"x" * 29,
            email_created_at=now_utc(),
        )

    with connect() as conn:
        assert conn.execute(
            "SELECT status FROM invites WHERE invite_id=%s", (invite.id,)
        ).fetchone()[0] == "PENDING"
        assert conn.execute(
            "SELECT count(*) FROM users WHERE user_id=%s", (attempted_user.id,)
        ).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM auth_email_outbox").fetchone()[0] == 0


def test_invite_registration_consumes_matching_invite_and_audits_atomically(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace = _insert_workspace(conn)
        owner = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)
        invite = _insert_invite(conn, workspace, owner, "reviewer@example.test")
    user = _registration_user(
        email="REVIEWER@example.test", role=UserRole.REVIEWER, workspace_id=workspace.id
    )

    created, accepted, event = repository.register_with_invite(
        user=user,
        token_hash=invite.token_hash,
        outbox_id=f"email_{uuid.uuid4().hex}",
        encrypted_code=b"v" * 29,
        email_created_at=now_utc(),
    )

    assert created == user
    assert accepted.status == InviteStatus.ACCEPTED
    assert accepted.accepted_by_user_id == user.id
    assert event.event_type == "auth.invite_accepted"
    assert event.workspace_id == workspace.id
    with connect() as conn:
        invite_payload = conn.execute(
            "SELECT payload FROM invites WHERE invite_id=%s", (invite.id,)
        ).fetchone()[0]
        assert Invite.model_validate(invite_payload).status == InviteStatus.ACCEPTED
        assert conn.execute("SELECT count(*) FROM users WHERE user_id=%s", (user.id,)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM auth_email_outbox WHERE recipient=%s AND status='PENDING'",
            (user.email,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type='invite' AND entity_id=%s",
            (invite.id,),
        ).fetchone()[0] == 1
    with pytest.raises(PostgresAuthConflictError, match="not pending"):
        repository.register_with_invite(
            user=_registration_user(
                email=user.email, role=UserRole.REVIEWER, workspace_id=workspace.id
            ),
            token_hash=invite.token_hash,
            outbox_id=f"email_{uuid.uuid4().hex}",
            encrypted_code=b"w" * 29,
            email_created_at=now_utc(),
        )


def test_invite_registration_rejects_mismatched_email_without_partial_rows(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace = _insert_workspace(conn)
        owner = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)
        invite = _insert_invite(conn, workspace, owner, "reviewer@example.test")
    user = _registration_user(
        email="attacker@example.test", role=UserRole.REVIEWER, workspace_id=workspace.id
    )

    with pytest.raises(PostgresAuthConflictError, match="does not match"):
        repository.register_with_invite(
            user=user,
            token_hash=invite.token_hash,
            outbox_id=f"email_{uuid.uuid4().hex}",
            encrypted_code=b"z" * 29,
            email_created_at=now_utc(),
        )
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM users WHERE user_id=%s", (user.id,)).fetchone()[0] == 0
        assert conn.execute("SELECT status FROM invites WHERE invite_id=%s", (invite.id,)).fetchone()[0] == "PENDING"
        assert conn.execute("SELECT count(*) FROM auth_email_outbox").fetchone()[0] == 0


def test_verification_rotation_updates_user_and_invalidates_older_sending_email(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    user = _registration_user(
        email="rotate@example.test",
        role=UserRole.OWNER,
        workspace_id=f"workspace_{uuid.uuid4().hex}",
    )
    original_outbox_id = f"email_{uuid.uuid4().hex}"
    repository.bootstrap_owner(
        user=user,
        workspace=Workspace(id=user.workspace_id, name="Rotate", owner_user_id=user.id),
        outbox_id=original_outbox_id,
        encrypted_code=b"o" * 29,
        email_created_at=now_utc(),
    )
    with connect() as conn:
        conn.execute(
            "UPDATE auth_email_outbox SET status='SENDING',lease_expires_at=%s WHERE outbox_id=%s",
            (now_utc() + timedelta(minutes=2), original_outbox_id),
        )
    sent_at = now_utc()
    updated = repository.rotate_email_verification(
        email=user.email,
        verification_hash="b" * 64,
        verification_salt="e" * 32,
        expires_at=sent_at + timedelta(minutes=30),
        sent_at=sent_at,
        outbox_id=f"email_{uuid.uuid4().hex}",
        encrypted_code=b"n" * 29,
        email_created_at=sent_at,
    )

    assert updated.email_verification_hash == "b" * 64
    assert updated.email_verification_salt == "e" * 32
    with connect() as conn:
        old = conn.execute(
            "SELECT status,encrypted_code,lease_expires_at FROM auth_email_outbox WHERE outbox_id=%s",
            (original_outbox_id,),
        ).fetchone()
        new = conn.execute(
            "SELECT status,encrypted_code FROM auth_email_outbox WHERE recipient=%s "
            "ORDER BY created_at DESC LIMIT 1",
            (user.email,),
        ).fetchone()
    assert old == ("DEAD", b"", None)
    assert new == ("PENDING", b"n" * 29)


def test_verification_success_consumes_code_and_invalidates_pending_email(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    user = _registration_user(
        email="verify@example.test",
        role=UserRole.OWNER,
        workspace_id=f"workspace_{uuid.uuid4().hex}",
    )
    outbox_id = f"email_{uuid.uuid4().hex}"
    repository.bootstrap_owner(
        user=user,
        workspace=Workspace(id=user.workspace_id, name="Verify", owner_user_id=user.id),
        outbox_id=outbox_id,
        encrypted_code=b"c" * 29,
        email_created_at=now_utc(),
    )
    verified = repository.verify_email(
        email=user.email,
        candidate_hash=user.email_verification_hash,
        now=now_utc(),
    )
    assert verified.email_verified is True
    assert verified.email_verification_hash is None
    assert verified.email_verification_salt is None
    assert verified.email_verification_expires_at is None
    assert repository.verify_email(
        email=user.email,
        candidate_hash="a" * 64,
        now=now_utc(),
    ) == verified
    with connect() as conn:
        outbox = conn.execute(
            "SELECT status,encrypted_code,last_error FROM auth_email_outbox WHERE outbox_id=%s",
            (outbox_id,),
        ).fetchone()
    assert outbox == ("DEAD", b"", "VERIFICATION_ALREADY_COMPLETE")


@pytest.mark.parametrize(
    ("state", "candidate", "reason"),
    [
        ("active", "e" * 64, "invalid"),
        ("expired", "f" * 64, "expired"),
        ("missing", "f" * 64, "no_active_code"),
    ],
)
def test_verification_failures_do_not_mutate_account_or_outbox(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
    state: str,
    candidate: str,
    reason: str,
) -> None:
    repository, connect = auth_repository
    user = _registration_user(
        email=f"{state}@example.test",
        role=UserRole.OWNER,
        workspace_id=f"workspace_{uuid.uuid4().hex}",
    )
    original_outbox_id = f"email_{uuid.uuid4().hex}"
    repository.bootstrap_owner(
        user=user,
        workspace=Workspace(id=user.workspace_id, name=state, owner_user_id=user.id),
        outbox_id=original_outbox_id,
        encrypted_code=b"c" * 29,
        email_created_at=now_utc(),
    )
    if state == "expired":
        sent_at = now_utc() - timedelta(hours=1)
        repository.rotate_email_verification(
            email=user.email,
            verification_hash="f" * 64,
            verification_salt="e" * 32,
            expires_at=now_utc() - timedelta(minutes=1),
            sent_at=sent_at,
            outbox_id=f"email_{uuid.uuid4().hex}",
            encrypted_code=b"e" * 29,
            email_created_at=sent_at,
        )
    elif state == "missing":
        with connect() as conn:
            row = conn.execute(
                "SELECT payload FROM users WHERE user_id=%s", (user.id,)
            ).fetchone()[0]
            user_without_code = User.model_validate(row).model_copy(
                update={
                    "email_verification_hash": None,
                    "email_verification_salt": None,
                    "email_verification_expires_at": None,
                }
            )
            conn.execute(
                "UPDATE users SET payload=%s WHERE user_id=%s",
                (Jsonb(user_without_code.model_dump(mode="json")), user.id),
            )

    with pytest.raises(PostgresEmailVerificationError) as error:
        repository.verify_email(email=user.email, candidate_hash=candidate, now=now_utc())
    assert error.value.reason == reason
    with connect() as conn:
        row = conn.execute(
            "SELECT email_verified,payload FROM users WHERE user_id=%s", (user.id,)
        ).fetchone()
        outbox_count = conn.execute(
            "SELECT count(*) FROM auth_email_outbox WHERE recipient=%s AND status='PENDING'",
            (user.email,),
        ).fetchone()[0]
    assert row[0] is False
    assert User.model_validate(row[1]).email_verified is False
    assert outbox_count >= 1


def test_verification_rotation_rolls_back_if_outbox_insert_fails(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    user = _registration_user(
        email="rollback-verify@example.test",
        role=UserRole.OWNER,
        workspace_id=f"workspace_{uuid.uuid4().hex}",
    )
    original_hash = user.email_verification_hash
    repository.bootstrap_owner(
        user=user,
        workspace=Workspace(id=user.workspace_id, name="Rollback", owner_user_id=user.id),
        outbox_id=f"email_{uuid.uuid4().hex}",
        encrypted_code=b"o" * 29,
        email_created_at=now_utc(),
    )
    with connect() as conn:
        conn.execute(
            "CREATE FUNCTION reject_verification_outbox_insert() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'outbox unavailable'; END; $$ LANGUAGE plpgsql"
        )
        conn.execute(
            "CREATE TRIGGER reject_verification_outbox_insert BEFORE INSERT ON auth_email_outbox "
            "FOR EACH ROW EXECUTE FUNCTION reject_verification_outbox_insert()"
        )

    with pytest.raises(psycopg.Error, match="outbox unavailable"):
        repository.rotate_email_verification(
            email=user.email,
            verification_hash="d" * 64,
            verification_salt="e" * 32,
            expires_at=now_utc() + timedelta(minutes=30),
            sent_at=now_utc(),
            outbox_id=f"email_{uuid.uuid4().hex}",
            encrypted_code=b"n" * 29,
            email_created_at=now_utc(),
        )
    persisted = repository.get_user_by_email(user.email)
    assert persisted is not None
    assert persisted.email_verification_hash == original_hash
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM auth_email_outbox WHERE status='PENDING' AND recipient=%s",
            (user.email,),
        ).fetchone()[0] == 1


def _new_invite(owner: User, *, email: str, expires_at=None) -> Invite:
    return Invite(
        email=email.lower().strip(),
        role=UserRole.REVIEWER,
        token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        expires_at=expires_at or (now_utc() + timedelta(days=3)),
        created_by_user_id=owner.id,
        workspace_id=owner.workspace_id,
    )


def test_invite_administration_is_owner_and_workspace_scoped(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace_a = _insert_workspace(conn, owner_user_id="owner-a")
        owner_a = _insert_user(conn, workspace_a, "owner-a", role=UserRole.OWNER)
        workspace_b = _insert_workspace(conn, owner_user_id="owner-b")
        owner_b = _insert_user(conn, workspace_b, "owner-b", role=UserRole.OWNER)
    invite = _new_invite(owner_a, email="reviewer@example.test")

    created, create_event = repository.create_invite(actor_user_id=owner_a.id, invite=invite)

    assert created == invite
    assert create_event.event_type == "auth.invite_created"
    assert repository.list_invites(actor_user_id=owner_a.id) == [invite]
    assert repository.list_invites(actor_user_id=owner_b.id) == []
    with pytest.raises(PostgresInviteConflictError, match="outside the owner account"):
        repository.create_invite(
            actor_user_id=owner_b.id,
            invite=invite.model_copy(
                update={
                    "id": f"invite_{uuid.uuid4().hex}",
                    "token_hash": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                    "created_by_user_id": owner_b.id,
                }
            ),
        )
    with pytest.raises(PostgresInviteConflictError, match="unavailable"):
        repository.revoke_invite(actor_user_id=owner_b.id, invite_id=invite.id)
    assert repository.list_invites(actor_user_id=owner_a.id)[0].status == InviteStatus.PENDING


def test_invite_administration_rejects_duplicate_pending_invite(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace = _insert_workspace(conn)
        owner = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)
    first = _new_invite(owner, email="reviewer@example.test")
    repository.create_invite(actor_user_id=owner.id, invite=first)

    with pytest.raises(PostgresInviteConflictError, match="Pending invite"):
        repository.create_invite(
            actor_user_id=owner.id,
            invite=_new_invite(owner, email="REVIEWER@example.test"),
        )
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM invites").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM audit_events WHERE event_type='auth.invite_created'").fetchone()[0] == 1


def test_listing_invites_persists_expiration_for_own_workspace_only(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    current = now_utc()
    with connect() as conn:
        workspace_a = _insert_workspace(conn, owner_user_id="owner-a")
        owner_a = _insert_user(conn, workspace_a, "owner-a", role=UserRole.OWNER)
        workspace_b = _insert_workspace(conn, owner_user_id="owner-b")
        owner_b = _insert_user(conn, workspace_b, "owner-b", role=UserRole.OWNER)
        expired = _insert_invite(
            conn,
            workspace_a,
            owner_a,
            "expired@example.test",
        ).model_copy(update={"expires_at": current - timedelta(seconds=1)})
        conn.execute(
            "UPDATE invites SET expires_at=%s,payload=%s WHERE invite_id=%s",
            (expired.expires_at, Jsonb(expired.model_dump(mode="json")), expired.id),
        )
        other = _insert_invite(
            conn,
            workspace_b,
            owner_b,
            "other@example.test",
        ).model_copy(update={"expires_at": current - timedelta(seconds=1)})
        conn.execute(
            "UPDATE invites SET expires_at=%s,payload=%s WHERE invite_id=%s",
            (other.expires_at, Jsonb(other.model_dump(mode="json")), other.id),
        )

    listed = repository.list_invites(actor_user_id=owner_a.id, now=current)
    assert len(listed) == 1
    assert listed[0].id == expired.id
    assert listed[0].status == InviteStatus.EXPIRED
    with connect() as conn:
        row = conn.execute(
            "SELECT status,payload FROM invites WHERE invite_id=%s", (expired.id,)
        ).fetchone()
        audit_rows = conn.execute(
            "SELECT event_type,actor,workspace_id,entity_type,entity_id,details "
            "FROM audit_events WHERE event_type='auth.invite_expired' ORDER BY entity_id"
        ).fetchall()
    assert row[0] == "EXPIRED"
    assert Invite.model_validate(row[1]).status == InviteStatus.EXPIRED
    assert audit_rows == [
        (
            "auth.invite_expired",
            owner_a.email,
            workspace_a.id,
            "invite",
            expired.id,
            {
                "invite_id": expired.id,
                "workspace_id": workspace_a.id,
                "email": expired.email,
                "role": expired.role.value,
                "status": InviteStatus.EXPIRED.value,
                "expires_at": expired.expires_at.isoformat(),
            },
        )
    ]
    with connect() as conn:
        other_status, other_payload = conn.execute(
            "SELECT status,payload FROM invites WHERE invite_id=%s", (other.id,)
        ).fetchone()
        assert other_status == InviteStatus.PENDING.value
        assert Invite.model_validate(other_payload).status == InviteStatus.PENDING
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_type='auth.invite_expired' "
            "AND workspace_id=%s",
            (workspace_b.id,),
        ).fetchone()[0] == 0


def test_invite_expiration_rolls_back_if_audit_write_fails(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    current = now_utc()
    with connect() as conn:
        workspace = _insert_workspace(conn)
        owner = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)
        expired = _insert_invite(
            conn,
            workspace,
            owner,
            "expired@example.test",
        ).model_copy(update={"expires_at": current - timedelta(seconds=1)})
        conn.execute(
            "UPDATE invites SET expires_at=%s,payload=%s WHERE invite_id=%s",
            (expired.expires_at, Jsonb(expired.model_dump(mode="json")), expired.id),
        )
        before_payload = conn.execute(
            "SELECT payload FROM invites WHERE invite_id=%s", (expired.id,)
        ).fetchone()[0]
        conn.execute(
            "CREATE FUNCTION reject_invite_expired_audit() RETURNS trigger AS $$ "
            "BEGIN IF NEW.event_type='auth.invite_expired' THEN "
            "RAISE EXCEPTION 'invite expiry audit unavailable'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        conn.execute(
            "CREATE TRIGGER reject_invite_expired_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_invite_expired_audit()"
        )

    with pytest.raises(psycopg.Error, match="invite expiry audit unavailable"):
        repository.list_invites(actor_user_id=owner.id, now=current)

    with connect() as conn:
        status_value, payload = conn.execute(
            "SELECT status,payload FROM invites WHERE invite_id=%s", (expired.id,)
        ).fetchone()
        assert status_value == InviteStatus.PENDING.value
        assert payload == before_payload
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_type='auth.invite_expired'"
        ).fetchone()[0] == 0


def test_invite_revoke_is_audited_and_idempotent_but_rejects_accepted_invites(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace = _insert_workspace(conn)
        owner = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)
        accepted = _insert_invite(conn, workspace, owner, "accepted@example.test")
        reviewer = _insert_user(conn, workspace, "reviewer")
        accepted = accepted.model_copy(
            update={"status": InviteStatus.ACCEPTED, "accepted_by_user_id": reviewer.id}
        )
        conn.execute(
            "UPDATE invites SET status='ACCEPTED',accepted_by_user_id=%s,payload=%s WHERE invite_id=%s",
            (
                reviewer.id,
                Jsonb(accepted.model_dump(mode="json")),
                accepted.id,
            ),
        )
        pending = _insert_invite(conn, workspace, owner, "pending@example.test")

    revoked, event = repository.revoke_invite(actor_user_id=owner.id, invite_id=pending.id)
    assert revoked.status == InviteStatus.REVOKED
    assert event is not None and event.event_type == "auth.invite_revoked"
    same, no_event = repository.revoke_invite(actor_user_id=owner.id, invite_id=pending.id)
    assert same == revoked
    assert no_event is None
    with pytest.raises(PostgresInviteConflictError, match="Accepted invites"):
        repository.revoke_invite(actor_user_id=owner.id, invite_id=accepted.id)
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_type='auth.invite_revoked' AND entity_id=%s",
            (pending.id,),
        ).fetchone()[0] == 1


def test_invite_creation_rolls_back_if_audit_write_fails(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace = _insert_workspace(conn)
        owner = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)
        conn.execute(
            "CREATE FUNCTION reject_invite_create_audit() RETURNS trigger AS $$ "
            "BEGIN IF NEW.event_type='auth.invite_created' THEN "
            "RAISE EXCEPTION 'invite audit unavailable'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        conn.execute(
            "CREATE TRIGGER reject_invite_create_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_invite_create_audit()"
        )
    invite = _new_invite(owner, email="rollback@example.test")

    with pytest.raises(psycopg.Error, match="invite audit unavailable"):
        repository.create_invite(actor_user_id=owner.id, invite=invite)

    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM invites WHERE invite_id=%s", (invite.id,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_type='auth.invite_created'"
        ).fetchone()[0] == 0


def test_workspace_user_listing_and_role_change_are_scoped_and_audited(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace_a = _insert_workspace(conn, owner_user_id="owner-a")
        owner_a = _insert_user(conn, workspace_a, "owner-a", role=UserRole.OWNER)
        reviewer_a = _insert_user(conn, workspace_a, "reviewer-a")
        workspace_b = _insert_workspace(conn, owner_user_id="owner-b")
        owner_b = _insert_user(conn, workspace_b, "owner-b", role=UserRole.OWNER)

    assert {user.id for user in repository.list_workspace_users(actor_user_id=owner_a.id)} == {
        owner_a.id,
        reviewer_a.id,
    }
    result = repository.update_user_role(
        actor_user_id=owner_a.id,
        target_user_id=reviewer_a.id,
        role=UserRole.MAINTAINER,
    )
    assert result.previous_role == UserRole.REVIEWER
    assert result.target.role == UserRole.MAINTAINER
    assert result.audit_event is not None
    with pytest.raises(PostgresRoleManagementError, match="outside the owner workspace"):
        repository.update_user_role(
            actor_user_id=owner_a.id,
            target_user_id=owner_b.id,
            role=UserRole.REVIEWER,
        )
    with pytest.raises(PostgresRoleManagementError, match="active workspace owner"):
        repository.list_workspace_users(actor_user_id=reviewer_a.id)
    with connect() as conn:
        row = conn.execute(
            "SELECT role,payload FROM users WHERE user_id=%s", (reviewer_a.id,)
        ).fetchone()
        events = conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_type='auth.role_updated'"
        ).fetchone()[0]
    assert row[0] == "MAINTAINER"
    assert User.model_validate(row[1]).role == UserRole.MAINTAINER
    assert events == 1


def test_last_active_owner_cannot_be_demoted_and_same_role_is_noop(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace = _insert_workspace(conn)
        owner = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)

    unchanged = repository.update_user_role(
        actor_user_id=owner.id, target_user_id=owner.id, role=UserRole.OWNER
    )
    assert unchanged.target == owner
    assert unchanged.audit_event is None
    with pytest.raises(PostgresRoleManagementError, match="last active OWNER"):
        repository.update_user_role(
            actor_user_id=owner.id, target_user_id=owner.id, role=UserRole.REVIEWER
        )
    assert repository.list_workspace_users(actor_user_id=owner.id)[0].role == UserRole.OWNER


def test_concurrent_owner_demotion_preserves_one_active_owner(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace = _insert_workspace(conn, owner_user_id="owner-a")
        owner_a = _insert_user(conn, workspace, "owner-a", role=UserRole.OWNER)
        owner_b = _insert_user(conn, workspace, "owner-b", role=UserRole.OWNER)
    barrier = Barrier(2)

    def demote(user_id: str) -> str:
        barrier.wait(timeout=10)
        try:
            repository.update_user_role(
                actor_user_id=user_id,
                target_user_id=user_id,
                role=UserRole.REVIEWER,
            )
            return "demoted"
        except PostgresRoleManagementError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(demote, (owner_a.id, owner_b.id)))
    assert sorted(outcomes) == ["blocked", "demoted"]
    with connect() as conn:
        active_owners = conn.execute(
            "SELECT count(*) FROM users WHERE workspace_id=%s AND role='OWNER' AND disabled=FALSE",
            (workspace.id,),
        ).fetchone()[0]
    assert active_owners == 1


def test_role_update_rolls_back_if_audit_write_fails(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace = _insert_workspace(conn)
        owner = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)
        reviewer = _insert_user(conn, workspace, "reviewer")
        conn.execute(
            "CREATE FUNCTION reject_role_update_audit() RETURNS trigger AS $$ "
            "BEGIN IF NEW.event_type='auth.role_updated' THEN "
            "RAISE EXCEPTION 'role audit unavailable'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        conn.execute(
            "CREATE TRIGGER reject_role_update_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_role_update_audit()"
        )

    with pytest.raises(psycopg.Error, match="role audit unavailable"):
        repository.update_user_role(
            actor_user_id=owner.id,
            target_user_id=reviewer.id,
            role=UserRole.MAINTAINER,
        )
    persisted = repository.get_user_by_email(reviewer.email)
    assert persisted is not None and persisted.role == UserRole.REVIEWER


def test_account_deletion_disables_user_revokes_sessions_and_audits_atomically(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    current = now_utc()
    with connect() as conn:
        workspace = _insert_workspace(conn)
        user = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)
        session = _insert_session(conn, user, token="deletion-session")

    result = repository.request_account_deletion(
        user_id=user.id, reason_provided=True, now=current
    )

    assert result.target.disabled is True
    assert result.revoked_sessions_count == 1
    assert result.audit_event.event_type == "auth.account_deletion_requested"
    assert result.audit_event.details["reason_provided"] is True
    assert repository.authenticate_session(session.token_hash, now=current) is None
    with connect() as conn:
        session_row = conn.execute(
            "SELECT revoked,payload FROM sessions WHERE session_id=%s", (session.id,)
        ).fetchone()
        user_row = conn.execute(
            "SELECT disabled,payload FROM users WHERE user_id=%s", (user.id,)
        ).fetchone()
    assert session_row[0] is True and SessionToken.model_validate(session_row[1]).revoked is True
    assert user_row[0] is True and User.model_validate(user_row[1]).disabled is True
    with pytest.raises(PostgresAccountDeletionError, match="already disabled"):
        repository.request_account_deletion(user_id=user.id, reason_provided=False)


def test_account_deletion_rolls_back_if_audit_write_fails(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    with connect() as conn:
        workspace = _insert_workspace(conn)
        user = _insert_user(conn, workspace, "owner", role=UserRole.OWNER)
        session = _insert_session(conn, user, token="deletion-rollback-session")
        conn.execute(
            "CREATE FUNCTION reject_account_deletion_audit() RETURNS trigger AS $$ "
            "BEGIN IF NEW.event_type='auth.account_deletion_requested' THEN "
            "RAISE EXCEPTION 'deletion audit unavailable'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        conn.execute(
            "CREATE TRIGGER reject_account_deletion_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_account_deletion_audit()"
        )

    with pytest.raises(psycopg.Error, match="deletion audit unavailable"):
        repository.request_account_deletion(user_id=user.id, reason_provided=True)
    with connect() as conn:
        user_row = conn.execute(
            "SELECT disabled,payload FROM users WHERE user_id=%s", (user.id,)
        ).fetchone()
        session_row = conn.execute(
            "SELECT revoked,payload FROM sessions WHERE session_id=%s", (session.id,)
        ).fetchone()
        audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_type='auth.account_deletion_requested'"
        ).fetchone()[0]
    assert user_row[0] is False and User.model_validate(user_row[1]).disabled is False
    assert session_row[0] is False and SessionToken.model_validate(session_row[1]).revoked is False
    assert audit_count == 0

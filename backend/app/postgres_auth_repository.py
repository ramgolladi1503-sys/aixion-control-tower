from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import ParamSpec, TypeVar

from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from .models import AuditEvent, Invite, InviteStatus, SessionToken, User, UserRole, Workspace, now_utc

_TOKEN_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SECRET_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SECRET_SALT_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


class PostgresAuthIntegrityError(RuntimeError):
    """A normalized auth row disagrees with its persisted model payload."""


class PostgresSessionAuthorizationError(PermissionError):
    """A session management request violates account/workspace authority."""


class PostgresAuthConflictError(RuntimeError):
    """A registration operation conflicts with current account state."""


class PostgresInviteConflictError(RuntimeError):
    """An invite operation conflicts with invite or workspace authority."""


class PostgresRoleManagementError(PermissionError):
    """A user-role operation violates owner or workspace authority."""


class PostgresAccountDeletionError(RuntimeError):
    """An account-deletion request conflicts with account state."""


class PostgresEmailVerificationError(ValueError):
    """A verification attempt failed with a stable, non-secret reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _translate_unique_conflict(message: str):
    """Map expected uniqueness races to stable repository conflicts."""
    def decorate(method: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(method)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                return method(*args, **kwargs)
            except UniqueViolation as exc:
                raise PostgresAuthConflictError(message) from exc

        return wrapped

    return decorate


@dataclass(frozen=True)
class AuthenticatedSession:
    user: User
    session: SessionToken


@dataclass(frozen=True)
class SessionRevocationResult:
    target: User
    revoked_count: int
    audit_event: AuditEvent


@dataclass(frozen=True)
class RoleUpdateResult:
    target: User
    previous_role: UserRole
    audit_event: AuditEvent | None


@dataclass(frozen=True)
class AccountDeletionResult:
    target: User
    revoked_sessions_count: int
    audit_event: AuditEvent


class PostgresAuthRepository:
    """Durable account/session operations for a PostgreSQL-backed API.

    This repository owns account registration, verification, and session state.
    Route and runtime selection remain separate until every auth operation is
    backed by PostgreSQL.
    """

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    @_translate_unique_conflict("Account registration conflicts with existing state")
    def bootstrap_owner(
        self,
        *,
        user: User,
        workspace: Workspace,
        outbox_id: str,
        encrypted_code: bytes,
        email_created_at: datetime,
    ) -> User:
        """Create one account workspace, its owner, and verification email atomically."""
        self._validate_registration(user, outbox_id, encrypted_code)
        if (
            user.role != UserRole.OWNER
            or user.workspace_id != workspace.id
            or workspace.owner_user_id != user.id
            or user.email_verified
        ):
            raise ValueError("Owner bootstrap must bind an unverified owner to its own workspace")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            self._insert_user(conn, user)
            self._insert_verification_email(
                conn,
                outbox_id=outbox_id,
                recipient=user.email,
                encrypted_code=encrypted_code,
                created_at=email_created_at,
            )
        return user

    @_translate_unique_conflict("Invite registration conflicts with existing account state")
    def register_with_invite(
        self,
        *,
        user: User,
        token_hash: str,
        outbox_id: str,
        encrypted_code: bytes,
        email_created_at: datetime,
        now: datetime | None = None,
    ) -> tuple[User, Invite, AuditEvent]:
        """Consume a matching invite and create its user and email in one transaction."""
        self._validate_registration(user, outbox_id, encrypted_code)
        if user.email_verified or not _TOKEN_HASH_PATTERN.fullmatch(token_hash):
            raise ValueError("Invite registration requires an unverified user and SHA-256 invite token hash")
        current_time = now or now_utc()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload,invite_id,workspace_id,email,normalized_email,role,token_hash,status,"
                "expires_at,created_by_user_id,accepted_by_user_id,created_at,updated_at "
                "FROM invites WHERE token_hash=%s FOR UPDATE",
                (token_hash,),
            ).fetchone()
            if row is None:
                raise PostgresAuthConflictError("Invite is unavailable")
            invite = Invite.model_validate(row[0])
            self._validate_invite_row(invite, row[1:])
            if invite.status != InviteStatus.PENDING or invite.expires_at <= current_time:
                raise PostgresAuthConflictError("Invite is not pending or has expired")
            if (
                user.email.lower().strip() != invite.email.lower().strip()
                or user.workspace_id != invite.workspace_id
                or user.role != invite.role
            ):
                raise PostgresAuthConflictError("Registration does not match the invited account scope")

            self._insert_user(conn, user)
            self._insert_verification_email(
                conn,
                outbox_id=outbox_id,
                recipient=user.email,
                encrypted_code=encrypted_code,
                created_at=email_created_at,
            )
            accepted = invite.model_copy(
                update={
                    "status": InviteStatus.ACCEPTED,
                    "accepted_by_user_id": user.id,
                    "updated_at": current_time,
                }
            )
            changed = conn.execute(
                "UPDATE invites SET status='ACCEPTED',accepted_by_user_id=%s,updated_at=%s,payload=%s "
                "WHERE invite_id=%s AND status='PENDING'",
                (
                    user.id,
                    current_time,
                    Jsonb(accepted.model_dump(mode="json")),
                    invite.id,
                ),
            )
            if changed.rowcount != 1:
                raise PostgresAuthIntegrityError("Invite changed during acceptance")
            event = AuditEvent(
                event_type="auth.invite_accepted",
                actor=user.email,
                entity_id=invite.id,
                workspace_id=invite.workspace_id,
                details={"invite_id": invite.id, "accepted_by_user_id": user.id},
                created_at=current_time,
            )
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                "actor,details,created_at,payload) VALUES(%s,%s,NULL,'invite',%s,%s,%s,%s,%s,%s)",
                (
                    event.id,
                    event.workspace_id,
                    event.entity_id,
                    event.event_type,
                    event.actor,
                    Jsonb(event.details),
                    event.created_at,
                    Jsonb(event.model_dump(mode="json")),
                ),
            )
        return user, accepted, event

    def rotate_email_verification(
        self,
        *,
        email: str,
        verification_hash: str,
        verification_salt: str,
        expires_at: datetime,
        sent_at: datetime,
        outbox_id: str,
        encrypted_code: bytes,
        email_created_at: datetime,
    ) -> User:
        """Replace an unverified account code and its outbox row atomically."""
        if (
            not _SECRET_HASH_PATTERN.fullmatch(verification_hash)
            or not _SECRET_SALT_PATTERN.fullmatch(verification_salt)
            or sent_at.tzinfo is None
            or expires_at.tzinfo is None
            or expires_at <= sent_at
            or email_created_at.tzinfo is None
            or not outbox_id.strip()
            or len(encrypted_code) < 29
        ):
            raise ValueError("Verification hash, salt, and expiration are invalid")
        normalized_email = email.lower().strip()
        with self._connect() as conn:
            row = self._user_row_by_email(conn, normalized_email, for_update=True)
            if row is None:
                raise PostgresAuthConflictError("User is unavailable")
            user = User.model_validate(row[0])
            self._validate_user_row(user, row[1:])
            if user.disabled:
                raise PostgresAuthConflictError("User is unavailable")
            if user.email_verified:
                raise PostgresAuthConflictError("Email is already verified")
            updated = user.model_copy(
                update={
                    "email_verification_hash": verification_hash,
                    "email_verification_salt": verification_salt,
                    "email_verification_expires_at": expires_at,
                    "email_verification_sent_at": sent_at,
                    "updated_at": sent_at,
                }
            )
            self._update_user_payload(conn, updated)
            self._insert_verification_email(
                conn,
                outbox_id=outbox_id,
                recipient=user.email,
                encrypted_code=encrypted_code,
                created_at=email_created_at,
            )
        return updated

    def verify_email(
        self,
        *,
        email: str,
        candidate_hash: str,
        now: datetime | None = None,
    ) -> User:
        """Verify under a row lock, rejecting stale codes and invalidating queued mail."""
        if not _SECRET_HASH_PATTERN.fullmatch(candidate_hash):
            raise ValueError("Verification candidate must be a SHA-256 digest")
        current_time = now or now_utc()
        if current_time.tzinfo is None:
            raise ValueError("Verification time must include a timezone")
        with self._connect() as conn:
            row = self._user_row_by_email(conn, email.lower().strip(), for_update=True)
            if row is None:
                raise PostgresEmailVerificationError("user_unavailable")
            user = User.model_validate(row[0])
            self._validate_user_row(user, row[1:])
            if user.disabled:
                raise PostgresEmailVerificationError("user_unavailable")
            if user.email_verified:
                return user
            if (
                not user.email_verification_hash
                or not user.email_verification_salt
                or not user.email_verification_expires_at
            ):
                raise PostgresEmailVerificationError("no_active_code")
            if user.email_verification_expires_at <= current_time:
                raise PostgresEmailVerificationError("expired")
            if not hmac.compare_digest(candidate_hash, user.email_verification_hash):
                raise PostgresEmailVerificationError("invalid")

            updated = user.model_copy(
                update={
                    "email_verified": True,
                    "email_verification_hash": None,
                    "email_verification_salt": None,
                    "email_verification_expires_at": None,
                    "updated_at": current_time,
                }
            )
            self._update_user_payload(conn, updated)
            conn.execute(
                "UPDATE auth_email_outbox SET status='DEAD',encrypted_code=%s,"
                "last_error='VERIFICATION_ALREADY_COMPLETE',lease_expires_at=NULL "
                "WHERE recipient=%s AND status IN ('PENDING','SENDING')",
                (b"", user.email),
            )
        return updated

    def create_invite(
        self,
        *,
        actor_user_id: str,
        invite: Invite,
        now: datetime | None = None,
    ) -> tuple[Invite, AuditEvent]:
        current_time = now or now_utc()
        if (
            invite.status != InviteStatus.PENDING
            or invite.accepted_by_user_id is not None
            or not invite.workspace_id
            or not _TOKEN_HASH_PATTERN.fullmatch(invite.token_hash)
            or not invite.email.strip()
            or invite.email.lower().strip() != invite.email
            or invite.expires_at <= current_time
            or invite.created_at.tzinfo is None
            or invite.updated_at.tzinfo is None
            or invite.expires_at.tzinfo is None
            or current_time.tzinfo is None
            or invite.created_by_user_id != actor_user_id
        ):
            raise ValueError("New invite fields are invalid")
        try:
            with self._connect() as conn:
                actor = self._active_owner(conn, actor_user_id)
                if actor.workspace_id != invite.workspace_id:
                    raise PostgresInviteConflictError("Invite workspace is outside the owner account")
                conn.execute(
                    "INSERT INTO invites(invite_id,workspace_id,email,normalized_email,role,token_hash,status,"
                    "expires_at,created_by_user_id,accepted_by_user_id,created_at,updated_at,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s)",
                    (
                        invite.id,
                        invite.workspace_id,
                        invite.email,
                        invite.email,
                        invite.role.value,
                        invite.token_hash,
                        invite.status.value,
                        invite.expires_at,
                        invite.created_by_user_id,
                        invite.created_at,
                        invite.updated_at,
                        Jsonb(invite.model_dump(mode="json")),
                    ),
                )
                event = AuditEvent(
                    event_type="auth.invite_created",
                    actor=actor.email,
                    entity_id=invite.id,
                    workspace_id=invite.workspace_id,
                    details=self._invite_audit_details(invite),
                    created_at=current_time,
                )
                self._insert_auth_audit(conn, event, entity_type="invite")
        except UniqueViolation as exc:
            raise PostgresInviteConflictError("Pending invite or invite identity already exists") from exc
        return invite, event

    def list_invites(
        self,
        *,
        actor_user_id: str,
        now: datetime | None = None,
    ) -> list[Invite]:
        current_time = now or now_utc()
        with self._connect() as conn:
            actor = self._active_owner(conn, actor_user_id)
            rows = conn.execute(
                "SELECT payload,invite_id,workspace_id,email,normalized_email,role,token_hash,status,"
                "expires_at,created_by_user_id,accepted_by_user_id,created_at,updated_at "
                "FROM invites WHERE workspace_id=%s ORDER BY created_at DESC,invite_id DESC FOR UPDATE",
                (actor.workspace_id,),
            ).fetchall()
            invites: list[Invite] = []
            for row in rows:
                invite = Invite.model_validate(row[0])
                self._validate_invite_row(invite, row[1:])
                if invite.status == InviteStatus.PENDING and invite.expires_at <= current_time:
                    invite = invite.model_copy(
                        update={"status": InviteStatus.EXPIRED, "updated_at": current_time}
                    )
                    result = conn.execute(
                        "UPDATE invites SET status='EXPIRED',updated_at=%s,payload=%s "
                        "WHERE invite_id=%s AND status='PENDING'",
                        (current_time, Jsonb(invite.model_dump(mode="json")), invite.id),
                    )
                    if result.rowcount != 1:
                        raise PostgresAuthIntegrityError("Invite changed while expiring")
                invites.append(invite)
        return invites

    def revoke_invite(
        self,
        *,
        actor_user_id: str,
        invite_id: str,
        now: datetime | None = None,
    ) -> tuple[Invite, AuditEvent | None]:
        current_time = now or now_utc()
        with self._connect() as conn:
            actor = self._active_owner(conn, actor_user_id)
            row = conn.execute(
                "SELECT payload,invite_id,workspace_id,email,normalized_email,role,token_hash,status,"
                "expires_at,created_by_user_id,accepted_by_user_id,created_at,updated_at "
                "FROM invites WHERE invite_id=%s AND workspace_id=%s FOR UPDATE",
                (invite_id, actor.workspace_id),
            ).fetchone()
            if row is None:
                raise PostgresInviteConflictError("Invite is unavailable")
            invite = Invite.model_validate(row[0])
            self._validate_invite_row(invite, row[1:])
            if invite.status == InviteStatus.ACCEPTED:
                raise PostgresInviteConflictError("Accepted invites cannot be revoked")
            if invite.status == InviteStatus.REVOKED:
                return invite, None
            if invite.status == InviteStatus.EXPIRED or invite.expires_at <= current_time:
                raise PostgresInviteConflictError("Expired invites cannot be revoked")

            revoked = invite.model_copy(
                update={"status": InviteStatus.REVOKED, "updated_at": current_time}
            )
            result = conn.execute(
                "UPDATE invites SET status='REVOKED',updated_at=%s,payload=%s "
                "WHERE invite_id=%s AND status='PENDING'",
                (current_time, Jsonb(revoked.model_dump(mode="json")), invite.id),
            )
            if result.rowcount != 1:
                raise PostgresAuthIntegrityError("Invite changed while revoking")
            event = AuditEvent(
                event_type="auth.invite_revoked",
                actor=actor.email,
                entity_id=invite.id,
                workspace_id=invite.workspace_id,
                details=self._invite_audit_details(revoked),
                created_at=current_time,
            )
            self._insert_auth_audit(conn, event, entity_type="invite")
        return revoked, event

    def list_workspace_users(self, *, actor_user_id: str) -> list[User]:
        with self._connect() as conn:
            try:
                actor = self._active_owner(conn, actor_user_id)
            except PostgresInviteConflictError as exc:
                raise PostgresRoleManagementError(str(exc)) from exc
            rows = conn.execute(
                "SELECT payload,user_id,workspace_id,email,normalized_email,role,disabled,email_verified "
                "FROM users WHERE workspace_id=%s ORDER BY normalized_email,user_id",
                (actor.workspace_id,),
            ).fetchall()
        users: list[User] = []
        for row in rows:
            user = User.model_validate(row[0])
            self._validate_user_row(user, row[1:])
            if user.workspace_id != actor.workspace_id:
                raise PostgresAuthIntegrityError("Workspace user listing crossed account scope")
            users.append(user)
        return users

    def update_user_role(
        self,
        *,
        actor_user_id: str,
        target_user_id: str,
        role: UserRole,
        now: datetime | None = None,
    ) -> RoleUpdateResult:
        current_time = now or now_utc()
        if current_time.tzinfo is None:
            raise ValueError("Role update time must include a timezone")
        with self._connect() as conn:
            actor_scope = conn.execute(
                "SELECT workspace_id FROM users WHERE user_id=%s", (actor_user_id,)
            ).fetchone()
            if actor_scope is None or actor_scope[0] is None:
                raise PostgresRoleManagementError("Workspace owner is unavailable")
            workspace_id = actor_scope[0]
            workspace_row = conn.execute(
                "SELECT workspace_id FROM workspaces WHERE workspace_id=%s FOR UPDATE",
                (workspace_id,),
            ).fetchone()
            if workspace_row is None:
                raise PostgresAuthIntegrityError("Owner workspace is missing")
            locked_rows = conn.execute(
                "SELECT payload,user_id,workspace_id,email,normalized_email,role,disabled,email_verified "
                "FROM users WHERE user_id=ANY(%s) ORDER BY user_id FOR UPDATE",
                ([actor_user_id, target_user_id],),
            ).fetchall()
            users: dict[str, User] = {}
            for row in locked_rows:
                user = User.model_validate(row[0])
                self._validate_user_row(user, row[1:])
                users[user.id] = user
            actor = users.get(actor_user_id)
            target = users.get(target_user_id)
            if actor is None or target is None:
                raise PostgresRoleManagementError("User is unavailable")
            if (
                actor.disabled
                or not actor.email_verified
                or actor.role != UserRole.OWNER
                or actor.workspace_id != workspace_id
            ):
                raise PostgresRoleManagementError("An active workspace owner is required")
            if target.workspace_id != actor.workspace_id:
                raise PostgresRoleManagementError("Target user is outside the owner workspace")
            previous_role = target.role
            if previous_role == role:
                return RoleUpdateResult(
                    target=target,
                    previous_role=previous_role,
                    audit_event=None,
                )
            if previous_role == UserRole.OWNER and role != UserRole.OWNER:
                active_owners = conn.execute(
                    "SELECT count(*) FROM users WHERE workspace_id=%s AND role='OWNER' AND disabled=FALSE",
                    (actor.workspace_id,),
                ).fetchone()[0]
                if active_owners <= 1:
                    raise PostgresRoleManagementError("Cannot remove the last active OWNER")

            updated_user = target.model_copy(
                update={"role": role, "updated_at": current_time}
            )
            result = conn.execute(
                "UPDATE users SET role=%s,payload=%s WHERE user_id=%s AND workspace_id=%s",
                (
                    role.value,
                    Jsonb(updated_user.model_dump(mode="json")),
                    target.id,
                    target.workspace_id,
                ),
            )
            if result.rowcount != 1:
                raise PostgresAuthIntegrityError("User changed during role update")
            event = AuditEvent(
                event_type="auth.role_updated",
                actor=actor.email,
                entity_id=target.id,
                workspace_id=target.workspace_id,
                details={
                    "target_user_id": target.id,
                    "target_email": target.email,
                    "previous_role": previous_role.value,
                    "new_role": role.value,
                },
                created_at=current_time,
            )
            self._insert_auth_audit(conn, event, entity_type="user")
        return RoleUpdateResult(
            target=updated_user,
            previous_role=previous_role,
            audit_event=event,
        )

    def request_account_deletion(
        self,
        *,
        user_id: str,
        reason_provided: bool,
        now: datetime | None = None,
    ) -> AccountDeletionResult:
        current_time = now or now_utc()
        if current_time.tzinfo is None:
            raise ValueError("Account-deletion time must include a timezone")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload,user_id,workspace_id,email,normalized_email,role,disabled,email_verified "
                "FROM users WHERE user_id=%s FOR UPDATE",
                (user_id,),
            ).fetchone()
            if row is None:
                raise PostgresAccountDeletionError("Account is unavailable")
            target = User.model_validate(row[0])
            self._validate_user_row(target, row[1:])
            if target.disabled:
                raise PostgresAccountDeletionError("Account is already disabled")
            if not target.workspace_id:
                raise PostgresAccountDeletionError("Account workspace is unavailable")

            session_rows = conn.execute(
                "SELECT payload,session_id,user_id,token_hash,expires_at,revoked FROM sessions "
                "WHERE user_id=%s AND revoked=FALSE AND expires_at>%s "
                "ORDER BY session_id FOR UPDATE",
                (target.id, current_time),
            ).fetchall()
            revoked_count = 0
            for session_row in session_rows:
                session = SessionToken.model_validate(session_row[0])
                self._validate_session_row(session, session_row[1:])
                revoked = session.model_copy(update={"revoked": True})
                changed = conn.execute(
                    "UPDATE sessions SET revoked=TRUE,payload=%s "
                    "WHERE session_id=%s AND user_id=%s AND revoked=FALSE",
                    (Jsonb(revoked.model_dump(mode="json")), session.id, target.id),
                )
                if changed.rowcount != 1:
                    raise PostgresAuthIntegrityError("Session changed during account deletion")
                revoked_count += 1

            disabled = target.model_copy(
                update={"disabled": True, "updated_at": current_time}
            )
            changed = conn.execute(
                "UPDATE users SET disabled=TRUE,payload=%s WHERE user_id=%s AND disabled=FALSE",
                (Jsonb(disabled.model_dump(mode="json")), target.id),
            )
            if changed.rowcount != 1:
                raise PostgresAuthIntegrityError("Account changed during deletion request")
            event = AuditEvent(
                event_type="auth.account_deletion_requested",
                actor=target.email,
                entity_id=target.id,
                workspace_id=target.workspace_id,
                details={
                    "target_user_id": target.id,
                    "target_email": target.email,
                    "active_sessions_revoked": revoked_count,
                    "status": "RECEIVED",
                    "reason_provided": bool(reason_provided),
                },
                created_at=current_time,
            )
            self._insert_auth_audit(conn, event, entity_type="user")
        return AccountDeletionResult(
            target=disabled,
            revoked_sessions_count=revoked_count,
            audit_event=event,
        )

    def _active_owner(self, conn: Connection, user_id: str) -> User:
        row = conn.execute(
            "SELECT payload,user_id,workspace_id,email,normalized_email,role,disabled,email_verified "
            "FROM users WHERE user_id=%s FOR UPDATE",
            (user_id,),
        ).fetchone()
        if row is None:
            raise PostgresInviteConflictError("Workspace owner is unavailable")
        actor = User.model_validate(row[0])
        self._validate_user_row(actor, row[1:])
        if (
            actor.disabled
            or not actor.email_verified
            or actor.role != UserRole.OWNER
            or not actor.workspace_id
        ):
            raise PostgresInviteConflictError("An active workspace owner is required")
        return actor

    @staticmethod
    def _invite_audit_details(invite: Invite) -> dict[str, object]:
        return {
            "invite_id": invite.id,
            "workspace_id": invite.workspace_id,
            "email": invite.email,
            "role": invite.role.value,
            "status": invite.status.value,
            "expires_at": invite.expires_at.isoformat(),
        }

    @staticmethod
    def _insert_auth_audit(conn: Connection, event: AuditEvent, *, entity_type: str) -> None:
        if not event.workspace_id:
            raise ValueError("Workspace audit events require a workspace")
        conn.execute(
            "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
            "actor,details,created_at,payload) VALUES(%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s)",
            (
                event.id,
                event.workspace_id,
                entity_type,
                event.entity_id,
                event.event_type,
                event.actor,
                Jsonb(event.details),
                event.created_at,
                Jsonb(event.model_dump(mode="json")),
            ),
        )

    @staticmethod
    def _user_row_by_email(conn: Connection, normalized_email: str, *, for_update: bool):
        suffix = " FOR UPDATE" if for_update else ""
        return conn.execute(
            "SELECT payload,user_id,workspace_id,email,normalized_email,role,disabled,email_verified "
            "FROM users WHERE normalized_email=%s" + suffix,
            (normalized_email,),
        ).fetchone()

    @staticmethod
    def _update_user_payload(conn: Connection, user: User) -> None:
        updated = conn.execute(
            "UPDATE users SET email_verified=%s,payload=%s "
            "WHERE user_id=%s AND workspace_id=%s",
            (
                user.email_verified,
                Jsonb(user.model_dump(mode="json")),
                user.id,
                user.workspace_id,
            ),
        )
        if updated.rowcount != 1:
            raise PostgresAuthIntegrityError("User changed during verification update")

    @staticmethod
    def _validate_registration(user: User, outbox_id: str, encrypted_code: bytes) -> None:
        if (
            not user.email.strip()
            or not _SECRET_HASH_PATTERN.fullmatch(user.password_hash)
            or not _SECRET_SALT_PATTERN.fullmatch(user.password_salt)
            or user.disabled
            or not outbox_id.strip()
            or len(encrypted_code) < 29
            or not _SECRET_HASH_PATTERN.fullmatch(user.email_verification_hash or "")
            or not _SECRET_SALT_PATTERN.fullmatch(user.email_verification_salt or "")
            or not user.email_verification_expires_at
        ):
            raise ValueError("Registration requires an active user, verification state, and encrypted email")

    @staticmethod
    def _insert_user(conn: Connection, user: User) -> None:
        if not user.workspace_id:
            raise ValueError("Registered users must belong to a workspace")
        conn.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                user.id,
                user.workspace_id,
                user.email,
                user.email.lower().strip(),
                user.role.value,
                user.disabled,
                user.email_verified,
                Jsonb(user.model_dump(mode="json")),
            ),
        )

    @staticmethod
    def _insert_verification_email(
        conn: Connection,
        *,
        outbox_id: str,
        recipient: str,
        encrypted_code: bytes,
        created_at: datetime,
    ) -> None:
        conn.execute(
            "UPDATE auth_email_outbox SET status='DEAD',encrypted_code=%s,"
            "last_error='VERIFICATION_CODE_SUPERSEDED',lease_expires_at=NULL "
            "WHERE recipient=%s AND status IN ('PENDING','SENDING')",
            (b"", recipient),
        )
        conn.execute(
            "INSERT INTO auth_email_outbox(outbox_id,recipient,encrypted_code,status,attempts,created_at,"
            "next_attempt_at) VALUES(%s,%s,%s,'PENDING',0,%s,%s)",
            (outbox_id, recipient, encrypted_code, created_at, created_at),
        )

    def get_user_by_email(self, email: str) -> User | None:
        normalized_email = email.lower().strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload,user_id,workspace_id,email,normalized_email,role,disabled,email_verified "
                "FROM users WHERE normalized_email=%s",
                (normalized_email,),
            ).fetchone()
        if row is None:
            return None
        user = User.model_validate(row[0])
        self._validate_user_row(user, row[1:])
        return user

    def get_invite_by_token_hash(self, token_hash: str) -> Invite | None:
        """Read an invite candidate without granting or consuming registration authority."""
        if not _TOKEN_HASH_PATTERN.fullmatch(token_hash):
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload,invite_id,workspace_id,email,normalized_email,role,token_hash,status,"
                "expires_at,created_by_user_id,accepted_by_user_id,created_at,updated_at "
                "FROM invites WHERE token_hash=%s",
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        invite = Invite.model_validate(row[0])
        self._validate_invite_row(invite, row[1:])
        return invite

    def create_session(self, session: SessionToken) -> User:
        self._validate_session_input(session)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload,user_id,workspace_id,email,normalized_email,role,disabled,email_verified "
                "FROM users WHERE user_id=%s FOR UPDATE",
                (session.user_id,),
            ).fetchone()
            if row is None:
                raise PostgresSessionAuthorizationError("Session user is unavailable")
            user = User.model_validate(row[0])
            self._validate_user_row(user, row[1:])
            if user.disabled or not user.email_verified:
                raise PostgresSessionAuthorizationError("Session user is not active")
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
        return user

    def authenticate_session(
        self,
        token_hash: str,
        *,
        now: datetime | None = None,
    ) -> AuthenticatedSession | None:
        if not _TOKEN_HASH_PATTERN.fullmatch(token_hash):
            return None
        current_time = now or now_utc()
        if not _is_timezone_aware(current_time):
            raise ValueError("Session authentication time must be timezone-aware")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT session.payload,session.session_id,session.user_id,session.token_hash,"
                "session.expires_at,session.revoked,account.payload,account.user_id,account.workspace_id,"
                "account.email,account.normalized_email,account.role,account.disabled,account.email_verified "
                "FROM sessions AS session JOIN users AS account ON account.user_id=session.user_id "
                "WHERE session.token_hash=%s",
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        session = SessionToken.model_validate(row[0])
        user = User.model_validate(row[6])
        self._validate_session_row(session, row[1:6])
        self._validate_user_row(user, row[7:14])
        if (
            session.revoked
            or session.created_at > current_time
            or session.expires_at <= session.created_at
            or session.expires_at <= current_time
            or user.disabled
            or not user.email_verified
        ):
            return None
        return AuthenticatedSession(user=user, session=session)

    def list_sessions(self, actor_user_id: str) -> list[tuple[SessionToken, User]]:
        with self._connect() as conn:
            actor_row = conn.execute(
                "SELECT payload,user_id,workspace_id,email,normalized_email,role,disabled,email_verified "
                "FROM users WHERE user_id=%s",
                (actor_user_id,),
            ).fetchone()
            if actor_row is None:
                raise PostgresSessionAuthorizationError("Session manager is unavailable")
            actor = User.model_validate(actor_row[0])
            self._validate_user_row(actor, actor_row[1:])
            if actor.disabled or not actor.email_verified or actor.role != UserRole.OWNER:
                raise PostgresSessionAuthorizationError("Session manager must be an active workspace owner")
            rows = conn.execute(
                "SELECT session.payload,session.session_id,session.user_id,session.token_hash,"
                "session.expires_at,session.revoked,account.payload,account.user_id,account.workspace_id,"
                "account.email,account.normalized_email,account.role,account.disabled,account.email_verified "
                "FROM sessions AS session JOIN users AS account ON account.user_id=session.user_id "
                "WHERE account.workspace_id=%s ORDER BY session.created_at DESC,session.session_id DESC",
                (actor.workspace_id,),
            ).fetchall()

        result: list[tuple[SessionToken, User]] = []
        for row in rows:
            session = SessionToken.model_validate(row[0])
            user = User.model_validate(row[6])
            self._validate_session_row(session, row[1:6])
            self._validate_user_row(user, row[7:14])
            if session.user_id != user.id or user.workspace_id != actor.workspace_id:
                raise PostgresAuthIntegrityError("Session row crosses its account workspace")
            result.append((session, user))
        return result

    def revoke_user_sessions(
        self,
        *,
        actor_user_id: str,
        target_user_id: str,
        now: datetime | None = None,
    ) -> SessionRevocationResult:
        if actor_user_id == target_user_id:
            raise PostgresSessionAuthorizationError("Owners cannot revoke their own sessions")
        current_time = now or now_utc()
        with self._connect() as conn:
            locked_rows = conn.execute(
                "SELECT payload,user_id,workspace_id,email,normalized_email,role,disabled,email_verified "
                "FROM users WHERE user_id = ANY(%s) ORDER BY user_id FOR UPDATE",
                ([actor_user_id, target_user_id],),
            ).fetchall()
            users: dict[str, User] = {}
            for row in locked_rows:
                user = User.model_validate(row[0])
                self._validate_user_row(user, row[1:])
                users[user.id] = user
            actor = users.get(actor_user_id)
            target = users.get(target_user_id)
            if actor is None or target is None:
                raise PostgresSessionAuthorizationError("User is unavailable")
            if (
                actor.disabled
                or not actor.email_verified
                or actor.role != UserRole.OWNER
                or actor.workspace_id is None
                or actor.workspace_id != target.workspace_id
            ):
                raise PostgresSessionAuthorizationError("Session revocation is outside the actor workspace")

            session_rows = conn.execute(
                "SELECT payload,session_id,user_id,token_hash,expires_at,revoked FROM sessions "
                "WHERE user_id=%s AND revoked=FALSE AND expires_at>%s "
                "ORDER BY session_id FOR UPDATE",
                (target.id, current_time),
            ).fetchall()
            revoked_count = 0
            for row in session_rows:
                session = SessionToken.model_validate(row[0])
                self._validate_session_row(session, row[1:])
                revoked = session.model_copy(update={"revoked": True})
                update = conn.execute(
                    "UPDATE sessions SET revoked=TRUE,payload=%s "
                    "WHERE session_id=%s AND user_id=%s AND revoked=FALSE",
                    (Jsonb(revoked.model_dump(mode="json")), session.id, target.id),
                )
                if update.rowcount != 1:
                    raise PostgresAuthIntegrityError("Session changed during revocation")
                revoked_count += 1

            event = AuditEvent(
                event_type="auth.sessions_revoked",
                actor=actor.email,
                entity_id=target.id,
                workspace_id=target.workspace_id,
                details={
                    "target_user_id": target.id,
                    "target_email": target.email,
                    "revoked_sessions_count": revoked_count,
                },
                created_at=current_time,
            )
            conn.execute(
                "INSERT INTO audit_events "
                "(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                "actor,details,created_at,payload) "
                "VALUES(%s,%s,NULL,'user',%s,%s,%s,%s,%s,%s)",
                (
                    event.id,
                    event.workspace_id,
                    event.entity_id,
                    event.event_type,
                    event.actor,
                    Jsonb(event.details),
                    event.created_at,
                    Jsonb(event.model_dump(mode="json")),
                ),
            )
        return SessionRevocationResult(target=target, revoked_count=revoked_count, audit_event=event)

    @staticmethod
    def _validate_session_input(session: SessionToken) -> None:
        if not _TOKEN_HASH_PATTERN.fullmatch(session.token_hash):
            raise ValueError("Session token hash must be a lowercase SHA-256 digest")
        if not _is_timezone_aware(session.created_at) or not _is_timezone_aware(session.expires_at):
            raise ValueError("Session timestamps must be timezone-aware")
        if session.created_at > now_utc():
            raise ValueError("New session creation time cannot be in the future")
        if session.expires_at <= session.created_at or session.revoked:
            raise ValueError("New session must be active with a future expiry")

    @staticmethod
    def _validate_session_row(session: SessionToken, columns: tuple[object, ...]) -> None:
        if (
            session.id != columns[0]
            or session.user_id != columns[1]
            or session.token_hash != columns[2]
            or session.expires_at != columns[3]
            or session.revoked != columns[4]
        ):
            raise PostgresAuthIntegrityError("Session payload does not match its normalized row")

    @staticmethod
    def _validate_user_row(user: User, columns: tuple[object, ...]) -> None:
        if (
            user.id != columns[0]
            or user.workspace_id != columns[1]
            or user.email != columns[2]
            or user.email.lower().strip() != columns[3]
            or user.role.value != columns[4]
            or user.disabled != columns[5]
            or user.email_verified != columns[6]
        ):
            raise PostgresAuthIntegrityError("User payload does not match its normalized row")

    @staticmethod
    def _validate_invite_row(invite: Invite, columns: tuple[object, ...]) -> None:
        if (
            invite.id != columns[0]
            or invite.workspace_id != columns[1]
            or invite.email != columns[2]
            or invite.email.lower().strip() != columns[3]
            or invite.role.value != columns[4]
            or invite.token_hash != columns[5]
            or invite.status.value != columns[6]
            or invite.expires_at != columns[7]
            or invite.created_by_user_id != columns[8]
            or invite.accepted_by_user_id != columns[9]
            or invite.created_at != columns[10]
            or invite.updated_at != columns[11]
        ):
            raise PostgresAuthIntegrityError("Invite payload does not match its normalized row")

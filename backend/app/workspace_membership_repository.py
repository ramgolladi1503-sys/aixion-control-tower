from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from psycopg import Connection
from .models import User, Workspace


class WorkspaceMembershipIntegrityError(RuntimeError):
    """Stored account or workspace identity disagrees with its indexed row."""


@dataclass(frozen=True)
class WorkspaceMembershipSnapshot:
    user: User | None
    workspace_count: int
    resolved_workspace_exists: bool


class WorkspaceMembershipRepository(Protocol):
    def get_workspace(self, workspace_id: str) -> Workspace | None: ...

    def snapshot(
        self,
        *,
        user_id: str,
        claimed_workspace_id: str | None,
    ) -> WorkspaceMembershipSnapshot: ...


def _workspace_from_row(
    *, workspace_id: str, row_workspace_id: str | None, payload: object | None
) -> Workspace | None:
    if row_workspace_id is None:
        if payload is not None:
            raise WorkspaceMembershipIntegrityError("Workspace row is incomplete")
        return None
    if payload is None:
        raise WorkspaceMembershipIntegrityError("Workspace payload is missing")
    try:
        workspace = (
            Workspace.model_validate_json(payload)
            if isinstance(payload, (str, bytes))
            else Workspace.model_validate(payload)
        )
    except Exception as error:
        raise WorkspaceMembershipIntegrityError("Workspace payload is invalid") from error
    if workspace.id != row_workspace_id or workspace.id != workspace_id:
        raise WorkspaceMembershipIntegrityError("Workspace identity is inconsistent")
    return workspace


def _snapshot_user(
    *,
    user_id: str,
    payload: object | None,
    row_user_id: str | None,
    row_workspace_id: str | None,
    validate_workspace_column: bool = True,
) -> User | None:
    if row_user_id is None:
        if payload is not None or row_workspace_id is not None:
            raise WorkspaceMembershipIntegrityError("User membership row is incomplete")
        return None
    if payload is None:
        raise WorkspaceMembershipIntegrityError("User membership payload is missing")
    try:
        user = User.model_validate_json(payload) if isinstance(payload, (str, bytes)) else User.model_validate(payload)
    except Exception as error:
        raise WorkspaceMembershipIntegrityError("User membership payload is invalid") from error
    if user.id != row_user_id or user.id != user_id or (
        validate_workspace_column and user.workspace_id != row_workspace_id
    ):
        raise WorkspaceMembershipIntegrityError("User membership identity is inconsistent")
    return user


class SQLiteWorkspaceMembershipRepository:
    """Read account membership from one SQLite statement snapshot."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT entity_id, payload FROM kv_store "
                "WHERE entity_type='workspace' AND entity_id=?",
                (workspace_id,),
            ).fetchone()
        if row is None:
            return None
        return _workspace_from_row(
            workspace_id=workspace_id, row_workspace_id=row[0], payload=row[1]
        )

    def snapshot(
        self,
        *,
        user_id: str,
        claimed_workspace_id: str | None,
    ) -> WorkspaceMembershipSnapshot:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT account.entity_id, account.payload, "
                "(SELECT COUNT(*) FROM kv_store WHERE entity_type='workspace'), "
                "workspace.entity_id, workspace.payload "
                "FROM (SELECT 1) AS request "
                "LEFT JOIN kv_store AS account ON account.entity_type='user' AND account.entity_id=? "
                "LEFT JOIN kv_store AS workspace ON workspace.entity_type='workspace' "
                "AND workspace.entity_id=COALESCE(json_extract(account.payload, '$.workspace_id'), ?)",
                (user_id, claimed_workspace_id),
            ).fetchone()
        if row is None:
            raise WorkspaceMembershipIntegrityError("Membership snapshot query returned no row")
        user = _snapshot_user(
            user_id=user_id,
            payload=row[1],
            row_user_id=row[0],
            row_workspace_id=None,
            validate_workspace_column=False,
        )
        workspace_exists = row[3] is not None
        if workspace_exists:
            try:
                workspace = Workspace.model_validate_json(row[4])
            except Exception as error:
                raise WorkspaceMembershipIntegrityError("Workspace membership payload is invalid") from error
            if workspace.id != row[3]:
                raise WorkspaceMembershipIntegrityError("Workspace membership identity is inconsistent")
        return WorkspaceMembershipSnapshot(
            user=user,
            workspace_count=int(row[2]),
            resolved_workspace_exists=workspace_exists,
        )


class PostgresWorkspaceMembershipRepository:
    """Read normalized PostgreSQL account membership and its workspace atomically."""

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT workspace_id, payload FROM workspaces WHERE workspace_id=%s",
                (workspace_id,),
            ).fetchone()
        if row is None:
            return None
        return _workspace_from_row(
            workspace_id=workspace_id, row_workspace_id=row[0], payload=row[1]
        )

    def snapshot(
        self,
        *,
        user_id: str,
        claimed_workspace_id: str | None,
    ) -> WorkspaceMembershipSnapshot:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT account.user_id, account.workspace_id, account.payload, "
                "workspace_count.total, workspace.workspace_id, workspace.payload "
                "FROM (SELECT 1) AS request "
                "CROSS JOIN (SELECT COUNT(*) AS total FROM workspaces) AS workspace_count "
                "LEFT JOIN users AS account ON account.user_id=%s "
                "LEFT JOIN workspaces AS workspace "
                "ON workspace.workspace_id=COALESCE(account.workspace_id, %s)",
                (user_id, claimed_workspace_id),
            ).fetchone()
        if row is None:
            raise WorkspaceMembershipIntegrityError("Membership snapshot query returned no row")
        user = _snapshot_user(
            user_id=user_id,
            payload=row[2],
            row_user_id=row[0],
            row_workspace_id=row[1],
        )
        workspace_exists = row[4] is not None
        if workspace_exists:
            try:
                workspace = Workspace.model_validate(row[5])
            except Exception as error:
                raise WorkspaceMembershipIntegrityError("Workspace membership payload is invalid") from error
            if workspace.id != row[4]:
                raise WorkspaceMembershipIntegrityError("Workspace membership identity is inconsistent")
        return WorkspaceMembershipSnapshot(
            user=user,
            workspace_count=int(row[3]),
            resolved_workspace_exists=workspace_exists,
        )

from __future__ import annotations

import multiprocessing
import os
import queue as queue_module
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb
from fastapi.testclient import TestClient

from app import application_repository_access, auth, main, workspace_access
from app.models import AuditEvent, Project, SessionToken, User, UserRole, Workspace, now_utc
from app.application_repositories import ApplicationRepositories
from app.postgres_database import run_postgres_migrations
from app.project_repository import PostgresProjectRepository
from app.settings import get_settings


def _read_projects_in_process(
    dsn: str,
    schema: str,
    workspace_id: str,
    user_id: str,
    bearer_token: str,
    results: Any,
) -> None:
    """Call the authenticated projects API using durable PostgreSQL identity."""
    try:

        def connect() -> Connection:
            connection = psycopg.connect(dsn)
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            return connection

        repositories = ApplicationRepositories.from_connector(connect)
        runtime = SimpleNamespace(repositories=repositories)
        main.app.state.postgres_runtime = runtime
        main.app.state.postgres_repositories = repositories
        settings = replace(
            get_settings(), profile="test", auth_enabled=True, postgres_auth_enabled=True
        )
        auth.get_settings = lambda: settings
        application_repository_access.get_settings = lambda: settings
        workspace_access.get_settings = lambda: settings
        client = TestClient(main.app)
        response = client.get("/projects", headers={"Authorization": f"Bearer {bearer_token}"})
        results.put(
            (
                "ok",
                os.getpid(),
                workspace_id,
                user_id,
                response.status_code,
                [project["id"] for project in response.json()]
                if response.status_code == 200
                else response.text,
            )
        )
    except BaseException as exc:
        results.put(("error", os.getpid(), f"{type(exc).__name__}: {exc}"))
        raise


@pytest.fixture
def postgres_project_scope_schema() -> Iterator[tuple[str, str, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")

    schema = f"ct_project_process_scope_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        yield dsn, schema, connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_authenticated_projects_api_isolates_postgres_workspaces_across_processes(
    postgres_project_scope_schema: tuple[str, str, Callable[[], Connection]],
) -> None:
    dsn, schema, connect = postgres_project_scope_schema
    workspace_ids = [f"workspace_{uuid.uuid4().hex}" for _ in range(2)]
    users = [
        User(
            id=f"user_{uuid.uuid4().hex}",
            email=f"owner-{uuid.uuid4().hex}@example.test",
            display_name=f"Workspace owner {index}",
            role=UserRole.OWNER,
            password_hash="0" * 64,
            password_salt="0" * 32,
            workspace_id=workspace_id,
            email_verified=True,
        )
        for index, workspace_id in enumerate(workspace_ids)
    ]
    bearer_tokens = [f"project-scope-session-{uuid.uuid4().hex}" for _ in range(2)]
    projects = [
        Project(
            id=f"project_{uuid.uuid4().hex}",
            name=f"Project {index}",
            workspace_id=workspace_id,
        )
        for index, workspace_id in enumerate(workspace_ids)
    ]

    with connect() as connection:
        for index, workspace_id in enumerate(workspace_ids):
            workspace = Workspace(
                id=workspace_id,
                name=f"Workspace {index}",
                owner_user_id=users[index].id,
            )
            connection.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES (%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            user = users[index]
            connection.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
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

    repository = PostgresProjectRepository(connect)
    for project in projects:
        repository.create(
            project=project,
            audit_event=AuditEvent(
                event_type="project.created",
                entity_id=project.id,
                workspace_id=project.workspace_id,
                actor="test:project-process-isolation",
                details={"name": project.name, "mode": project.mode},
            ),
        )

    for user, bearer_token in zip(users, bearer_tokens, strict=True):
        session = SessionToken(
            user_id=user.id,
            token_hash=sha256(bearer_token.encode("utf-8")).hexdigest(),
            expires_at=now_utc() + timedelta(hours=1),
        )
        ApplicationRepositories.from_connector(connect).auth.create_session(session)

    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [
        context.Process(
            target=_read_projects_in_process,
            args=(
                dsn,
                schema,
                workspace_ids[index],
                users[index].id,
                bearer_tokens[index],
                results,
            ),
        )
        for index in range(2)
    ]
    try:
        for process in processes:
            process.start()

        received: list[tuple[Any, ...]] = []
        for _ in processes:
            try:
                received.append(results.get(timeout=20))
            except queue_module.Empty:
                pytest.fail("Timed out waiting for independent PostgreSQL project readers")

        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                pytest.fail("PostgreSQL project reader did not exit before timeout")

        assert [process.exitcode for process in processes] == [0, 0]
        assert all(result[0] == "ok" for result in received), received
        assert len({result[1] for result in received}) == 2
        expected = {workspace_ids[index]: projects[index].id for index in range(2)}
        for _, _pid, own_workspace, user_id, status_code, own_ids in received:
            assert status_code == 200, own_ids
            assert own_ids == [expected[own_workspace]]
            assert users[workspace_ids.index(own_workspace)].id == user_id
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        results.close()
        results.join_thread()

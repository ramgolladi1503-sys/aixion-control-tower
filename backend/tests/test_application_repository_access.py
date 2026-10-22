from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.application_repository_access as repository_access
from app.application_repository_access import (
    agent_task_repository_for_request,
    application_repositories_for_request,
    approval_decision_repository_for_request,
    project_repository_for_request,
    workspace_membership_repository_for_request,
)
from app.models import AuthUser, User, UserRole
from app.application_repositories import ApplicationRepositories
from app.approval_repository import PostgresApprovalDecisionRepository
from app.test_run_repository import PostgresTestRunRepository
from app.workspace_membership_repository import WorkspaceMembershipSnapshot


def test_agent_task_repository_access_returns_app_binding() -> None:
    repository = object()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent_task_repository=repository)))

    assert agent_task_repository_for_request(request) is repository


@pytest.mark.parametrize(
    "test_request",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent_task_repository=None))),
    ],
)
def test_agent_task_repository_access_fails_closed_without_binding(
    test_request: object,
) -> None:
    with pytest.raises(RuntimeError, match="AgentTask repository is unavailable"):
        agent_task_repository_for_request(test_request)  # type: ignore[arg-type]


def test_application_repository_access_returns_lifespan_bundle() -> None:
    bundle = object()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(postgres_repositories=bundle),
        ),
    )

    assert application_repositories_for_request(request) is bundle


@pytest.mark.parametrize(
    "test_request",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(postgres_repositories=None))),
    ],
)
def test_application_repository_access_fails_closed_without_bundle(test_request: object) -> None:
    with pytest.raises(RuntimeError, match="repository runtime is unavailable"):
        application_repositories_for_request(test_request)  # type: ignore[arg-type]


def test_approval_decision_repository_access_returns_app_binding() -> None:
    repository = object()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(approval_decision_repository=repository),
        ),
    )

    assert approval_decision_repository_for_request(request) is repository


@pytest.mark.parametrize(
    "test_request",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(approval_decision_repository=None)),
        ),
    ],
)
def test_approval_decision_repository_access_fails_closed_without_binding(
    test_request: object,
) -> None:
    with pytest.raises(RuntimeError, match="approval decision repository is unavailable"):
        approval_decision_repository_for_request(test_request)  # type: ignore[arg-type]


def test_approval_repository_uses_matching_postgres_runtime_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = PostgresApprovalDecisionRepository(lambda: None)  # type: ignore[arg-type]
    repositories = object.__new__(ApplicationRepositories)
    object.__setattr__(repositories, "approval_decisions", repository)
    runtime = SimpleNamespace(repositories=repositories)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                postgres_runtime=runtime,
                postgres_repositories=repositories,
                approval_decision_repository=object(),
            ),
        ),
    )
    monkeypatch.setattr(
        repository_access,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )

    assert approval_decision_repository_for_request(request) is repository


def test_approval_repository_rejects_mismatched_postgres_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = object.__new__(ApplicationRepositories)
    object.__setattr__(
        repositories,
        "approval_decisions",
        PostgresApprovalDecisionRepository(lambda: None),  # type: ignore[arg-type]
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                postgres_runtime=SimpleNamespace(repositories=object()),
                postgres_repositories=repositories,
                approval_decision_repository=object(),
            ),
        ),
    )
    monkeypatch.setattr(
        repository_access,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )

    with pytest.raises(RuntimeError, match="unavailable or mismatched"):
        approval_decision_repository_for_request(request)


def test_project_repository_access_returns_app_binding() -> None:
    repository = object()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_repository=repository)))

    assert project_repository_for_request(request) is repository


@pytest.mark.parametrize(
    "test_request",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_repository=None))),
    ],
)
def test_project_repository_access_fails_closed_without_binding(test_request: object) -> None:
    with pytest.raises(RuntimeError, match="project repository is unavailable"):
        project_repository_for_request(test_request)  # type: ignore[arg-type]


def test_test_run_repository_uses_matching_postgres_runtime_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = PostgresTestRunRepository(lambda: None)  # type: ignore[arg-type]
    repositories = object.__new__(ApplicationRepositories)
    object.__setattr__(repositories, "test_runs", repository)
    runtime = SimpleNamespace(repositories=repositories)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                postgres_runtime=runtime,
                postgres_repositories=repositories,
                test_run_repository=object(),
            ),
        ),
    )
    monkeypatch.setattr(
        repository_access,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )

    assert repository_access.test_run_repository_for_request(request) is repository


def test_test_run_repository_rejects_mismatched_postgres_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = object.__new__(ApplicationRepositories)
    object.__setattr__(
        repositories,
        "test_runs",
        PostgresTestRunRepository(lambda: None),  # type: ignore[arg-type]
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                postgres_runtime=SimpleNamespace(repositories=object()),
                postgres_repositories=repositories,
                test_run_repository=object(),
            ),
        ),
    )
    monkeypatch.setattr(
        repository_access,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )

    with pytest.raises(RuntimeError, match="unavailable or mismatched"):
        repository_access.test_run_repository_for_request(request)


def test_workspace_membership_repository_access_returns_app_binding() -> None:
    repository = object()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(workspace_membership_repository=repository))
    )

    assert workspace_membership_repository_for_request(request) is repository


def test_workspace_membership_repository_uses_postgres_auth_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    postgres_repository = object()
    sqlite_repository = object()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                postgres_repositories=SimpleNamespace(workspace_membership=postgres_repository),
                workspace_membership_repository=sqlite_repository,
            ),
        ),
    )
    monkeypatch.setattr(
        repository_access,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )

    assert workspace_membership_repository_for_request(request) is postgres_repository


def test_workspace_membership_postgres_selection_fails_closed_without_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(workspace_membership_repository=object()),
        ),
    )
    monkeypatch.setattr(
        repository_access,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )

    with pytest.raises(RuntimeError, match="PostgreSQL application repository runtime is unavailable"):
        workspace_membership_repository_for_request(request)


def test_workspace_id_resolution_requires_request_for_postgres_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        repository_access,
        "get_settings",
        lambda: SimpleNamespace(auth_enabled=True, postgres_auth_enabled=True),
    )

    with pytest.raises(RuntimeError, match="PostgreSQL application repository runtime is unavailable"):
        repository_access.workspace_id_for_request(
            AuthUser(
                id="pg-user",
                email="pg-user@example.com",
                display_name="PG User",
                role=UserRole.OWNER,
                workspace_id="pg-workspace",
            ),
            None,
        )


def test_workspace_membership_resolution_uses_postgres_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "pg-workspace"
    user_id = "pg-user"

    class PostgresMembership:
        def snapshot(self, *, user_id: str, claimed_workspace_id: str | None):
            assert user_id == "pg-user"
            assert claimed_workspace_id == workspace_id
            return WorkspaceMembershipSnapshot(
                user=User(
                    id=user_id,
                    email="pg-user@example.com",
                    password_hash="hash",
                    password_salt="salt",
                    workspace_id=workspace_id,
                ),
                workspace_count=1,
                resolved_workspace_exists=True,
            )

    class SQLiteMembership:
        def snapshot(self, **kwargs):
            raise AssertionError("SQLite membership must not be read for PostgreSQL auth")

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                postgres_repositories=SimpleNamespace(
                    workspace_membership=PostgresMembership(),
                ),
                workspace_membership_repository=SQLiteMembership(),
            ),
        ),
    )
    monkeypatch.setattr(
        repository_access,
        "get_settings",
        lambda: SimpleNamespace(auth_enabled=True, postgres_auth_enabled=True),
    )
    monkeypatch.setattr(
        "app.workspace_access.get_settings",
        lambda: SimpleNamespace(is_production=False),
    )
    user = AuthUser(
        id=user_id,
        email="pg-user@example.com",
        display_name="PG User",
        role=UserRole.OWNER,
        workspace_id=workspace_id,
    )

    assert repository_access.workspace_id_for_request(user, request) == workspace_id


def test_workspace_membership_repository_keeps_local_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_repository = object()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                postgres_repositories=SimpleNamespace(workspace_membership=object()),
                workspace_membership_repository=sqlite_repository,
            ),
        ),
    )
    monkeypatch.setattr(
        repository_access,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=False),
    )

    assert workspace_membership_repository_for_request(request) is sqlite_repository


@pytest.mark.parametrize(
    "test_request",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
    ],
)
def test_workspace_membership_repository_access_fails_closed(test_request: object) -> None:
    with pytest.raises(RuntimeError, match="workspace-membership repository is unavailable"):
        workspace_membership_repository_for_request(test_request)  # type: ignore[arg-type]

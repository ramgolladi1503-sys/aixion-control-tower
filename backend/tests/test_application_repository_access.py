from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.application_repository_access import (
    agent_task_repository_for_request,
    application_repositories_for_request,
    approval_decision_repository_for_request,
    project_repository_for_request,
    workspace_membership_repository_for_request,
)


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


def test_workspace_membership_repository_access_returns_app_binding() -> None:
    repository = object()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(workspace_membership_repository=repository))
    )

    assert workspace_membership_repository_for_request(request) is repository


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

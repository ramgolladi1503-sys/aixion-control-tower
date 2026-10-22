from __future__ import annotations

from typing import Protocol

from .application_repositories import ApplicationRepositories
from .audit_repository import AuditEventRepository
from .agent_task_repository import AgentTaskRepository, PostgresAgentTaskRepository
from .agent_run_repository import AgentRunRepository, PostgresAgentRunRepository
from .agent_auth_repository import ExternalAgentAuthenticationRepository
from .agent_auth_repository import SQLiteExternalAgentAuthRepository
from .approval_repository import ApprovalDecisionRepository, PostgresApprovalDecisionRepository
from .project_repository import ProjectRepository
from .connector_repository import ConnectorRepository, PostgresConnectorRepository
from .postgres_external_agent_auth_repository import PostgresExternalAgentAuthRepository
from .idea_repository import IdeaRepository
from .settings import get_settings
from .workspace_access import workspace_id_for_user as resolve_workspace_id_for_user
from .workspace_membership_repository import WorkspaceMembershipRepository
from .work_order_repository import WorkOrderRepository
from .models import AuthUser
from .mcp_child_server_repository import MCPChildServerRepository
from .test_run_repository import PostgresTestRunRepository, TestRunRepository


class _RequestState(Protocol):
    app: object


def application_repositories_for_request(request: _RequestState | None) -> ApplicationRepositories:
    """Return the lifespan-owned PostgreSQL bundle or fail closed.

    This helper deliberately has no SQLite fallback. Callers must choose their
    local/test adapter explicitly before asking for the PostgreSQL bundle.
    """
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    repositories = getattr(state, "postgres_repositories", None)
    if repositories is None:
        raise RuntimeError("PostgreSQL application repository runtime is unavailable")
    return repositories


def approval_decision_repository_for_request(
    request: _RequestState | None,
) -> ApprovalDecisionRepository:
    """Return the approval repository explicitly bound to this FastAPI app.

    PostgreSQL auth mode selects the approval adapter from the same lifespan-
    owned repository bundle. A diagnostic probe without PostgreSQL auth never
    switches the local/test app away from its explicit SQLite binding.
    """
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    if getattr(get_settings(), "postgres_auth_enabled", False):
        repositories = application_repositories_for_request(request)
        runtime = getattr(state, "postgres_runtime", None)
        repository = getattr(repositories, "approval_decisions", None)
        if (
            runtime is None
            or getattr(runtime, "repositories", None) is not repositories
            or not isinstance(repositories, ApplicationRepositories)
            or not isinstance(repository, PostgresApprovalDecisionRepository)
        ):
            raise RuntimeError(
                "PostgreSQL approval repository is unavailable or mismatched"
            )
        return repository
    repository = getattr(state, "approval_decision_repository", None)
    if repository is None:
        raise RuntimeError("Application approval decision repository is unavailable")
    return repository


def agent_task_repository_for_request(request: _RequestState | None) -> AgentTaskRepository:
    """Return the AgentTask adapter selected by this application's store mode.

    PostgreSQL auth mode selects from the same lifespan-owned bundle as linked
    approvals. Probe-only PostgreSQL never switches local/test task routes.
    """
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    if getattr(get_settings(), "postgres_auth_enabled", False):
        repositories = application_repositories_for_request(request)
        runtime = getattr(state, "postgres_runtime", None)
        repository = getattr(repositories, "agent_tasks", None)
        if (
            runtime is None
            or getattr(runtime, "repositories", None) is not repositories
            or not isinstance(repositories, ApplicationRepositories)
            or not isinstance(repository, PostgresAgentTaskRepository)
        ):
            raise RuntimeError("PostgreSQL AgentTask repository is unavailable or mismatched")
        return repository
    repository = getattr(state, "agent_task_repository", None)
    if repository is None:
        raise RuntimeError("Application AgentTask repository is unavailable")
    return repository


def agent_run_repository_for_request(request: _RequestState | None) -> AgentRunRepository:
    """Return the AgentRun adapter selected by this application's store mode."""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    if getattr(get_settings(), "postgres_auth_enabled", False):
        repositories = application_repositories_for_request(request)
        runtime = getattr(state, "postgres_runtime", None)
        repository = getattr(repositories, "agent_runs", None)
        if (
            runtime is None
            or getattr(runtime, "repositories", None) is not repositories
            or not isinstance(repositories, ApplicationRepositories)
            or not isinstance(repository, PostgresAgentRunRepository)
        ):
            raise RuntimeError("PostgreSQL AgentRun repository is unavailable or mismatched")
        return repository
    repository = getattr(state, "agent_run_repository", None)
    if repository is None:
        raise RuntimeError("Application AgentRun repository is unavailable")
    return repository


def audit_event_repository_for_request(request: _RequestState | None) -> AuditEventRepository:
    """Return the audit reader explicitly bound to this FastAPI application."""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    repository = getattr(state, "audit_event_repository", None)
    if repository is None:
        raise RuntimeError("Application audit-event repository is unavailable")
    return repository


def project_repository_for_request(request: _RequestState | None) -> ProjectRepository:
    """Return the project adapter selected by this application's auth runtime.

    PostgreSQL-backed account authentication makes the lifespan-owned durable
    repository bundle the authority for projects too. There is deliberately no
    fallback to the legacy app binding in that mode, because that binding may
    still point at SQLite. Local and test configurations continue to use their
    explicitly app-bound adapter.
    """
    if getattr(get_settings(), "postgres_auth_enabled", False):
        return application_repositories_for_request(request).projects

    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    repository = getattr(state, "project_repository", None)
    if repository is None:
        raise RuntimeError("Application project repository is unavailable")
    return repository


def connector_repository_for_request(request: _RequestState | None) -> ConnectorRepository:
    """Return the connector repository selected by this application's authority.

    PostgreSQL auth mode uses the connector adapter from the lifespan-owned
    repository bundle. It must never fall back to a separate app binding or the
    process-local store. Local and test modes continue to use the explicit
    app-bound adapter.
    """
    if getattr(get_settings(), "postgres_auth_enabled", False):
        repositories = application_repositories_for_request(request)
        app = getattr(request, "app", None)
        state = getattr(app, "state", None)
        runtime = getattr(state, "postgres_runtime", None)
        if runtime is None or getattr(runtime, "repositories", None) is not repositories:
            raise RuntimeError(
                "PostgreSQL connector repository is not bound to the active runtime"
            )
        repository = getattr(repositories, "connectors", None)
        if not isinstance(repositories, ApplicationRepositories) or not isinstance(
            repository, PostgresConnectorRepository
        ):
            raise RuntimeError("PostgreSQL connector repository bundle is unavailable or mismatched")
        return repository

    if request is None:
        raise RuntimeError("Application connector repository requires an app-bound request")
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    repository = getattr(state, "connector_repository", None)
    if repository is None:
        raise RuntimeError("Application connector repository is unavailable")
    return repository


def idea_repository_for_request(request: _RequestState | None) -> IdeaRepository:
    """Return the idea adapter explicitly bound to this FastAPI application."""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    repository = getattr(state, "idea_repository", None)
    if repository is None:
        raise RuntimeError("Application idea repository is unavailable")
    return repository


def work_order_repository_for_request(request: _RequestState | None) -> WorkOrderRepository:
    """Return the WorkOrder adapter bound to this FastAPI application."""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    repository = getattr(state, "work_order_repository", None)
    if repository is None:
        raise RuntimeError("Application WorkOrder repository is unavailable")
    return repository


def test_run_repository_for_request(request: _RequestState | None) -> TestRunRepository:
    """Return the test-run adapter selected by this application's store mode."""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    if getattr(get_settings(), "postgres_auth_enabled", False):
        repositories = application_repositories_for_request(request)
        runtime = getattr(state, "postgres_runtime", None)
        repository = getattr(repositories, "test_runs", None)
        if (
            runtime is None
            or getattr(runtime, "repositories", None) is not repositories
            or not isinstance(repositories, ApplicationRepositories)
            or not isinstance(repository, PostgresTestRunRepository)
        ):
            raise RuntimeError(
                "PostgreSQL test-run repository is unavailable or mismatched"
            )
        return repository
    repository = getattr(state, "test_run_repository", None)
    if repository is None:
        raise RuntimeError("Application test-run repository is unavailable")
    return repository


def mcp_child_server_repository_for_request(
    request: _RequestState | None,
) -> MCPChildServerRepository:
    """Return the MCP registry adapter bound to this FastAPI application."""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    repository = getattr(state, "mcp_child_server_repository", None)
    if repository is None:
        raise RuntimeError("Application MCP child-server repository is unavailable")
    return repository


def external_agent_auth_repository_for_request(
    request: _RequestState | None,
) -> ExternalAgentAuthenticationRepository:
    """Return the external-agent auth adapter bound to this FastAPI app."""
    if getattr(get_settings(), "postgres_auth_enabled", False):
        repositories = application_repositories_for_request(request)
        app = getattr(request, "app", None)
        state = getattr(app, "state", None)
        runtime = getattr(state, "postgres_runtime", None)
        if runtime is None or getattr(runtime, "repositories", None) is not repositories:
            raise RuntimeError(
                "PostgreSQL external-agent auth repository is not bound to the active runtime"
            )
        repository = getattr(repositories, "external_agent_auth", None)
        if not isinstance(repositories, ApplicationRepositories) or not isinstance(
            repository, PostgresExternalAgentAuthRepository
        ):
            raise RuntimeError(
                "PostgreSQL external-agent auth repository bundle is unavailable or mismatched"
            )
        return repository

    if request is None:
        raise RuntimeError("External-agent authentication requires an app-bound request")
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    repository = getattr(state, "external_agent_auth_repository", None)
    if repository is None:
        raise RuntimeError("Application external-agent auth repository is unavailable")
    return repository


def is_sqlite_external_agent_auth_repository(
    repository: ExternalAgentAuthenticationRepository,
) -> bool:
    return isinstance(repository, SQLiteExternalAgentAuthRepository)


def workspace_membership_repository_for_request(
    request: _RequestState | None,
) -> WorkspaceMembershipRepository:
    """Return membership reads from the active authentication authority."""
    if getattr(get_settings(), "postgres_auth_enabled", False):
        return application_repositories_for_request(request).workspace_membership

    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    repository = getattr(state, "workspace_membership_repository", None)
    if repository is None:
        raise RuntimeError("Application workspace-membership repository is unavailable")
    return repository


def workspace_id_for_request(user: AuthUser, request: _RequestState | None) -> str | None:
    """Resolve membership from PostgreSQL auth or the local app binding."""
    settings = get_settings()
    if getattr(settings, "postgres_auth_enabled", False):
        return resolve_workspace_id_for_user(
            user,
            membership_repository=workspace_membership_repository_for_request(request),
        )
    if request is None or not getattr(settings, "auth_enabled", False):
        return resolve_workspace_id_for_user(user)
    return resolve_workspace_id_for_user(
        user,
        membership_repository=workspace_membership_repository_for_request(request),
    )

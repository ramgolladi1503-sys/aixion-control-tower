from __future__ import annotations

from typing import Protocol

from .application_repositories import ApplicationRepositories
from .audit_repository import AuditEventRepository
from .agent_task_repository import AgentTaskRepository
from .agent_run_repository import AgentRunRepository
from .agent_auth_repository import ExternalAgentAuthenticationRepository
from .agent_auth_repository import SQLiteExternalAgentAuthRepository
from .approval_repository import ApprovalDecisionRepository
from .project_repository import ProjectRepository
from .idea_repository import IdeaRepository
from .settings import get_settings
from .workspace_access import workspace_id_for_user as resolve_workspace_id_for_user
from .workspace_membership_repository import WorkspaceMembershipRepository
from .work_order_repository import WorkOrderRepository
from .models import AuthUser
from .mcp_child_server_repository import MCPChildServerRepository
from .test_run_repository import TestRunRepository


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

    This app-scoped accessor is separate from the diagnostic PostgreSQL bundle:
    a PostgreSQL probe must never switch approval routes independently from the
    rest of the application. The local/test app binds its SQLite adapter here;
    a future application-store cutover must bind the complete compatible
    runtime before changing this selection.
    """
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    repository = getattr(state, "approval_decision_repository", None)
    if repository is None:
        raise RuntimeError("Application approval decision repository is unavailable")
    return repository


def agent_task_repository_for_request(request: _RequestState | None) -> AgentTaskRepository:
    """Return the AgentTask adapter bound to this FastAPI application.

    There is intentionally no repository-bundle fallback here: task routes
    must not switch persistence independently from their linked approval path.
    """
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    repository = getattr(state, "agent_task_repository", None)
    if repository is None:
        raise RuntimeError("Application AgentTask repository is unavailable")
    return repository


def agent_run_repository_for_request(request: _RequestState | None) -> AgentRunRepository:
    """Return the AgentRun adapter bound to this FastAPI application."""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
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
    """Return the project adapter explicitly bound to this FastAPI application."""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    repository = getattr(state, "project_repository", None)
    if repository is None:
        raise RuntimeError("Application project repository is unavailable")
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
    """Return the test-run adapter bound to this FastAPI application."""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
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
    """Return membership reads from the same app-owned persistence boundary."""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    repository = getattr(state, "workspace_membership_repository", None)
    if repository is None:
        raise RuntimeError("Application workspace-membership repository is unavailable")
    return repository


def workspace_id_for_request(user: AuthUser, request: _RequestState | None) -> str | None:
    """Resolve auth-enabled membership from the app bundle; preserve local demo mode."""
    if request is None or not get_settings().auth_enabled:
        return resolve_workspace_id_for_user(user)
    return resolve_workspace_id_for_user(
        user,
        membership_repository=workspace_membership_repository_for_request(request),
    )

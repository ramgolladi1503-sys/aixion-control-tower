from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from .agent_run_models import AgentRun
from .agent_task_models import AgentTask
from .approval_decisions import (
    agent_run_repository,
    agent_task_repository,
    approval_decision_repository,
    project_repository,
    workspace_membership_repository,
)
from .connector_models import AgentConnector
from .models import (
    ApprovalRequest,
    AuthUser,
    AuditEvent,
    ExternalAgent,
    Idea,
    MCPChildServer,
    WorkOrder,
)
from .project_repository import ProjectRepository
from .settings import get_settings
from .store import store
from .workspace_membership_repository import (
    WorkspaceMembershipRepository,
)


def workspace_id_for_user(
    user: AuthUser,
    *,
    membership_repository: WorkspaceMembershipRepository | None = None,
) -> str | None:
    """Resolve and validate a user's durable workspace membership.

    Callers operating with PostgreSQL-backed authentication must pass the
    membership repository from the same lifespan-owned PostgreSQL bundle.
    The SQLite adapter remains the default for the local/test application.
    """
    settings = get_settings()
    if membership_repository is None and (
        getattr(settings, "is_production", False)
        or getattr(settings, "postgres_auth_enabled", False)
    ):
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL workspace membership repository is required",
            headers={"Retry-After": "1"},
        )
    repository = membership_repository or workspace_membership_repository
    membership = repository.snapshot(
        user_id=user.id,
        claimed_workspace_id=user.workspace_id,
    )
    stored_user = membership.user
    stored_workspace_id = stored_user.workspace_id if stored_user else None
    if (
        stored_workspace_id is not None
        and user.workspace_id is not None
        and stored_workspace_id != user.workspace_id
    ):
        raise HTTPException(status_code=403, detail="Workspace membership is invalid")
    workspace_id = stored_workspace_id or user.workspace_id
    if stored_user is not None and workspace_id is None and membership.workspace_count:
        raise HTTPException(status_code=403, detail="Workspace membership is required")
    if workspace_id is None and membership.workspace_count > 1:
        raise HTTPException(status_code=403, detail="Workspace membership is ambiguous")
    if membership_repository is not None and stored_user is None:
        raise HTTPException(status_code=403, detail="Workspace membership is required")
    if get_settings().is_production and (
        stored_user is None or workspace_id is None or not membership.resolved_workspace_exists
    ):
        raise HTTPException(status_code=403, detail="Workspace membership is required")
    return workspace_id


def project_workspace_id(project_id: str) -> str | None:
    project = project_repository.get(project_id)
    return project.workspace_id if project else None


def connector_scope_is_consistent(
    connector: AgentConnector,
    *,
    projects: ProjectRepository | None = None,
    memberships: WorkspaceMembershipRepository | None = None,
) -> bool:
    project_repository_for_scope = projects or project_repository
    membership_repository_for_scope = memberships or workspace_membership_repository
    if (
        connector.workspace_id is not None
        and membership_repository_for_scope.get_workspace(connector.workspace_id) is None
    ):
        return False
    if get_settings().is_production and connector.workspace_id is None:
        return False
    for project_id in connector.allowed_project_ids:
        project = project_repository_for_scope.get(project_id)
        if project is None:
            return False
        if connector.workspace_id is not None and project.workspace_id != connector.workspace_id:
            return False
    return True


def connector_workspace_id(connector: AgentConnector) -> str | None:
    if not connector_scope_is_consistent(connector):
        return None
    return connector.workspace_id


def mcp_child_server_scope_is_consistent(server: MCPChildServer) -> bool:
    project = project_repository.get(server.project_id)
    if project is None:
        return False
    if get_settings().is_production and server.workspace_id is None:
        return False
    return server.workspace_id is None or server.workspace_id == project.workspace_id


def mcp_child_server_workspace_id(server: MCPChildServer) -> str | None:
    if not mcp_child_server_scope_is_consistent(server):
        return None
    return server.workspace_id or project_workspace_id(server.project_id)


def external_agent_scope_is_consistent(
    agent: ExternalAgent,
    *,
    projects: ProjectRepository | None = None,
    memberships: WorkspaceMembershipRepository | None = None,
) -> bool:
    project_repository_for_scope = projects or project_repository
    membership_repository_for_scope = memberships or workspace_membership_repository
    if (
        agent.workspace_id is not None
        and membership_repository_for_scope.get_workspace(agent.workspace_id) is None
    ):
        return False
    if get_settings().is_production and agent.workspace_id is None:
        return False
    for project_id in agent.allowed_project_ids:
        project = project_repository_for_scope.get(project_id)
        if project is None or project.workspace_id != agent.workspace_id:
            return False
    return True


def approval_workspace_id(approval: ApprovalRequest) -> str | None:
    if not approval_scope_is_consistent(approval):
        return None
    return approval.workspace_id or project_workspace_id(approval.project_id)


def approval_scope_is_consistent(approval: ApprovalRequest) -> bool:
    project = project_repository.get(approval.project_id)
    if project is None:
        return False
    return approval.workspace_id is None or approval.workspace_id == project.workspace_id


def work_order_scope_is_consistent(work_order: WorkOrder) -> bool:
    project = project_repository.get(work_order.project_id)
    if project is None:
        return False
    return work_order.workspace_id is None or work_order.workspace_id == project.workspace_id


def work_order_workspace_id(work_order: WorkOrder) -> str | None:
    if not work_order_scope_is_consistent(work_order):
        return None
    return work_order.workspace_id or project_workspace_id(work_order.project_id)


def idea_scope_is_consistent(idea: Idea) -> bool:
    if not idea.project_id:
        return True
    project = project_repository.get(idea.project_id)
    if project is None:
        return False
    return idea.workspace_id is None or idea.workspace_id == project.workspace_id


def idea_workspace_id(idea: Idea) -> str | None:
    if not idea_scope_is_consistent(idea):
        return None
    return idea.workspace_id or (
        project_workspace_id(idea.project_id) if idea.project_id else None
    )


def agent_task_scope_is_consistent(task: AgentTask) -> bool:
    if not task.project_id:
        return True
    project = project_repository.get(task.project_id)
    if project is None:
        return False
    return task.workspace_id is None or task.workspace_id == project.workspace_id


def agent_task_workspace_id(task: AgentTask) -> str | None:
    if not agent_task_scope_is_consistent(task):
        return None
    return task.workspace_id or (
        project_workspace_id(task.project_id) if task.project_id else None
    )


def agent_run_scope_is_consistent(run: AgentRun) -> bool:
    task = agent_task_repository.get(run.task_id)
    if task is None or not agent_task_scope_is_consistent(task):
        return False
    task_workspace_id = agent_task_workspace_id(task)
    return run.workspace_id is None or run.workspace_id == task_workspace_id


def agent_run_workspace_id(run: AgentRun) -> str | None:
    if not agent_run_scope_is_consistent(run):
        return None
    task = agent_task_repository.get(run.task_id)
    task_workspace_id = agent_task_workspace_id(task) if task else None
    return run.workspace_id or task_workspace_id


def capability_lease_scope_is_consistent(lease: Any) -> bool:
    approval = approval_decision_repository.get(lease.approval_request_id)
    if approval is None or not approval_scope_is_consistent(approval):
        return False
    if (
        not approval.approved_payload_hash
        or lease.approved_payload_hash != approval.approved_payload_hash
    ):
        return False
    approval_scope = approval_workspace_id(approval)
    if lease.workspace_id is not None and lease.workspace_id != approval_scope:
        return False
    if lease.agent_id:
        agent = store.external_agents.get(lease.agent_id)
        if agent is None or agent.workspace_id != approval_scope:
            return False
    linked_tasks = agent_task_repository.list_for_approval(approval.id)
    if not linked_tasks or not any(
        agent_task_scope_is_consistent(task)
        and agent_task_workspace_id(task) == approval_scope
        for task in linked_tasks
    ):
        return False
    return True


def capability_lease_workspace_id(lease: Any) -> str | None:
    if not capability_lease_scope_is_consistent(lease):
        return None
    approval = approval_decision_repository.get(lease.approval_request_id)
    return lease.workspace_id or (approval_workspace_id(approval) if approval else None)


def proposed_action_scope_is_consistent(action: Any) -> bool:
    scope: str | None = action.workspace_id
    if action.project_id:
        project = project_repository.get(action.project_id)
        if project is None or (scope is not None and scope != project.workspace_id):
            return False
        scope = scope or project.workspace_id
    if action.task_id:
        task = agent_task_repository.get(action.task_id)
        if task is None or not agent_task_scope_is_consistent(task):
            return False
        task_scope = agent_task_workspace_id(task)
        if scope is not None and scope != task_scope:
            return False
        scope = scope or task_scope
    if action.run_id:
        run = agent_run_repository.get(action.run_id)
        if run is None or not agent_run_scope_is_consistent(run):
            return False
        if action.task_id is not None and run.task_id != action.task_id:
            return False
        run_scope = agent_run_workspace_id(run)
        if scope is not None and scope != run_scope:
            return False
        scope = scope or run_scope
    if action.agent_id:
        agent = store.external_agents.get(action.agent_id)
        if agent is None or (scope is not None and agent.workspace_id != scope):
            return False
    if action.lease_id:
        lease = store.capability_leases.get(action.lease_id)
        if lease is None or not capability_lease_scope_is_consistent(lease):
            return False
        lease_scope = capability_lease_workspace_id(lease)
        if scope is not None and scope != lease_scope:
            return False
        scope = scope or lease_scope
        if lease.agent_id and action.agent_id and lease.agent_id != action.agent_id:
            return False
    return True


def proposed_action_workspace_id(action: Any) -> str | None:
    if not proposed_action_scope_is_consistent(action):
        return None
    if action.workspace_id is not None:
        return action.workspace_id
    if action.project_id:
        return project_workspace_id(action.project_id)
    if action.task_id:
        task = agent_task_repository.get(action.task_id)
        return agent_task_workspace_id(task) if task else None
    if action.run_id:
        run = agent_run_repository.get(action.run_id)
        return agent_run_workspace_id(run) if run else None
    if action.lease_id:
        lease = store.capability_leases.get(action.lease_id)
        return capability_lease_workspace_id(lease) if lease else None
    return None


def reviewer_attestation_scope_is_consistent(item: Any) -> bool:
    approval = approval_decision_repository.get(item.approval_request_id)
    if approval is None or not approval_scope_is_consistent(approval):
        return False
    scope = approval_workspace_id(approval)
    return item.workspace_id is None or item.workspace_id == scope


def credential_grant_scope_is_consistent(grant: Any) -> bool:
    lease = store.capability_leases.get(grant.lease_id)
    if lease is None or not capability_lease_scope_is_consistent(lease):
        return False
    scope = capability_lease_workspace_id(lease)
    return grant.workspace_id is None or grant.workspace_id == scope


def action_authorization_scope_is_consistent(item: Any) -> bool:
    action = store.proposed_actions.get(item.action_id)
    if action is None or not proposed_action_scope_is_consistent(action):
        return False
    scope = proposed_action_workspace_id(action)
    return item.workspace_id is None or item.workspace_id == scope


def entity_workspace_id(entity_id: str, details: dict[str, Any] | None = None) -> str | None:
    project = project_repository.get(entity_id)
    if project is not None:
        return project.workspace_id
    if entity_id in store.approval_requests:
        return approval_workspace_id(store.approval_requests[entity_id])
    if entity_id in store.work_orders:
        return work_order_workspace_id(store.work_orders[entity_id])
    if entity_id in store.ideas:
        return idea_workspace_id(store.ideas[entity_id])
    task = agent_task_repository.get(entity_id)
    if task is not None:
        return agent_task_workspace_id(task)
    if entity_id in store.external_agents:
        agent = store.external_agents[entity_id]
        return agent.workspace_id if external_agent_scope_is_consistent(agent) else None
    if entity_id in store.device_registrations:
        device = store.device_registrations[entity_id]
        account = store.users.get(device.user_id or "")
        return device.workspace_id or (account.workspace_id if account else None)
    run = agent_run_repository.get(entity_id)
    if run is not None:
        return agent_run_workspace_id(run)
    if entity_id in store.agent_connectors:
        return connector_workspace_id(store.agent_connectors[entity_id])
    if entity_id in store.mcp_child_servers:
        return mcp_child_server_workspace_id(store.mcp_child_servers[entity_id])
    if entity_id in store.mcp_pending_requests:
        pending = store.mcp_pending_requests[entity_id]
        return pending.workspace_id or project_workspace_id(pending.project_id)
    if entity_id in store.capability_leases:
        return capability_lease_workspace_id(store.capability_leases[entity_id])
    if entity_id in store.proposed_actions:
        return proposed_action_workspace_id(store.proposed_actions[entity_id])
    if entity_id in store.credential_grants:
        grant = store.credential_grants[entity_id]
        if not credential_grant_scope_is_consistent(grant):
            return None
        lease = store.capability_leases.get(grant.lease_id)
        return capability_lease_workspace_id(lease) if lease else None
    if entity_id in store.reviewer_attestations:
        item = store.reviewer_attestations[entity_id]
        if not reviewer_attestation_scope_is_consistent(item):
            return None
        approval = approval_decision_repository.get(item.approval_request_id)
        return item.workspace_id or (approval_workspace_id(approval) if approval else None)
    if entity_id in store.action_authorizations:
        item = store.action_authorizations[entity_id]
        if not action_authorization_scope_is_consistent(item):
            return None
        action = store.proposed_actions.get(item.action_id)
        return item.workspace_id or (proposed_action_workspace_id(action) if action else None)
    if entity_id in store.users:
        return store.users[entity_id].workspace_id
    if entity_id in store.sessions:
        session = store.sessions[entity_id]
        account = store.users.get(session.user_id)
        return account.workspace_id if account else None
    if details:
        project_id = details.get("project_id")
        if isinstance(project_id, str):
            return project_workspace_id(project_id)
        approval_id = details.get("approval_request_id")
        if isinstance(approval_id, str):
            approval = approval_decision_repository.get(approval_id)
            if approval is not None:
                return approval_workspace_id(approval)
        task_id = details.get("agent_task_id") or details.get("task_id")
        if isinstance(task_id, str):
            task = agent_task_repository.get(task_id)
            if task is not None:
                return agent_task_workspace_id(task)
    return None


def audit_event_workspace_id(event: AuditEvent) -> str | None:
    return event.workspace_id or entity_workspace_id(event.entity_id, event.details)

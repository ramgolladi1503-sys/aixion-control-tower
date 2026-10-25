from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import agent_routes, agent_run_routes, agent_task_routes, audit_routes, connector_routes, connector_webhook, main, mcp_gateway_routes, mcp_registry_routes, mcp_transport_routes, notifications, role_routes, session_routes, trust_routes
from app.auth import RegisterRequest, create_user, require_user
from app.auth_routes import register, register_with_invite
from app.invite_routes import InviteCreateRequest, create_invite
from app.models import (
    ApprovalRequest,
    ApprovalRequestCreate,
    AgentAction,
    AgentProvider,
    AuditEvent,
    ApprovalStatus,
    AuthUser,
    AgentWorkOrderCreate,
    FileChange,
    ExternalAgent,
    Idea,
    Notification,
    MCPChildServer,
    MCPPendingRequest,
    Project,
    SessionToken,
    TestRunCreate as ApprovalTestRunPayload,
    RiskAssessment,
    RiskLevel,
    User,
    UserRole,
    Workspace,
    WorkOrder,
    WorkOrderCreate,
)
from app.settings import get_settings
from app.recovery_routes import export_recovery_snapshot
from app.agent_run_models import AgentRun, AgentRunStatus, AgentRunFaultCreate, AgentRunFaultKind
from app.agent_task_models import AgentTask
from app.agent_credential_models import AgentCreateWithCredentialPolicy
from app.trust_models import (
    CapabilityLease,
    CapabilityLeaseMode,
    CapabilityScope,
    CapabilityActionType,
    CapabilityLeaseCreate,
    CredentialGrant,
    CredentialGrantType,
    ProposedAction,
    ReviewerAttestationCreate,
    ReviewerAttestationDecision,
)
from app.store import store
from app.workspace_access import agent_task_workspace_id, entity_workspace_id, project_workspace_id


def setup_function() -> None:
    store.reset()


def _workspace(name: str) -> Workspace:
    workspace = Workspace(name=name, owner_user_id=f"owner-{name}")
    store.workspaces[workspace.id] = workspace
    store.persist_records(inserts=[("workspace", workspace.id, workspace)])
    return workspace


def _user(workspace: Workspace) -> AuthUser:
    user = AuthUser(
        id=f"reviewer-{workspace.id}",
        email=f"{workspace.name}@example.test",
        display_name=workspace.name,
        role=UserRole.REVIEWER,
        workspace_id=workspace.id,
    )
    store.users[user.id] = User(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        password_hash="test-hash",
        password_salt="test-salt",
        workspace_id=workspace.id,
    )
    with store._lock, store._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            ("user", user.id, store.users[user.id].model_dump_json()),
        )
    return user


def _persist_projects(*projects: Project) -> None:
    with store._lock, store._connect() as conn:
        for project in projects:
            conn.execute(
                "INSERT OR REPLACE INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
                ("project", project.id, project.model_dump_json()),
            )


def test_project_scope_resolution_uses_durable_row_when_cache_is_empty() -> None:
    workspace = _workspace("durable-project-scope")
    project = Project(name="Persisted scope", workspace_id=workspace.id)
    store.projects[project.id] = project
    _persist_projects(project)
    store.projects.clear()

    assert project_workspace_id(project.id) == workspace.id


def test_agent_task_scope_resolution_uses_durable_row_when_cache_is_empty() -> None:
    workspace = _workspace("durable-task-scope")
    project = Project(name="Task scope project", workspace_id=workspace.id)
    task = AgentTask(
        project_id=project.id,
        workspace_id=workspace.id,
        title="Persisted task scope",
        goal="Resolve task scope from durable storage",
    )
    store.projects[project.id] = project
    _persist_projects(project)
    store.agent_tasks[task.id] = task
    store.persist_records(inserts=[("agent_task", task.id, task)])
    store.agent_tasks.clear()

    assert agent_task_workspace_id(task) == workspace.id


def test_agent_run_entity_scope_uses_durable_run_when_cache_is_empty() -> None:
    workspace = _workspace("durable-run-scope")
    project = Project(name="Run scope project", workspace_id=workspace.id)
    task = AgentTask(
        project_id=project.id,
        workspace_id=workspace.id,
        title="Run parent task",
        goal="Resolve durable run scope",
    )
    run = AgentRun(
        id="run_durable_scope_test",
        task_id=task.id,
        project_id=project.id,
        workspace_id=workspace.id,
    )
    store.projects[project.id] = project
    _persist_projects(project)
    store.persist_records(inserts=[("agent_task", task.id, task)])
    store.agent_runs[run.id] = run
    store.persist_records(inserts=[("agent_run", run.id, run)])
    store.agent_runs.clear()

    assert entity_workspace_id(run.id) == workspace.id


def _approval(project: Project) -> ApprovalRequest:
    approval = ApprovalRequest(
        project_id=project.id,
        workspace_id=project.workspace_id,
        title=f"Approval for {project.name}",
        summary="Workspace isolation case",
        agent_name="test-agent",
        target_branch="feature/workspace-scope",
        files=[FileChange(path="README.md", diff="+safe change")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    store.approval_requests[approval.id] = approval
    store.persist()
    return approval


def test_human_approval_creation_commits_scoped_audit_and_notification() -> None:
    workspace = _workspace("human-approval")
    project = Project(name="Human approval project", workspace_id=workspace.id)
    store.projects[project.id] = project
    _persist_projects(project)
    store.persist()
    payload = ApprovalRequestCreate(
        project_id=project.id,
        title="Review a bounded change",
        summary="A human-created approval must be durably recorded.",
        target_branch="feature/bounded-change",
        files=[FileChange(path="README.md", diff="+safe change")],
    )

    request = main.create_approval_request(payload, _user(workspace))

    durable_request = main.approval_decision_repository.get(request.id)
    assert durable_request == request
    assert any(
        event.entity_id == request.id
        and event.event_type == "approval.created"
        and event.workspace_id == workspace.id
        for event in store.audit_events
    )
    assert any(
        item.entity_id == request.id
        and item.entity_type == "approval_request"
        and item.workspace_id == workspace.id
        for item in store.notifications.values()
    )


def test_human_approval_resolves_linked_work_order_after_cache_is_cleared() -> None:
    workspace = _workspace("approval-durable-work-order")
    project = Project(name="Durable WorkOrder project", workspace_id=workspace.id)
    store.projects[project.id] = project
    _persist_projects(project)
    user = _user(workspace)
    work_order = main.create_work_order(
        WorkOrderCreate(project_id=project.id, goal="Persist approval context"), user
    )
    store.work_orders.clear()
    payload = ApprovalRequestCreate(
        project_id=project.id,
        work_order_id=work_order.id,
        title="Approval with durable context",
        summary="Linked WorkOrder lookup must not depend on the process cache.",
        target_branch="feature/durable-work-order",
        files=[FileChange(path="README.md", diff="+durable lookup")],
    )

    request = main.create_approval_request(payload, user)

    assert request.work_order_id == work_order.id
    assert store.work_orders == {}


def test_human_approval_decision_audit_is_bound_to_workspace() -> None:
    workspace = _workspace("decision-audit-scope")
    project = Project(name="Decision audit project", workspace_id=workspace.id)
    store.projects[project.id] = project
    _persist_projects(project)
    approval = _approval(project)

    decided = main.decide_approval(
        approval.id,
        main.DecisionCreate(decision="approve"),
        _user(workspace),
    )
    audit_event = main.approval_decision_repository.latest_decision_audit(
        approval_id=approval.id,
        workspace_id=workspace.id,
        new_status=ApprovalStatus.APPROVED,
    )

    assert decided.status == ApprovalStatus.APPROVED
    assert audit_event is not None
    assert audit_event.workspace_id == workspace.id


def test_first_created_user_gets_a_workspace_and_invited_user_inherits_it() -> None:
    owner = create_user("owner@example.com", "ValidPassword123!", "Owner")
    assert owner.workspace_id is not None
    assert owner.workspace_id in store.workspaces
    assert store.workspaces[owner.workspace_id].owner_user_id == owner.id

    invite = create_invite(
        InviteCreateRequest(email="reviewer@example.com"),
        AuthUser(
            id=owner.id,
            email=owner.email,
            display_name=owner.display_name,
            role=owner.role,
            workspace_id=owner.workspace_id,
        ),
    )
    response = register_with_invite(
        RegisterRequest(
            email="reviewer@example.com",
            password="ValidPassword123!",
            display_name="Reviewer",
            invite_token=invite.token,
        )
    )

    invited = store.users[response.user.id]
    assert invite.id in store.invites
    assert store.invites[invite.id].workspace_id == owner.workspace_id
    assert invited.workspace_id == owner.workspace_id


def test_concurrent_first_registration_creates_one_owner_workspace() -> None:
    barrier = Barrier(2)

    def register(email: str) -> str:
        barrier.wait(timeout=5)
        try:
            create_user(email, "ValidPassword123!", email.split("@")[0])
        except HTTPException as error:
            return f"rejected:{error.status_code}"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(register, ("first@example.com", "second@example.com"))
        )

    assert sorted(results) == ["created", "created"]
    assert len(store.users) == 2
    assert len(store.workspaces) == 1
    assert {user.workspace_id for user in store.users.values()} == set(store.workspaces)
    assert sum(user.role == UserRole.OWNER for user in store.users.values()) == 1


def test_concurrent_public_registration_creates_isolated_owner_workspaces() -> None:
    barrier = Barrier(2)

    def register_without_invite(email: str) -> str:
        barrier.wait(timeout=5)
        try:
            register(
                RegisterRequest(
                    email=email,
                    password="ValidPassword123!",
                    display_name=email.split("@")[0],
                ),
                Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/auth/register",
                        "headers": [],
                        "client": ("127.0.0.1", 12345),
                    }
                ),
            )
        except HTTPException as error:
            return f"rejected:{error.status_code}"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                register_without_invite,
                ("bootstrap-a@example.com", "bootstrap-b@example.com"),
            )
        )

    assert sorted(results) == ["created", "created"]
    assert len(store.users) == 2
    assert len(store.workspaces) == 2
    owners = list(store.users.values())
    assert len(owners) == 2
    assert all(owner.role == UserRole.OWNER for owner in owners)
    assert {owner.workspace_id for owner in owners} == set(store.workspaces)
    assert {
        workspace.owner_user_id: workspace_id
        for workspace_id, workspace in store.workspaces.items()
    } == {owner.id: owner.workspace_id for owner in owners}


def test_user_creation_fails_closed_when_workspace_membership_is_ambiguous() -> None:
    workspace_a = _workspace("alpha")
    _workspace("bravo")
    create_user(
        "initial@example.com",
        "ValidPassword123!",
        "Initial",
        workspace_id=workspace_a.id,
    )

    with pytest.raises(HTTPException) as exc_info:
        create_user("unassigned@example.com", "ValidPassword123!", "Unassigned")

    assert exc_info.value.status_code == 403
    assert "workspace membership" in exc_info.value.detail.lower()
    assert "unassigned@example.com" not in {user.email for user in store.users.values()}


def test_workspace_user_lists_only_own_projects_and_approvals() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_a = Project(name="Alpha Project", workspace_id=workspace_a.id)
    project_b = Project(name="Bravo Project", workspace_id=workspace_b.id)
    store.projects.update({project_a.id: project_a, project_b.id: project_b})
    _persist_projects(project_a, project_b)
    approval_a = _approval(project_a)
    approval_b = _approval(project_b)
    user_b = _user(workspace_b)
    store.approval_requests.clear()

    assert [item.id for item in main.list_projects(user_b)] == [project_b.id]
    listed_approvals = main.list_approval_requests(user_b)
    assert [item.id for item in listed_approvals] == [approval_b.id]
    assert listed_approvals[0].workspace_id == workspace_b.id
    assert listed_approvals[0].expires_at == approval_b.created_at + timedelta(
        seconds=get_settings().agent_approval_ttl_seconds
    )
    assert main.list_grouped_approval_requests(user_b).action_required == listed_approvals
    assert approval_a.id not in {item.id for item in main.list_approval_requests(user_b)}


def test_approval_detail_reads_from_repository_when_cache_is_empty() -> None:
    workspace = _workspace("durable-detail")
    project = Project(name="Durable detail project", workspace_id=workspace.id)
    store.projects[project.id] = project
    _persist_projects(project)
    approval = _approval(project)
    store.approval_requests.clear()

    result = main.get_approval_request(approval.id, _user(workspace))

    assert result.id == approval.id
    assert result.expires_at == approval.created_at + timedelta(
        seconds=get_settings().agent_approval_ttl_seconds
    )
    assert store.approval_requests[approval.id] == result


def test_test_run_creation_resolves_approval_from_repository_when_cache_is_empty(
    monkeypatch,
) -> None:
    workspace = _workspace("test-run-durable-approval")
    project = Project(name="Test run project", workspace_id=workspace.id)
    store.projects[project.id] = project
    _persist_projects(project)
    approval = _approval(project)
    store.approval_requests.clear()
    original_get = main.approval_decision_repository.get
    resolved_ids = []

    def observed_get(approval_id: str):
        resolved_ids.append(approval_id)
        return original_get(approval_id)

    monkeypatch.setattr(main.approval_decision_repository, "get", observed_get)

    test_run = main.create_test_run(
        ApprovalTestRunPayload(
            approval_request_id=approval.id,
            command="python -m pytest",
            status="PASSED",
            output_summary="Validation passed.",
        ),
        _user(workspace),
    )

    assert test_run.approval_request_id == approval.id
    assert approval.id in resolved_ids


def test_production_approval_list_requires_workspace_membership(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    user = AuthUser(
        id="unscoped-reviewer",
        email="reviewer@example.test",
        display_name="Unscoped reviewer",
        role=UserRole.REVIEWER,
    )

    with pytest.raises(HTTPException) as error:
        main.list_approval_requests(user)

    # Production has no durable membership repository in this direct call,
    # so authorization must fail as an unavailable trust dependency rather
    # than falling back to the process-local SQLite membership adapter.
    assert error.value.status_code == 503


def test_local_auth_bypass_is_not_a_cross_workspace_principal(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "false")
    workspace_a = _workspace("alpha")
    local_user = require_user(None)
    assert local_user.workspace_id == workspace_a.id

    _workspace("bravo")
    ambiguous_local_user = require_user(None)
    assert ambiguous_local_user.workspace_id is None


def test_connectors_and_connector_project_scopes_are_workspace_bound() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_a = Project(name="Alpha project", workspace_id=workspace_a.id)
    project_b = Project(name="Bravo project", workspace_id=workspace_b.id)
    store.projects.update({project_a.id: project_a, project_b.id: project_b})
    _persist_projects(project_a, project_b)
    owner_a = AuthUser(id="owner-a", email="a@test", display_name="A", role=UserRole.OWNER, workspace_id=workspace_a.id)
    owner_b = AuthUser(id="owner-b", email="b@test", display_name="B", role=UserRole.OWNER, workspace_id=workspace_b.id)
    store.users[owner_a.id] = User(id=owner_a.id, email=owner_a.email, role=owner_a.role, password_hash="", password_salt="", workspace_id=workspace_a.id)
    store.users[owner_b.id] = User(id=owner_b.id, email=owner_b.email, role=owner_b.role, password_hash="", password_salt="", workspace_id=workspace_b.id)

    created = connector_routes.create_connector(
        connector_routes.ConnectorCreate(name="A connector", allowed_project_ids=[project_a.id]), owner_a
    )
    assert created.id in store.agent_connectors
    assert store.agent_connectors[created.id].workspace_id == workspace_a.id
    with pytest.raises(HTTPException) as error:
        connector_routes.get_connector(created.id, owner_b)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        connector_routes.create_connector(
            connector_routes.ConnectorCreate(name="Bad scope", allowed_project_ids=[project_b.id]), owner_a
        )
    assert error.value.status_code == 404


def test_mcp_registry_and_pending_requests_are_workspace_bound() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_a = Project(name="Alpha project", workspace_id=workspace_a.id)
    project_b = Project(name="Bravo project", workspace_id=workspace_b.id)
    store.projects.update({project_a.id: project_a, project_b.id: project_b})
    _persist_projects(project_a, project_b)
    user_b = _user(workspace_b)
    server = MCPChildServer(project_id=project_a.id, name="private server", workspace_id=workspace_a.id)
    pending = MCPPendingRequest(project_id=project_a.id, workspace_id=workspace_a.id, approval_request_id="appr_a", server_name="private", tool_name="write")
    store.mcp_child_servers[server.id] = server
    store.mcp_pending_requests[pending.id] = pending

    assert mcp_registry_routes.list_child_servers(user=user_b) == []
    with pytest.raises(HTTPException) as error:
        mcp_registry_routes.get_child_server(server.id, user_b)
    assert error.value.status_code == 404
    assert mcp_gateway_routes.list_pending_gateway_requests(user=user_b) == []
    with pytest.raises(HTTPException) as error:
        mcp_gateway_routes.get_pending_gateway_request(pending.id, user_b)
    assert error.value.status_code == 404


def test_integrations_with_conflicting_project_workspaces_are_quarantined() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_b = Project(name="Bravo project", workspace_id=workspace_b.id)
    store.projects[project_b.id] = project_b
    _persist_projects(project_b)
    connector = connector_routes.AgentConnector(
        name="Corrupt connector",
        workspace_id=workspace_a.id,
        allowed_project_ids=[project_b.id],
        allowed_actions=[AgentAction.APPEND_AGENT_TASK_EVENT],
    )
    task_connector = connector_routes.AgentConnector(
        name="Task connector",
        workspace_id=workspace_a.id,
        allowed_actions=[AgentAction.APPEND_AGENT_TASK_EVENT],
    )
    external_agent = ExternalAgent(
        provider=AgentProvider.CODEX,
        display_name="Corrupt external agent",
        workspace_id=workspace_a.id,
        allowed_project_ids=[project_b.id],
        allowed_actions=[AgentAction.CREATE_APPROVAL],
    )
    server = MCPChildServer(
        project_id=project_b.id,
        name="Corrupt server",
        workspace_id=workspace_a.id,
    )
    task = AgentTask(
        provider=AgentProvider.MANUAL,
        project_id=project_b.id,
        title="Conflicting task",
        goal="Must not accept connector events",
        workspace_id=workspace_a.id,
        external_agent_id=task_connector.id,
    )
    original_task = task.model_copy(deep=True)
    store.agent_connectors[connector.id] = connector
    store.agent_connectors[task_connector.id] = task_connector
    store.external_agents[external_agent.id] = external_agent
    store.mcp_child_servers[server.id] = server
    store.agent_tasks[task.id] = task

    owner_a = AuthUser(
        id="owner-a",
        email="a@example.test",
        display_name="A",
        role=UserRole.OWNER,
        workspace_id=workspace_a.id,
    )
    owner_b = AuthUser(
        id="owner-b",
        email="b@example.test",
        display_name="B",
        role=UserRole.OWNER,
        workspace_id=workspace_b.id,
    )
    for owner in (owner_a, owner_b):
        listed_connectors = connector_routes.list_connectors(owner)
        listed_ids = {item.id for item in listed_connectors}
        assert connector.id not in listed_ids
        assert (task_connector.id in listed_ids) == (owner.workspace_id == workspace_a.id)
        assert agent_routes.list_agents(owner) == []
        with pytest.raises(HTTPException) as agent_error:
            agent_routes.get_agent(external_agent.id, owner)
        assert agent_error.value.status_code == 404
        with pytest.raises(HTTPException) as connector_error:
            connector_routes.get_connector(connector.id, owner)
        assert connector_error.value.status_code == 404
        with pytest.raises(HTTPException) as server_error:
            mcp_registry_routes.get_child_server(server.id, owner)
        assert server_error.value.status_code == 404

    assert entity_workspace_id(external_agent.id) is None
    with pytest.raises(HTTPException) as agent_scope_error:
        agent_routes.assert_agent_can(
            external_agent,
            AgentAction.CREATE_APPROVAL,
            project_id=project_b.id,
        )
    assert agent_scope_error.value.status_code == 403

    with pytest.raises(HTTPException) as event_error:
        connector_webhook._append_task_event_from_connector(
            task_connector,
            connector_webhook.ConnectorWebhookPayload(
                action=AgentAction.APPEND_AGENT_TASK_EVENT,
                task_id=task.id,
                message="Must be rejected",
            ),
        )
    assert event_error.value.status_code == 404
    assert store.agent_tasks[task.id] == original_task
    assert not store.agent_task_events


def test_mcp_pending_request_with_conflicting_explicit_workspace_is_quarantined() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_b = Project(name="Bravo project", workspace_id=workspace_b.id)
    store.projects[project_b.id] = project_b
    _persist_projects(project_b)
    pending = MCPPendingRequest(
        project_id=project_b.id,
        workspace_id=workspace_a.id,
        approval_request_id="approval_conflicting_scope",
        server_name="private-server",
        tool_name="write",
        arguments={"payload": "private"},
    )
    store.mcp_pending_requests[pending.id] = pending

    assert mcp_gateway_routes.list_pending_gateway_requests(user=_user(workspace_a)) == []
    assert mcp_gateway_routes.list_pending_gateway_requests(user=_user(workspace_b)) == []
    assert mcp_gateway_routes.pending_gateway_health(user=_user(workspace_b)).total == 0

    for user in (_user(workspace_a), _user(workspace_b)):
        with pytest.raises(HTTPException) as read_error:
            mcp_gateway_routes.get_pending_gateway_request(pending.id, user)
        assert read_error.value.status_code == 404
        with pytest.raises(HTTPException) as retry_error:
            mcp_gateway_routes.retry_pending_gateway_request(pending.id, user=user)
        assert retry_error.value.status_code == 404

    assert store.mcp_pending_requests[pending.id] == pending


def test_mcp_transport_denies_cross_workspace_project_before_protocol_response() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_a = Project(name="Alpha project", workspace_id=workspace_a.id)
    store.projects[project_a.id] = project_a
    _persist_projects(project_a)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            mcp_transport_routes.handle_mcp_jsonrpc(
                project_a.id,
                mcp_transport_routes.MCPJsonRpcRequest(method="tools/list"),
                _user(workspace_b),
            )
        )
    assert error.value.status_code == 404


def test_agent_runs_and_capability_leases_are_workspace_bound() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_a = Project(name="Alpha project", workspace_id=workspace_a.id)
    project_b = Project(name="Bravo project", workspace_id=workspace_b.id)
    store.projects.update({project_a.id: project_a, project_b.id: project_b})
    _persist_projects(project_a, project_b)
    task = AgentTask(
        provider=AgentProvider.MANUAL,
        project_id=project_a.id,
        title="Private task",
        goal="Private goal",
        workspace_id=workspace_a.id,
    )
    run = AgentRun(
        task_id=task.id,
        workspace_id=workspace_a.id,
        status=AgentRunStatus.FAILED,
        lease_owner="stale-worker",
        lease_token="stale-lease-token",
        lease_expires_at=main.now_utc() - timedelta(minutes=1),
    )
    store.agent_tasks[task.id] = task
    store.agent_runs[run.id] = run
    user_b = _user(workspace_b)
    with pytest.raises(HTTPException) as error:
        agent_run_routes.get_run(run.id, user_b)
    assert error.value.status_code == 404

    lease = CapabilityLease(
        approval_request_id="approval-private",
        workspace_id=workspace_a.id,
        approved_payload_hash="hash",
        mode=CapabilityLeaseMode.STRICT,
        scope=CapabilityScope(repository="org/repo", branch="feature/a", allowed_actions=[CapabilityActionType.READ_REPOSITORY]),
        issued_by_user_id="owner-a",
        expires_at=main.now_utc() + timedelta(minutes=5),
        receipt_nonce="nonce",
        receipt_signature="signature",
    )
    store.capability_leases[lease.id] = lease
    with pytest.raises(HTTPException) as error:
        trust_routes.get_lease(lease.id, user_b)
    assert error.value.status_code == 404


@pytest.mark.parametrize("operation", ["read", "decide"])
def test_workspace_user_cannot_read_or_decide_another_workspaces_approval(operation: str) -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_a = Project(name="Alpha Project", workspace_id=workspace_a.id)
    store.projects[project_a.id] = project_a
    _persist_projects(project_a)
    approval_a = _approval(project_a)
    user_b = _user(workspace_b)

    with pytest.raises(HTTPException) as error:
        if operation == "read":
            main.get_approval_request(approval_a.id, user_b)
        else:
            main.decide_approval(
                approval_a.id,
                main.DecisionCreate(decision="approve"),
                user_b,
            )

    assert error.value.status_code == 404
    assert approval_a.status == ApprovalStatus.REQUESTED


def test_approval_with_conflicting_project_workspace_is_quarantined() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_b = Project(name="Bravo project", workspace_id=workspace_b.id)
    store.projects[project_b.id] = project_b
    _persist_projects(project_b)
    approval = _approval(project_b).model_copy(update={"workspace_id": workspace_a.id})
    store.approval_requests[approval.id] = approval
    store.persist()
    lease_payload = CapabilityLeaseCreate(
        approval_request_id=approval.id,
        scope=CapabilityScope(
            repository="owner/repo",
            branch="feature/scope-check",
            allowed_actions=[CapabilityActionType.MODIFY_FILES],
        ),
    )
    attestation_payload = ReviewerAttestationCreate(
        approval_request_id=approval.id,
        decision=ReviewerAttestationDecision.APPROVE,
    )

    for user in (_user(workspace_a), _user(workspace_b)):
        assert main.list_approval_requests(user=user) == []
        with pytest.raises(HTTPException) as approval_error:
            main.get_approval_request(approval.id, user)
        assert approval_error.value.status_code == 404
        with pytest.raises(HTTPException) as decision_error:
            main.decide_approval(
                approval.id,
                main.DecisionCreate(decision="approve"),
                user,
            )
        assert decision_error.value.status_code == 404
        with pytest.raises(HTTPException) as attestation_error:
            trust_routes.create_attestation(attestation_payload, user)
        assert attestation_error.value.status_code == 404
        with pytest.raises(HTTPException) as lease_error:
            trust_routes.create_lease(lease_payload, user)
        assert lease_error.value.status_code == 404
        with pytest.raises(HTTPException) as resolve_error:
            mcp_gateway_routes.resolve_gateway_request(approval.id, user)
        assert resolve_error.value.status_code == 404

    assert approval.id not in store.approval_requests
    assert approval.status == ApprovalStatus.REQUESTED


def test_ideas_and_work_orders_with_conflicting_project_workspace_are_quarantined() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_b = Project(name="Bravo project", workspace_id=workspace_b.id)
    store.projects[project_b.id] = project_b
    _persist_projects(project_b)
    idea = Idea(
        project_id=project_b.id,
        title="Conflicting idea",
        raw_text="Must not cross workspace boundary",
        workspace_id=workspace_a.id,
    )
    work_order = WorkOrder(
        project_id=project_b.id,
        goal="Conflicting work order",
        workspace_id=workspace_a.id,
    )
    store.ideas[idea.id] = idea
    store.work_orders[work_order.id] = work_order

    for user in (_user(workspace_a), _user(workspace_b)):
        assert main.list_ideas(user=user) == []
        assert main.list_work_orders(user=user) == []
        snapshot = export_recovery_snapshot(user)
        assert snapshot.counts["ideas"] == 0
        assert snapshot.counts["work_orders"] == 0

    assert store.ideas[idea.id] == idea
    assert store.work_orders[work_order.id] == work_order


def test_external_agent_cannot_create_approval_for_another_workspace() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_a = Project(name="Alpha Project", workspace_id=workspace_a.id)
    store.projects[project_a.id] = project_a
    _persist_projects(project_a)
    agent_b = ExternalAgent(
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Bravo Agent",
        workspace_id=workspace_b.id,
        allowed_project_ids=[project_a.id],
        allowed_actions=[AgentAction.CREATE_APPROVAL],
    )
    payload = ApprovalRequestCreate(
        project_id=project_a.id,
        title="Cross-workspace attempt",
        summary="Must be rejected",
        target_branch="main",
        files=[FileChange(path="README.md", diff="+change")],
        source_invocation_id="cross-workspace-invocation",
    )

    with pytest.raises(HTTPException) as error:
        agent_routes.create_agent_approval(payload, agent_b)

    assert error.value.status_code == 403
    assert store.approval_requests == {}


@pytest.mark.parametrize("path", ["human", "agent_task", "external_agent"])
def test_approval_rejects_work_order_from_different_project_in_same_workspace(path: str) -> None:
    workspace = _workspace("shared")
    project = Project(name="Approval project", workspace_id=workspace.id)
    other_project = Project(name="Work order project", workspace_id=workspace.id)
    store.projects.update({project.id: project, other_project.id: other_project})
    _persist_projects(project, other_project)
    work_order = WorkOrder(
        project_id=other_project.id,
        goal="Different parent project",
        workspace_id=workspace.id,
    )
    store.work_orders[work_order.id] = work_order
    payload = ApprovalRequestCreate(
        project_id=project.id,
        work_order_id=work_order.id,
        title="Mismatched parent",
        summary="Same workspace must not hide a broken project link.",
        target_branch="feature/mismatched-parent",
        files=[FileChange(path="README.md", diff="+change")],
        source_invocation_id=("mismatched-parent-invocation" if path == "external_agent" else None),
    )

    with pytest.raises(HTTPException) as error:
        if path == "human":
            main.create_approval_request(payload, _user(workspace))
        elif path == "agent_task":
            task = AgentTask(
                provider=AgentProvider.MANUAL,
                project_id=project.id,
                title="Task",
                goal="Request approval",
                workspace_id=workspace.id,
            )
            store.agent_tasks[task.id] = task
            owner = AuthUser(
                id="owner",
                email="owner@example.test",
                display_name="Owner",
                role=UserRole.OWNER,
                workspace_id=workspace.id,
            )
            agent_task_routes.create_agent_task_approval(task.id, payload, owner)
        else:
            external_agent = ExternalAgent(
                provider=AgentProvider.ANTIGRAVITY,
                display_name="Scoped agent",
                workspace_id=workspace.id,
                allowed_project_ids=[project.id],
                allowed_actions=[AgentAction.CREATE_APPROVAL],
            )
            agent_routes.create_agent_approval(payload, external_agent)

    assert error.value.status_code == 404
    assert store.approval_requests == {}


@pytest.mark.parametrize("path", ["human", "external_agent"])
def test_work_order_rejects_idea_from_different_project_in_same_workspace(path: str) -> None:
    workspace = _workspace("shared")
    project = Project(name="Work order project", workspace_id=workspace.id)
    other_project = Project(name="Idea project", workspace_id=workspace.id)
    store.projects.update({project.id: project, other_project.id: other_project})
    _persist_projects(project, other_project)
    idea = Idea(
        project_id=other_project.id,
        workspace_id=workspace.id,
        title="Foreign project idea",
        raw_text="This parent mismatch must be rejected.",
    )
    store.ideas[idea.id] = idea

    with pytest.raises(HTTPException) as error:
        if path == "human":
            main.create_work_order(
                WorkOrderCreate(
                    project_id=project.id,
                    idea_id=idea.id,
                    goal="Attempt cross-project link",
                ),
                _user(workspace),
            )
        else:
            external_agent = ExternalAgent(
                provider=AgentProvider.ANTIGRAVITY,
                display_name="Scoped agent",
                workspace_id=workspace.id,
                allowed_project_ids=[project.id],
                allowed_actions=[AgentAction.CREATE_WORK_ORDER],
            )
            agent_routes.create_agent_work_order(
                AgentWorkOrderCreate(
                    project_id=project.id,
                    idea_id=idea.id,
                    goal="Attempt cross-project link",
                ),
                external_agent,
            )

    assert error.value.status_code == 404
    assert store.work_orders == {}


def test_workspace_notification_inbox_and_read_action_are_scoped() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    notification_a = Notification(
        title="Alpha approval",
        body="Review requested",
        entity_type="approval_request",
        entity_id="approval-alpha",
        workspace_id=workspace_a.id,
    )
    notification_b = Notification(
        title="Bravo approval",
        body="Review requested",
        entity_type="approval_request",
        entity_id="approval-bravo",
        workspace_id=workspace_b.id,
    )
    user_b = _user(workspace_b)
    improperly_routed = Notification(
        title="Wrong workspace",
        body="Must remain isolated even when user ID matches",
        entity_type="approval_request",
        entity_id="approval-alpha-private",
        workspace_id=workspace_a.id,
        user_id=user_b.id,
    )
    store.notifications.update(
        {
            notification_a.id: notification_a,
            notification_b.id: notification_b,
            improperly_routed.id: improperly_routed,
        }
    )

    assert notifications.list_notifications(user_b) == [notification_b]
    with pytest.raises(HTTPException) as error:
        notifications.mark_notification_read(notification_a.id, user_b)
    assert error.value.status_code == 404
    assert notification_a.status.value == "UNREAD"
    with pytest.raises(HTTPException) as wrong_workspace_error:
        notifications.mark_notification_read(improperly_routed.id, user_b)
    assert wrong_workspace_error.value.status_code == 404


def test_audit_queries_and_direct_api_are_workspace_scoped() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_a = Project(name="Alpha Project", workspace_id=workspace_a.id)
    project_b = Project(name="Bravo Project", workspace_id=workspace_b.id)
    store.projects.update({project_a.id: project_a, project_b.id: project_b})
    _persist_projects(project_a, project_b)
    approval_a = _approval(project_a)
    approval_b = _approval(project_b)
    event_a = AuditEvent(
        event_type="approval.created",
        entity_id=approval_a.id,
        details={"project_id": project_a.id},
    )
    event_b = AuditEvent(
        event_type="approval.created",
        entity_id=approval_b.id,
        details={"project_id": project_b.id},
    )
    store.audit_events.extend([event_a, event_b])
    store.persist()
    user_b = _user(workspace_b)

    events = audit_routes.query_audit_events(
        event_type=None,
        entity_id=None,
        actor=None,
        detail_key=None,
        detail_value=None,
        limit=100,
        request=None,  # type: ignore[arg-type]
        user=user_b,
    )
    assert events == [event_b]
    assert main.list_audit_events(user_b) == [event_b]
    with pytest.raises(HTTPException) as error:
        audit_routes.get_audit_event(event_a.id, None, user_b)  # type: ignore[arg-type]
    assert error.value.status_code == 404

    snapshot = export_recovery_snapshot(user_b)
    assert snapshot.counts["projects"] == 1
    assert snapshot.counts["approval_requests"] == 1
    assert [item["id"] for item in snapshot.entities["projects"]] == [project_b.id]
    assert [item["id"] for item in snapshot.entities["approval_requests"]] == [approval_b.id]


def test_account_user_management_sessions_and_devices_are_workspace_bound() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    owner_a = User(id="owner-a", email="a@test", display_name="A", role=UserRole.OWNER, password_hash="", password_salt="", workspace_id=workspace_a.id)
    reviewer_a = User(id="reviewer-a", email="reviewer-a@test", display_name="Reviewer A", role=UserRole.REVIEWER, password_hash="", password_salt="", workspace_id=workspace_a.id)
    reviewer_b = User(id="reviewer-b", email="reviewer-b@test", display_name="Reviewer B", role=UserRole.REVIEWER, password_hash="", password_salt="", workspace_id=workspace_b.id)
    store.users.update({item.id: item for item in (owner_a, reviewer_a, reviewer_b)})
    store.sessions["session-a"] = SessionToken(user_id=reviewer_a.id, token_hash="a", expires_at=main.now_utc() + timedelta(minutes=5))
    store.sessions["session-b"] = SessionToken(user_id=reviewer_b.id, token_hash="b", expires_at=main.now_utc() + timedelta(minutes=5))
    actor = AuthUser(id=owner_a.id, email=owner_a.email, display_name=owner_a.display_name, role=owner_a.role, workspace_id=workspace_a.id)

    assert {item.id for item in role_routes.list_users(actor)} == {owner_a.id, reviewer_a.id}
    assert {item.user_id for item in session_routes.list_sessions(actor)} == {reviewer_a.id}
    with pytest.raises(HTTPException) as error:
        role_routes.update_user_role(reviewer_b.id, role_routes.RoleUpdateRequest(role=UserRole.MAINTAINER), actor)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        session_routes.revoke_user_sessions(reviewer_b.id, actor)
    assert error.value.status_code == 404

    registered = notifications.register_device(notifications.DeviceRegistrationRequest(token="token-a"), AuthUser(id=reviewer_a.id, email=reviewer_a.email, display_name=reviewer_a.display_name, role=reviewer_a.role, workspace_id=workspace_a.id))
    assert registered.workspace_id == workspace_a.id


def test_ambiguous_local_principal_cannot_list_workspace_users(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "false")
    _workspace("alpha")
    _workspace("bravo")
    local_user = require_user(None)
    with pytest.raises(HTTPException) as error:
        role_routes.list_users(local_user)
    assert error.value.status_code == 403


def test_unassigned_stored_account_cannot_become_global_reader() -> None:
    _workspace("alpha")
    unassigned = User(id="unassigned", email="orphan@test", password_hash="", password_salt="")
    store.users[unassigned.id] = unassigned
    store.persist()
    actor = AuthUser(id=unassigned.id, email=unassigned.email, display_name="Orphan", role=UserRole.OWNER)
    with pytest.raises(HTTPException) as error:
        role_routes.list_users(actor)
    assert error.value.status_code == 403


def test_external_agent_management_and_project_scopes_are_workspace_bound() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_a = Project(name="Alpha project", workspace_id=workspace_a.id)
    project_b = Project(name="Bravo project", workspace_id=workspace_b.id)
    store.projects.update({project_a.id: project_a, project_b.id: project_b})
    _persist_projects(project_a, project_b)
    owner_a = User(id="owner-a", email="a@test", display_name="A", role=UserRole.OWNER, password_hash="", password_salt="", workspace_id=workspace_a.id)
    owner_b = User(id="owner-b", email="b@test", display_name="B", role=UserRole.OWNER, password_hash="", password_salt="", workspace_id=workspace_b.id)
    store.users.update({owner_a.id: owner_a, owner_b.id: owner_b})
    auth_a = AuthUser(id=owner_a.id, email=owner_a.email, display_name="A", role=UserRole.OWNER, workspace_id=workspace_a.id)
    auth_b = AuthUser(id=owner_b.id, email=owner_b.email, display_name="B", role=UserRole.OWNER, workspace_id=workspace_b.id)

    created = agent_routes.create_agent(
        AgentCreateWithCredentialPolicy(provider=AgentProvider.CODEX, display_name="A agent", allowed_project_ids=[project_a.id]),
        auth_a,
    )
    assert [item.id for item in agent_routes.list_agents(auth_a)] == [created.agent.id]
    assert agent_routes.list_agents(auth_b) == []
    with pytest.raises(HTTPException) as error:
        agent_routes.get_agent(created.agent.id, auth_b)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        agent_routes.get_agent_credentials(created.agent.id, auth_b)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        agent_routes.create_agent(
            AgentCreateWithCredentialPolicy(provider=AgentProvider.CODEX, display_name="bad", allowed_project_ids=[project_b.id]),
            auth_a,
        )
    assert error.value.status_code == 404


def test_run_and_trust_metrics_and_fault_controls_are_workspace_scoped() -> None:
    from app.agent_run_metrics import build_agent_run_summary, prometheus_agent_run_metrics
    from app.trust_metrics import prometheus_trust_metrics

    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    task_a = AgentTask(provider=AgentProvider.MANUAL, title="A", goal="A", workspace_id=workspace_a.id)
    task_b = AgentTask(provider=AgentProvider.MANUAL, title="B", goal="B", workspace_id=workspace_b.id)
    run_a = AgentRun(task_id=task_a.id, workspace_id=workspace_a.id, status=AgentRunStatus.RUNNING)
    run_b = AgentRun(task_id=task_b.id, workspace_id=workspace_b.id, status=AgentRunStatus.FAILED)
    store.agent_tasks.update({task_a.id: task_a, task_b.id: task_b})
    store.persist_records(
        inserts=[
            ("agent_task", task.id, task)
            for task in (task_a, task_b)
        ]
    )
    store.agent_runs.update({run_a.id: run_a, run_b.id: run_b})
    owner_a = User(id="owner-a", email="a@test", display_name="A", role=UserRole.OWNER, password_hash="", password_salt="", workspace_id=workspace_a.id)
    store.users[owner_a.id] = owner_a
    auth_a = AuthUser(id=owner_a.id, email=owner_a.email, display_name="A", role=UserRole.OWNER, workspace_id=workspace_a.id)

    summary = build_agent_run_summary(workspace_a.id)
    assert summary.total == 1 and summary.active == 1 and summary.failed == 0
    metrics = prometheus_agent_run_metrics(workspace_a.id)
    assert 'aixion_agent_runs_total{status="RUNNING"} 1' in metrics
    assert 'aixion_agent_runs_total{status="FAILED"} 0' in metrics
    assert "aixion_capability_leases_total" in prometheus_trust_metrics(workspace_a.id)

    with pytest.raises(HTTPException) as error:
        agent_run_routes.create_fault(
            AgentRunFaultCreate(run_id=run_b.id, fault_kind=AgentRunFaultKind.GITHUB_TIMEOUT),
            auth_a,
        )
    assert error.value.status_code == 404


def test_agent_tasks_and_runs_with_conflicting_parent_scopes_are_quarantined() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_b = Project(name="Bravo project", workspace_id=workspace_b.id)
    store.projects[project_b.id] = project_b
    _persist_projects(project_b)
    task = AgentTask(
        provider=AgentProvider.MANUAL,
        title="Conflicting task",
        goal="Must not cross workspace boundary",
        project_id=project_b.id,
        workspace_id=workspace_a.id,
    )
    run = AgentRun(
        task_id=task.id,
        workspace_id=workspace_a.id,
        status=AgentRunStatus.FAILED,
        lease_owner="stale-worker",
        lease_token="stale-lease-token",
        lease_expires_at=main.now_utc() - timedelta(minutes=1),
    )
    store.agent_tasks[task.id] = task
    store.agent_runs[run.id] = run

    for user in (_user(workspace_a), _user(workspace_b)):
        assert agent_task_routes.list_agent_tasks(user=user, limit=100) == []
        with pytest.raises(HTTPException) as task_error:
            agent_task_routes.get_agent_task(task.id, user)
        assert task_error.value.status_code == 404

        assert agent_run_routes.list_runs(user=user, limit=100) == []
        assert agent_run_routes.get_run_summary(user=user).total == 0
        assert agent_run_routes.get_run_metrics(user=user).body.decode().count(
            'aixion_agent_runs_total{status="FAILED"} 0'
        ) == 1
        assert trust_routes.list_exceptions(user=user) == []
        assert agent_run_routes.recover_stale_run_leases(user=user) == {"recovered": 0}
        with pytest.raises(HTTPException) as run_error:
            agent_run_routes.get_run(run.id, user)
        assert run_error.value.status_code == 404

    assert store.agent_tasks[task.id] == task
    assert store.agent_runs[run.id] == run
    assert store.agent_runs[run.id].lease_owner == "stale-worker"


def test_trust_objects_with_conflicting_parent_scopes_are_quarantined() -> None:
    workspace_a = _workspace("alpha")
    workspace_b = _workspace("bravo")
    project_b = Project(name="Bravo project", workspace_id=workspace_b.id)
    store.projects[project_b.id] = project_b
    _persist_projects(project_b)
    approval_b = _approval(project_b).model_copy(
        update={"approved_payload_hash": "payload-hash"}
    )
    store.approval_requests[approval_b.id] = approval_b
    task_b = AgentTask(
        provider=AgentProvider.MANUAL,
        project_id=project_b.id,
        title="Linked task",
        goal="Scoped lease regression",
        repository="owner/repo",
        branch_preference="feature/scope",
        workspace_id=workspace_b.id,
        approval_request_id=approval_b.id,
    )
    store.agent_tasks[task_b.id] = task_b
    lease = CapabilityLease(
        approval_request_id=approval_b.id,
        workspace_id=workspace_a.id,
        approved_payload_hash="payload-hash",
        mode=CapabilityLeaseMode.STRICT,
        scope=CapabilityScope(
            repository="owner/repo",
            branch="feature/scope",
            allowed_actions=[CapabilityActionType.READ_REPOSITORY],
        ),
        issued_by_user_id="owner-a",
        expires_at=main.now_utc() + timedelta(minutes=5),
        receipt_nonce="nonce",
        receipt_signature="signature",
    )
    store.capability_leases[lease.id] = lease
    grant = CredentialGrant(
        lease_id=lease.id,
        workspace_id=workspace_a.id,
        grant_type=CredentialGrantType.INTERNAL_CAPABILITY_TOKEN,
        audience="test",
        subject="agent",
        expires_at=main.now_utc() + timedelta(minutes=2),
        token_hash="token-hash",
    )
    store.credential_grants[grant.id] = grant
    action = ProposedAction(
        lease_id=lease.id,
        project_id=project_b.id,
        workspace_id=workspace_a.id,
        action_type=CapabilityActionType.READ_REPOSITORY,
    )
    store.proposed_actions[action.id] = action

    for user in (_user(workspace_a), _user(workspace_b)):
        with pytest.raises(HTTPException) as lease_error:
            trust_routes.get_lease(lease.id, user)
        assert lease_error.value.status_code == 404
        with pytest.raises(HTTPException) as grant_error:
            trust_routes._credential_or_404(grant.id, user)
        assert grant_error.value.status_code == 404
        with pytest.raises(HTTPException) as action_error:
            trust_routes.get_exact_action_decision(action.id, user)
        assert action_error.value.status_code == 404

    assert store.capability_leases[lease.id] == lease
    assert store.credential_grants[grant.id] == grant
    assert store.proposed_actions[action.id] == action

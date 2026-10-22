from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Request

from .agent_auth import agent_auth_repository
from .agent_routes import router as agent_router
from .agent_run_routes import router as agent_run_router
from .agent_task_routes import propagate_approval_decision_to_agent_task, router as agent_task_router
from .approval_integrity import compute_approval_payload_hash
from .approval_expiry import (
    approval_repository_publishes_compatibility_state,
    approval_repository_scope,
    expire_due_approval,
    expire_due_approvals,
)
from .approval_lifecycle import can_transition, grouped_approvals
from .approval_recovery import reconcile_linked_approval_tasks
from .approval_repository import (
    ApprovalDecisionConflict,
    ApprovalDecisionRepository,
    ApprovalRecordMissing,
)
from .application_repository_access import (
    agent_run_repository_for_request,
    agent_task_repository_for_request,
    audit_event_repository_for_request,
    approval_decision_repository_for_request,
    idea_repository_for_request,
    project_repository_for_request,
    test_run_repository_for_request,
    work_order_repository_for_request,
    workspace_id_for_request,
)
from .approval_decisions import (
    agent_run_repository,
    agent_task_repository,
    approval_decision_repository,
    workspace_membership_repository,
)
from .audit_repository import SQLiteAuditEventRepository
from .audit_routes import redact_audit_event, router as audit_router
from .auth import require_maintainer, require_reviewer
from .auth_routes import router as auth_router
from .email_delivery import email_dispatch_loop
from .connector_routes import router as connector_router
from .github_runner import router as github_runner_router
from .invite_routes import router as invite_router
from .mcp_gateway_routes import router as mcp_gateway_router
from .mcp_registry_routes import router as mcp_registry_router
from .mcp_transport_routes import router as mcp_transport_router
from .models import (
    ApprovalGroups,
    ApprovalRequest,
    ApprovalRequestCreate,
    ApprovalStatus,
    AuditEvent,
    AuthUser,
    DecisionCreate,
    FileChange,
    Idea,
    IdeaCreate,
    Notification,
    Project,
    ProjectCreate,
    TestRun,
    TestRunCreate,
    WorkOrder,
    WorkOrderCreate,
    WorkOrderSourceType,
    now_utc,
)
from .mcp_child_server_repository import SQLiteMCPChildServerRepository
from .idea_repository import IdeaRepository, SQLiteIdeaRepository
from .work_order_repository import SQLiteWorkOrderRepository
from .notifications import (
    create_notification,
    notification_dispatch_loop,
    router as notifications_router,
)
from .postgres_runtime import PostgresRuntime
from .project_repository import ProjectRepository, SQLiteProjectRepository
from .notification_outbox_repository import PostgresNotificationOutboxRepository
from .postgres_notification_dispatcher import postgres_notification_dispatch_loop
from .ops_routes import router as ops_router
from .risk_engine import assess_approval_request, assess_work_order
from .role_routes import router as role_router
from .session_routes import router as session_router
from .settings import get_settings
from .store import store
from .test_run_repository import SQLiteTestRunRepository
from .trust_routes import router as trust_router
from .workspace_access import (
    audit_event_workspace_id,
    approval_workspace_id,
    idea_scope_is_consistent,
    idea_workspace_id,
    work_order_scope_is_consistent,
    work_order_workspace_id,
    workspace_id_for_user,
)

logger = logging.getLogger(__name__)


def _set_approval_recovery_state(
    application: FastAPI,
    *,
    failure_count: int,
    completed_at: datetime,
) -> None:
    application.state.approval_recovery_failure_count = failure_count
    application.state.approval_recovery_last_completed_at = completed_at
    application.state.approval_recovery_status = "degraded" if failure_count else "healthy"


async def _approval_recovery_loop(
    application: FastAPI,
    *,
    approval_repository: object,
    task_repository: object,
    run_repository: object,
    poll_seconds: int,
) -> None:
    """Retry idempotent approval-to-task/run reconciliation while the API is alive."""
    while True:
        await asyncio.sleep(poll_seconds)
        failures: list[tuple[str, str, str]] = []
        try:
            reconcile_linked_approval_tasks(
                approval_repository=approval_repository,
                task_repository=task_repository,
                run_repository=run_repository,
                on_failure=lambda approval_id, task_id, phase: failures.append(
                    (approval_id, task_id, phase)
                ),
            )
            _set_approval_recovery_state(
                application,
                failure_count=len(failures),
                completed_at=now_utc(),
            )
            if failures:
                logger.error(
                    "Approval recovery pass completed with failures count=%s",
                    len(failures),
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            application.state.approval_recovery_failure_count = 1
            application.state.approval_recovery_last_completed_at = now_utc()
            application.state.approval_recovery_status = "failed"
            logger.exception(
                "Approval recovery pass failed error_type=%s", type(error).__name__
            )


def _validate_postgres_notification_runtime(
    application: FastAPI,
    *,
    postgres_runtime: PostgresRuntime | None,
    notification_repository: PostgresNotificationOutboxRepository | None,
    settings: object,
) -> None:
    """Refuse a push worker whose producers/readers use another store."""
    if notification_repository is None:
        return
    if postgres_runtime is None:
        raise RuntimeError(
            "PostgreSQL notification dispatch requires a lifespan-owned PostgreSQL runtime"
        )
    if not getattr(settings, "postgres_auth_enabled", False):
        raise RuntimeError(
            "PostgreSQL notification dispatch requires PostgreSQL-backed account authentication"
        )

    repositories = postgres_runtime.repositories
    required_bindings = {
        "notification_outbox_repository": "notification_outbox",
        "approval_decision_repository": "approval_decisions",
        "agent_task_repository": "agent_tasks",
        "agent_run_repository": "agent_runs",
        "project_repository": "projects",
        "workspace_membership_repository": "workspace_membership",
        "external_agent_auth_repository": "external_agent_auth",
        "audit_event_repository": "audit_events",
    }
    for state_name, repository_name in required_bindings.items():
        expected_repository = getattr(repositories, repository_name, None)
        if (
            expected_repository is None
            or getattr(application.state, state_name, None) is not expected_repository
        ):
            raise RuntimeError(
                "PostgreSQL notification dispatch requires all approval, task, project, "
                "connector-auth, audit, and outbox repositories from the same application runtime"
            )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    postgres_runtime: PostgresRuntime | None = None
    settings = get_settings()
    original_notification_repository = getattr(
        application.state, "notification_outbox_repository", None
    )
    notification_repository = getattr(
        application.state, "notification_outbox_repository", None
    )
    if notification_repository is not None and not isinstance(
        notification_repository, PostgresNotificationOutboxRepository
    ):
        raise RuntimeError("Application notification repository has an unsupported adapter")
    application.state.postgres_runtime = None
    application.state.postgres_repositories = None
    if (
        settings.postgres_runtime_probe_enabled
        or settings.postgres_auth_rate_limits_enabled
        or getattr(settings, "postgres_auth_enabled", False)
    ):
        postgres_runtime = PostgresRuntime.from_environment()
        try:
            postgres_runtime.open(wait=True)
            runtime_probe = postgres_runtime.probe()
            if not runtime_probe.ready:
                raise RuntimeError(
                    "PostgreSQL runtime probe failed: migration schema is incomplete or mismatched"
                )
        except Exception:
            postgres_runtime.close()
            raise
        application.state.postgres_runtime = postgres_runtime
        application.state.postgres_repositories = postgres_runtime.repositories
        if getattr(settings, "postgres_auth_enabled", False):
            notification_repository = getattr(
                postgres_runtime.repositories, "notification_outbox", None
            )
            if not isinstance(
                notification_repository, PostgresNotificationOutboxRepository
            ):
                postgres_runtime.close()
                application.state.postgres_runtime = None
                application.state.postgres_repositories = None
                raise RuntimeError(
                    "PostgreSQL notification repository bundle is unavailable"
                )
            # Notification reads and device registration must use the same
            # lifespan-owned pool as the PostgreSQL account and membership data.
            application.state.notification_outbox_repository = notification_repository
        if settings.postgres_auth_rate_limits_enabled:
            logger.info("PostgreSQL-backed authentication rate limiting enabled")
    dispatch_task: asyncio.Task[None] | None = None
    email_task: asyncio.Task[None] | None = None
    recovery_task: asyncio.Task[None] | None = None
    try:
        push_worker_repository = (
            notification_repository
            if settings.outbox_dispatch_mode == "inline"
            and settings.push_dispatch_enabled
            else None
        )
        _validate_postgres_notification_runtime(
            application,
            postgres_runtime=postgres_runtime,
            notification_repository=push_worker_repository,
            settings=settings,
        )
        if (
            getattr(settings, "postgres_auth_enabled", False)
            and settings.outbox_dispatch_mode == "inline"
            and settings.email_dispatch_enabled
        ):
            email_repository = getattr(
                getattr(application.state, "postgres_repositories", None),
                "email_outbox",
                None,
            )
            if email_repository is None:
                raise RuntimeError(
                    "PostgreSQL email dispatch requires the lifespan-owned PostgreSQL outbox"
                )
        if getattr(settings, "postgres_auth_enabled", False):
            recovery_repositories = getattr(
                application.state, "postgres_repositories", None
            )
            if (
                postgres_runtime is None
                or recovery_repositories is None
                or recovery_repositories is not postgres_runtime.repositories
            ):
                raise RuntimeError(
                    "PostgreSQL approval recovery requires the lifespan-owned runtime repository bundle"
                )
            recovery_approval_repository = recovery_repositories.approval_decisions
            recovery_task_repository = recovery_repositories.agent_tasks
            recovery_run_repository = recovery_repositories.agent_runs
        else:
            recovery_approval_repository = getattr(
                application.state,
                "approval_decision_repository",
                approval_decision_repository,
            )
            recovery_task_repository = getattr(
                application.state,
                "agent_task_repository",
                agent_task_repository,
            )
            recovery_run_repository = getattr(
                application.state,
                "agent_run_repository",
                agent_run_repository,
            )
        application.state.approval_recovery_status = "starting"
        application.state.approval_recovery_failure_count = 0
        startup_recovery_failures: list[tuple[str, str, str]] = []
        reconcile_linked_approval_tasks(
            approval_repository=recovery_approval_repository,
            task_repository=recovery_task_repository,
            run_repository=recovery_run_repository,
            on_failure=lambda approval_id, task_id, phase: startup_recovery_failures.append(
                (approval_id, task_id, phase)
            ),
        )
        _set_approval_recovery_state(
            application,
            failure_count=len(startup_recovery_failures),
            completed_at=now_utc(),
        )
        recovery_task = asyncio.create_task(
            _approval_recovery_loop(
                application,
                approval_repository=recovery_approval_repository,
                task_repository=recovery_task_repository,
                run_repository=recovery_run_repository,
                poll_seconds=getattr(settings, "approval_recovery_poll_seconds", 60),
            )
        )
        logger.info(
            "Outbox dispatch mode=%s; in-process dispatch loops %s",
            settings.outbox_dispatch_mode,
            "enabled" if settings.outbox_dispatch_mode == "inline" else "disabled",
        )
        if settings.outbox_dispatch_mode == "inline":
            if settings.push_dispatch_enabled:
                if notification_repository is None:
                    dispatch_task = asyncio.create_task(notification_dispatch_loop())
                else:
                    dispatch_task = asyncio.create_task(
                        postgres_notification_dispatch_loop(notification_repository)
                    )
            if settings.email_dispatch_enabled:
                if getattr(settings, "postgres_auth_enabled", False):
                    if postgres_runtime is None:
                        raise RuntimeError("PostgreSQL auth is enabled but the runtime is unavailable")
                    email_task = asyncio.create_task(
                        email_dispatch_loop(
                            postgres_repository=application.state.postgres_repositories.email_outbox
                        )
                    )
                else:
                    email_task = asyncio.create_task(email_dispatch_loop())
        yield
    finally:
        for task in (dispatch_task, email_task, recovery_task):
            if task is not None:
                task.cancel()
        for task in (dispatch_task, email_task, recovery_task):
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task
        if postgres_runtime is not None:
            runtime_repositories = getattr(postgres_runtime, "repositories", None)
            runtime_notification_repository = getattr(
                runtime_repositories, "notification_outbox", None
            )
            postgres_runtime.close()
            application.state.postgres_runtime = None
            application.state.postgres_repositories = None
            if (
                runtime_notification_repository is not None
                and getattr(application.state, "notification_outbox_repository", None)
                is runtime_notification_repository
            ):
                application.state.notification_outbox_repository = (
                    original_notification_repository
                )


app = FastAPI(
    title="Aixion Control Tower API",
    version="0.3.0",
    description=(
        "Independent governance, resilient execution, signed capability leases, "
        "policy enforcement, and tamper-evident evidence for AI agents."
    ),
    lifespan=lifespan,
)
# The local/test application binds the existing SQLite adapter once at app
# construction. The PostgreSQL probe bundle remains diagnostic and never
# selects this approval repository independently from the complete store.
app.state.approval_decision_repository = approval_decision_repository
app.state.agent_task_repository = agent_task_repository
app.state.agent_run_repository = agent_run_repository
app.state.audit_event_repository = SQLiteAuditEventRepository(store._connect)
app.state.test_run_repository = SQLiteTestRunRepository(store._connect)
idea_repository: IdeaRepository = SQLiteIdeaRepository(store._connect)
app.state.idea_repository = idea_repository
app.state.external_agent_auth_repository = agent_auth_repository
app.include_router(auth_router)
app.include_router(role_router)
app.include_router(invite_router)
app.include_router(session_router)
app.include_router(agent_router)
app.include_router(agent_task_router)
app.include_router(agent_run_router)
app.include_router(trust_router)
app.include_router(connector_router)
app.include_router(notifications_router)
app.include_router(github_runner_router)
app.include_router(mcp_gateway_router)
app.include_router(mcp_registry_router)
app.include_router(mcp_transport_router)
app.include_router(audit_router)
app.include_router(ops_router)
ReviewerDependency = Depends(require_reviewer)
MaintainerDependency = Depends(require_maintainer)

# This API remains on SQLite until the complete PostgreSQL store boundary is
# implemented. The repository keeps Project creation from rewriting unrelated
# process-local records and is transactionally replaceable in a later slice.
project_repository: ProjectRepository = SQLiteProjectRepository(store._connect)
app.state.project_repository = project_repository
app.state.workspace_membership_repository = workspace_membership_repository
app.state.audit_event_repository = SQLiteAuditEventRepository(store._connect)
app.state.mcp_child_server_repository = SQLiteMCPChildServerRepository(store._connect)
work_order_repository = store.work_order_repository
app.state.work_order_repository = work_order_repository


def _work_order_repository_for_route(http_request: Request | None):
    if http_request is None:
        # Direct function calls in unit tests use the explicit local adapter.
        return work_order_repository
    return work_order_repository_for_request(http_request)


def audit(event_type: str, entity_id: str, details: dict, actor: str = "system") -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def persist() -> None:
    store.persist()


def _workspace_id_for_user(user: AuthUser) -> str | None:
    return workspace_id_for_user(user)


def _workspace_id_for_route(user: AuthUser, http_request: Request | None) -> str | None:
    if http_request is None:
        return _workspace_id_for_user(user)
    return workspace_id_for_request(user, http_request)


def _project_for_user(
    project_id: str,
    user: AuthUser,
    http_request: Request | None = None,
) -> Project:
    project = _project_repository_for_route(http_request).get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    workspace_id = _workspace_id_for_route(user, http_request)
    if workspace_id is not None and project.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _project_repository_for_route(http_request: Request | None) -> ProjectRepository:
    if http_request is None:
        # Direct function calls in local tests use the explicit local adapter.
        return project_repository
    return project_repository_for_request(http_request)


def _idea_repository_for_route(http_request: Request | None) -> IdeaRepository:
    if http_request is None:
        return idea_repository
    return idea_repository_for_request(http_request)


def _approval_workspace_id(request: ApprovalRequest) -> str | None:
    return approval_workspace_id(request)


def _approval_for_user(
    approval_id: str,
    user: AuthUser,
    repository: ApprovalDecisionRepository | None = None,
    http_request: Request | None = None,
) -> ApprovalRequest:
    active_repository = (
        repository if repository is not None else approval_decision_repository
    )
    approval = active_repository.get(approval_id)
    if not approval:
        if approval_repository_publishes_compatibility_state(active_repository):
            store.approval_requests.pop(approval_id, None)
        raise HTTPException(status_code=404, detail="Approval request not found")
    project = _project_repository_for_route(http_request).get(approval.project_id)
    if (
        project is None
        or (approval.workspace_id is not None and approval.workspace_id != project.workspace_id)
    ):
        raise HTTPException(status_code=404, detail="Approval request not found")
    workspace_id = _workspace_id_for_route(user, http_request)
    if workspace_id is None and get_settings().is_production:
        raise HTTPException(status_code=403, detail="Workspace membership is required")
    if workspace_id is not None and project.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if approval_repository_publishes_compatibility_state(active_repository):
        store.approval_requests[approval_id] = approval
    return approval


def _approval_repository_for_route(
    http_request: Request | None,
) -> ApprovalDecisionRepository:
    if http_request is None:
        # Direct function calls in existing unit tests use the explicit
        # local/test adapter. Real HTTP requests must use app.state binding.
        return approval_decision_repository
    return approval_decision_repository_for_request(http_request)


def _sync_approval_cache(
    requests: list[ApprovalRequest], workspace_id: str | None
) -> None:
    """Refresh the compatibility map from durable approval records."""
    with store.atomic():
        for approval_id, cached in list(store.approval_requests.items()):
            if workspace_id is None or _approval_workspace_id(cached) == workspace_id:
                store.approval_requests.pop(approval_id, None)
        store.approval_requests.update({request.id: request for request in requests})


def counts() -> dict[str, int]:
    return {
        "users": len(store.users),
        "sessions": len(store.sessions),
        "invites": len(store.invites),
        "agents": len(store.external_agents),
        "connectors": len(store.agent_connectors),
        "devices": len(store.device_registrations),
        "agent_tasks": len(store.agent_tasks),
        "agent_runs": len(store.agent_runs),
        "capability_leases": len(store.capability_leases),
        "proposed_actions": len(store.proposed_actions),
        "policy_decisions": len(store.policy_decisions),
        "trust_events": len(store.trust_events),
        "projects": len(store.projects),
        "ideas": len(store.ideas),
        "work_orders": len(store.work_orders),
        "approvals": len(store.approval_requests),
        "test_runs": len(store.test_runs),
        "notifications": len(store.notifications),
        "audit_events": len(store.audit_events),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "aixion-control-tower"}


@app.post("/demo/seed")
def seed_demo_data(_: AuthUser = MaintainerDependency) -> dict[str, int]:
    """Seed realistic MVP data for mobile development and demos.

    This endpoint is intentionally explicit and auditable. It does not wipe existing data.
    """
    if store.projects:
        return counts()

    tradebot = Project(
        name="Tradebot",
        description="Options trading signal and execution control system.",
        mode="STRICT",
        rules=["Execution changes require tests", "No direct main branch edits"],
    )
    mcp = Project(
        name="MCP Shield",
        description="Policy, audit, and tool-call risk gateway.",
        mode="STRICT",
        rules=["Policy changes require audit tests", "Auth changes require high-risk approval"],
    )
    store.projects[tradebot.id] = tradebot
    store.projects[mcp.id] = mcp

    idea = Idea(
        project_id=tradebot.id,
        title="Improve stale feed handling",
        raw_text="Make sure stale market data never becomes executable.",
    )
    store.ideas[idea.id] = idea

    work_order = WorkOrder(
        project_id=tradebot.id,
        idea_id=idea.id,
        goal="Improve stale feed handling and execution gating.",
        context="Tradebot execution changes are critical and require evidence before approval.",
        tasks=["Inspect feed freshness", "Update execution gate", "Add regression tests"],
        affected_areas=["core/feed", "core/execution"],
        required_tests=["pytest tests/test_execution_gate.py"],
        rollback_plan="Revert feature branch commit and rerun execution regression tests.",
        risk_level="CRITICAL",
        source_type=WorkOrderSourceType.MANUAL,
    )
    store.work_orders[work_order.id] = work_order

    approval_payload = ApprovalRequestCreate(
        project_id=tradebot.id,
        work_order_id=work_order.id,
        title="Update stale LTP execution guard",
        summary="Blocks stale market data from becoming executable trades.",
        agent_name="builder-agent",
        target_branch="feature/stale-ltp-guard",
        files=[
            FileChange(
                path="core/execution_gate.py",
                change_type="update",
                diff='+ if stale_ltp:\n+     return ExecutionDecision(allowed=False, reason="STALE_LTP_BLOCK")',
                new_content='if stale_ltp:\n    return ExecutionDecision(allowed=False, reason="STALE_LTP_BLOCK")\n',
            )
        ],
        test_plan=["pytest tests/test_execution_gate.py"],
        rollback_plan="Revert the feature branch commit and rerun execution regression tests.",
    )
    approval = ApprovalRequest(
        **approval_payload.model_dump(),
        risk=assess_approval_request(approval_payload),
        status=ApprovalStatus.REQUESTED,
    )
    store.approval_requests[approval.id] = approval

    test_run = TestRun(
        approval_request_id=approval.id,
        command="pytest tests/test_execution_gate.py",
        status="PENDING",
        output_summary="Seeded test evidence placeholder.",
    )
    store.test_runs[test_run.id] = test_run

    create_notification(
        title="Approval needed: stale LTP guard",
        body="Tradebot has a critical approval request waiting for review.",
        entity_type="approval_request",
        entity_id=approval.id,
    )
    audit("demo.seeded", tradebot.id, {"projects": [tradebot.name, mcp.name]})
    persist()
    return counts()


@app.post("/projects", response_model=Project)
def create_project(
    payload: ProjectCreate,
    user: AuthUser = MaintainerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> Project:
    project = Project(
        **payload.model_dump(),
        workspace_id=_workspace_id_for_route(user, http_request),
        created_by_user_id=user.id,
    )
    audit_event = AuditEvent(
        event_type="project.created",
        entity_id=project.id,
        workspace_id=project.workspace_id,
        details={"name": project.name, "mode": project.mode},
        actor=user.email,
    )
    repository = _project_repository_for_route(http_request)
    repository.create(project=project, audit_event=audit_event)
    # Compatibility projections are updated only after the durable transaction
    # commits. Other legacy routes still read these maps while SQLite is active.
    if isinstance(repository, SQLiteProjectRepository):
        with store.atomic():
            store.projects[project.id] = project
            store.audit_events.append(audit_event)
    return project


@app.get("/projects", response_model=list[Project])
def list_projects(
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> list[Project]:
    workspace_id = _workspace_id_for_route(user, http_request)
    try:
        return _project_repository_for_route(http_request).list_for_workspace(workspace_id)
    except Exception as exc:
        logger.error("Project listing failed because durable storage is unavailable")
        raise HTTPException(
            status_code=503, detail="Project data is temporarily unavailable"
        ) from exc


@app.post("/ideas", response_model=Idea)
def create_idea(
    payload: IdeaCreate,
    user: AuthUser = MaintainerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> Idea:
    workspace_id = _workspace_id_for_route(user, http_request)
    if payload.project_id:
        _project_for_user(payload.project_id, user, http_request)
    idea = Idea(**payload.model_dump(), workspace_id=workspace_id)
    repository = _idea_repository_for_route(http_request)
    if not isinstance(repository, SQLiteIdeaRepository):
        audit_event = AuditEvent(
            event_type="idea.created",
            entity_id=idea.id,
            workspace_id=idea.workspace_id,
            details={"title": idea.title, "project_id": idea.project_id},
            actor=user.email,
        )
        repository.create(idea=idea, audit_event=audit_event)
        return idea
    store.ideas[idea.id] = idea
    audit("idea.created", idea.id, {"title": idea.title, "project_id": idea.project_id}, actor=user.email)
    persist()
    return idea


@app.get("/ideas", response_model=list[Idea])
def list_ideas(
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> list[Idea]:
    workspace_id = _workspace_id_for_route(user, http_request)
    repository = _idea_repository_for_route(http_request)
    if not isinstance(repository, SQLiteIdeaRepository):
        if not workspace_id:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        return repository.list_for_workspace(workspace_id)
    ideas = [idea for idea in store.ideas.values() if idea_scope_is_consistent(idea)]
    if workspace_id is None:
        return ideas
    return [idea for idea in ideas if idea_workspace_id(idea) == workspace_id]


@app.post("/work-orders", response_model=WorkOrder)
def create_work_order(
    payload: WorkOrderCreate,
    user: AuthUser = MaintainerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> WorkOrder:
    project = _project_for_user(payload.project_id, user, http_request)
    if payload.idea_id:
        idea_repository = _idea_repository_for_route(http_request)
        idea = idea_repository.get(payload.idea_id)
        if isinstance(idea_repository, SQLiteIdeaRepository):
            idea = store.ideas.get(payload.idea_id)
        if http_request is None:
            idea = store.ideas.get(payload.idea_id)
        idea_scope_workspace_id = (
            idea_workspace_id(idea)
            if isinstance(idea_repository, SQLiteIdeaRepository) and idea is not None
            else (idea.workspace_id if idea is not None else None)
        )
        if (
            idea is None
            or (
                isinstance(idea_repository, SQLiteIdeaRepository)
                and not idea_scope_is_consistent(idea)
            )
            or (idea.project_id is not None and idea.project_id != project.id)
            or idea_scope_workspace_id != project.workspace_id
        ):
            raise HTTPException(status_code=404, detail="Idea not found")

    risk_level = assess_work_order(payload)
    work_order = WorkOrder(
        **payload.model_dump(),
        risk_level=risk_level,
        source_type=WorkOrderSourceType.MANUAL,
        created_by_user_id=user.id,
        workspace_id=project.workspace_id,
        verified_source=False,
    )
    audit_event = AuditEvent(
        event_type="work_order.created",
        entity_id=work_order.id,
        workspace_id=work_order.workspace_id,
        details={
            "project_id": work_order.project_id,
            "risk_level": work_order.risk_level,
            "source_type": work_order.source_type,
            "source_provider": work_order.source_provider,
            "source_agent_id": work_order.source_agent_id,
            "source_agent_name": work_order.source_agent_name,
            "created_by_user_id": work_order.created_by_user_id,
            "verified_source": work_order.verified_source,
        },
        actor=user.email,
    )
    _work_order_repository_for_route(http_request).create(
        work_order=work_order, audit_event=audit_event
    )
    # Keep legacy projections only for the SQLite runtime. PostgreSQL is the
    # authority whenever that app-bound repository is selected.
    if isinstance(_work_order_repository_for_route(http_request), SQLiteWorkOrderRepository):
        store.project_work_order_creation(work_order, audit_event)
    return work_order


@app.get("/work-orders", response_model=list[WorkOrder])
def list_work_orders(
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> list[WorkOrder]:
    workspace_id = _workspace_id_for_route(user, http_request)
    work_orders = [
        item
        for item in _work_order_repository_for_route(http_request).list_for_workspace(
            workspace_id
        )
        if (
            work_order_scope_is_consistent(item)
            if isinstance(_work_order_repository_for_route(http_request), SQLiteWorkOrderRepository)
            else item.workspace_id == workspace_id
        )
    ]
    return work_orders


@app.post("/approvals", response_model=ApprovalRequest)
def create_approval_request(
    payload: ApprovalRequestCreate,
    user: AuthUser = MaintainerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ApprovalRequest:
    repository = _approval_repository_for_route(http_request)
    work_order_repository = _work_order_repository_for_route(http_request)
    project = _project_for_user(payload.project_id, user, http_request)
    if payload.work_order_id:
        work_order = work_order_repository.get(payload.work_order_id)
        if (
            work_order is None
            or not work_order_scope_is_consistent(work_order)
            or work_order.project_id != project.id
            or work_order_workspace_id(work_order) != project.workspace_id
        ):
            raise HTTPException(status_code=404, detail="Work order not found")

    risk = assess_approval_request(payload)
    status = ApprovalStatus.BLOCKED if risk.blocked else ApprovalStatus.REQUESTED
    created_at = now_utc()
    expires_at = (
        created_at + timedelta(seconds=get_settings().agent_approval_ttl_seconds)
        if status == ApprovalStatus.REQUESTED
        else None
    )
    request = ApprovalRequest(
        **payload.model_dump(),
        risk=risk,
        status=status,
        created_by_user_id=user.id,
        workspace_id=project.workspace_id,
        verified_source=False,
        created_at=created_at,
        updated_at=created_at,
        expires_at=expires_at,
    )
    audit_event = AuditEvent(
        event_type="approval.created",
        entity_id=request.id,
        workspace_id=request.workspace_id,
        details={
            "project_id": request.project_id,
            "risk_level": request.risk.level,
            "status": request.status,
            "blocked": request.risk.blocked,
            "source_provider": request.source_provider,
            "source_agent_id": request.source_agent_id,
            "source_agent_name": request.source_agent_name,
            "verified_source": request.verified_source,
            "created_by_user_id": request.created_by_user_id,
            "expires_at": request.expires_at,
        },
        actor=user.email,
    )
    notification = Notification(
        title=f"Approval needed: {request.title}",
        body=f"{request.risk.level} risk request on {request.target_branch}",
        entity_type="approval_request",
        entity_id=request.id,
        workspace_id=request.workspace_id,
    )
    try:
        repository.create(
            approval=request,
            audit_event=audit_event,
            notification=notification,
        )
    except ApprovalDecisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if approval_repository_publishes_compatibility_state(repository):
        store.approval_requests[request.id] = request
        store.audit_events.append(audit_event)
        store.notifications[notification.id] = notification
    return request


@app.get("/approvals", response_model=list[ApprovalRequest])
def list_approval_requests(
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> list[ApprovalRequest]:
    repository = _approval_repository_for_route(http_request)
    workspace_id = _workspace_id_for_route(user, http_request)
    if workspace_id is None and get_settings().is_production:
        raise HTTPException(status_code=403, detail="Workspace membership is required")
    requests = repository.list_for_workspace(workspace_id)
    publish_compatibility_state = approval_repository_publishes_compatibility_state(repository)
    if publish_compatibility_state:
        _sync_approval_cache(requests, workspace_id)
    expire_due_approvals(repository=repository)
    requests = repository.list_for_workspace(workspace_id)
    if publish_compatibility_state:
        _sync_approval_cache(requests, workspace_id)
    projects = _project_repository_for_route(http_request)
    requests = [
        request
        for request in requests
        if (project := projects.get(request.project_id)) is not None
        and (request.workspace_id is None or request.workspace_id == project.workspace_id)
    ]
    return requests


@app.get("/approvals/grouped", response_model=ApprovalGroups)
def list_grouped_approval_requests(
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ApprovalGroups:
    repository = _approval_repository_for_route(http_request)
    expire_due_approvals(repository=repository)
    requests = list_approval_requests(user, http_request)
    return ApprovalGroups(**grouped_approvals(requests))


@app.get("/approvals/{approval_id}", response_model=ApprovalRequest)
def get_approval_request(
    approval_id: str,
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ApprovalRequest:
    repository = _approval_repository_for_route(http_request)
    approval = _approval_for_user(approval_id, user, repository, http_request)
    expire_due_approval(approval, repository=repository)
    refreshed = repository.get(approval_id)
    project = (
        _project_repository_for_route(http_request).get(refreshed.project_id)
        if refreshed is not None
        else None
    )
    if (
        refreshed is None
        or project is None
        or (refreshed.workspace_id is not None and refreshed.workspace_id != project.workspace_id)
    ):
        if approval_repository_publishes_compatibility_state(repository):
            store.approval_requests.pop(approval_id, None)
        raise HTTPException(status_code=404, detail="Approval request not found")
    workspace_id = _workspace_id_for_route(user, http_request)
    if workspace_id is not None and project.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if approval_repository_publishes_compatibility_state(repository):
        store.approval_requests[approval_id] = refreshed
    return refreshed


@app.post("/approvals/{approval_id}/approve", response_model=ApprovalRequest)
def approve_approval(
    approval_id: str,
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ApprovalRequest:
    return decide_approval(
        approval_id,
        DecisionCreate(decision="approve"),
        user,
        http_request,
    )


@app.post("/approvals/{approval_id}/deny", response_model=ApprovalRequest)
def deny_approval(
    approval_id: str,
    payload: DecisionCreate,
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ApprovalRequest:
    return decide_approval(
        approval_id,
        DecisionCreate(decision="deny", reason=payload.reason),
        user,
        http_request,
    )


@app.post("/approvals/{approval_id}/decision", response_model=ApprovalRequest)
def decide_approval(
    approval_id: str,
    payload: DecisionCreate,
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ApprovalRequest:
    repository = _approval_repository_for_route(http_request)
    publish_compatibility_state = approval_repository_publishes_compatibility_state(repository)
    with approval_repository_scope(repository):
        durable_request = repository.get(approval_id)
        if durable_request is None:
            if publish_compatibility_state:
                store.approval_requests.pop(approval_id, None)
            raise HTTPException(status_code=404, detail="Approval request not found")
        # Reconcile this approval from the repository before applying tenant,
        # lifecycle, and transition checks. The process-local map may be stale
        # after another request or process commits a timeout/decision.
        if publish_compatibility_state:
            store.approval_requests[approval_id] = durable_request
        approval = _approval_for_user(approval_id, user, repository, http_request)

        decision = payload.decision.lower().strip()
        expire_due_approval(approval, repository=repository)
        durable_request = repository.get(approval_id)
        if durable_request is None:
            if publish_compatibility_state:
                store.approval_requests.pop(approval_id, None)
            raise HTTPException(status_code=404, detail="Approval request not found")
        if publish_compatibility_state:
            store.approval_requests[approval_id] = durable_request
        approval = _approval_for_user(approval_id, user, repository, http_request)
        previous_status = approval.status
        previous_request = approval.model_copy(deep=True)

        if approval.status == ApprovalStatus.BLOCKED:
            raise HTTPException(status_code=409, detail="Blocked requests cannot be approved from mobile")

        target_status = {
            "approve": ApprovalStatus.APPROVED,
            "deny": ApprovalStatus.DENIED,
            "denied": ApprovalStatus.DENIED,
            "reject": ApprovalStatus.DENIED,
            "revise": ApprovalStatus.REVISION_REQUESTED,
            "request_revision": ApprovalStatus.REVISION_REQUESTED,
            "revision": ApprovalStatus.REVISION_REQUESTED,
        }.get(decision)
        if target_status is None:
            raise HTTPException(status_code=400, detail="Decision must be approve, deny, reject, or revise")
        if not can_transition(approval.status, target_status):
            raise HTTPException(
                status_code=409,
                detail=f"Approval cannot transition from {approval.status} to {target_status}",
            )

        candidate = approval.model_copy(deep=True)
        if decision == "approve":
            if candidate.risk.required_actions:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Request cannot be approved until required actions are satisfied.",
                        "required_actions": candidate.risk.required_actions,
                    },
                )
            candidate.approved_payload_hash_version = 2
            candidate.approved_payload_hash = compute_approval_payload_hash(candidate)
            candidate.approved_at = now_utc()
            candidate.approved_by_user_id = user.id
            candidate.status = ApprovalStatus.APPROVED
        elif decision in {"reject", "deny", "denied"}:
            candidate.status = ApprovalStatus.DENIED
        elif decision in {"revise", "request_revision", "revision"}:
            candidate.status = ApprovalStatus.REVISION_REQUESTED
        else:
            raise HTTPException(status_code=400, detail="Decision must be approve, deny, reject, or revise")

        candidate.updated_at = now_utc()
        project = _project_repository_for_route(http_request).get(candidate.project_id)
        if project is None or (
            candidate.workspace_id is not None
            and candidate.workspace_id != project.workspace_id
        ):
            raise HTTPException(status_code=404, detail="Approval request not found")
        decision_workspace_id = candidate.workspace_id or project.workspace_id
        decision_audit_event = AuditEvent(
            event_type="approval.decision",
            entity_id=candidate.id,
            workspace_id=decision_workspace_id,
            details={
                "decision": decision,
                "reason": payload.reason,
                "previous_status": previous_status,
                "new_status": candidate.status,
                "approved_payload_hash": candidate.approved_payload_hash,
                "source_provider": candidate.source_provider,
                "source_agent_id": candidate.source_agent_id,
                "verified_source": candidate.verified_source,
            },
            actor=user.email,
        )
        # Commit the decision and all linked side effects through the selected
        # repository transaction. Compatibility maps are only projections for
        # the explicitly local SQLite adapter.
        selected_run_repository = (
            agent_run_repository_for_request(http_request)
            if http_request is not None
            else None
        )
        try:
            committed = repository.commit_decision(
                approval_id=approval.id,
                expected_status=previous_status,
                expected_approval=previous_request,
                updated_approval=candidate,
                audit_event=decision_audit_event,
                decision_notification=Notification(
                    title=f"Approval {candidate.status.lower()}: {candidate.title}",
                    body=payload.reason or "Decision recorded from mobile review.",
                    entity_type="approval_request",
                    entity_id=candidate.id,
                    workspace_id=decision_workspace_id,
                ),
                run_repository=selected_run_repository,
            )
            if publish_compatibility_state:
                store.approval_requests[candidate.id] = candidate
                store.audit_events.append(decision_audit_event)
                if committed.linked_task is not None:
                    store.agent_tasks[committed.linked_task.id] = committed.linked_task
                store.notifications[committed.notification.id] = committed.notification
        except ApprovalDecisionConflict as exc:
            latest = repository.get(approval_id)
            if publish_compatibility_state and latest is None:
                store.approval_requests.pop(approval_id, None)
            elif publish_compatibility_state:
                assert latest is not None
                store.approval_requests[approval_id] = latest
            raise HTTPException(
                status_code=409,
                detail="Approval changed before this decision committed. Reload and review it again.",
            ) from exc
        except ApprovalRecordMissing as exc:
            if publish_compatibility_state:
                store.approval_requests.pop(approval_id, None)
            raise HTTPException(status_code=404, detail="Approval request not found") from exc
        except Exception:
            # Candidate state was never published, so readers continue to see
            # the last committed approval even when storage fails.
            raise

    propagate_approval_decision_to_agent_task(
        candidate,
        previous_status,
        user.email,
        committed=committed,
        task_repository=(
            agent_task_repository_for_request(http_request)
            if http_request is not None
            else None
        ),
        approval_repository=repository,
        run_repository=(
            selected_run_repository
        ),
    )
    if publish_compatibility_state and committed.linked_task is not None:
        store.agent_tasks[committed.linked_task.id] = committed.linked_task
        if committed.linked_task_event is not None:
            store.agent_task_events[committed.linked_task_event.id] = committed.linked_task_event
        if committed.linked_task_audit_event is not None:
            store.audit_events.append(committed.linked_task_audit_event)
        if committed.linked_task_notification is not None:
            store.notifications[committed.linked_task_notification.id] = committed.linked_task_notification
    # ApprovalDecisionRepository commits the approval, audit event,
    # notification and linked task records in one transaction. Do not run the
    # legacy whole-store rewrite here: stale compatibility maps can overwrite
    # newer durable records unrelated to this decision.
    return candidate


@app.post("/test-runs", response_model=TestRun)
def create_test_run(
    payload: TestRunCreate,
    user: AuthUser = MaintainerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> TestRun:
    approval_repository = (
        approval_decision_repository_for_request(http_request)
        if http_request is not None
        else approval_decision_repository
    )
    approval = _approval_for_user(
        payload.approval_request_id,
        user,
        repository=approval_repository,
        http_request=http_request,
    )
    test_run = TestRun(**payload.model_dump())
    project = _project_repository_for_route(http_request).get(approval.project_id)
    workspace_id = approval.workspace_id or (project.workspace_id if project is not None else None)
    audit_event = AuditEvent(
        event_type="test_run.recorded",
        entity_id=test_run.id,
        workspace_id=workspace_id,
        details={
            "approval_request_id": test_run.approval_request_id,
            "status": test_run.status,
            "command": test_run.command,
        },
        actor=user.email,
    )
    repository = (
        test_run_repository_for_request(http_request)
        if http_request is not None
        else app.state.test_run_repository
    )
    repository.create(test_run=test_run, workspace_id=workspace_id, audit_event=audit_event)
    if isinstance(repository, SQLiteTestRunRepository):
        store.test_runs[test_run.id] = test_run
        store.audit_events.append(audit_event)
    return test_run


@app.get("/test-runs", response_model=list[TestRun])
def list_test_runs(
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> list[TestRun]:
    workspace_id = _workspace_id_for_route(user, http_request)
    repository = (
        test_run_repository_for_request(http_request)
        if http_request is not None
        else app.state.test_run_repository
    )
    return repository.list_for_workspace(workspace_id)


@app.get("/audit", response_model=list[AuditEvent])
def list_audit_events(
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> list[AuditEvent]:
    if http_request is not None:
        repository = audit_event_repository_for_request(http_request)
        if not isinstance(repository, SQLiteAuditEventRepository):
            workspace_id = _workspace_id_for_route(user, http_request)
            if workspace_id is None:
                raise HTTPException(status_code=403, detail="Workspace membership is required")
            events = repository.list_for_workspace(workspace_id)
            return [redact_audit_event(event) for event in events]
    workspace_id = _workspace_id_for_user(user)
    if workspace_id is None:
        events = store.audit_events
    else:
        events = [
            event
            for event in store.audit_events
            if audit_event_workspace_id(event) == workspace_id
        ]
    return [redact_audit_event(event) for event in events]

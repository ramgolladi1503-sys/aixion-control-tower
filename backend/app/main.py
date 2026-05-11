from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException

from .agent_routes import router as agent_router
from .approval_integrity import compute_approval_payload_hash
from .approval_lifecycle import grouped_approvals
from .audit_routes import router as audit_router
from .auth import require_maintainer, require_reviewer
from .auth_routes import router as auth_router
from .github_runner import router as github_runner_router
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
    Project,
    ProjectCreate,
    TestRun,
    TestRunCreate,
    WorkOrder,
    WorkOrderCreate,
    now_utc,
)
from .notifications import create_notification, router as notifications_router
from .risk_engine import assess_approval_request, assess_work_order
from .store import store

app = FastAPI(
    title="Aixion Control Tower API",
    version="0.1.0",
    description="MVP backend for AI project execution control, approvals, risk scoring, and audit logs.",
)
app.include_router(auth_router)
app.include_router(agent_router)
app.include_router(notifications_router)
app.include_router(github_runner_router)
app.include_router(mcp_gateway_router)
app.include_router(mcp_registry_router)
app.include_router(mcp_transport_router)
app.include_router(audit_router)
ReviewerDependency = Depends(require_reviewer)
MaintainerDependency = Depends(require_maintainer)


def audit(event_type: str, entity_id: str, details: dict, actor: str = "system") -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def persist() -> None:
    store.persist()


def counts() -> dict[str, int]:
    return {
        "users": len(store.users),
        "sessions": len(store.sessions),
        "agents": len(store.external_agents),
        "devices": len(store.device_registrations),
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
def create_project(payload: ProjectCreate, user: AuthUser = MaintainerDependency) -> Project:
    project = Project(**payload.model_dump())
    store.projects[project.id] = project
    audit("project.created", project.id, {"name": project.name, "mode": project.mode}, actor=user.email)
    persist()
    return project


@app.get("/projects", response_model=list[Project])
def list_projects(_: AuthUser = ReviewerDependency) -> list[Project]:
    return list(store.projects.values())


@app.post("/ideas", response_model=Idea)
def create_idea(payload: IdeaCreate, user: AuthUser = MaintainerDependency) -> Idea:
    if payload.project_id and payload.project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    idea = Idea(**payload.model_dump())
    store.ideas[idea.id] = idea
    audit("idea.created", idea.id, {"title": idea.title, "project_id": idea.project_id}, actor=user.email)
    persist()
    return idea


@app.get("/ideas", response_model=list[Idea])
def list_ideas(_: AuthUser = ReviewerDependency) -> list[Idea]:
    return list(store.ideas.values())


@app.post("/work-orders", response_model=WorkOrder)
def create_work_order(payload: WorkOrderCreate, user: AuthUser = MaintainerDependency) -> WorkOrder:
    if payload.project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    if payload.idea_id and payload.idea_id not in store.ideas:
        raise HTTPException(status_code=404, detail="Idea not found")

    risk_level = assess_work_order(payload)
    work_order = WorkOrder(**payload.model_dump(), risk_level=risk_level)
    store.work_orders[work_order.id] = work_order
    audit(
        "work_order.created",
        work_order.id,
        {"project_id": work_order.project_id, "risk_level": work_order.risk_level},
        actor=user.email,
    )
    persist()
    return work_order


@app.get("/work-orders", response_model=list[WorkOrder])
def list_work_orders(_: AuthUser = ReviewerDependency) -> list[WorkOrder]:
    return list(store.work_orders.values())


@app.post("/approvals", response_model=ApprovalRequest)
def create_approval_request(payload: ApprovalRequestCreate, user: AuthUser = MaintainerDependency) -> ApprovalRequest:
    if payload.project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    if payload.work_order_id and payload.work_order_id not in store.work_orders:
        raise HTTPException(status_code=404, detail="Work order not found")

    risk = assess_approval_request(payload)
    status = ApprovalStatus.BLOCKED if risk.blocked else ApprovalStatus.REQUESTED
    request = ApprovalRequest(
        **payload.model_dump(),
        risk=risk,
        status=status,
        created_by_user_id=user.id,
        verified_source=False,
    )
    store.approval_requests[request.id] = request
    audit(
        "approval.created",
        request.id,
        {
            "project_id": request.project_id,
            "risk_level": request.risk.level,
            "status": request.status,
            "blocked": request.risk.blocked,
            "source_provider": request.source_provider,
            "source_agent_id": request.source_agent_id,
            "source_agent_name": request.source_agent_name,
            "verified_source": request.verified_source,
            "created_by_user_id": request.created_by_user_id,
        },
        actor=user.email,
    )
    create_notification(
        title=f"Approval needed: {request.title}",
        body=f"{request.risk.level} risk request on {request.target_branch}",
        entity_type="approval_request",
        entity_id=request.id,
    )
    persist()
    return request


@app.get("/approvals", response_model=list[ApprovalRequest])
def list_approval_requests(_: AuthUser = ReviewerDependency) -> list[ApprovalRequest]:
    return list(store.approval_requests.values())


@app.get("/approvals/grouped", response_model=ApprovalGroups)
def list_grouped_approval_requests(_: AuthUser = ReviewerDependency) -> ApprovalGroups:
    return ApprovalGroups(**grouped_approvals(store.approval_requests.values()))


@app.get("/approvals/{approval_id}", response_model=ApprovalRequest)
def get_approval_request(approval_id: str, _: AuthUser = ReviewerDependency) -> ApprovalRequest:
    request = store.approval_requests.get(approval_id)
    if not request:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return request


@app.post("/approvals/{approval_id}/approve", response_model=ApprovalRequest)
def approve_approval(approval_id: str, user: AuthUser = ReviewerDependency) -> ApprovalRequest:
    return decide_approval(approval_id, DecisionCreate(decision="approve"), user)


@app.post("/approvals/{approval_id}/deny", response_model=ApprovalRequest)
def deny_approval(approval_id: str, payload: DecisionCreate, user: AuthUser = ReviewerDependency) -> ApprovalRequest:
    return decide_approval(approval_id, DecisionCreate(decision="deny", reason=payload.reason), user)


@app.post("/approvals/{approval_id}/decision", response_model=ApprovalRequest)
def decide_approval(approval_id: str, payload: DecisionCreate, user: AuthUser = ReviewerDependency) -> ApprovalRequest:
    request = store.approval_requests.get(approval_id)
    if not request:
        raise HTTPException(status_code=404, detail="Approval request not found")

    decision = payload.decision.lower().strip()
    previous_status = request.status

    if request.status == ApprovalStatus.BLOCKED:
        raise HTTPException(status_code=409, detail="Blocked requests cannot be approved from mobile")

    if decision == "approve":
        if request.risk.required_actions:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Request cannot be approved until required actions are satisfied.",
                    "required_actions": request.risk.required_actions,
                },
            )
        request.approved_payload_hash = compute_approval_payload_hash(request)
        request.approved_at = now_utc()
        request.approved_by_user_id = user.id
        request.status = ApprovalStatus.APPROVED
    elif decision in {"reject", "deny", "denied"}:
        request.status = ApprovalStatus.DENIED
    elif decision in {"revise", "request_revision", "revision"}:
        request.status = ApprovalStatus.REVISION_REQUESTED
    else:
        raise HTTPException(status_code=400, detail="Decision must be approve, deny, reject, or revise")

    request.updated_at = now_utc()
    audit(
        "approval.decision",
        request.id,
        {
            "decision": decision,
            "reason": payload.reason,
            "previous_status": previous_status,
            "new_status": request.status,
            "approved_payload_hash": request.approved_payload_hash,
            "source_provider": request.source_provider,
            "source_agent_id": request.source_agent_id,
            "verified_source": request.verified_source,
        },
        actor=user.email,
    )
    create_notification(
        title=f"Approval {request.status.lower()}: {request.title}",
        body=payload.reason or "Decision recorded from mobile review.",
        entity_type="approval_request",
        entity_id=request.id,
    )
    persist()
    return request


@app.post("/test-runs", response_model=TestRun)
def create_test_run(payload: TestRunCreate, user: AuthUser = MaintainerDependency) -> TestRun:
    if payload.approval_request_id not in store.approval_requests:
        raise HTTPException(status_code=404, detail="Approval request not found")
    test_run = TestRun(**payload.model_dump())
    store.test_runs[test_run.id] = test_run
    audit(
        "test_run.recorded",
        test_run.id,
        {
            "approval_request_id": test_run.approval_request_id,
            "status": test_run.status,
            "command": test_run.command,
        },
        actor=user.email,
    )
    persist()
    return test_run


@app.get("/test-runs", response_model=list[TestRun])
def list_test_runs(_: AuthUser = ReviewerDependency) -> list[TestRun]:
    return list(store.test_runs.values())


@app.get("/audit", response_model=list[AuditEvent])
def list_audit_events(_: AuthUser = ReviewerDependency) -> list[AuditEvent]:
    return store.audit_events

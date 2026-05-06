from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .models import (
    ApprovalRequest,
    ApprovalRequestCreate,
    ApprovalStatus,
    AuditEvent,
    DecisionCreate,
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
from .risk_engine import assess_approval_request, assess_work_order
from .store import store

app = FastAPI(
    title="Aixion Control Tower API",
    version="0.1.0",
    description="MVP backend for AI project execution control, approvals, risk scoring, and audit logs.",
)


def audit(event_type: str, entity_id: str, details: dict, actor: str = "system") -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "aixion-control-tower"}


@app.post("/projects", response_model=Project)
def create_project(payload: ProjectCreate) -> Project:
    project = Project(**payload.model_dump())
    store.projects[project.id] = project
    audit("project.created", project.id, {"name": project.name, "mode": project.mode})
    return project


@app.get("/projects", response_model=list[Project])
def list_projects() -> list[Project]:
    return list(store.projects.values())


@app.post("/ideas", response_model=Idea)
def create_idea(payload: IdeaCreate) -> Idea:
    if payload.project_id and payload.project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    idea = Idea(**payload.model_dump())
    store.ideas[idea.id] = idea
    audit("idea.created", idea.id, {"title": idea.title, "project_id": idea.project_id})
    return idea


@app.get("/ideas", response_model=list[Idea])
def list_ideas() -> list[Idea]:
    return list(store.ideas.values())


@app.post("/work-orders", response_model=WorkOrder)
def create_work_order(payload: WorkOrderCreate) -> WorkOrder:
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
    )
    return work_order


@app.get("/work-orders", response_model=list[WorkOrder])
def list_work_orders() -> list[WorkOrder]:
    return list(store.work_orders.values())


@app.post("/approvals", response_model=ApprovalRequest)
def create_approval_request(payload: ApprovalRequestCreate) -> ApprovalRequest:
    if payload.project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    if payload.work_order_id and payload.work_order_id not in store.work_orders:
        raise HTTPException(status_code=404, detail="Work order not found")

    risk = assess_approval_request(payload)
    status = ApprovalStatus.BLOCKED if risk.blocked else ApprovalStatus.PENDING_REVIEW
    request = ApprovalRequest(**payload.model_dump(), risk=risk, status=status)
    store.approval_requests[request.id] = request
    audit(
        "approval.created",
        request.id,
        {
            "project_id": request.project_id,
            "risk_level": request.risk.level,
            "status": request.status,
            "blocked": request.risk.blocked,
        },
    )
    return request


@app.get("/approvals", response_model=list[ApprovalRequest])
def list_approval_requests() -> list[ApprovalRequest]:
    return list(store.approval_requests.values())


@app.get("/approvals/{approval_id}", response_model=ApprovalRequest)
def get_approval_request(approval_id: str) -> ApprovalRequest:
    request = store.approval_requests.get(approval_id)
    if not request:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return request


@app.post("/approvals/{approval_id}/decision", response_model=ApprovalRequest)
def decide_approval(approval_id: str, payload: DecisionCreate) -> ApprovalRequest:
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
        request.status = ApprovalStatus.APPROVED
    elif decision == "reject":
        request.status = ApprovalStatus.REJECTED
    elif decision in {"revise", "request_revision", "revision"}:
        request.status = ApprovalStatus.REVISION_REQUESTED
    else:
        raise HTTPException(status_code=400, detail="Decision must be approve, reject, or revise")

    request.updated_at = now_utc()
    audit(
        "approval.decision",
        request.id,
        {
            "decision": decision,
            "reason": payload.reason,
            "previous_status": previous_status,
            "new_status": request.status,
        },
        actor="mobile-user",
    )
    return request


@app.post("/test-runs", response_model=TestRun)
def create_test_run(payload: TestRunCreate) -> TestRun:
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
    )
    return test_run


@app.get("/test-runs", response_model=list[TestRun])
def list_test_runs() -> list[TestRun]:
    return list(store.test_runs.values())


@app.get("/audit", response_model=list[AuditEvent])
def list_audit_events() -> list[AuditEvent]:
    return store.audit_events

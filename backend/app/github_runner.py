from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_api_key
from .models import ApprovalStatus
from .store import store

router = APIRouter(prefix="/github-runner", tags=["github-runner"])
AuthDependency = Depends(require_api_key)


class PatchPlanRequest(BaseModel):
    approval_request_id: str
    repository_full_name: str
    base_branch: str = "main"
    feature_branch: str


class PatchPlanResponse(BaseModel):
    ready: bool
    approval_request_id: str
    repository_full_name: str
    base_branch: str
    feature_branch: str
    files: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


@router.post("/patch-plan", response_model=PatchPlanResponse)
def create_patch_plan(payload: PatchPlanRequest, _: None = AuthDependency) -> PatchPlanResponse:
    """Prepare a safe GitHub patch execution plan.

    This endpoint deliberately does not write to GitHub yet. It validates that the
    approval is safe enough to be handed to a GitHub worker in a later phase.
    """
    approval = store.approval_requests.get(payload.approval_request_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")

    blockers: list[str] = []
    if approval.status != ApprovalStatus.APPROVED:
        blockers.append("Approval request must be APPROVED before patch execution planning.")
    if approval.risk.blocked:
        blockers.append("Blocked approval requests cannot be sent to GitHub runner.")
    if payload.base_branch.lower() in {"main", "master", "production", "release"} and not payload.feature_branch:
        blockers.append("A feature branch is required; direct protected branch patching is not allowed.")
    if payload.feature_branch.lower() in {"main", "master", "production", "release"}:
        blockers.append("Feature branch cannot be a protected branch.")
    if not approval.files:
        blockers.append("Approval request has no file changes.")

    return PatchPlanResponse(
        ready=not blockers,
        approval_request_id=approval.id,
        repository_full_name=payload.repository_full_name,
        base_branch=payload.base_branch,
        feature_branch=payload.feature_branch,
        files=[file.path for file in approval.files],
        blockers=blockers,
        next_steps=[
            "Create feature branch from base branch.",
            "Apply approved patch only to listed files.",
            "Commit with approval id in message.",
            "Run configured tests and report status back to /test-runs.",
            "Open pull request; never auto-merge in MVP.",
        ],
    )

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .approval_integrity import compute_approval_payload_hash
from .auth import require_maintainer
from .models import ApprovalStatus, AuthUser, TestRun, now_utc
from .store import store

router = APIRouter(prefix="/github-runner", tags=["github-runner"])
MaintainerDependency = Depends(require_maintainer)
PROTECTED_BRANCHES = {"main", "master", "production", "release"}
GITHUB_API_URL = "https://api.github.com"


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


class ExecuteGitHubRequest(PatchPlanRequest):
    commit_message: str = "Apply approved Aixion Control Tower change"
    pr_title: str | None = None
    pr_body: str | None = None
    run_tests: bool = False


class ExecuteGitHubResponse(BaseModel):
    approval_request_id: str
    repository_full_name: str
    branch: str
    commit_sha: str
    pull_request_url: str
    changed_files: list[str]
    test_status: str = "NOT_RUN"


def _github_headers() -> dict[str, str]:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN is not configured for GitHub execution")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _validate_execution(payload: PatchPlanRequest) -> PatchPlanResponse:
    approval = store.approval_requests.get(payload.approval_request_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")

    blockers: list[str] = []
    if approval.status != ApprovalStatus.APPROVED:
        blockers.append("Approval request must be APPROVED before GitHub execution.")
    if approval.risk.blocked:
        blockers.append("Blocked approval requests cannot be sent to GitHub runner.")
    if not approval.approved_payload_hash:
        blockers.append("Approval request is missing approved payload hash; re-approval is required.")
    else:
        current_hash = compute_approval_payload_hash(approval)
        if current_hash != approval.approved_payload_hash:
            blockers.append("Approval payload changed after approval; re-approval is required before execution.")
    if payload.base_branch.lower() in PROTECTED_BRANCHES and not payload.feature_branch:
        blockers.append("A feature branch is required; direct protected branch patching is not allowed.")
    if payload.feature_branch.lower() in PROTECTED_BRANCHES:
        blockers.append("Feature branch cannot be a protected branch.")
    if payload.feature_branch == payload.base_branch:
        blockers.append("Feature branch must be different from base branch.")
    if not approval.files:
        blockers.append("Approval request has no file changes.")
    if any(not file.new_content for file in approval.files):
        blockers.append("Every file change must include new_content for deterministic GitHub execution.")

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
            "Apply approved full-file content only to listed files.",
            "Commit with approval id in message.",
            "Optionally record test command status back to /test-runs.",
            "Open pull request; never auto-merge in MVP.",
        ],
    )


async def _github(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    response = await client.request(method, f"{GITHUB_API_URL}{path}", headers=_github_headers(), **kwargs)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    if not response.content:
        return {}
    return response.json()


@router.post("/patch-plan", response_model=PatchPlanResponse)
def create_patch_plan(payload: PatchPlanRequest, _: AuthUser = MaintainerDependency) -> PatchPlanResponse:
    return _validate_execution(payload)


@router.post("/execute", response_model=ExecuteGitHubResponse)
async def execute_github_change(payload: ExecuteGitHubRequest, _: AuthUser = MaintainerDependency) -> ExecuteGitHubResponse:
    plan = _validate_execution(payload)
    if not plan.ready:
        raise HTTPException(status_code=409, detail={"blockers": plan.blockers})

    approval = store.approval_requests[payload.approval_request_id]
    repo = payload.repository_full_name
    approval.status = ApprovalStatus.EXECUTING
    approval.updated_at = now_utc()
    store.persist()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            base_ref = await _github(client, "GET", f"/repos/{repo}/git/ref/heads/{payload.base_branch}")
            base_sha = base_ref["object"]["sha"]

            branch_ref = f"refs/heads/{payload.feature_branch}"
            branch_exists = True
            try:
                await _github(client, "GET", f"/repos/{repo}/git/ref/heads/{payload.feature_branch}")
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                branch_exists = False

            if not branch_exists:
                await _github(
                    client,
                    "POST",
                    f"/repos/{repo}/git/refs",
                    json={"ref": branch_ref, "sha": base_sha},
                )

            changed_files: list[str] = []
            commit_response: dict[str, Any] | None = None
            for file in approval.files:
                existing_sha: str | None = None
                try:
                    existing = await _github(
                        client,
                        "GET",
                        f"/repos/{repo}/contents/{file.path}",
                        params={"ref": payload.feature_branch},
                    )
                    existing_sha = existing.get("sha")
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise

                body: dict[str, Any] = {
                    "message": f"{payload.commit_message}\n\nApproval: {approval.id}",
                    "content": file.new_content or "",
                    "branch": payload.feature_branch,
                }
                import base64

                body["content"] = base64.b64encode((file.new_content or "").encode("utf-8")).decode("ascii")
                if existing_sha:
                    body["sha"] = existing_sha
                commit_response = await _github(client, "PUT", f"/repos/{repo}/contents/{file.path}", json=body)
                changed_files.append(file.path)

            pr = await _github(
                client,
                "POST",
                f"/repos/{repo}/pulls",
                json={
                    "title": payload.pr_title or approval.title,
                    "body": payload.pr_body
                    or f"Aixion approved change.\n\nApproval: `{approval.id}`\nApproved Payload: `{approval.approved_payload_hash}`\nRisk: `{approval.risk.level}`\n\nNo auto-merge. Review required.",
                    "head": payload.feature_branch,
                    "base": payload.base_branch,
                },
            )
    except Exception:
        approval.status = ApprovalStatus.FAILED
        approval.updated_at = now_utc()
        store.persist()
        raise

    approval.status = ApprovalStatus.READY_FOR_PR
    approval.updated_at = now_utc()

    test_status = "NOT_RUN"
    if payload.run_tests:
        test_status = "PENDING_EXTERNAL_CI"
        test_run = TestRun(
            approval_request_id=approval.id,
            command="GitHub Actions / external CI",
            status=test_status,
            output_summary="PR opened. CI must be verified before merge.",
        )
        store.test_runs[test_run.id] = test_run

    store.persist()

    if commit_response is None:
        raise HTTPException(status_code=500, detail="No commit was created during GitHub execution")

    return ExecuteGitHubResponse(
        approval_request_id=approval.id,
        repository_full_name=repo,
        branch=payload.feature_branch,
        commit_sha=commit_response["commit"]["sha"],
        pull_request_url=pr["html_url"],
        changed_files=changed_files,
        test_status=test_status,
    )

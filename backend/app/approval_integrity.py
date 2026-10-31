from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import ApprovalRequest


def approval_payload_snapshot(approval: ApprovalRequest) -> dict[str, Any]:
    """Return the exact approval payload that must be frozen at approval time.

    Version 1 preserves the original action-only snapshot for historical approvals.
    Version 2 additionally binds workspace and the reviewed risk assessment.
    Mutable lifecycle fields such as status, timestamps, audit, and execution
    results remain excluded.
    """
    snapshot = {
        "project_id": approval.project_id,
        "work_order_id": approval.work_order_id,
        "title": approval.title,
        "summary": approval.summary,
        "agent_name": approval.agent_name,
        "target_branch": approval.target_branch,
        "files": [
            {
                "path": file.path,
                "change_type": file.change_type,
                "diff": file.diff,
                "new_content": file.new_content,
            }
            for file in approval.files
        ],
        "test_plan": list(approval.test_plan),
        "rollback_plan": approval.rollback_plan,
        "source_provider": approval.source_provider,
        "source_agent_id": approval.source_agent_id,
        "source_agent_name": approval.source_agent_name,
        "source_session_id": approval.source_session_id,
        "source_invocation_id": approval.source_invocation_id,
        "source_task_url": approval.source_task_url,
    }
    if approval.approved_payload_hash_version == 2:
        snapshot["workspace_id"] = approval.workspace_id
        snapshot["risk"] = approval.risk.model_dump(mode="json")
    return snapshot


def compute_approval_payload_hash(approval: ApprovalRequest) -> str:
    payload = approval_payload_snapshot(approval)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    digest = hashlib.sha256(encoded).hexdigest()
    if approval.approved_payload_hash_version == 1:
        return "sha256:" + digest
    if approval.approved_payload_hash_version == 2:
        return "sha256:v2:" + digest
    raise ValueError("Unsupported approval payload hash version")


def compute_approval_review_fingerprint(approval: ApprovalRequest) -> str:
    """Fingerprint every field used to identify and review a pending decision.

    Unlike the execution payload hash, this binds lifecycle status and expiry too.
    It is returned to the reviewer and must be echoed when the reviewer decides.
    """
    snapshot = {
        "version": 1,
        "id": approval.id,
        "workspace_id": approval.workspace_id,
        "project_id": approval.project_id,
        "work_order_id": approval.work_order_id,
        "status": approval.status.value,
        "title": approval.title,
        "summary": approval.summary,
        "agent_name": approval.agent_name,
        "target_branch": approval.target_branch,
        "files": [
            {
                "path": item.path,
                "change_type": item.change_type,
                "diff": item.diff,
                "new_content": item.new_content,
            }
            for item in approval.files
        ],
        "test_plan": list(approval.test_plan),
        "rollback_plan": approval.rollback_plan,
        "source_provider": approval.source_provider.value,
        "verified_source": approval.verified_source,
        "source_agent_id": approval.source_agent_id,
        "source_agent_name": approval.source_agent_name,
        "source_session_id": approval.source_session_id,
        "source_invocation_id": approval.source_invocation_id,
        "source_task_url": approval.source_task_url,
        "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
        "risk": approval.risk.model_dump(mode="json"),
    }
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:review-v1:" + hashlib.sha256(encoded).hexdigest()

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import ApprovalRequest


def approval_payload_snapshot(approval: ApprovalRequest) -> dict[str, Any]:
    """Return the exact approval payload that must be frozen at approval time.

    Deliberately excludes mutable lifecycle fields such as status, timestamps, risk,
    audit, and execution results. The GitHub runner must execute only this approved
    content, not a later silently edited version.
    """
    return {
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
        "source_task_url": approval.source_task_url,
    }


def compute_approval_payload_hash(approval: ApprovalRequest) -> str:
    payload = approval_payload_snapshot(approval)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()

from __future__ import annotations

from .agent_task_models import AgentTask, AgentTaskStatus
from .models import ApprovalRequest, ApprovalStatus, Notification
from .notifications import create_notification


def notify_agent_task_approval_created(task: AgentTask, approval: ApprovalRequest) -> Notification:
    return create_notification(
        title=f"Agent task approval needed: {task.title}",
        body=f"{approval.risk.level} risk approval is waiting for review from {task.provider}.",
        entity_type="agent_task",
        entity_id=task.id,
        user_id=task.created_by_user_id,
    )


def notify_agent_task_approval_decision(task: AgentTask, approval: ApprovalRequest) -> Notification:
    if approval.status == ApprovalStatus.APPROVED:
        title = f"Agent task approved: {task.title}"
        body = "Worker can now continue after the linked approval decision."
    elif approval.status == ApprovalStatus.DENIED:
        title = f"Agent task denied: {task.title}"
        body = "Linked approval was denied. Worker must not continue."
    elif approval.status == ApprovalStatus.REVISION_REQUESTED:
        title = f"Agent task needs revision: {task.title}"
        body = "Linked approval requested revision before work can continue."
    else:
        title = f"Agent task updated: {task.title}"
        body = f"Linked approval moved to {approval.status}."

    return create_notification(
        title=title,
        body=body,
        entity_type="agent_task",
        entity_id=task.id,
        user_id=task.created_by_user_id,
    )


def notify_agent_task_worker_status(task: AgentTask) -> Notification | None:
    if task.status == AgentTaskStatus.EXECUTING:
        title = f"Agent task executing: {task.title}"
        body = "Worker has started processing the approved task."
    elif task.status == AgentTaskStatus.READY_FOR_PR:
        title = f"Agent task ready for PR review: {task.title}"
        body = "Worker reported a pull request or review-ready result."
    elif task.status == AgentTaskStatus.FAILED:
        title = f"Agent task failed: {task.title}"
        body = "Worker reported a failure. Review the task timeline for evidence."
    elif task.status == AgentTaskStatus.DONE:
        title = f"Agent task done: {task.title}"
        body = "Worker reported completion. Review the task timeline for final evidence."
    else:
        return None

    return create_notification(
        title=title,
        body=body,
        entity_type="agent_task",
        entity_id=task.id,
        user_id=task.created_by_user_id,
    )

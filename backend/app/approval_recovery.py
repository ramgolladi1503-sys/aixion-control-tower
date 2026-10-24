from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import nullcontext

from .agent_run_models import AgentRunCreate
from .agent_run_repository import (
    AgentRunRepository,
    PostgresAgentRunRepository,
    SQLiteAgentRunRepository,
)
from .agent_run_supervisor import create_agent_run
from .agent_task_models import AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from .agent_task_repository import (
    AgentTaskRepository,
    PostgresAgentTaskRepository,
    SQLiteAgentTaskRepository,
)
from .approval_decisions import (
    agent_run_repository,
    agent_task_repository,
    approval_decision_repository as default_approval_decision_repository,
)
from .approval_repository import (
    ApprovalDecisionRepository,
    PostgresApprovalDecisionRepository,
    SQLiteApprovalDecisionRepository,
)
from .models import ApprovalStatus, AuditEvent, now_utc
from .store import store
from .workspace_access import agent_task_workspace_id, approval_workspace_id

logger = logging.getLogger(__name__)

_TASK_STATUS_BY_APPROVAL = {
    ApprovalStatus.APPROVED: AgentTaskStatus.APPROVED,
    ApprovalStatus.DENIED: AgentTaskStatus.DENIED,
    ApprovalStatus.REVISION_REQUESTED: AgentTaskStatus.PLANNING,
}
_TASK_EVENT_BY_APPROVAL = {
    ApprovalStatus.APPROVED: (AgentTaskEventType.APPROVED, "Linked approval was approved."),
    ApprovalStatus.DENIED: (AgentTaskEventType.DENIED, "Linked approval was denied."),
    ApprovalStatus.REVISION_REQUESTED: (
        AgentTaskEventType.NOTE,
        "Linked approval requested revision.",
    ),
}
def reconcile_linked_approval_tasks(
    *,
    approval_repository: ApprovalDecisionRepository | None = None,
    task_repository: AgentTaskRepository | None = None,
    run_repository: AgentRunRepository | None = None,
    on_failure: Callable[[str, str, str], None] | None = None,
) -> int:
    """Repair committed approval propagation and missing first-run scheduling.

    Approval state remains authoritative. Each repair is persisted through the
    selected repository profile; PostgreSQL operations are transactionally
    scoped by the individual repositories.
    """
    provided_repositories = (
        approval_repository is not None,
        task_repository is not None,
        run_repository is not None,
    )
    if any(provided_repositories) and not all(provided_repositories):
        raise RuntimeError(
            "Approval recovery requires approval, AgentTask, and AgentRun repositories together"
        )
    active_approval_repository = (
        approval_repository
        if approval_repository is not None
        else default_approval_decision_repository
    )
    active_task_repository = (
        task_repository if task_repository is not None else agent_task_repository
    )
    active_run_repository = (
        run_repository if run_repository is not None else agent_run_repository
    )
    sqlite_profile = (
        isinstance(active_approval_repository, SQLiteApprovalDecisionRepository)
        and isinstance(active_task_repository, SQLiteAgentTaskRepository)
        and isinstance(active_run_repository, SQLiteAgentRunRepository)
    )
    postgres_profile = (
        isinstance(active_approval_repository, PostgresApprovalDecisionRepository)
        and isinstance(active_task_repository, PostgresAgentTaskRepository)
        and isinstance(active_run_repository, PostgresAgentRunRepository)
    )
    if not (sqlite_profile or postgres_profile):
        raise RuntimeError("Linked approval recovery requires one supported repository profile")
    if sqlite_profile and not (
        active_approval_repository._connect
        == active_task_repository._connect
        == active_run_repository._connect
    ):
        raise RuntimeError(
            "Linked approval recovery requires approval, task, and run repositories "
            "to share one SQLite connection provider"
        )

    reconciled = 0

    def report_failure(approval_id: str, task_id: str, phase: str) -> None:
        if on_failure is None:
            return
        try:
            on_failure(approval_id, task_id, phase)
        except Exception:
            logger.exception("Approval recovery failure observer raised unexpectedly")

    with (store.atomic() if sqlite_profile else nullcontext()):
        for approval in active_approval_repository.list_for_workspace():
            expected_task_status = _TASK_STATUS_BY_APPROVAL.get(approval.status)
            if expected_task_status is None:
                continue
            for task in active_task_repository.list_for_approval(approval.id):
                task_runs = active_run_repository.list_for_task(task.id)
                decision_event = next(
                    (
                        event
                        for event in active_task_repository.list_events(
                            task.id,
                            workspace_id=task.workspace_id,
                        )
                        if event.metadata.get("approval_request_id") == approval.id
                        and event.metadata.get("new_status") == approval.status
                    ),
                    None,
                )
                repair_status = task.status != expected_task_status
                repair_event = decision_event is None
                schedule_first_run = (
                    approval.status == ApprovalStatus.APPROVED and not task_runs
                )
                scheduled_run = next(
                    (
                        candidate
                        for candidate in reversed(task_runs)
                        if candidate.approval_request_id == approval.id
                        and candidate.metadata.get("scheduled_from")
                        in {"approval_decision", "approval_recovery"}
                    ),
                    None,
                )
                scheduled_workspace_id = task.workspace_id or (
                    agent_task_workspace_id(task) if sqlite_profile else None
                )
                repair_schedule_audit = False
                if (
                    approval.status == ApprovalStatus.APPROVED
                    and scheduled_run is not None
                    and scheduled_workspace_id is not None
                ):
                    repair_schedule_audit = not active_run_repository.has_run_scheduled_audit(
                        task_id=task.id,
                        run_id=scheduled_run.id,
                        workspace_id=scheduled_workspace_id,
                    )
                if (
                    repair_status
                    and task.status != AgentTaskStatus.WAITING_FOR_APPROVAL
                ):
                    logger.error(
                        "Skipping approval recovery for task %s: status %s is not recoverable "
                        "from the pending-approval state",
                        task.id,
                        task.status,
                    )
                    report_failure(approval.id, task.id, "unrecoverable_task_status")
                    continue
                if not (
                    repair_status
                    or repair_event
                    or schedule_first_run
                    or repair_schedule_audit
                ):
                    continue
                try:
                    recovery_phase = "load_decision_audit"
                    if repair_status:
                        task_status_changed_at = now_utc()
                    else:
                        task_status_changed_at = task.updated_at

                    decision_audit = active_approval_repository.latest_decision_audit(
                        approval_id=approval.id,
                        workspace_id=(
                            approval.workspace_id
                            or task.workspace_id
                            or (
                                approval_workspace_id(approval)
                                or agent_task_workspace_id(task)
                                if sqlite_profile
                                else None
                            )
                        ),
                        new_status=approval.status,
                    )
                    previous_status = (
                        decision_audit.details.get("previous_status")
                        if decision_audit is not None
                        else None
                    )

                    if repair_event or repair_status:
                        recovery_phase = "propagate_task_decision"
                        event_type, message = _TASK_EVENT_BY_APPROVAL[approval.status]
                        updated_task = task.model_copy(
                            update={
                                "status": expected_task_status,
                                "updated_at": task_status_changed_at,
                            }
                        )
                        decision_event = AgentTaskEvent(
                            task_id=task.id,
                            event_type=event_type,
                            message=message,
                            status=expected_task_status,
                            actor="approval-recovery",
                            metadata={
                                "approval_request_id": approval.id,
                                "previous_status": previous_status,
                                "new_status": approval.status,
                                "reconciled": True,
                                "source_decision_audit_id": (
                                    decision_audit.id if decision_audit is not None else None
                                ),
                            },
                        )
                        task_audit = AuditEvent(
                            event_type="agent_task.approval_decision_propagated",
                            entity_id=task.id,
                            workspace_id=(
                                updated_task.workspace_id
                                or (
                                    agent_task_workspace_id(updated_task)
                                    if sqlite_profile
                                    else None
                                )
                            ),
                            actor="approval-recovery",
                            details={
                                "approval_request_id": approval.id,
                                "previous_status": previous_status,
                                "new_status": approval.status,
                                "task_status": updated_task.status,
                                "event_id": decision_event.id,
                                "status": decision_event.status,
                                "message": decision_event.message,
                                "notification_id": None,
                                "reconciled": True,
                            },
                        )
                        active_task_repository.commit_event(
                            expected=task,
                            updated=updated_task,
                            event=decision_event,
                            audit=task_audit,
                        )
                        if sqlite_profile:
                            store.project_agent_task_transition(
                                updated_task,
                                decision_event,
                                task_audit,
                            )
                        task = updated_task

                    if schedule_first_run:
                        recovery_phase = "schedule_first_run"
                        scheduled_run = create_agent_run(
                            AgentRunCreate(
                                task_id=task.id,
                                metadata={
                                    "scheduled_from": "approval_recovery",
                                    "approval_decision_actor": "approval-recovery",
                                },
                            ),
                            actor="approval-recovery",
                            task_repository=active_task_repository,
                            approval_repository=active_approval_repository,
                            run_repository=active_run_repository,
                        )
                        repair_schedule_audit = False
                    if repair_schedule_audit and scheduled_run is not None:
                        recovery_phase = "repair_schedule_audit"
                        # Query the durable audit ledger above rather than the
                        # compatibility cache: a restart can lose or stale it.
                        schedule_audit = AuditEvent(
                            event_type="agent_task.run_scheduled",
                            entity_id=task.id,
                            workspace_id=scheduled_workspace_id,
                            actor="approval-recovery",
                            details={
                                "agent_run_id": scheduled_run.id,
                                "approval_request_id": approval.id,
                                "run_status": scheduled_run.status,
                                "reconciled": True,
                            },
                        )
                        inserted = active_run_repository.record_run_scheduled_audit(
                            schedule_audit
                        )
                        if sqlite_profile and inserted and all(
                            event.id != schedule_audit.id for event in store.audit_events
                        ):
                            store.audit_events.append(schedule_audit)
                except Exception as error:
                    # Keep the approval's committed decision authoritative and
                    # fail closed: no run is scheduled unless the normal
                    # approved-task checks succeed. The phase and both entity
                    # identifiers make the interrupted, retryable boundary
                    # visible to operators without implying a shared transaction.
                    logger.exception(
                        "Approval recovery failed: approval_id=%s task_id=%s phase=%s error_type=%s",
                        approval.id,
                        task.id,
                        recovery_phase,
                        type(error).__name__,
                    )
                    report_failure(approval.id, task.id, recovery_phase)
                    continue
                reconciled += 1
    return reconciled

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta

from .approval_decisions import approval_decision_repository
from .approval_repository import (
    ApprovalDecisionConflict,
    ApprovalDecisionRepository,
    ApprovalRecordMissing,
    SQLiteApprovalDecisionRepository,
)
from .models import ApprovalRequest, ApprovalStatus, AuditEvent, now_utc
from .settings import get_settings
from .store import store

PENDING_STATUSES = {ApprovalStatus.REQUESTED, ApprovalStatus.PENDING_REVIEW}


def _resolve_approval_repository(
    repository: ApprovalDecisionRepository | None,
) -> ApprovalDecisionRepository:
    if repository is not None:
        return repository
    settings = get_settings()
    if settings.is_production or settings.postgres_auth_enabled:
        raise RuntimeError(
            "Approval expiry requires an explicit durable repository in production "
            "or PostgreSQL-auth mode"
        )
    return approval_decision_repository


def approval_repository_scope(repository: ApprovalDecisionRepository):
    """Use the process lock only for the SQLite compatibility adapter."""
    if isinstance(repository, SQLiteApprovalDecisionRepository):
        return store.atomic()
    return nullcontext()


def approval_repository_publishes_compatibility_state(
    repository: ApprovalDecisionRepository,
) -> bool:
    """Only the SQLite adapter may publish approval effects into `store`."""
    return isinstance(repository, SQLiteApprovalDecisionRepository)


def expire_approval(
    approval: ApprovalRequest,
    *,
    actor: str,
    reason: str,
    now: datetime | None = None,
    force: bool = False,
    repository: ApprovalDecisionRepository | None = None,
) -> bool:
    """Expire one pending approval, optionally before its configured deadline."""
    active_repository = _resolve_approval_repository(repository)
    publish_compatibility_state = approval_repository_publishes_compatibility_state(
        active_repository
    )
    current_time = now or now_utc()
    durable_approval = active_repository.get(approval.id)
    if durable_approval is None:
        return False
    if durable_approval != approval:
        approval = durable_approval
        if publish_compatibility_state:
            store.approval_requests[approval.id] = approval
    if approval.status not in PENDING_STATUSES:
        return False
    expected_approval = approval.model_copy(deep=True)
    effective_expiry = approval.expires_at
    if effective_expiry is None:
        effective_expiry = approval.created_at + timedelta(
            seconds=get_settings().agent_approval_ttl_seconds
        )
    if not force and (effective_expiry is None or effective_expiry > current_time):
        if approval.expires_at is None:
            updated_approval = approval.model_copy(
                update={"expires_at": effective_expiry, "updated_at": current_time}
            )
            backfill_audit = AuditEvent(
                event_type="approval.expiry_backfilled",
                entity_id=approval.id,
                workspace_id=approval.workspace_id,
                actor=actor,
                created_at=current_time,
                details={
                    "previous_expires_at": None,
                    "expires_at": effective_expiry,
                    "source_provider": approval.source_provider,
                    "source_agent_id": approval.source_agent_id,
                },
            )
            try:
                persisted_approval, inserted = active_repository.backfill_expiry(
                    approval_id=approval.id,
                    expected_approval=expected_approval,
                    updated_approval=updated_approval,
                    audit_event=backfill_audit,
                )
            except (ApprovalDecisionConflict, ApprovalRecordMissing) as exc:
                latest = active_repository.get(approval.id)
                if latest is not None and publish_compatibility_state:
                    store.approval_requests[approval.id] = latest
                if latest is None or (
                    latest.status in PENDING_STATUSES and latest.expires_at is None
                ):
                    raise ApprovalDecisionConflict(
                        "Approval expiry could not be safely persisted."
                    ) from exc
                return False
            if publish_compatibility_state:
                store.approval_requests[approval.id] = persisted_approval
                if inserted:
                    store.audit_events.append(backfill_audit)
        return False

    previous_status = approval.status
    expired_approval = approval.model_copy(
        update={
            "status": ApprovalStatus.EXPIRED,
            "updated_at": current_time,
            "expires_at": effective_expiry,
        }
    )
    audit_event = AuditEvent(
        event_type="approval.expired",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        actor=actor,
        details={
            "project_id": approval.project_id,
            "previous_status": previous_status,
            "new_status": ApprovalStatus.EXPIRED,
            "reason": reason,
            "expires_at": effective_expiry,
            "source_provider": approval.source_provider,
            "source_agent_id": approval.source_agent_id,
        },
    )
    try:
        commit = active_repository.expire_pending(
            approval_id=approval.id,
            expected_approval=expected_approval,
            expired_approval=expired_approval,
            audit_event=audit_event,
        )
    except (ApprovalDecisionConflict, ApprovalRecordMissing):
        latest = active_repository.get(approval.id)
        if latest is not None and publish_compatibility_state:
            store.approval_requests[approval.id] = latest
        return False

    if publish_compatibility_state:
        approval.status = expired_approval.status
        approval.updated_at = expired_approval.updated_at
        approval.expires_at = expired_approval.expires_at
        store.approval_requests[approval.id] = approval
        store.audit_events.append(audit_event)
        if (
            commit.linked_task is not None
            and commit.linked_task_event is not None
            and commit.linked_task_audit_event is not None
        ):
            store.project_agent_task_transition(
                commit.linked_task,
                commit.linked_task_event,
                commit.linked_task_audit_event,
                commit.linked_task_notification,
            )
    return True


def expire_due_approval(
    approval: ApprovalRequest,
    *,
    actor: str = "system",
    repository: ApprovalDecisionRepository | None = None,
) -> bool:
    active_repository = _resolve_approval_repository(repository)
    with approval_repository_scope(active_repository):
        return expire_approval(
            approval,
            actor=actor,
            reason="deadline_elapsed",
            repository=active_repository,
        )


def expire_due_approvals(
    *,
    actor: str = "system",
    repository: ApprovalDecisionRepository | None = None,
    workspace_id: str | None = None,
    limit: int = 250,
) -> int:
    active_repository = _resolve_approval_repository(repository)
    with approval_repository_scope(active_repository):
        changed = 0
        # Selection is bounded and repository-backed. Expiration itself stays
        # compare-and-set transactional, so concurrent application instances
        # may safely select the same row and only one commits its audit/event.
        now = now_utc()
        ttl_seconds = get_settings().agent_approval_ttl_seconds
        for approval in active_repository.list_due_pending(
            now=now,
            legacy_ttl_seconds=ttl_seconds,
            limit=limit,
            workspace_id=workspace_id,
        ):
            expired = expire_approval(
                approval,
                actor=actor,
                reason="deadline_elapsed",
                now=now,
                repository=active_repository,
            )
            if expired:
                changed += 1
        return changed

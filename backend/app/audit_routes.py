from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .auth import require_maintainer, require_owner, require_reviewer
from .application_repository_access import (
    audit_event_repository_for_request,
    workspace_id_for_request,
)
from .audit_repository import AuditEventRepository, SQLiteAuditEventRepository
from .audit_redaction import redact_audit_event
from .models import AuditEvent, AuthUser, now_utc
from .recovery_routes import (
    RecoverySnapshot,
    RecoveryValidationRequest,
    RecoveryValidationResponse,
    export_recovery_snapshot,
    validate_recovery_snapshot,
)
from .store import store
from .settings import get_settings
from .workspace_access import workspace_id_for_user

router = APIRouter(prefix="/audit", tags=["audit"])
ReviewerDependency = Depends(require_reviewer)
MaintainerDependency = Depends(require_maintainer)
OwnerDependency = Depends(require_owner)

AUDIT_EXPORT_MAX_LIMIT = 1_000
AUDIT_RETENTION_POLICY_VERSION = "aixion-audit-retention-v1"
_local_audit_repository = SQLiteAuditEventRepository(store._connect)


class AuditRetentionPolicy(BaseModel):
    policy_version: str = AUDIT_RETENTION_POLICY_VERSION
    recommended_online_retention_days: int = 365
    recommended_archive_retention_days: int = 2555
    deletion_job_enabled: bool = False
    notes: list[str] = Field(
        default_factory=lambda: [
            "Retention is documented but not automatically enforced in this MVP foundation.",
            "Audit deletion must not be enabled until legal, incident, and recovery requirements are explicit.",
        ]
    )


class AuditExportResponse(BaseModel):
    generated_at: datetime = Field(default_factory=now_utc)
    count: int
    limit: int
    truncated: bool
    filters: dict[str, Any] = Field(default_factory=dict)
    retention_policy: AuditRetentionPolicy = Field(default_factory=AuditRetentionPolicy)
    events: list[AuditEvent] = Field(default_factory=list)


def _audit_repository_for_route(request: Request | None) -> AuditEventRepository:
    if request is None:
        return _local_audit_repository
    _require_authoritative_audit_runtime(request)
    return audit_event_repository_for_request(request)


def _require_authoritative_audit_runtime(request: Request) -> None:
    """Fail closed when PostgreSQL is selected without a complete audit cutover."""
    if (
        get_settings().postgres_auth_enabled
        or getattr(request.app.state, "postgres_runtime", None) is not None
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                "Audit reads are unavailable while PostgreSQL is configured without "
                "a unified audit writer and recovery repository."
            ),
        )


def _workspace_for_route(user: AuthUser, request: Request | None) -> str | None:
    if request is None:
        return workspace_id_for_user(user)
    return workspace_id_for_request(user, request)


def _filter_audit_events(
    repository: AuditEventRepository,
    *,
    limit: int = 1_000,
    workspace_id: str | None = None,
    event_type: str | None = None,
    entity_id: str | None = None,
    actor: str | None = None,
    detail_key: str | None = None,
    detail_value: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> list[AuditEvent]:
    if workspace_id is None:
        # PostgreSQL audit access is always tenant-scoped. Local unauthenticated
        # demo mode may have no workspace and retains its SQLite-only behavior.
        events = repository.list_for_workspace(
            "",
            event_type=event_type,
            entity_id=entity_id,
            actor=actor,
            detail_key=detail_key,
            detail_value=detail_value,
            created_after=created_after,
            created_before=created_before,
            limit=limit,
        )
        return events
    return repository.list_for_workspace(
        workspace_id,
        event_type=event_type,
        entity_id=entity_id,
        actor=actor,
        detail_key=detail_key,
        detail_value=detail_value,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
    )


def _filter_summary(
    *,
    event_type: str | None = None,
    entity_id: str | None = None,
    actor: str | None = None,
    detail_key: str | None = None,
    detail_value: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    for key, value in {
        "event_type": event_type,
        "entity_id": entity_id,
        "actor": actor,
        "detail_key": detail_key,
        "detail_value": detail_value,
        "created_after": created_after,
        "created_before": created_before,
    }.items():
        if value is not None:
            filters[key] = value
    return filters


@router.get("/events", response_model=list[AuditEvent])
def query_audit_events(
    request: Request,
    event_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    detail_key: str | None = Query(default=None),
    detail_value: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user: AuthUser = ReviewerDependency,
) -> list[AuditEvent]:
    events = _filter_audit_events(
        _audit_repository_for_route(request),
        limit=limit,
        workspace_id=_workspace_for_route(user, request),
        event_type=event_type,
        entity_id=entity_id,
        actor=actor,
        detail_key=detail_key,
        detail_value=detail_value,
    )[:limit]
    return [redact_audit_event(event) for event in events]


@router.get("/export", response_model=AuditExportResponse)
def export_audit_events(
    request: Request,
    event_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    detail_key: str | None = Query(default=None),
    detail_value: str | None = Query(default=None),
    created_after: datetime | None = Query(default=None),
    created_before: datetime | None = Query(default=None),
    limit: int = Query(default=1_000, ge=1, le=AUDIT_EXPORT_MAX_LIMIT),
    user: AuthUser = MaintainerDependency,
) -> AuditExportResponse:
    events = _filter_audit_events(
        _audit_repository_for_route(request),
        limit=limit + 1,
        workspace_id=_workspace_for_route(user, request),
        event_type=event_type,
        entity_id=entity_id,
        actor=actor,
        detail_key=detail_key,
        detail_value=detail_value,
        created_after=created_after,
        created_before=created_before,
    )
    exported_events = [redact_audit_event(event) for event in events[:limit]]
    return AuditExportResponse(
        count=len(exported_events),
        limit=limit,
        truncated=len(events) > limit,
        filters=_filter_summary(
            event_type=event_type,
            entity_id=entity_id,
            actor=actor,
            detail_key=detail_key,
            detail_value=detail_value,
            created_after=created_after,
            created_before=created_before,
        ),
        events=exported_events,
    )


@router.get("/retention-policy", response_model=AuditRetentionPolicy)
def get_audit_retention_policy(_: AuthUser = MaintainerDependency) -> AuditRetentionPolicy:
    return AuditRetentionPolicy()


@router.get("/events/{audit_event_id}", response_model=AuditEvent)
def get_audit_event(
    audit_event_id: str,
    request: Request,
    user: AuthUser = ReviewerDependency,
) -> AuditEvent:
    workspace_id = _workspace_for_route(user, request)
    repository = _audit_repository_for_route(request)
    if workspace_id is None and not isinstance(repository, SQLiteAuditEventRepository):
        raise HTTPException(status_code=404, detail="Audit event not found")
    event = repository.get_for_workspace(
        audit_event_id,
        workspace_id or "",
    )
    if event is None:
        raise HTTPException(status_code=404, detail="Audit event not found")
    return redact_audit_event(event)


@router.get("/recovery/export", response_model=RecoverySnapshot)
def export_recovery(request: Request, _: AuthUser = OwnerDependency) -> RecoverySnapshot:
    if get_settings().postgres_auth_enabled or get_settings().is_production:
        return export_recovery_snapshot(_, request=request)
    _require_authoritative_audit_runtime(request)
    return export_recovery_snapshot(_, request=request)


@router.post("/recovery/validate", response_model=RecoveryValidationResponse)
def validate_recovery(
    request: Request,
    payload: RecoveryValidationRequest,
    _: AuthUser = OwnerDependency,
) -> RecoveryValidationResponse:
    _require_authoritative_audit_runtime(request)
    return validate_recovery_snapshot(payload.snapshot)

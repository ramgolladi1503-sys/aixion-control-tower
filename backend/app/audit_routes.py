from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import require_maintainer, require_owner, require_reviewer
from .models import AuditEvent, AuthUser, now_utc
from .recovery_routes import (
    RecoverySnapshot,
    RecoveryValidationRequest,
    RecoveryValidationResponse,
    export_recovery_snapshot,
    validate_recovery_snapshot,
)
from .store import store

router = APIRouter(prefix="/audit", tags=["audit"])
ReviewerDependency = Depends(require_reviewer)
MaintainerDependency = Depends(require_maintainer)
OwnerDependency = Depends(require_owner)

AUDIT_EXPORT_MAX_LIMIT = 1_000
AUDIT_RETENTION_POLICY_VERSION = "aixion-audit-retention-v1"
REDACTED_DETAIL_VALUE = "[REDACTED]"
SENSITIVE_DETAIL_KEY_MARKERS = (
    "password",
    "secret",
    "token",
    "apikey",
    "authorization",
    "credential",
    "serverkey",
)


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


def _normalize_detail_key(key: str) -> str:
    return key.strip().lower().replace("_", "").replace("-", "")


def _is_sensitive_detail_key(key: str) -> bool:
    normalized = _normalize_detail_key(key)
    return any(marker in normalized for marker in SENSITIVE_DETAIL_KEY_MARKERS)


def _redact_details(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_detail_key(str(key)):
                redacted[str(key)] = REDACTED_DETAIL_VALUE
            else:
                redacted[str(key)] = _redact_details(item)
        return redacted
    if isinstance(value, list):
        return [_redact_details(item) for item in value]
    return value


def _redact_audit_event(event: AuditEvent) -> AuditEvent:
    return event.model_copy(update={"details": _redact_details(event.details)})


def _filter_audit_events(
    *,
    event_type: str | None = None,
    entity_id: str | None = None,
    actor: str | None = None,
    detail_key: str | None = None,
    detail_value: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> list[AuditEvent]:
    events = list(store.audit_events)
    if event_type is not None:
        events = [event for event in events if event.event_type == event_type]
    if entity_id is not None:
        events = [event for event in events if event.entity_id == entity_id]
    if actor is not None:
        events = [event for event in events if event.actor == actor]
    if detail_key is not None:
        events = [event for event in events if detail_key in event.details]
    if detail_key is not None and detail_value is not None:
        events = [event for event in events if str(event.details.get(detail_key)) == detail_value]
    if created_after is not None:
        events = [event for event in events if event.created_at >= created_after]
    if created_before is not None:
        events = [event for event in events if event.created_at <= created_before]
    return sorted(events, key=lambda event: event.created_at, reverse=True)


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
    event_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    detail_key: str | None = Query(default=None),
    detail_value: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _: AuthUser = ReviewerDependency,
) -> list[AuditEvent]:
    return _filter_audit_events(
        event_type=event_type,
        entity_id=entity_id,
        actor=actor,
        detail_key=detail_key,
        detail_value=detail_value,
    )[:limit]


@router.get("/export", response_model=AuditExportResponse)
def export_audit_events(
    event_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    detail_key: str | None = Query(default=None),
    detail_value: str | None = Query(default=None),
    created_after: datetime | None = Query(default=None),
    created_before: datetime | None = Query(default=None),
    limit: int = Query(default=1_000, ge=1, le=AUDIT_EXPORT_MAX_LIMIT),
    _: AuthUser = MaintainerDependency,
) -> AuditExportResponse:
    events = _filter_audit_events(
        event_type=event_type,
        entity_id=entity_id,
        actor=actor,
        detail_key=detail_key,
        detail_value=detail_value,
        created_after=created_after,
        created_before=created_before,
    )
    exported_events = [_redact_audit_event(event) for event in events[:limit]]
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
    _: AuthUser = ReviewerDependency,
) -> AuditEvent:
    event = next((item for item in store.audit_events if item.id == audit_event_id), None)
    if event is None:
        raise HTTPException(status_code=404, detail="Audit event not found")
    return event


@router.get("/recovery/export", response_model=RecoverySnapshot)
def export_recovery(_: AuthUser = OwnerDependency) -> RecoverySnapshot:
    return export_recovery_snapshot(_)


@router.post("/recovery/validate", response_model=RecoveryValidationResponse)
def validate_recovery(
    payload: RecoveryValidationRequest,
    _: AuthUser = OwnerDependency,
) -> RecoveryValidationResponse:
    return validate_recovery_snapshot(payload.snapshot)

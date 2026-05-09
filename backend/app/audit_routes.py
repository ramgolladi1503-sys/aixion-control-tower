from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import require_api_key
from .models import AuditEvent
from .store import store

router = APIRouter(prefix="/audit", tags=["audit"])
AuthDependency = Depends(require_api_key)


@router.get("/events", response_model=list[AuditEvent])
def query_audit_events(
    event_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    detail_key: str | None = Query(default=None),
    detail_value: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _: None = AuthDependency,
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

    return sorted(events, key=lambda event: event.created_at, reverse=True)[:limit]


@router.get("/events/{audit_event_id}", response_model=AuditEvent)
def get_audit_event(
    audit_event_id: str,
    _: None = AuthDependency,
) -> AuditEvent:
    event = next((item for item in store.audit_events if item.id == audit_event_id), None)
    if event is None:
        raise HTTPException(status_code=404, detail="Audit event not found")
    return event

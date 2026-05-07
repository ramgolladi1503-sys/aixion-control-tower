from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .auth import require_api_key
from .models import Notification, NotificationStatus
from .store import store

router = APIRouter(prefix="/notifications", tags=["notifications"])
AuthDependency = Depends(require_api_key)


@router.get("", response_model=list[Notification])
def list_notifications(_: None = AuthDependency) -> list[Notification]:
    return sorted(store.notifications.values(), key=lambda item: item.created_at, reverse=True)


@router.post("/{notification_id}/read", response_model=Notification)
def mark_notification_read(notification_id: str, _: None = AuthDependency) -> Notification:
    notification = store.notifications.get(notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.status = NotificationStatus.READ
    store.persist()
    return notification


def create_notification(title: str, body: str, entity_type: str, entity_id: str) -> Notification:
    notification = Notification(title=title, body=body, entity_type=entity_type, entity_id=entity_id)
    store.notifications[notification.id] = notification
    return notification

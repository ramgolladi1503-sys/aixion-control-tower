from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_user
from .models import AuthUser, DeviceRegistration, Notification, NotificationStatus, now_utc
from .store import store

router = APIRouter(prefix="/notifications", tags=["notifications"])
AuthDependency = Depends(require_user)
FCM_SEND_URL = "https://fcm.googleapis.com/fcm/send"


class DeviceRegistrationRequest(BaseModel):
    token: str
    platform: str = "android"
    app_version: str = ""


@router.get("", response_model=list[Notification])
def list_notifications(user: AuthUser = AuthDependency) -> list[Notification]:
    return sorted(
        [item for item in store.notifications.values() if item.user_id in {None, user.id}],
        key=lambda item: item.created_at,
        reverse=True,
    )


@router.post("/devices", response_model=DeviceRegistration)
def register_device(
    payload: DeviceRegistrationRequest,
    user: AuthUser = AuthDependency,
) -> DeviceRegistration:
    existing = next(
        (
            device
            for device in store.device_registrations.values()
            if device.token == payload.token and device.user_id == user.id
        ),
        None,
    )
    if existing:
        existing.platform = payload.platform
        existing.app_version = payload.app_version
        existing.updated_at = now_utc()
        store.persist()
        return existing

    device = DeviceRegistration(
        user_id=user.id,
        token=payload.token,
        platform=payload.platform,
        app_version=payload.app_version,
    )
    store.device_registrations[device.id] = device
    store.persist()
    return device


@router.post("/{notification_id}/read", response_model=Notification)
def mark_notification_read(notification_id: str, user: AuthUser = AuthDependency) -> Notification:
    notification = store.notifications.get(notification_id)
    if not notification or notification.user_id not in {None, user.id}:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.status = NotificationStatus.READ
    store.persist()
    return notification


def _send_fcm_to_token(token: str, title: str, body: str, data: dict[str, str]) -> str | None:
    server_key = os.getenv("FCM_SERVER_KEY", "").strip()
    if not server_key:
        return "SKIPPED_FCM_SERVER_KEY_NOT_CONFIGURED"

    payload: dict[str, Any] = {
        "to": token,
        "priority": "high",
        "notification": {"title": title, "body": body},
        "data": data,
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                FCM_SEND_URL,
                headers={"Authorization": f"key={server_key}", "Content-Type": "application/json"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        return f"FCM_NETWORK_ERROR: {exc}"

    if response.status_code >= 400:
        return f"FCM_ERROR_{response.status_code}: {response.text[:300]}"
    return None


def dispatch_push(notification: Notification) -> Notification:
    target_devices = [
        device
        for device in store.device_registrations.values()
        if notification.user_id in {None, device.user_id}
    ]
    if not target_devices:
        notification.push_status = "NO_DEVICE"
        return notification

    errors: list[str] = []
    skipped = False
    for device in target_devices:
        error = _send_fcm_to_token(
            token=device.token,
            title=notification.title,
            body=notification.body,
            data={
                "notification_id": notification.id,
                "entity_type": notification.entity_type,
                "entity_id": notification.entity_id,
            },
        )
        if error == "SKIPPED_FCM_SERVER_KEY_NOT_CONFIGURED":
            skipped = True
        elif error:
            errors.append(error)

    if errors:
        notification.push_status = "FAILED"
        notification.push_error = " | ".join(errors)
    elif skipped:
        notification.push_status = "SKIPPED"
        notification.push_error = "FCM_SERVER_KEY is not configured"
    else:
        notification.push_status = "SENT"
        notification.push_error = None
    return notification


def create_notification(
    title: str,
    body: str,
    entity_type: str,
    entity_id: str,
    user_id: str | None = None,
) -> Notification:
    notification = Notification(
        title=title,
        body=body,
        entity_type=entity_type,
        entity_id=entity_id,
        user_id=user_id,
    )
    dispatch_push(notification)
    store.notifications[notification.id] = notification
    return notification

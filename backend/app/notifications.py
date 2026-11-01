from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .auth import require_user
from .application_repository_access import workspace_id_for_request
from .application_repositories import ApplicationRepositories
from .models import AuditEvent, AuthUser, DeviceRegistration, Notification, NotificationStatus, now_utc
from .notification_outbox_repository import PostgresNotificationOutboxRepository
from .settings import get_settings
from .store import store
from .workspace_access import workspace_id_for_user
from .workspace_membership_repository import PostgresWorkspaceMembershipRepository

router = APIRouter(prefix="/notifications", tags=["notifications"])
logger = logging.getLogger(__name__)
AuthDependency = Depends(require_user)
FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_TIMEOUT_SECONDS = 15.0
PUSH_TITLE = "Aixion Control Tower"
PUSH_BODY = "A request is waiting for your review. Open the app to view details."


def _require_sqlite_notification_runtime() -> None:
    """Keep local notification helpers from becoming a PostgreSQL fallback."""
    settings = get_settings()
    if (
        getattr(settings, "is_production", False)
        or getattr(settings, "postgres_auth_enabled", False)
    ):
        raise RuntimeError(
            "PostgreSQL notification dispatcher is required; SQLite notification operations "
            "are unavailable in PostgreSQL or production mode"
        )


def _postgres_notification_repository(
    http_request: Request | None,
) -> PostgresNotificationOutboxRepository | None:
    settings = get_settings()
    # Production notification/device state must be owned by the same
    # app-bound PostgreSQL runtime as the authenticated workspace. The auth
    # rollout flag can be disabled independently, so it cannot authorize a
    # fallback to process-local SQLite in production.
    if (
        getattr(settings, "is_production", False)
        or getattr(settings, "postgres_auth_enabled", False)
    ):
        if http_request is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PostgreSQL notification runtime is unavailable",
            )
        state = http_request.app.state
        runtime = getattr(state, "postgres_runtime", None)
        repositories = getattr(state, "postgres_repositories", None)
        if (
            runtime is None
            or repositories is None
            or getattr(runtime, "repositories", None) is not repositories
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PostgreSQL notification runtime is unavailable",
            )
        repository = getattr(state, "notification_outbox_repository", None)
        expected_repository = getattr(repositories, "notification_outbox", None)
        membership_repository = getattr(repositories, "workspace_membership", None)
        if (
            not isinstance(repositories, ApplicationRepositories)
            or not isinstance(expected_repository, PostgresNotificationOutboxRepository)
            or not isinstance(
                membership_repository, PostgresWorkspaceMembershipRepository
            )
            or repository is not expected_repository
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PostgreSQL notification repository is unavailable",
            )
        return expected_repository

    repository = (
        getattr(http_request.app.state, "notification_outbox_repository", None)
        if http_request is not None
        else None
    )
    if repository is None:
        return None
    if not isinstance(repository, PostgresNotificationOutboxRepository):
        raise RuntimeError("Application notification repository has an unsupported adapter")
    return repository


class DeviceRegistrationRequest(BaseModel):
    # FCM tokens are opaque; bound their size and reject whitespace/control
    # characters without assuming a provider-specific token alphabet.
    token: str = Field(min_length=1, max_length=4096, pattern=r"^[^\s\x00-\x1f\x7f]+$")
    platform: Literal["android"] = "android"
    app_version: str = Field(default="", max_length=64)


@router.get("", response_model=list[Notification])
def list_notifications(
    user: AuthUser = AuthDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> list[Notification]:
    repository = _postgres_notification_repository(http_request)
    if repository is not None:
        workspace_id = workspace_id_for_request(user, http_request)
        if workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        try:
            return repository.list_for_user(user_id=user.id, workspace_id=workspace_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Workspace membership is required") from exc
    stored_user = store.users.get(user.id)
    workspace_id = user.workspace_id or (stored_user.workspace_id if stored_user else None)
    if workspace_id is None:
        if get_settings().is_production:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        visible = [item for item in store.notifications.values() if item.user_id in {None, user.id}]
    else:
        visible = [
            item
            for item in store.notifications.values()
            if item.workspace_id == workspace_id
            or (item.workspace_id is None and item.user_id == user.id)
        ]
    return sorted(
        visible,
        key=lambda item: item.created_at,
        reverse=True,
    )


@router.post("/devices", response_model=DeviceRegistration)
def register_device(
    payload: DeviceRegistrationRequest,
    user: AuthUser = AuthDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> DeviceRegistration:
    repository = _postgres_notification_repository(http_request)
    if repository is not None:
        workspace_id = workspace_id_for_request(user, http_request)
        if workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        session_id = getattr(http_request.state, "authenticated_session_id", None)
        if not isinstance(session_id, str) or not session_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="An authenticated session is required for device registration",
            )
        try:
            return repository.register_device(
                DeviceRegistration(
                    user_id=user.id,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    token=payload.token,
                    platform=payload.platform,
                    app_version=payload.app_version,
                )
            ).device
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Workspace membership is required") from exc
    with store.atomic():
        workspace_id = workspace_id_for_user(user)
        if get_settings().is_production and workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        mutation = store.device_registration_repository.register(
            DeviceRegistration(
                user_id=user.id,
                workspace_id=workspace_id,
                token=payload.token,
                platform=payload.platform,
                app_version=payload.app_version,
            )
        )
        for cached_id, cached_device in tuple(store.device_registrations.items()):
            if cached_id != mutation.device.id and cached_device.token == mutation.device.token:
                store.device_registrations.pop(cached_id, None)
        store.device_registrations[mutation.device.id] = mutation.device
        if mutation.audit_event is not None and all(
            item.id != mutation.audit_event.id for item in store.audit_events
        ):
            store.audit_events.append(mutation.audit_event)
        return mutation.device


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def unregister_device(
    device_id: str,
    user: AuthUser = AuthDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> Response:
    """Remove this user's device registration; unknown/foreign IDs are no-ops."""
    repository = _postgres_notification_repository(http_request)
    if repository is not None:
        workspace_id = workspace_id_for_request(user, http_request)
        if workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        repository.unregister_device(
            device_id=device_id,
            user_id=user.id,
            workspace_id=workspace_id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    with store.atomic():
        workspace_id = workspace_id_for_user(user)
        if get_settings().is_production and workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        mutation = store.device_registration_repository.unregister(
            device_id=device_id,
            user_id=user.id,
            workspace_id=workspace_id,
        )
        if mutation is not None:
            store.device_registrations.pop(device_id, None)
            if mutation.audit_event is not None and all(
                item.id != mutation.audit_event.id for item in store.audit_events
            ):
                store.audit_events.append(mutation.audit_event)
        else:
            cached_device = store.device_registrations.get(device_id)
            if (
                cached_device is not None
                and cached_device.user_id == user.id
                and cached_device.workspace_id == workspace_id
            ):
                store.device_registrations.pop(device_id, None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{notification_id}/read", response_model=Notification)
def mark_notification_read(
    notification_id: str,
    user: AuthUser = AuthDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> Notification:
    repository = _postgres_notification_repository(http_request)
    if repository is not None:
        workspace_id = workspace_id_for_request(user, http_request)
        if workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        try:
            notification = repository.mark_read(
                notification_id=notification_id,
                user_id=user.id,
                workspace_id=workspace_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Workspace membership is required") from exc
        if notification is None:
            raise HTTPException(status_code=404, detail="Notification not found")
        return notification
    with store.atomic():
        notification = store.notifications.get(notification_id)
        stored_user = store.users.get(user.id)
        workspace_id = user.workspace_id or (stored_user.workspace_id if stored_user else None)
        if not notification:
            raise HTTPException(status_code=404, detail="Notification not found")
        if workspace_id is not None:
            allowed = (
                notification.workspace_id == workspace_id
                if notification.workspace_id is not None
                else notification.user_id == user.id
            )
        elif get_settings().is_production:
            allowed = False
        else:
            allowed = notification.user_id in {None, user.id}
        if not allowed:
            raise HTTPException(status_code=404, detail="Notification not found")
        previous_status = notification.status
        notification.status = NotificationStatus.READ
        try:
            store.persist()
        except Exception:
            notification.status = previous_status
            raise
        return notification


def _fcm_access_token() -> str:
    # ADC supports workload identity in hosted environments and a protected
    # GOOGLE_APPLICATION_CREDENTIALS file for local/on-prem deployments.
    from google.auth import default
    from google.auth.transport.requests import Request

    credentials, _ = default(scopes=[FCM_SCOPE])
    request = Request()

    def bounded_request(url: str, **kwargs: Any) -> Any:
        # google-auth's Requests transport defaults to 120 seconds. Bound each
        # token-endpoint HTTP request to the FCM request timeout. google-auth
        # may retry, so this is not a total refresh or notification-lease bound.
        kwargs.setdefault("timeout", FCM_TIMEOUT_SECONDS)
        return request(url, **kwargs)

    credentials.refresh(bounded_request)
    if not credentials.token:
        raise RuntimeError("FCM credentials did not produce an access token")
    return credentials.token


def _send_fcm_to_token(token: str, title: str, body: str, data: dict[str, str]) -> str | None:
    project_id = get_settings().fcm_project_id
    if not project_id:
        return "SKIPPED_FCM_PROJECT_ID_NOT_CONFIGURED"

    try:
        access_token = _fcm_access_token()
    except Exception:
        # Credential/provider exception details can expose local paths or identity data.
        return "FCM_CREDENTIALS_UNAVAILABLE"

    payload: dict[str, Any] = {
        "message": {
            "token": token,
            "android": {"priority": "HIGH"},
            # Use a data-only message so delivered foreground/background pushes are
            # handled by the app service, which attaches the exact approval deep link.
            # FCM notification messages can be displayed directly by Android without
            # invoking onMessageReceived. Keep lock-screen copy generic; authenticated
            # app fetches expose approval details after the user opens the app.
            "data": {**data, "title": PUSH_TITLE, "body": PUSH_BODY},
        },
    }
    try:
        with httpx.Client(timeout=FCM_TIMEOUT_SECONDS) as client:
            response = client.post(
                FCM_SEND_URL.format(project_id=project_id),
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json=payload,
            )
    except httpx.HTTPError:
        # Transport exception strings can contain request/provider details. Keep
        # persisted diagnostics bounded and independent of exception contents.
        return "FCM_NETWORK_ERROR"

    if response.status_code >= 400:
        # Provider response bodies may contain identifiers or implementation details.
        # Preserve the status for diagnostics without copying provider content into API state.
        # FCM's documented UNREGISTERED response is authoritative evidence that this
        # installation token is no longer valid; do not retain or retry it.
        try:
            provider_status = response.json().get("error", {}).get("status")
        except (ValueError, AttributeError):
            provider_status = None
        if provider_status == "UNREGISTERED":
            return "FCM_TOKEN_UNREGISTERED"
        return f"FCM_ERROR_{response.status_code}"
    return None


def dispatch_push(notification: Notification) -> Notification:
    _require_sqlite_notification_runtime()
    production = get_settings().is_production
    return _dispatch_push_from_local_state(notification, production=production)


def _dispatch_push_from_local_state(
    notification: Notification, *, production: bool
) -> Notification:
    with store.atomic():
        devices = [
            device.model_copy(deep=True)
            for device in store.device_registrations.values()
        ]
        users = dict(store.users)
    if production and notification.workspace_id is None and notification.user_id is None:
        # An unscoped broadcast has no safe recipient set in the account model.
        # Keep it durable for operator review, but never fan it out to all devices.
        notification.push_status = "SKIPPED"
        notification.push_error = "RECIPIENT_SCOPE_MISSING"
        return notification
    target_devices = [
        device
        for device in devices
        if (
            (notification.user_id is None or notification.user_id == device.user_id)
            and (
                notification.workspace_id is not None
                and device.workspace_id == notification.workspace_id
                and (
                    not production
                    or (
                        device.user_id in users
                        and users[device.user_id].workspace_id == notification.workspace_id
                    )
                )
                or not production and notification.workspace_id is None
            )
        )
    ]
    if not target_devices:
        notification.push_status = "NO_DEVICE"
        return notification

    errors: list[str] = []
    accepted = 0
    invalid_tokens: list[tuple[str, str]] = []
    skipped = False
    for device in target_devices:
        error = _send_fcm_to_token(
            token=device.token,
            title=notification.title,
            body=notification.body,
            data={
                "notification_id": notification.id,
                "entity_type": notification.push_entity_type,
                "entity_id": notification.push_entity_id,
            },
        )
        if error == "SKIPPED_FCM_PROJECT_ID_NOT_CONFIGURED":
            skipped = True
        elif error is None:
            accepted += 1
        elif error == "FCM_TOKEN_UNREGISTERED":
            invalid_tokens.append((device.id, device.token))
            errors.append("FCM_TOKEN_UNREGISTERED")
        elif error:
            errors.append(error)

    if invalid_tokens:
        with store.atomic():
            removed_devices: list[DeviceRegistration] = []
            audit_count = len(store.audit_events)
            for device_id, token in invalid_tokens:
                current = store.device_registrations.get(device_id)
                # A registration may have been rebound during the provider call.
                # Remove it only if it still represents the rejected token.
                if current is not None and current.token == token:
                    removed_devices.append(current.model_copy(deep=True))
                    store.device_registrations.pop(device_id, None)
                    store.audit_events.append(
                        AuditEvent(
                            event_type="notification.device_token_invalidated",
                            actor="system:fcm",
                            entity_id=device_id,
                            workspace_id=current.workspace_id,
                            details={"platform": current.platform},
                        )
                    )
            try:
                store.persist()
            except Exception:
                # Token cleanup is maintenance; it must not turn a partially
                # delivered notification into a whole-fanout retry.
                for device in removed_devices:
                    store.device_registrations[device.id] = device
                del store.audit_events[audit_count:]
                logger.warning("Invalid push token cleanup persistence failed")

    if accepted:
        # A push reached at least one device. Retrying the whole fan-out would
        # duplicate it on successful devices; retain bounded diagnostics only.
        notification.push_status = "SENT"
        notification.push_error = "PARTIAL_DELIVERY" if errors or skipped else None
    elif errors:
        notification.push_status = "FAILED"
        notification.push_error = " | ".join(errors)
    elif skipped:
        notification.push_status = "SKIPPED"
        notification.push_error = "AIXION_FCM_PROJECT_ID is not configured"
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
    workspace_id: str | None = None,
    approval_request_id: str | None = None,
) -> Notification:
    _require_sqlite_notification_runtime()
    if workspace_id is None:
        if entity_type == "approval_request":
            approval = store.approval_requests.get(entity_id)
            if approval:
                project = store.projects.get(approval.project_id)
                workspace_id = approval.workspace_id or (project.workspace_id if project else None)
        elif entity_type == "agent_task":
            task = store.agent_tasks.get(entity_id)
            if task and task.project_id:
                project = store.projects.get(task.project_id)
                workspace_id = project.workspace_id if project else None
    notification = Notification(
        title=title,
        body=body,
        entity_type=entity_type,
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        approval_request_id=approval_request_id,
    )
    # Persist the notification with the associated approval/task before it is
    # eligible for asynchronous FCM dispatch.
    with store.atomic():
        store.notifications[notification.id] = notification
        try:
            store.persist()
        except Exception:
            store.notifications.pop(notification.id, None)
            raise
    return notification


def _push_retry_delay_seconds(*, attempts: int, base_seconds: int, maximum_seconds: int) -> int:
    return min(base_seconds * (2 ** max(0, attempts - 1)), maximum_seconds)


def _is_retryable_push_failure(error: str | None) -> bool:
    if error in {"FCM_NETWORK_ERROR", "FCM_CREDENTIALS_UNAVAILABLE", "FCM_DISPATCH_FAILED"}:
        return True
    if not error:
        return False
    statuses = [int(value) for value in re.findall(r"FCM_ERROR_(\d{3})", error)]
    return any(status in {408, 425, 429} or status >= 500 for status in statuses)


def dispatch_one_due_push(*, now: datetime | None = None) -> str | None:
    """Claim and send one durable notification from the single-process SQLite queue."""
    _require_sqlite_notification_runtime()
    settings = get_settings()
    if not settings.push_dispatch_enabled:
        return None

    current_time = now or now_utc()
    with store.atomic():
        eligible = []
        for item in list(store.notifications.values()):
            due_time = item.push_next_attempt_at
            if item.push_status == "SENDING":
                due_time = item.push_lease_expires_at
            elif item.push_status not in {"PENDING", "RETRY_WAIT"}:
                continue
            if due_time is None or due_time <= current_time:
                eligible.append(item)
        if not eligible:
            return None

        notification = min(eligible, key=lambda item: (item.created_at, item.id))
        if notification.push_attempts >= settings.push_max_attempts:
            notification.push_status = "FAILED"
            notification.push_error = "FCM_RETRIES_EXHAUSTED"
            notification.push_next_attempt_at = None
            notification.push_lease_expires_at = None
            store.persist()
            return notification.id

        notification.push_attempts += 1
        notification.push_status = "SENDING"
        notification.push_error = None
        notification.push_next_attempt_at = None
        notification.push_lease_expires_at = current_time + timedelta(
            seconds=settings.push_lease_seconds
        )
        # This durable claim bounds retries across an API restart. If a process
        # dies mid-send, the record becomes eligible again when this lease ends.
        store.persist()

    # The provider call runs outside the store lock. Keep its mutable result on
    # a detached model so concurrent persistence cannot observe an outcome
    # before this worker records it under the lock.
    dispatch_result = notification.model_copy(deep=True)
    try:
        dispatch_push(dispatch_result)
    except Exception:
        dispatch_result.push_status = "FAILED"
        dispatch_result.push_error = "FCM_DISPATCH_FAILED"
        logger.warning("Push dispatch failed for notification id=%s", notification.id)

    with store.atomic():
        notification.push_status = dispatch_result.push_status
        notification.push_error = dispatch_result.push_error
        notification.push_lease_expires_at = None
        completed_at = current_time if now is not None else now_utc()
        if notification.push_status == "FAILED":
            if (
                _is_retryable_push_failure(notification.push_error)
                and notification.push_attempts < settings.push_max_attempts
            ):
                delay = _push_retry_delay_seconds(
                    attempts=notification.push_attempts,
                    base_seconds=settings.push_retry_base_seconds,
                    maximum_seconds=settings.push_retry_max_seconds,
                )
                notification.push_status = "RETRY_WAIT"
                notification.push_next_attempt_at = completed_at + timedelta(seconds=delay)
            elif _is_retryable_push_failure(notification.push_error):
                notification.push_error = "FCM_RETRIES_EXHAUSTED"
                notification.push_next_attempt_at = None
            else:
                notification.push_next_attempt_at = None
        else:
            notification.push_next_attempt_at = None
        try:
            store.persist()
        except Exception:
            # The durable SENDING claim expires and permits safe at-least-once
            # recovery. This can duplicate a delivered push, so clients must
            # treat the notification id as the deduplication key.
            logger.warning("Push outcome persistence failed for notification id=%s", notification.id)
    return notification.id


async def notification_dispatch_loop() -> None:
    """Run the opt-in local/demo outbox worker in the API process."""
    _require_sqlite_notification_runtime()
    settings = get_settings()
    if not settings.push_dispatch_enabled:
        logger.info("Push dispatch loop disabled by configuration")
        return
    while True:
        try:
            await asyncio.to_thread(dispatch_one_due_push)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Keep the worker alive while avoiding secrets/provider details in logs.
            logger.warning("Push dispatch loop iteration failed")
        await asyncio.sleep(settings.push_dispatch_poll_seconds)

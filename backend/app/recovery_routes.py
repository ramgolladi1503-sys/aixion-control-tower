from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .auth import require_owner
from .models import (
    ApprovalRequest,
    AuditEvent,
    AuthUser,
    Idea,
    MCPChildServer,
    MCPPendingRequest,
    Notification,
    Project,
    TestRun,
    WorkOrder,
    now_utc,
)
from .store import store

router = APIRouter(prefix="/ops/recovery", tags=["recovery"])
RECOVERY_FORMAT_VERSION = "aixion-control-tower-recovery-v1"

SAFE_ENTITY_MODELS: dict[str, type[BaseModel]] = {
    "projects": Project,
    "mcp_child_servers": MCPChildServer,
    "ideas": Idea,
    "work_orders": WorkOrder,
    "approval_requests": ApprovalRequest,
    "mcp_pending_requests": MCPPendingRequest,
    "test_runs": TestRun,
    "notifications": Notification,
    "audit_events": AuditEvent,
}


class RecoverySnapshot(BaseModel):
    format_version: str = RECOVERY_FORMAT_VERSION
    generated_at: datetime = Field(default_factory=now_utc)
    applied_migrations: list[dict[str, str]] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    excluded_sensitive_collections: list[str] = Field(default_factory=list)
    entities: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class RecoveryValidationRequest(BaseModel):
    snapshot: RecoverySnapshot


class RecoveryValidationResponse(BaseModel):
    valid: bool
    format_version: str
    counts: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _safe_counts() -> dict[str, int]:
    return {
        "projects": len(store.projects),
        "mcp_child_servers": len(store.mcp_child_servers),
        "ideas": len(store.ideas),
        "work_orders": len(store.work_orders),
        "approval_requests": len(store.approval_requests),
        "mcp_pending_requests": len(store.mcp_pending_requests),
        "test_runs": len(store.test_runs),
        "notifications": len(store.notifications),
        "audit_events": len(store.audit_events),
    }


def _safe_entities() -> dict[str, list[dict[str, Any]]]:
    return {
        "projects": [item.model_dump(mode="json") for item in store.projects.values()],
        "mcp_child_servers": [item.model_dump(mode="json") for item in store.mcp_child_servers.values()],
        "ideas": [item.model_dump(mode="json") for item in store.ideas.values()],
        "work_orders": [item.model_dump(mode="json") for item in store.work_orders.values()],
        "approval_requests": [item.model_dump(mode="json") for item in store.approval_requests.values()],
        "mcp_pending_requests": [item.model_dump(mode="json") for item in store.mcp_pending_requests.values()],
        "test_runs": [item.model_dump(mode="json") for item in store.test_runs.values()],
        "notifications": [item.model_dump(mode="json") for item in store.notifications.values()],
        "audit_events": [item.model_dump(mode="json") for item in store.audit_events],
    }


def validate_recovery_snapshot(snapshot: RecoverySnapshot) -> RecoveryValidationResponse:
    errors: list[str] = []
    warnings: list[str] = []
    counts: dict[str, int] = {}

    if snapshot.format_version != RECOVERY_FORMAT_VERSION:
        errors.append(f"Unsupported recovery format_version: {snapshot.format_version}")

    for entity_name, model in SAFE_ENTITY_MODELS.items():
        raw_items = snapshot.entities.get(entity_name, [])
        if not isinstance(raw_items, list):
            errors.append(f"{entity_name} must be a list")
            continue
        counts[entity_name] = len(raw_items)
        for index, raw_item in enumerate(raw_items):
            try:
                model.model_validate(raw_item)
            except Exception as exc:
                errors.append(f"{entity_name}[{index}] is invalid: {exc}")

    unknown_entities = sorted(set(snapshot.entities) - set(SAFE_ENTITY_MODELS))
    for entity_name in unknown_entities:
        errors.append(f"Unknown entity collection: {entity_name}")

    if not snapshot.applied_migrations:
        warnings.append("Snapshot has no migration metadata; restore should not proceed without manual DB inspection.")

    if snapshot.excluded_sensitive_collections:
        warnings.append("Snapshot excludes auth/session/device/agent secrets and is not a full credential restore artifact.")

    return RecoveryValidationResponse(
        valid=not errors,
        format_version=snapshot.format_version,
        counts=counts,
        errors=errors,
        warnings=warnings,
    )


@router.get("/export", response_model=RecoverySnapshot)
def export_recovery_snapshot(_: AuthUser = Depends(require_owner)) -> RecoverySnapshot:
    return RecoverySnapshot(
        applied_migrations=store.applied_migrations(),
        counts=_safe_counts(),
        excluded_sensitive_collections=[
            "users",
            "sessions",
            "invites",
            "external_agents",
            "device_registrations",
        ],
        entities=_safe_entities(),
    )


@router.post("/validate", response_model=RecoveryValidationResponse)
def validate_recovery(
    payload: RecoveryValidationRequest,
    _: AuthUser = Depends(require_owner),
) -> RecoveryValidationResponse:
    return validate_recovery_snapshot(payload.snapshot)

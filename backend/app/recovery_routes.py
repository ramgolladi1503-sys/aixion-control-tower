from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from .agent_task_models import AgentTask
from .auth import require_owner
from .settings import get_settings
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
    User,
    Workspace,
    WorkOrder,
    now_utc,
)
from .recovery_repository import SQLiteRecoverySnapshotRepository
from .store import store
from .workspace_access import workspace_id_for_user

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
SAFE_ENTITY_STORAGE_TYPES = {
    "projects": "project",
    "mcp_child_servers": "mcp_child_server",
    "ideas": "idea",
    "work_orders": "work_order",
    "approval_requests": "approval_request",
    "mcp_pending_requests": "mcp_pending_request",
    "test_runs": "test_run",
    "notifications": "notification",
    "audit_events": "audit_event",
}
recovery_snapshot_repository = SQLiteRecoverySnapshotRepository(store._connect)

EXCLUDED_COLLECTIONS = [
    "workspaces",
    "users",
    "sessions",
    "invites",
    "external_agents",
    "external_agent_credentials",
    "agent_connectors",
    "device_registrations",
    "agent_tasks",
    "agent_task_events",
    "agent_runs",
    "agent_run_steps",
    "agent_run_events",
    "agent_run_faults",
    "capability_leases",
    "reviewer_attestations",
    "proposed_actions",
    "policy_decisions",
    "action_authorizations",
    "action_consumptions",
    "credential_grants",
    "trust_events",
]
EXCLUDED_SENSITIVE_COLLECTIONS = [
    "users",
    "sessions",
    "invites",
    "external_agents",
    "external_agent_credentials",
    "agent_connectors",
    "device_registrations",
    "capability_leases",
    "reviewer_attestations",
    "proposed_actions",
    "policy_decisions",
    "action_authorizations",
    "action_consumptions",
    "credential_grants",
    "trust_events",
]


class RecoverySnapshot(BaseModel):
    format_version: str = RECOVERY_FORMAT_VERSION
    generated_at: datetime = Field(default_factory=now_utc)
    applied_migrations: list[dict[str, str]] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    excluded_collections: list[str] = Field(default_factory=list)
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


def _decode_snapshot_records(
    records: dict[str, list[tuple[str, str]]],
) -> dict[str, list[BaseModel]]:
    entities: dict[str, list[BaseModel]] = {}
    for entity_name, model in SAFE_ENTITY_MODELS.items():
        decoded: list[BaseModel] = []
        for entity_id, payload in records[entity_name]:
            item = model.model_validate_json(payload)
            if getattr(item, "id", None) != entity_id:
                raise ValueError(f"Recovery source identity mismatch in {entity_name}.")
            decoded.append(item)
        entities[entity_name] = decoded
    return entities


def _workspace_entities(
    workspace_id: str | None,
    records: dict[str, list[tuple[str, str]]] | None = None,
    task_records: list[tuple[str, str]] | None = None,
) -> dict[str, list[BaseModel]]:
    if records is None or task_records is None:
        records, _, task_records, _ = recovery_snapshot_repository.read_snapshot(
            SAFE_ENTITY_STORAGE_TYPES,
            scope_entity_type="agent_task",
        )
    entities = _decode_snapshot_records(records)
    if workspace_id is None:
        return entities

    by_id = {
        name: {getattr(item, "id"): item for item in items}
        for name, items in entities.items()
    }
    tasks: dict[str, AgentTask] = {}
    for entity_id, payload in task_records:
        try:
            task = AgentTask.model_validate_json(payload)
        except ValidationError:
            # Tasks are excluded from the export. A corrupt scope-only row
            # cannot safely authorize an audit event to a workspace.
            continue
        if task.id != entity_id:
            continue
        tasks[task.id] = task
    projects = by_id["projects"]
    approvals = by_id["approval_requests"]

    def project_workspace(project_id: str | None) -> str | None:
        project = projects.get(project_id) if project_id else None
        return getattr(project, "workspace_id", None)

    def approval_workspace(approval_id: str | None) -> str | None:
        approval = approvals.get(approval_id) if approval_id else None
        if approval is None:
            return None
        parent_workspace = project_workspace(getattr(approval, "project_id", None))
        explicit_workspace = getattr(approval, "workspace_id", None)
        if parent_workspace is None or (
            explicit_workspace is not None and explicit_workspace != parent_workspace
        ):
            return None
        return explicit_workspace or parent_workspace

    def project_child_workspace(item: BaseModel) -> str | None:
        explicit = getattr(item, "workspace_id", None)
        parent = project_workspace(getattr(item, "project_id", None))
        if parent is None or (explicit is not None and explicit != parent):
            return None
        return explicit or parent

    def task_workspace(task_id: str | None) -> str | None:
        task = tasks.get(task_id) if task_id else None
        if task is None:
            return None
        parent = project_workspace(task.project_id)
        if parent is None or (task.workspace_id is not None and task.workspace_id != parent):
            return None
        return task.workspace_id or parent

    def item_workspace(entity_name: str, item: BaseModel) -> str | None:
        explicit = getattr(item, "workspace_id", None)
        if entity_name == "projects":
            return explicit
        if entity_name == "ideas":
            project_id = getattr(item, "project_id", None)
            if project_id is None:
                return explicit
            return project_child_workspace(item)
        if entity_name in {"work_orders", "mcp_child_servers"}:
            return project_child_workspace(item)
        if entity_name == "approval_requests":
            return approval_workspace(getattr(item, "id", None))
        if entity_name in {"mcp_pending_requests", "test_runs"}:
            approval_id = getattr(item, "approval_request_id", None)
            scope = approval_workspace(approval_id)
            approval = approvals.get(approval_id)
            if scope is None or approval is None:
                return None
            if (
                entity_name == "mcp_pending_requests"
                and getattr(item, "project_id", None) != getattr(approval, "project_id", None)
            ):
                return None
            return scope if explicit is None or explicit == scope else None
        if entity_name == "notifications":
            return explicit
        if entity_name == "audit_events":
            details = getattr(item, "details", {})
            entity_id = getattr(item, "entity_id", None)
            inferred = None
            if entity_id in projects:
                inferred = project_workspace(entity_id)
            elif entity_id in approvals:
                inferred = approval_workspace(entity_id)
            else:
                project_id = details.get("project_id")
                approval_id = details.get("approval_request_id")
                task_id = details.get("agent_task_id") or details.get("task_id")
                if isinstance(project_id, str):
                    inferred = project_workspace(project_id)
                elif isinstance(approval_id, str):
                    inferred = approval_workspace(approval_id)
                elif isinstance(task_id, str):
                    inferred = task_workspace(task_id)
            if explicit is not None and inferred is not None and explicit != inferred:
                return None
            return explicit or inferred
        return None

    return {
        entity_name: [
            item for item in items if item_workspace(entity_name, item) == workspace_id
        ]
        for entity_name, items in entities.items()
    }


def _safe_entities(
    workspace_id: str | None = None,
    *,
    records: dict[str, list[tuple[str, str]]] | None = None,
    task_records: list[tuple[str, str]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    entities = _workspace_entities(workspace_id, records, task_records)
    return {
        entity_name: [item.model_dump(mode="json") for item in items]
        for entity_name, items in entities.items()
    }


def validate_recovery_snapshot(snapshot: RecoverySnapshot) -> RecoveryValidationResponse:
    errors: list[str] = []
    warnings: list[str] = []
    counts: dict[str, int] = {}
    validated_entities: dict[str, list[BaseModel]] = {}

    if snapshot.format_version != RECOVERY_FORMAT_VERSION:
        errors.append(f"Unsupported recovery format_version: {snapshot.format_version}")

    missing_entities = sorted(set(SAFE_ENTITY_MODELS) - set(snapshot.entities))
    for entity_name in missing_entities:
        errors.append(f"Missing entity collection: {entity_name}")

    unknown_count_collections = sorted(set(snapshot.counts) - set(SAFE_ENTITY_MODELS))
    for entity_name in unknown_count_collections:
        errors.append(f"Unknown count collection: {entity_name}")

    for entity_name in sorted(set(SAFE_ENTITY_MODELS) - set(snapshot.counts)):
        errors.append(f"Missing count for entity collection: {entity_name}")

    for entity_name, model in SAFE_ENTITY_MODELS.items():
        raw_items = snapshot.entities.get(entity_name, [])
        if not isinstance(raw_items, list):
            errors.append(f"{entity_name} must be a list")
            continue
        counts[entity_name] = len(raw_items)
        validated_entities[entity_name] = []
        declared_count = snapshot.counts.get(entity_name)
        if declared_count is not None and declared_count != len(raw_items):
            errors.append(
                f"{entity_name} count mismatch: declared {declared_count}, found {len(raw_items)}"
            )
        seen_ids: set[str] = set()
        for index, raw_item in enumerate(raw_items):
            try:
                validated = model.model_validate(raw_item)
            except Exception as exc:
                errors.append(f"{entity_name}[{index}] is invalid: {exc}")
                continue
            validated_entities[entity_name].append(validated)
            entity_id = getattr(validated, "id", None)
            if isinstance(entity_id, str):
                if entity_id in seen_ids:
                    errors.append(f"{entity_name}[{index}] duplicates id {entity_id}")
                seen_ids.add(entity_id)

    projects = {
        item.id: item for item in validated_entities.get("projects", [])
        if isinstance(item, Project)
    }
    ideas = {
        item.id: item for item in validated_entities.get("ideas", [])
        if isinstance(item, Idea)
    }
    work_orders = {
        item.id: item for item in validated_entities.get("work_orders", [])
        if isinstance(item, WorkOrder)
    }
    approvals = {
        item.id: item for item in validated_entities.get("approval_requests", [])
        if isinstance(item, ApprovalRequest)
    }

    def require_parent(collection: str, index: int, field: str, parent_id: str) -> None:
        parent_collection = {
            "project_id": "projects",
            "idea_id": "ideas",
            "work_order_id": "work_orders",
            "approval_request_id": "approval_requests",
        }[field]
        parent_ids = {
            "projects": projects,
            "ideas": ideas,
            "work_orders": work_orders,
            "approval_requests": approvals,
        }[parent_collection]
        if parent_id not in parent_ids:
            errors.append(
                f"{collection}[{index}] references missing {parent_collection} id {parent_id}"
            )

    def check_workspace_scope(collection: str, index: int, item: BaseModel, project: Project) -> None:
        item_workspace_id = getattr(item, "workspace_id", None)
        if item_workspace_id and project.workspace_id and item_workspace_id != project.workspace_id:
            errors.append(f"{collection}[{index}] workspace does not match its project")

    for index, item in enumerate(validated_entities.get("ideas", [])):
        if isinstance(item, Idea) and item.project_id is not None:
            require_parent("ideas", index, "project_id", item.project_id)
            project = projects.get(item.project_id)
            if project:
                check_workspace_scope("ideas", index, item, project)

    for index, item in enumerate(validated_entities.get("work_orders", [])):
        if not isinstance(item, WorkOrder):
            continue
        require_parent("work_orders", index, "project_id", item.project_id)
        if item.idea_id is not None:
            require_parent("work_orders", index, "idea_id", item.idea_id)
        project = projects.get(item.project_id)
        if project:
            check_workspace_scope("work_orders", index, item, project)

    for index, item in enumerate(validated_entities.get("mcp_child_servers", [])):
        if isinstance(item, MCPChildServer):
            require_parent("mcp_child_servers", index, "project_id", item.project_id)
            project = projects.get(item.project_id)
            if project:
                check_workspace_scope("mcp_child_servers", index, item, project)

    for index, item in enumerate(validated_entities.get("approval_requests", [])):
        if not isinstance(item, ApprovalRequest):
            continue
        require_parent("approval_requests", index, "project_id", item.project_id)
        if item.work_order_id is not None:
            require_parent("approval_requests", index, "work_order_id", item.work_order_id)
        project = projects.get(item.project_id)
        if project and item.workspace_id and project.workspace_id and item.workspace_id != project.workspace_id:
            errors.append(f"approval_requests[{index}] workspace does not match its project")
        if item.work_order_id and item.work_order_id in work_orders:
            if work_orders[item.work_order_id].project_id != item.project_id:
                errors.append(f"approval_requests[{index}] work order belongs to a different project")
        if project:
            check_workspace_scope("approval_requests", index, item, project)

    for index, item in enumerate(validated_entities.get("mcp_pending_requests", [])):
        if not isinstance(item, MCPPendingRequest):
            continue
        require_parent("mcp_pending_requests", index, "project_id", item.project_id)
        require_parent("mcp_pending_requests", index, "approval_request_id", item.approval_request_id)
        approval = approvals.get(item.approval_request_id)
        if approval and approval.project_id != item.project_id:
            errors.append(f"mcp_pending_requests[{index}] approval belongs to a different project")
        project = projects.get(item.project_id)
        if project:
            check_workspace_scope("mcp_pending_requests", index, item, project)

    for index, item in enumerate(validated_entities.get("test_runs", [])):
        if isinstance(item, TestRun):
            require_parent("test_runs", index, "approval_request_id", item.approval_request_id)

    unknown_entities = sorted(set(snapshot.entities) - set(SAFE_ENTITY_MODELS))
    for entity_name in unknown_entities:
        errors.append(f"Unknown entity collection: {entity_name}")

    if not snapshot.applied_migrations:
        warnings.append("Snapshot has no migration metadata; restore should not proceed without manual DB inspection.")

    if snapshot.excluded_sensitive_collections:
        warnings.append("Snapshot excludes auth/session/device/agent secrets and is not a full credential restore artifact.")
    if not snapshot.excluded_collections:
        warnings.append("Snapshot has no complete excluded-collection inventory; data completeness is not established.")
    elif set(snapshot.excluded_collections) != set(EXCLUDED_COLLECTIONS):
        warnings.append("Snapshot excluded-collection inventory differs from this backend; review before recovery.")

    return RecoveryValidationResponse(
        valid=not errors,
        format_version=snapshot.format_version,
        counts=counts,
        errors=errors,
        warnings=warnings,
    )


@router.get("/export", response_model=RecoverySnapshot)
def export_recovery_snapshot(
    user: AuthUser = Depends(require_owner),
    request: Request = None,  # type: ignore[assignment]
) -> RecoverySnapshot:
    _require_sqlite_recovery_runtime(request)
    # Read all rows and migration metadata from one SQLite read transaction.
    # In-memory maps can lag repository transactions and are not snapshot authority.
    records, migration_rows, task_records, membership_records = recovery_snapshot_repository.read_snapshot(
        SAFE_ENTITY_STORAGE_TYPES,
        scope_entity_type="agent_task",
        membership_user_id=user.id,
        membership_workspace_id=user.workspace_id,
    )
    stored_users = membership_records["user"]
    if stored_users:
        user_id, user_payload = stored_users[0]
        stored_user = User.model_validate_json(user_payload)
        if stored_user.id != user_id or stored_user.id != user.id:
            raise HTTPException(status_code=403, detail="Workspace membership is invalid")
        workspace_id = stored_user.workspace_id
        if workspace_id is None or (
            user.workspace_id is not None and user.workspace_id != workspace_id
        ):
            raise HTTPException(status_code=403, detail="Workspace membership is invalid")
        stored_workspaces = membership_records["workspace"]
        if not stored_workspaces:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        workspace_key, workspace_payload = stored_workspaces[0]
        workspace = Workspace.model_validate_json(workspace_payload)
        if workspace_key != workspace_id or workspace.id != workspace_key:
            raise HTTPException(status_code=403, detail="Workspace membership is invalid")
    elif get_settings().auth_enabled:
        raise HTTPException(status_code=403, detail="Durable account membership is required")
    else:
        # Auth-disabled local/demo mode retains compatibility for synthetic users.
        workspace_id = workspace_id_for_user(user)
    entities = _safe_entities(
        workspace_id,
        records=records,
        task_records=task_records,
    )
    counts = {entity_name: len(items) for entity_name, items in entities.items()}
    applied_migrations = [
        {"migration_id": migration_id, "name": name, "applied_at": applied_at}
        for migration_id, name, applied_at in migration_rows
    ]
    return RecoverySnapshot(
        applied_migrations=applied_migrations,
        counts=counts,
        excluded_collections=EXCLUDED_COLLECTIONS.copy(),
        excluded_sensitive_collections=EXCLUDED_SENSITIVE_COLLECTIONS.copy(),
        entities=entities,
    )


def _require_sqlite_recovery_runtime(request: Request | None = None) -> None:
    """Prevent recovery exports from silently reading SQLite in PostgreSQL mode."""
    postgres_runtime = (
        getattr(request.app.state, "postgres_runtime", None) if request is not None else None
    )
    postgres_repositories = (
        getattr(request.app.state, "postgres_repositories", None)
        if request is not None
        else None
    )
    if (
        postgres_runtime is not None
        or postgres_repositories is not None
        or get_settings().postgres_auth_enabled
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                "Recovery exports are unavailable while PostgreSQL is configured "
                "without an authoritative recovery repository."
            ),
        )


@router.post("/validate", response_model=RecoveryValidationResponse)
def validate_recovery(
    payload: RecoveryValidationRequest,
    _: AuthUser = Depends(require_owner),
) -> RecoveryValidationResponse:
    return validate_recovery_snapshot(payload.snapshot)

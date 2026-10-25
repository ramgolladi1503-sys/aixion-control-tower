from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
import psycopg
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, Field

from .auth import require_maintainer, require_reviewer
from .approval_decisions import approval_decision_repository
from .child_mcp_test_server import ChildMCPToolCall
from .mcp_child_router import MCPChildServerRouter
from .mcp_gateway import (
    MCP_MUTATING_TOOL_NAMES,
    MCP_PENDING_FORWARD_OUTCOME_UNKNOWN,
    MCPGatewayApprovalDecisionLayer,
    MCPGatewayDecision,
    MCPGatewayToolCall,
)
from .mcp_pending_repository import (
    MCPPendingRepositoryConflict,
    MCPPendingRepositoryError,
    PostgresMCPPendingRepository,
)
from .project_repository import PostgresProjectRepository
from .approval_repository import PostgresApprovalDecisionRepository
from .mcp_tool_validation import validate_arguments_against_schema
from .application_repository_access import workspace_id_for_request
from .models import (
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    AuthUser,
    FileChange,
    MCPPendingRequest,
    MCPPendingStatus,
    Notification,
    RiskAssessment,
    RiskLevel,
    now_utc,
)
from .store import store
from .settings import get_settings
from .workspace_access import (
    approval_scope_is_consistent,
    project_workspace_id,
    workspace_id_for_user,
)

router = APIRouter(prefix="/mcp-gateway", tags=["mcp-gateway"])
ReviewerDependency = Depends(require_reviewer)
MaintainerDependency = Depends(require_maintainer)
ReadResult = TypeVar("ReadResult")

_child_router = MCPChildServerRouter()
_gateways: dict[str, MCPGatewayApprovalDecisionLayer] = {}
MCP_PENDING_RETRIED = "MCP_PENDING_RETRIED"
MCP_PENDING_LEASE_RELEASED = "MCP_PENDING_LEASE_RELEASED"
MAX_MCP_ARGUMENTS_BYTES = 48 * 1024


class GatewaySubmitRequest(BaseModel):
    project_id: str
    request: MCPGatewayToolCall


class PendingRetryRequest(BaseModel):
    reason: str = Field(default="", max_length=500)
    reset_attempts: bool = True


class MCPPendingHealthSummary(BaseModel):
    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    waiting_for_approval: int = 0
    forwarding: int = 0
    active_leases: int = 0
    expired_leases: int = 0
    retryable: int = 0
    dead_letter: int = 0
    terminal: int = 0
    attention_required: int = 0
    oldest_waiting_created_at: datetime | None = None
    oldest_dead_letter_created_at: datetime | None = None


def gateway_for_project(project_id: str) -> MCPGatewayApprovalDecisionLayer:
    if project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    if project_id not in _gateways:
        _gateways[project_id] = MCPGatewayApprovalDecisionLayer(_child_router, project_id=project_id)
    return _gateways[project_id]


def reset_gateway_runtime_for_test() -> None:
    _child_router.reset_for_test()
    _gateways.clear()


def child_received_count_for_test() -> int:
    return _child_router.received_count_for_test()


def child_received_count_for_server_for_test(server_name: str) -> int:
    return _child_router.received_count_for_server_for_test(server_name)


def _get_pending_or_404(pending_request_id: str) -> MCPPendingRequest:
    pending = store.mcp_pending_requests.get(pending_request_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="MCP pending request not found")
    return pending


def _pending_for_user(pending_request_id: str, user: AuthUser) -> MCPPendingRequest:
    pending = _get_pending_or_404(pending_request_id)
    workspace_id = workspace_id_for_user(user)
    if not _pending_belongs_to_workspace(pending, workspace_id):
        raise HTTPException(status_code=404, detail="MCP pending request not found")
    return pending


def _pending_belongs_to_workspace(
    pending: MCPPendingRequest,
    workspace_id: str | None,
) -> bool:
    project_workspace = project_workspace_id(pending.project_id)
    if workspace_id is None:
        return pending.workspace_id is None and project_workspace is None
    if project_workspace != workspace_id:
        return False
    # A legacy pending row may inherit ownership from its parent project, but
    # an explicit child scope that conflicts with that project is corrupt and
    # must not be disclosed or mutated by either workspace.
    return pending.workspace_id is None or pending.workspace_id == project_workspace


def _active_lease_exists(pending: MCPPendingRequest) -> bool:
    return bool(
        pending.status == MCPPendingStatus.FORWARDING
        and pending.lease_expires_at is not None
        and pending.lease_expires_at > now_utc()
    )


def _expired_lease_exists(pending: MCPPendingRequest) -> bool:
    return bool(
        pending.status == MCPPendingStatus.FORWARDING
        and pending.lease_expires_at is not None
        and pending.lease_expires_at <= now_utc()
    )


def _pending_requests_for_project(project_id: str | None) -> list[MCPPendingRequest]:
    requests = list(store.mcp_pending_requests.values())
    if project_id is not None:
        requests = [request for request in requests if request.project_id == project_id]
    return requests


def _postgres_pending_repository_for_request(request: Request):
    """Return the app-owned PostgreSQL adapter, or None for local SQLite mode.

    A partially initialized PostgreSQL runtime is an error. Falling through to
    the process-local store in that state could disclose stale or incomplete
    approval data.
    """
    state = request.app.state
    runtime = getattr(state, "postgres_runtime", None)
    repositories = getattr(state, "postgres_repositories", None)
    if not get_settings().postgres_auth_enabled:
        if runtime is not None or repositories is not None:
            raise HTTPException(
                status_code=503,
                detail="MCP PostgreSQL reads require PostgreSQL-backed authentication",
            )
        return None
    if runtime is None or repositories is None:
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL MCP pending repository runtime is unavailable",
        )
    if getattr(runtime, "repositories", None) is not repositories:
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL MCP pending repository is not bound to the active runtime",
        )
    repository = getattr(repositories, "mcp_pending_requests", None)
    if (
        not isinstance(repository, PostgresMCPPendingRepository)
        or not isinstance(getattr(repositories, "projects", None), PostgresProjectRepository)
        or not isinstance(
            getattr(repositories, "approval_decisions", None),
            PostgresApprovalDecisionRepository,
        )
    ):
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL MCP submission repository bundle is incomplete",
        )
    return repository


def _postgres_pending_read(operation: Callable[[], ReadResult]) -> ReadResult:
    try:
        return operation()
    except (MCPPendingRepositoryError, RuntimeError, psycopg.Error) as error:
        raise HTTPException(status_code=503, detail="MCP pending request data is unavailable") from error


def _pending_read_workspace_id(user: AuthUser, request: Request | None) -> str | None:
    try:
        return workspace_id_for_request(user, request) if request is not None else workspace_id_for_user(user)
    except (RuntimeError, psycopg.Error) as error:
        raise HTTPException(status_code=503, detail="MCP pending request workspace is unavailable") from error


def _require_local_mcp_write_runtime(request: Request) -> None:
    """Reject local MCP mutation fallbacks in durable or production modes."""
    settings = get_settings()
    state = request.app.state
    if (
        settings.is_production
        or settings.postgres_auth_enabled
        or getattr(state, "postgres_runtime", None) is not None
        or getattr(state, "postgres_repositories", None) is not None
    ):
        raise HTTPException(
            status_code=503,
            detail="MCP mutations require the active PostgreSQL runtime in production/PostgreSQL mode",
            headers={"Retry-After": "1"},
        )


def _require_mcp_request_context(request: Request | None) -> None:
    """Prevent direct route invocation from selecting SQLite in durable modes.

    FastAPI supplies ``Request`` for HTTP calls. Route functions are also
    callable from in-process integrations, though, and their optional request
    parameter must not silently turn a PostgreSQL-authenticated deployment
    into the process-local compatibility store.
    """
    settings = get_settings()
    if request is None and (settings.is_production or settings.postgres_auth_enabled):
        raise HTTPException(
            status_code=503,
            detail="MCP route requires an app-bound request context in production/PostgreSQL mode",
            headers={"Retry-After": "1"},
        )


def _postgres_mcp_submission(
    *,
    payload: GatewaySubmitRequest,
    user: AuthUser,
    request: Request,
    idempotency_key: str | None,
) -> MCPGatewayDecision:
    repository = _postgres_pending_repository_for_request(request)
    if repository is None:
        raise HTTPException(status_code=503, detail="PostgreSQL MCP pending repository is unavailable")
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required for PostgreSQL MCP submissions")
    key = idempotency_key.strip()
    if len(key) > 256:
        raise HTTPException(status_code=400, detail="Idempotency-Key must contain at most 256 characters")

    call = payload.request
    mutating = call.mutating if call.mutating is not None else call.tool_name in MCP_MUTATING_TOOL_NAMES
    if not mutating:
        # Non-mutating calls are currently forwarded by the process-local test
        # child router. Never execute them via that projection in PostgreSQL mode.
        raise HTTPException(status_code=503, detail="PostgreSQL MCP non-mutating forwarding is unavailable")
    approved_call = call.model_copy(update={"mutating": True})

    try:
        workspace_id = workspace_id_for_request(user, request)
    except (RuntimeError, psycopg.Error) as error:
        raise HTTPException(status_code=503, detail="MCP submission workspace is unavailable") from error
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        project = request.app.state.postgres_repositories.projects.get(payload.project_id)
    except (RuntimeError, psycopg.Error) as error:
        raise HTTPException(status_code=503, detail="MCP submission project data is unavailable") from error
    if project is None or project.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        registered_servers = request.app.state.postgres_repositories.mcp_child_servers.list_for_workspace(
            workspace_id,
            project_id=project.id,
            enabled=True,
        )
    except (RuntimeError, psycopg.Error) as error:
        raise HTTPException(status_code=503, detail="MCP child server registry is unavailable") from error
    matching_servers = [server for server in registered_servers if server.name == call.server_name]
    if len(matching_servers) != 1:
        raise HTTPException(status_code=404, detail="Enabled MCP child server not found")
    registered_server = matching_servers[0]
    registered_tool = next(
        (tool for tool in registered_server.tools if tool.name == call.tool_name),
        None,
    )
    if registered_tool is None or not registered_tool.mutating:
        raise HTTPException(status_code=409, detail="MCP tool is not registered as mutating on this server")
    try:
        validate_arguments_against_schema(call.arguments, registered_tool.input_schema)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    approved_action = {
        **approved_call.model_dump(mode="json"),
        "target_server_id": registered_server.id,
        "target_server_revision": registered_server.updated_at.isoformat(),
    }

    # A deterministic pending primary key gives this route an idempotency
    # constraint without fabricating a source_agent_id: PostgreSQL requires
    # non-null source_agent_id values to reference a registered external agent.
    key_digest = hashlib.sha256(f"{workspace_id}\0{user.id}\0{key}".encode()).hexdigest()
    idempotent_pending_id = f"mcp_pending_{key_digest}"

    def read_existing_submission() -> tuple[ApprovalRequest, MCPPendingRequest] | None:
        existing_pending = repository.get(idempotent_pending_id)
        if existing_pending is None:
            return None
        existing_approval = request.app.state.postgres_repositories.approval_decisions.get(
            existing_pending.approval_request_id
        )
        if (
            existing_approval is None
            or existing_approval.workspace_id != workspace_id
            or existing_approval.project_id != existing_pending.project_id
            or existing_pending.workspace_id != workspace_id
        ):
            raise MCPPendingRepositoryError("Stored idempotent MCP request has inconsistent approval scope")
        return existing_approval, existing_pending

    def response_for_existing(
        existing: tuple[ApprovalRequest, MCPPendingRequest],
    ) -> MCPGatewayDecision:
        approval, pending = existing
        try:
            approved_action = json.loads(approval.files[0].new_content or "")
        except (IndexError, TypeError, ValueError, json.JSONDecodeError):
            approved_action = {}
        same_call = (
            approval.project_id == project.id
            and pending.server_name == call.server_name
            and pending.tool_name == call.tool_name
            and pending.arguments == call.arguments
            and pending.session_id == call.session_id
            and pending.requested_by == call.requested_by
            and approved_action.get("target_server_id") == registered_server.id
            and approved_action.get("target_server_revision") == registered_server.updated_at.isoformat()
        )
        if not same_call:
            raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different MCP request")
        return MCPGatewayDecision(
            forwarded=False,
            approval_required=True,
            approval_request_id=approval.id,
            status=approval.status,
            reason="MCP submission already exists for this idempotency key.",
        )

    try:
        existing = read_existing_submission()
    except (MCPPendingRepositoryError, RuntimeError, psycopg.Error) as error:
        raise HTTPException(status_code=503, detail="MCP idempotency state is unavailable") from error
    if existing is not None:
        return response_for_existing(existing)

    approval = ApprovalRequest(
        project_id=project.id,
        workspace_id=workspace_id,
        title=f"Approve MCP tool call: {call.tool_name}",
        summary=f"Gateway intercepted mutating MCP call for {call.server_name}.{call.tool_name}",
        agent_name="mcp-gateway",
        target_branch=f"mcp-gateway/{call.tool_name}",
        files=[FileChange(
            path=f"mcp://{call.server_name}/{call.tool_name}",
            change_type="mcp_tool_call",
            diff=f"MCP mutating tool call requires approval: {approved_action}",
            new_content=json.dumps(
                approved_action,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )],
        test_plan=["MCP gateway must forward only after approval."],
        rollback_plan="Deny or expire the approval; the child MCP server receives nothing.",
        risk=RiskAssessment(level=RiskLevel.HIGH, reasons=["Mutating MCP tool call intercepted by gateway."]),
        status=ApprovalStatus.REQUESTED,
        source_provider=AgentProvider.OTHER,
        source_agent_name="mcp-gateway",
        source_session_id=call.session_id,
        created_by_user_id=user.id,
        verified_source=True,
    )
    pending = MCPPendingRequest(
        id=idempotent_pending_id,
        project_id=project.id,
        workspace_id=workspace_id,
        approval_request_id=approval.id,
        server_name=call.server_name,
        tool_name=call.tool_name,
        arguments=call.arguments,
        session_id=call.session_id,
        requested_by=call.requested_by,
    )
    audit_event = AuditEvent(
        event_type="mcp.approval_requested",
        actor=user.email,
        entity_id=approval.id,
        workspace_id=workspace_id,
        details={
            "server_name": call.server_name,
            "tool_name": call.tool_name,
            "mutating": True,
            "mcp_pending_request_id": pending.id,
            "project_id": project.id,
            "idempotency_key_present": True,
        },
    )
    notification = Notification(
        title="MCP approval required",
        body=f"{call.server_name}.{call.tool_name} is waiting for approval.",
        entity_type="approval_request",
        entity_id=approval.id,
        approval_request_id=approval.id,
        workspace_id=workspace_id,
        user_id=user.id,
    )
    try:
        repository.create_request(
            approval=approval,
            pending=pending,
            audit_event=audit_event,
            notification=notification,
        )
    except UniqueViolation as error:
        # The deterministic pending primary key closes the concurrent
        # idempotency race. Its enclosing transaction rolls back the approval
        # insert before we re-read and verify the winning request.
        try:
            existing = read_existing_submission()
        except (MCPPendingRepositoryError, RuntimeError, psycopg.Error) as read_error:
            raise HTTPException(status_code=503, detail="MCP idempotency state is unavailable") from read_error
        if existing is None:
            raise HTTPException(status_code=409, detail="MCP request conflicts with an existing record") from error
        return response_for_existing(existing)
    except MCPPendingRepositoryConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (MCPPendingRepositoryError, RuntimeError, psycopg.Error) as error:
        raise HTTPException(status_code=503, detail="MCP approval request could not be persisted") from error
    return MCPGatewayDecision(
        forwarded=False,
        approval_required=True,
        approval_request_id=approval.id,
        status=approval.status,
        reason="Mutating MCP tool call is waiting for mobile approval.",
    )


def _audit_pending_recovery(event_type: str, pending: MCPPendingRequest, details: dict, actor: str) -> None:
    store.audit_events.append(
        AuditEvent(
            event_type=event_type,
            actor=actor,
            entity_id=pending.approval_request_id,
            details={
                "mcp_pending_request_id": pending.id,
                "project_id": pending.project_id,
                "server_name": pending.server_name,
                "tool_name": pending.tool_name,
                **details,
            },
        )
    )


def _pending_health_summary(requests: list[MCPPendingRequest]) -> MCPPendingHealthSummary:
    by_status = {status.value: 0 for status in MCPPendingStatus}
    for pending in requests:
        by_status[pending.status.value] = by_status.get(pending.status.value, 0) + 1

    waiting = [pending for pending in requests if pending.status == MCPPendingStatus.WAITING_FOR_APPROVAL]
    dead_letters = [pending for pending in requests if pending.status == MCPPendingStatus.DEAD_LETTER]
    active_leases = [pending for pending in requests if _active_lease_exists(pending)]
    expired_leases = [pending for pending in requests if _expired_lease_exists(pending)]
    retryable = [
        pending
        for pending in requests
        if pending.status == MCPPendingStatus.WAITING_FOR_APPROVAL
        or (
            pending.status == MCPPendingStatus.DEAD_LETTER
            and pending.last_error != MCP_PENDING_FORWARD_OUTCOME_UNKNOWN
        )
    ]
    terminal_statuses = {
        MCPPendingStatus.FORWARDED,
        MCPPendingStatus.BLOCKED_BY_DECISION,
        MCPPendingStatus.ORPHANED,
    }
    terminal = [pending for pending in requests if pending.status in terminal_statuses]

    return MCPPendingHealthSummary(
        total=len(requests),
        by_status=by_status,
        waiting_for_approval=len(waiting),
        forwarding=by_status.get(MCPPendingStatus.FORWARDING.value, 0),
        active_leases=len(active_leases),
        expired_leases=len(expired_leases),
        retryable=len(retryable),
        dead_letter=len(dead_letters),
        terminal=len(terminal),
        attention_required=len(dead_letters) + len(expired_leases),
        oldest_waiting_created_at=min((pending.created_at for pending in waiting), default=None),
        oldest_dead_letter_created_at=min((pending.created_at for pending in dead_letters), default=None),
    )


@router.post("/requests", response_model=MCPGatewayDecision)
def submit_gateway_request(
    payload: GatewaySubmitRequest,
    user: AuthUser = MaintainerDependency,
    request: Request = None,  # type: ignore[assignment]
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> MCPGatewayDecision:
    _require_mcp_request_context(request)
    try:
        serialized_arguments = json.dumps(
            payload.request.arguments,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise HTTPException(status_code=400, detail="MCP tool arguments must be valid JSON data") from error
    if len(serialized_arguments) > MAX_MCP_ARGUMENTS_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Serialized MCP tool arguments exceed the 48 KiB approval limit",
        )
    if request is not None:
        repository = _postgres_pending_repository_for_request(request)
        if repository is not None:
            return _postgres_mcp_submission(
                payload=payload,
                user=user,
                request=request,
                idempotency_key=idempotency_key,
            )
        _require_local_mcp_write_runtime(request)
    workspace_id = workspace_id_for_user(user)
    if workspace_id is not None and project_workspace_id(payload.project_id) != workspace_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return gateway_for_project(payload.project_id).submit_tool_call(payload.request)


@router.get("/pending-requests", response_model=list[MCPPendingRequest])
def list_pending_gateway_requests(
    project_id: str | None = Query(default=None),
    status: MCPPendingStatus | None = Query(default=None),
    approval_request_id: str | None = Query(default=None),
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> list[MCPPendingRequest]:
    _require_mcp_request_context(request)
    repository = _postgres_pending_repository_for_request(request) if request is not None else None
    workspace_id = _pending_read_workspace_id(user, request)
    if repository is not None:
        if workspace_id is None:
            raise HTTPException(status_code=404, detail="MCP pending request not found")
        requests = _postgres_pending_read(
            lambda: repository.list_for_workspace(
                workspace_id,
                project_id=project_id,
                status=status,
                approval_id=approval_request_id,
            )
        )
        return requests

    requests = [
        pending for pending in _pending_requests_for_project(project_id)
        if _pending_belongs_to_workspace(pending, workspace_id)
    ]
    if status is not None:
        requests = [request for request in requests if request.status == status]
    if approval_request_id is not None:
        requests = [request for request in requests if request.approval_request_id == approval_request_id]
    return sorted(requests, key=lambda request: request.created_at, reverse=True)


@router.get("/pending-requests/health", response_model=MCPPendingHealthSummary)
def pending_gateway_health(
    project_id: str | None = Query(default=None),
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> MCPPendingHealthSummary:
    _require_mcp_request_context(request)
    repository = _postgres_pending_repository_for_request(request) if request is not None else None
    workspace_id = _pending_read_workspace_id(user, request)
    if repository is not None:
        if workspace_id is None:
            raise HTTPException(status_code=404, detail="MCP pending request not found")
        requests = _postgres_pending_read(
            lambda: repository.list_for_workspace(workspace_id, project_id=project_id)
        )
        return _pending_health_summary(requests)
    if workspace_id is None and project_id is not None and project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    return _pending_health_summary([
        request for request in _pending_requests_for_project(project_id)
        if _pending_belongs_to_workspace(request, workspace_id)
    ])


@router.get("/pending-requests/{pending_request_id}", response_model=MCPPendingRequest)
def get_pending_gateway_request(
    pending_request_id: str,
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> MCPPendingRequest:
    _require_mcp_request_context(request)
    repository = _postgres_pending_repository_for_request(request) if request is not None else None
    if repository is not None:
        workspace_id = _pending_read_workspace_id(user, request)
        if workspace_id is None:
            raise HTTPException(status_code=404, detail="MCP pending request not found")
        pending = _postgres_pending_read(lambda: repository.get(pending_request_id))
        if pending is None or pending.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="MCP pending request not found")
        return pending
    return _pending_for_user(pending_request_id, user)


@router.post("/pending-requests/{pending_request_id}/retry", response_model=MCPPendingRequest)
def retry_pending_gateway_request(
    pending_request_id: str,
    payload: PendingRetryRequest | None = None,
    user: AuthUser = MaintainerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> MCPPendingRequest:
    _require_mcp_request_context(request)
    if request is not None:
        repository = _postgres_pending_repository_for_request(request)
        if repository is not None:
            workspace_id = _pending_read_workspace_id(user, request)
            if workspace_id is None:
                raise HTTPException(status_code=404, detail="MCP pending request not found")
            payload = payload or PendingRetryRequest()
            retry = getattr(repository, "retry_for_workspace", None)
            if not callable(retry):
                raise HTTPException(
                    status_code=503,
                    detail="PostgreSQL MCP retry transaction is unavailable",
                )
            try:
                pending = retry(
                    pending_id=pending_request_id,
                    workspace_id=workspace_id,
                    reason=payload.reason,
                    reset_attempts=payload.reset_attempts,
                    actor=user.email,
                    now=now_utc(),
                )
            except MCPPendingRepositoryConflict as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except (MCPPendingRepositoryError, RuntimeError, psycopg.Error) as error:
                raise HTTPException(
                    status_code=503,
                    detail="PostgreSQL MCP retry transaction failed",
                ) from error
            if pending is None:
                raise HTTPException(status_code=404, detail="MCP pending request not found")
            return pending
        _require_local_mcp_write_runtime(request)
    pending = _pending_for_user(pending_request_id, user)
    payload = payload or PendingRetryRequest()
    previous_status = pending.status

    if pending.last_error == MCP_PENDING_FORWARD_OUTCOME_UNKNOWN:
        raise HTTPException(
            status_code=409,
            detail="MCP forwarding outcome is unknown; create a new request instead of replaying this invocation",
        )

    if previous_status in {
        MCPPendingStatus.FORWARDED,
        MCPPendingStatus.BLOCKED_BY_DECISION,
        MCPPendingStatus.ORPHANED,
    }:
        raise HTTPException(status_code=409, detail=f"MCP pending request cannot be retried from {previous_status}")

    if _active_lease_exists(pending):
        raise HTTPException(status_code=409, detail="MCP pending request has an active lease")

    if previous_status == MCPPendingStatus.FORWARDING:
        MCPGatewayApprovalDecisionLayer._mark_forward_outcome_unknown(pending)
        raise HTTPException(
            status_code=409,
            detail="MCP forwarding lease expired with an unknown child-server outcome; create a new request instead of replaying",
        )

    pending.status = MCPPendingStatus.WAITING_FOR_APPROVAL
    if payload.reset_attempts:
        pending.attempts = 0
    pending.lease_owner = None
    pending.lease_expires_at = None
    pending.last_error = None
    pending.updated_at = now_utc()

    _audit_pending_recovery(
        MCP_PENDING_RETRIED,
        pending,
        {
            "reason": payload.reason,
            "previous_status": previous_status,
            "reset_attempts": payload.reset_attempts,
            "attempts": pending.attempts,
        },
        actor=user.email,
    )
    store.persist()
    return pending


@router.post("/approvals/{approval_request_id}/resolve", response_model=MCPGatewayDecision)
def resolve_gateway_request(
    approval_request_id: str,
    user: AuthUser = MaintainerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> MCPGatewayDecision:
    _require_mcp_request_context(request)
    if request is not None:
        postgres_pending_repository = _postgres_pending_repository_for_request(request)
        if postgres_pending_repository is not None:
            return _resolve_postgres_gateway_request(
                approval_request_id=approval_request_id,
                user=user,
                request=request,
                pending_repository=postgres_pending_repository,
            )
        _require_local_mcp_write_runtime(request)
    approval = approval_decision_repository.get(approval_request_id)
    if not approval or not approval_scope_is_consistent(approval):
        raise HTTPException(status_code=404, detail="Approval request not found")
    workspace_id = workspace_id_for_user(user)
    if workspace_id is not None and project_workspace_id(approval.project_id) != workspace_id:
        raise HTTPException(status_code=404, detail="Approval request not found")
    # Refresh the compatibility view from the durable record before the gateway
    # consumes the decision; another API process may have committed it.
    store.approval_requests[approval_request_id] = approval
    gateway = gateway_for_project(approval.project_id)
    gateway.wait_handle(approval_request_id).notify_decision_changed()
    return gateway.resolve_after_side_channel_decision(approval_request_id)


def _resolve_postgres_gateway_request(
    *,
    approval_request_id: str,
    user: AuthUser,
    request: Request,
    pending_repository,
) -> MCPGatewayDecision:
    """Claim and forward one approved MCP action through durable PostgreSQL state.

    The repository commits the forwarding claim before this function invokes a
    child server. A lost response or an exception after that boundary is treated
    as an unknown outcome and can never be replayed under this request ID.
    """
    repositories = request.app.state.postgres_repositories
    try:
        workspace_id = workspace_id_for_request(user, request)
        if workspace_id is None:
            raise HTTPException(status_code=404, detail="Approval request not found")
        approval = repositories.approval_decisions.get(approval_request_id)
        project = repositories.projects.get(approval.project_id) if approval is not None else None
        if (
            approval is None
            or approval.workspace_id != workspace_id
            or project is None
            or project.id != approval.project_id
            or project.workspace_id != workspace_id
        ):
            raise HTTPException(status_code=404, detail="Approval request not found")
        if approval.status != ApprovalStatus.APPROVED:
            raise HTTPException(status_code=503, detail="MCP forwarding requires a durable approved decision")
        pending = pending_repository.get_for_approval(approval_request_id)
        if (
            pending is None
            or pending.workspace_id != workspace_id
            or pending.project_id != approval.project_id
            or pending.approval_request_id != approval.id
        ):
            raise HTTPException(status_code=404, detail="MCP pending request not found")
        if len(approval.files) != 1:
            raise HTTPException(status_code=409, detail="Approved MCP target binding is invalid")
        try:
            approved_action = json.loads(approval.files[0].new_content or "")
            target_server_id = approved_action["target_server_id"]
            target_revision_text = approved_action["target_server_revision"]
            if not isinstance(target_server_id, str) or not isinstance(target_revision_text, str):
                raise ValueError("MCP target identity fields must be strings")
            target_server_revision = datetime.fromisoformat(target_revision_text)
            if target_server_revision.tzinfo is None:
                raise ValueError("MCP target revision must have a timezone")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=409, detail="Approved MCP target binding is invalid") from error
        child_server = repositories.mcp_child_servers.get(target_server_id)
        if (
            child_server is None
            or child_server.id != target_server_id
            or child_server.workspace_id != workspace_id
            or child_server.project_id != pending.project_id
            or child_server.name != pending.server_name
            or not child_server.enabled
            or child_server.updated_at != target_server_revision
        ):
            raise HTTPException(
                status_code=409,
                detail="Approved MCP server configuration changed; create a new request",
            )
        registered_tool = next(
            (tool for tool in child_server.tools if tool.name == pending.tool_name),
            None,
        )
        if registered_tool is None or not registered_tool.mutating:
            raise HTTPException(
                status_code=409,
                detail="Approved MCP tool is no longer registered as mutating",
            )
        now = now_utc()
        claimed = pending_repository.claim_forwarding(
            pending_id=pending.id,
            approval_id=approval.id,
            workspace_id=workspace_id,
            project_id=approval.project_id,
            server_name=pending.server_name,
            expected_server_id=target_server_id,
            expected_server_revision=target_server_revision,
            owner="mcp-gateway-resolver",
            now=now,
            lease_expires_at=now + timedelta(seconds=30),
        )
    except HTTPException:
        raise
    except (MCPPendingRepositoryError, RuntimeError, psycopg.Error, ValueError) as error:
        raise HTTPException(status_code=503, detail="MCP forwarding claim is unavailable") from error

    if claimed is None:
        # Re-read the durable row to distinguish a committed unknown outcome
        # from an ordinary competing claim. Neither case invokes the child.
        try:
            latest = pending_repository.get_for_approval(approval_request_id)
        except (MCPPendingRepositoryError, RuntimeError, psycopg.Error) as error:
            raise HTTPException(status_code=503, detail="MCP forwarding state is unavailable") from error
        if latest and latest.last_error == MCP_PENDING_FORWARD_OUTCOME_UNKNOWN:
            raise HTTPException(
                status_code=409,
                detail="MCP forwarding outcome is unknown; create a new request instead of replaying",
            )
        raise HTTPException(status_code=409, detail="MCP request is not eligible for another forwarding attempt")

    # Dispatch the exact server snapshot whose ID and revision were verified
    # under the forwarding claim. Never re-resolve the target by mutable name.
    child_call = ChildMCPToolCall(
        server_name=claimed.server_name,
        tool_name=claimed.tool_name,
        arguments=claimed.arguments,
        session_id=claimed.session_id,
        requested_by=claimed.requested_by,
    )
    try:
        if child_server.transport == "test":
            result = _child_router._call_test_server(child_server, child_call)
        elif child_server.transport == "http":
            result = MCPChildServerRouter._call_http_server(child_server, child_call)
        elif child_server.transport == "http-jsonrpc":
            result = MCPChildServerRouter._call_http_jsonrpc_server(child_server, child_call)
        else:
            # No fallback transport is allowed after the single durable claim.
            result = {"ok": False, "reason_code": "UNSUPPORTED_CHILD_TRANSPORT"}
    except Exception:
        result = {"ok": False, "reason_code": MCP_PENDING_FORWARD_OUTCOME_UNKNOWN}

    if not isinstance(result, dict) or result.get("ok") is False:
        _finish_postgres_mcp_forwarding(
            pending_repository,
            expected=claimed,
            status=MCPPendingStatus.DEAD_LETTER,
            actor="mcp-gateway",
            event_type="mcp.forward_outcome_unknown",
            reason_code=MCP_PENDING_FORWARD_OUTCOME_UNKNOWN,
            details={"server_name": claimed.server_name, "tool_name": claimed.tool_name},
        )
        raise HTTPException(
            status_code=503,
            detail="MCP child-server outcome is unknown; this request will not be replayed",
        )

    _finish_postgres_mcp_forwarding(
        pending_repository,
        expected=claimed,
        status=MCPPendingStatus.FORWARDED,
        actor="mcp-gateway",
        event_type="mcp.forwarded_after_approval",
        reason_code=None,
        details={
            "server_name": claimed.server_name,
            "tool_name": claimed.tool_name,
            "attempts": claimed.attempts,
            "approved_payload_hash": approval.approved_payload_hash,
        },
    )
    return MCPGatewayDecision(
        forwarded=True,
        approval_required=True,
        approval_request_id=approval.id,
        status=approval.status,
        result=result,
        reason="Mutating MCP tool call forwarded after durable approval.",
    )


def _finish_postgres_mcp_forwarding(
    pending_repository,
    *,
    expected: MCPPendingRequest,
    status: MCPPendingStatus,
    actor: str,
    event_type: str,
    reason_code: str | None,
    details: dict,
) -> None:
    now = now_utc()
    updated = expected.model_copy(
        update={
            "status": status,
            "lease_owner": None,
            "lease_expires_at": None,
            "last_error": reason_code,
            "updated_at": now,
        }
    )
    audit_event = AuditEvent(
        event_type=event_type,
        actor=actor,
        entity_id=expected.approval_request_id,
        workspace_id=expected.workspace_id,
        details={
            "mcp_pending_request_id": expected.id,
            "project_id": expected.project_id,
            "server_name": expected.server_name,
            "tool_name": expected.tool_name,
            "previous_status": expected.status,
            "new_status": updated.status,
            **details,
            **({"reason_code": reason_code} if reason_code else {}),
        },
        created_at=now,
    )
    try:
        pending_repository.finish_forwarding(
            expected=expected,
            updated=updated,
            audit_event=audit_event,
        )
    except (MCPPendingRepositoryError, RuntimeError, psycopg.Error) as error:
        # A child call may already have happened. Keep the lease for recovery;
        # its expiry must be marked unknown by the repository, never replayed.
        raise HTTPException(status_code=503, detail="MCP forwarding outcome could not be persisted") from error

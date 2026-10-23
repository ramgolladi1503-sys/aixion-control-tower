from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable
from urllib.parse import urlparse

from pydantic import BaseModel
from psycopg import Connection
from psycopg.types.json import Jsonb

from .agent_run_models import AgentRun, AgentRunEvent, AgentRunEventType, AgentRunStep, AgentRunStepStatus
from .agent_task_models import AgentTask, AgentTaskStatus
from .approval_integrity import compute_approval_payload_hash
from .models import ApprovalRequest, ApprovalStatus, AuditEvent
from .postgres_trust_event_repository import PostgresTrustEventRepository
from .postgres_trust_repository import PostgresTrustRepository, PostgresTrustRepositoryConflict, PostgresTrustRepositoryError
from .trust_crypto import sha256_hex, verify_payload_signature
from .trust_models import (
    ActionConsumption, CapabilityActionType, CapabilityLeaseStatus,
    PolicyDecisionType, ProposedAction,
)
from .trust_policy import lease_receipt_payload


class PullRequestIntentStatus(StrEnum):
    INTENT = "INTENT"
    POST_CLAIMED = "POST_CLAIMED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    REMOTE_VERIFIED = "REMOTE_VERIFIED"
    COMPLETED = "COMPLETED"


class AgentRunPullRequestIntent(BaseModel):
    id: str
    workspace_id: str
    run_id: str
    task_id: str
    approval_id: str
    step_id: str
    action_id: str
    lease_id: str
    idempotency_key: str
    repository: str
    head_branch: str
    base_branch: str
    expected_head_sha: str
    expected_base_sha: str
    title_sha256: str
    body_sha256: str
    intent_marker: str
    lease_token_hash: str
    run_revision: datetime
    step_revision: datetime
    status: PullRequestIntentStatus = PullRequestIntentStatus.INTENT
    pull_number: int | None = None
    pull_url: str | None = None
    observed_repository: str | None = None
    observed_head_branch: str | None = None
    observed_base_branch: str | None = None
    marker_matches: bool | None = None
    observed_head_sha: str | None = None
    observed_base_sha: str | None = None
    response_sha256: str | None = None
    last_get_status: str | None = None
    get_match_count: int | None = None
    get_response_sha256: str | None = None
    reason: str = ""
    created_at: datetime
    updated_at: datetime


_COLUMNS = (
    "intent_id,workspace_id,run_id,task_id,approval_id,step_id,action_id,lease_id,idempotency_key,"
    "repository,head_branch,base_branch,expected_head_sha,expected_base_sha,title_sha256,body_sha256,intent_marker,lease_token_hash,"
    "run_revision,step_revision,status,pull_number,pull_url,observed_repository,observed_head_branch,"
    "observed_base_branch,marker_matches,observed_head_sha,observed_base_sha,response_sha256,"
    "last_get_status,get_match_count,get_response_sha256,reason,"
    "created_at,updated_at,payload"
)
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_GIT_SHA = re.compile(r"^[a-f0-9]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MARKER = re.compile(r"^aixion-intent:[a-f0-9-]{36}$")


def _payload(record: AgentRunPullRequestIntent) -> dict[str, Any]:
    return record.model_dump(mode="json")


def _decode(row: tuple[Any, ...]) -> AgentRunPullRequestIntent:
    try:
        record = AgentRunPullRequestIntent.model_validate(row[36])
    except Exception as error:
        raise PostgresTrustRepositoryError("Stored pull request intent payload is invalid") from error
    indexed = (
        record.id, record.workspace_id, record.run_id, record.task_id, record.approval_id,
        record.step_id, record.action_id, record.lease_id, record.idempotency_key,
        record.repository, record.head_branch, record.base_branch, record.expected_head_sha,
        record.expected_base_sha, record.title_sha256,
        record.body_sha256, record.intent_marker, record.lease_token_hash, record.run_revision,
        record.step_revision, record.status.value, record.pull_number, record.pull_url,
        record.observed_repository, record.observed_head_branch, record.observed_base_branch,
        record.marker_matches, record.observed_head_sha, record.observed_base_sha,
        record.response_sha256, record.last_get_status, record.get_match_count,
        record.get_response_sha256, record.reason, record.created_at, record.updated_at,
    )
    if indexed != tuple(row[:36]):
        raise PostgresTrustRepositoryError("Pull request intent payload differs from indexed state")
    if (
        not _REPOSITORY.fullmatch(record.repository)
        or not _GIT_SHA.fullmatch(record.expected_head_sha)
        or not _GIT_SHA.fullmatch(record.expected_base_sha)
        or not _DIGEST.fullmatch(record.title_sha256)
        or not _DIGEST.fullmatch(record.body_sha256)
        or not _DIGEST.fullmatch(record.lease_token_hash)
        or not _MARKER.fullmatch(record.intent_marker)
        or (record.response_sha256 is not None and not _DIGEST.fullmatch(record.response_sha256))
        or (record.get_response_sha256 is not None and not _DIGEST.fullmatch(record.get_response_sha256))
        or (record.get_match_count is not None and (type(record.get_match_count) is not int or record.get_match_count < 0))
        or (record.last_get_status is not None and record.last_get_status not in {"CERTAIN", "UNCERTAIN"})
        or (record.observed_head_sha is not None and not _GIT_SHA.fullmatch(record.observed_head_sha))
        or (record.observed_base_sha is not None and not _GIT_SHA.fullmatch(record.observed_base_sha))
    ):
        raise PostgresTrustRepositoryError("Stored pull request intent has invalid digest or identity")
    return record


def _valid_pull_url(repository: str, url: str | None, number: int | None) -> bool:
    if not url or not number:
        return False
    parsed = urlparse(url)
    expected_path = f"/{repository}/pull/{number}"
    return parsed.scheme == "https" and parsed.hostname == "github.com" and parsed.path.rstrip("/") == expected_path and not parsed.query and not parsed.fragment


def _exact_get_match(
    intent: AgentRunPullRequestIntent, *, observation_status: str, matching_count: int,
    observed_repository: str | None, observed_head_branch: str | None,
    observed_base_branch: str | None, marker_matches: bool | None,
    pull_number: int | None, pull_url: str | None, head_sha: str | None,
    base_sha: str | None, response_sha256: str | None,
) -> bool:
    return (
        observation_status == "CERTAIN" and type(matching_count) is int and matching_count == 1
        and observed_repository == intent.repository
        and observed_head_branch == intent.head_branch
        and observed_base_branch == intent.base_branch
        and marker_matches is True
        and type(pull_number) is int and pull_number > 0
        and _valid_pull_url(intent.repository, pull_url, pull_number)
        and isinstance(head_sha, str) and _GIT_SHA.fullmatch(head_sha) is not None
        and isinstance(base_sha, str) and _GIT_SHA.fullmatch(base_sha) is not None
        and head_sha == intent.expected_head_sha
        and base_sha == intent.expected_base_sha
        and isinstance(response_sha256, str) and _DIGEST.fullmatch(response_sha256) is not None
    )


class PostgresAgentRunPullRequestIntentRepository:
    """Workspace-scoped, one-shot durable fence for CREATE_PULL_REQUEST."""

    def __init__(
        self,
        connect: Callable[[], Connection],
        *,
        trust_repository: PostgresTrustRepository | None = None,
        trust_event_repository: PostgresTrustEventRepository | None = None,
    ) -> None:
        self._connect = connect
        self._trust = trust_repository or PostgresTrustRepository()
        self._trust_events = trust_event_repository or PostgresTrustEventRepository(connect)

    def get(self, *, intent_id: str, workspace_id: str) -> AgentRunPullRequestIntent | None:
        if not workspace_id:
            raise ValueError("Pull request intent reads require workspace scope")
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM agent_run_pull_request_intents WHERE intent_id=%s AND workspace_id=%s",
                (intent_id, workspace_id),
            ).fetchone()
        return _decode(row) if row else None

    def get_for_step(self, *, workspace_id: str, run_id: str, step_id: str, idempotency_key: str) -> AgentRunPullRequestIntent | None:
        if not workspace_id:
            raise ValueError("Pull request intent reads require workspace scope")
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM agent_run_pull_request_intents WHERE workspace_id=%s AND run_id=%s AND step_id=%s AND idempotency_key=%s",
                (workspace_id, run_id, step_id, idempotency_key),
            ).fetchone()
        return _decode(row) if row else None

    def record_authorized_intent(
        self, *, record: AgentRunPullRequestIntent, action: ProposedAction,
        expected_run: AgentRun, expected_step: AgentRunStep, timeout_seconds: int,
        actor: str, now: datetime, expected_base_branch: str | None = None,
    ) -> AgentRunPullRequestIntent:
        if (
            record.status != PullRequestIntentStatus.INTENT
            or action.action_type != CapabilityActionType.CREATE_PULL_REQUEST
            or expected_run.id != record.run_id or expected_run.workspace_id != record.workspace_id
            or expected_step.id != record.step_id or expected_step.run_id != record.run_id
            or expected_step.step_type.value != "CREATE_PULL_REQUEST"
            or expected_step.idempotency_key != record.idempotency_key
            or not 1 <= timeout_seconds <= 3600
            or not _REPOSITORY.fullmatch(record.repository)
            or not _GIT_SHA.fullmatch(record.expected_head_sha)
            or not _GIT_SHA.fullmatch(record.expected_base_sha)
            or not all(_DIGEST.fullmatch(value) for value in (record.title_sha256, record.body_sha256, record.lease_token_hash))
            or not _MARKER.fullmatch(record.intent_marker)
            or expected_base_branch is None or record.base_branch != expected_base_branch
            or action.action_type.value != "CREATE_PULL_REQUEST"
            or action.workspace_id != record.workspace_id or action.run_id != record.run_id
            or action.task_id != record.task_id or action.lease_id != record.lease_id
            or action.repository != record.repository or action.branch != record.head_branch
            or any(action.metadata.get(key) != value for key, value in {
                "repository": record.repository, "head_branch": record.head_branch,
                "base_branch": record.base_branch, "title_sha256": record.title_sha256,
                "validated_head_sha": record.expected_head_sha,
                "validated_base_sha": record.expected_base_sha,
                "body_sha256": record.body_sha256, "intent_marker": record.intent_marker,
                "idempotency_key": record.idempotency_key, "step_id": record.step_id,
                "task_id": record.task_id, "approval_id": record.approval_id,
                "lease_id": record.lease_id,
            }.items())
        ):
            raise PostgresTrustRepositoryConflict("Pull request intent differs from exact authorized action")
        with self._connect() as conn:
            with conn.transaction():
                approval_row = conn.execute(
                    "SELECT approval_id,project_id,workspace_id,status,payload FROM approval_requests "
                    "WHERE approval_id=%s AND workspace_id=%s FOR SHARE",
                    (record.approval_id, record.workspace_id),
                ).fetchone()
                task_row = conn.execute(
                    "SELECT task_id,project_id,workspace_id,approval_request_id,status,payload "
                    "FROM agent_tasks WHERE task_id=%s AND workspace_id=%s FOR SHARE",
                    (record.task_id, record.workspace_id),
                ).fetchone()
                if not approval_row or not task_row:
                    raise PostgresTrustRepositoryConflict("Pull request intent approval or task is missing")
                approval = ApprovalRequest.model_validate(approval_row[4])
                task = AgentTask.model_validate(task_row[5])
                if (
                    approval.id != approval_row[0] or approval.project_id != approval_row[1]
                    or approval.workspace_id != approval_row[2] or approval.status.value != approval_row[3]
                    or approval.status != ApprovalStatus.APPROVED
                    or approval.approved_payload_hash is None
                    or approval.approved_payload_hash != compute_approval_payload_hash(approval)
                    or task.id != task_row[0] or task.project_id != task_row[1]
                    or task.workspace_id != task_row[2] or task.approval_request_id != task_row[3]
                    or task.status.value != task_row[4] or task.status != AgentTaskStatus.APPROVED
                    or task.project_id != expected_run.project_id
                    or task.approval_request_id != record.approval_id
                    or task.repository != record.repository
                    or task.branch_preference != record.head_branch
                    or approval.project_id != expected_run.project_id
                    or approval.target_branch != record.head_branch
                ):
                    raise PostgresTrustRepositoryConflict("Pull request approval or task authority changed")
                run_row = conn.execute(
                    "SELECT run_id,workspace_id,task_id,project_id,status,current_step_id,updated_at,payload "
                    "FROM agent_runs WHERE run_id=%s AND workspace_id=%s FOR UPDATE",
                    (record.run_id, record.workspace_id),
                ).fetchone()
                step_row = conn.execute(
                    "SELECT step_id,run_id,task_id,workspace_id,status,updated_at,payload "
                    "FROM agent_run_steps WHERE step_id=%s AND run_id=%s AND workspace_id=%s FOR UPDATE",
                    (record.step_id, record.run_id, record.workspace_id),
                ).fetchone()
                if not run_row or not step_row:
                    raise PostgresTrustRepositoryConflict("Pull request intent run or step is missing")
                try:
                    durable_run = AgentRun.model_validate(run_row[7])
                    durable_step = AgentRunStep.model_validate(step_row[6])
                except Exception as error:
                    raise PostgresTrustRepositoryError("Stored pull request run or step is invalid") from error
                if (
                    durable_run != expected_run or durable_step != expected_step
                    or durable_run.id != run_row[0] or durable_run.workspace_id != run_row[1]
                    or durable_run.task_id != run_row[2] or durable_run.project_id != run_row[3]
                    or durable_run.status.value != run_row[4] or durable_run.current_step_id != run_row[5]
                    or durable_run.updated_at != run_row[6]
                    or durable_step.id != step_row[0] or durable_step.run_id != step_row[1]
                    or durable_step.task_id != step_row[2] or step_row[3] != record.workspace_id
                    or durable_step.status.value != step_row[4] or durable_step.updated_at != step_row[5]
                    or durable_run.status.value != "RUNNING" or durable_run.current_step_id != record.step_id
                    or durable_step.status != AgentRunStepStatus.RUNNING
                    or durable_step.step_type.value != "CREATE_PULL_REQUEST"
                    or durable_step.idempotency_key != record.idempotency_key
                    or durable_run.capability_lease_id != record.lease_id
                    or durable_run.lease_token is None
                    or sha256_hex({"lease_token": durable_run.lease_token}) != record.lease_token_hash
                    or record.run_revision != durable_run.updated_at
                    or record.step_revision != durable_step.updated_at
                ):
                    raise PostgresTrustRepositoryConflict("Pull request run or step authority changed")
                db_now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                decision, lease = self._trust.evaluate_agent_run_action(
                    conn, action=action, workspace_id=record.workspace_id,
                    run_id=record.run_id, step_id=record.step_id, actor=actor,
                    now=db_now, trust_event_repository=self._trust_events,
                    expected_base_branch=expected_base_branch,
                )
                if (
                    decision.decision != PolicyDecisionType.ALLOW
                    or decision.proposed_action_id != action.id or decision.lease_id != record.lease_id
                    or lease.id != record.lease_id
                ):
                    raise PostgresTrustRepositoryConflict("Pull request action was not allowed")
                if expected_run.lease_token is None or sha256_hex({"lease_token": expected_run.lease_token}) != record.lease_token_hash:
                    raise PostgresTrustRepositoryConflict("Pull request worker lease hash is invalid")
                if record.run_revision != expected_run.updated_at or record.step_revision != expected_step.updated_at:
                    raise PostgresTrustRepositoryConflict("Pull request run or step revision is stale")
                self._trust.consume_agent_run_action(
                    conn, action_id=record.action_id, workspace_id=record.workspace_id,
                    run_id=record.run_id, step_id=record.step_id,
                    consumption=ActionConsumption(
                        actual_runtime_seconds=timeout_seconds,
                        retry_consumed=expected_step.attempt_count > 1,
                        pull_requests_created=1,
                        evidence={"workspace_id": record.workspace_id, "run_id": record.run_id,
                                  "task_id": record.task_id, "approval_id": record.approval_id,
                                  "step_id": record.step_id, "action_id": record.action_id,
                                  "lease_id": record.lease_id, "intent_id": record.id,
                                  "repository": record.repository, "head_branch": record.head_branch,
                                  "base_branch": record.base_branch, "intent_marker": record.intent_marker,
                                  "runtime_seconds_reserved": timeout_seconds,
                                  "authorized_attempt_count": expected_step.attempt_count,
                                  "authorized_retry_consumed": expected_step.attempt_count > 1},
                    ), actor=actor, now=db_now,
                )
                conn.execute(
                    f"INSERT INTO agent_run_pull_request_intents({_COLUMNS.replace(',payload', '')},payload) VALUES ({','.join(['%s'] * 36)},%s)",
                    self._values(record) + (Jsonb(_payload(record)),),
                )
                event = AgentRunEvent(
                    run_id=record.run_id, task_id=record.task_id, step_id=record.step_id,
                    correlation_id=expected_run.correlation_id,
                    event_type=AgentRunEventType.OPERATOR_ACTION, actor=actor,
                    attempt_number=expected_step.attempt_count,
                    message="Durable pull request intent and capability reservation recorded before POST.",
                    metadata={"intent_id": record.id, "action_id": record.action_id,
                              "repository": record.repository, "head_branch": record.head_branch,
                              "base_branch": record.base_branch, "intent_marker": record.intent_marker,
                              "capability_consumed": True},
                )
                audit = AuditEvent(
                    event_type="agent_run.pull_request_intent_recorded", entity_id=record.run_id,
                    workspace_id=record.workspace_id, actor=actor,
                    details={"intent_id": record.id, "action_id": record.action_id,
                             "repository": record.repository, "head_branch": record.head_branch,
                             "base_branch": record.base_branch, "intent_marker": record.intent_marker},
                    created_at=db_now,
                )
                conn.execute("INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,created_at,event_type,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                             (event.id, event.run_id, event.task_id, record.workspace_id, event.created_at, event.event_type.value, Jsonb(event.model_dump(mode="json"))))
                conn.execute("INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,actor,details,created_at,payload) VALUES(%s,%s,NULL,'agent_run',%s,%s,%s,%s,%s,%s)",
                             (audit.id, record.workspace_id, record.run_id, audit.event_type, actor, Jsonb(audit.details), db_now, Jsonb(audit.model_dump(mode="json"))))
        return record

    @staticmethod
    def _values(record: AgentRunPullRequestIntent) -> tuple[Any, ...]:
        return (
            record.id, record.workspace_id, record.run_id, record.task_id, record.approval_id,
            record.step_id, record.action_id, record.lease_id, record.idempotency_key,
            record.repository, record.head_branch, record.base_branch,
            record.expected_head_sha, record.expected_base_sha, record.title_sha256,
            record.body_sha256, record.intent_marker, record.lease_token_hash,
            record.run_revision, record.step_revision, record.status.value, record.pull_number,
            record.pull_url, record.observed_repository, record.observed_head_branch,
            record.observed_base_branch, record.marker_matches, record.observed_head_sha,
            record.observed_base_sha, record.response_sha256, record.last_get_status,
            record.get_match_count, record.get_response_sha256, record.reason,
            record.created_at, record.updated_at,
        )

    def claim_post_once(
        self, *, intent: AgentRunPullRequestIntent, expected_run: AgentRun,
        expected_step: AgentRunStep, worker_lease_token: str,
    ) -> AgentRunPullRequestIntent:
        """Commit the only POST claim. Any claim state after this point is GET-only forever."""
        if not worker_lease_token or not intent.workspace_id:
            raise ValueError("POST claim requires worker lease and workspace scope")
        with self._connect() as conn:
            with conn.transaction():
                approval_row = conn.execute("SELECT approval_id,project_id,workspace_id,status,payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s FOR SHARE", (intent.approval_id, intent.workspace_id)).fetchone()
                task_row = conn.execute("SELECT task_id,project_id,workspace_id,approval_request_id,status,payload FROM agent_tasks WHERE task_id=%s AND workspace_id=%s FOR SHARE", (intent.task_id, intent.workspace_id)).fetchone()
                run_row = conn.execute("SELECT run_id,workspace_id,task_id,project_id,status,current_step_id,updated_at,payload FROM agent_runs WHERE run_id=%s AND workspace_id=%s FOR UPDATE", (intent.run_id, intent.workspace_id)).fetchone()
                step_row = conn.execute("SELECT step_id,run_id,task_id,workspace_id,status,updated_at,payload FROM agent_run_steps WHERE step_id=%s AND run_id=%s AND workspace_id=%s FOR UPDATE", (intent.step_id, intent.run_id, intent.workspace_id)).fetchone()
                row = conn.execute(f"SELECT {_COLUMNS} FROM agent_run_pull_request_intents WHERE intent_id=%s AND workspace_id=%s FOR UPDATE", (intent.id, intent.workspace_id)).fetchone()
                if not approval_row or not task_row or not run_row or not step_row or not row:
                    raise PostgresTrustRepositoryConflict("Pull request claim authority is missing")
                approval = ApprovalRequest.model_validate(approval_row[4])
                task = AgentTask.model_validate(task_row[5])
                run = AgentRun.model_validate(run_row[7])
                step = AgentRunStep.model_validate(step_row[6])
                current = _decode(row)
                db_now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                if (
                    current != intent or current.status != PullRequestIntentStatus.INTENT
                    or task.status != AgentTaskStatus.APPROVED or task.workspace_id != intent.workspace_id
                    or task.project_id != run.project_id or task.approval_request_id != intent.approval_id
                    or approval.id != approval_row[0] or approval.project_id != approval_row[1]
                    or approval.workspace_id != intent.workspace_id or approval.status != ApprovalStatus.APPROVED
                    or approval_row[3] != "APPROVED" or approval.approved_payload_hash is None
                    or approval.approved_payload_hash != compute_approval_payload_hash(approval)
                    or run != expected_run or run_row[4] != "RUNNING" or run.current_step_id != step.id
                    or run.lease_token != worker_lease_token or run.lease_expires_at is None or run.lease_expires_at <= db_now
                    or sha256_hex({"lease_token": worker_lease_token}) != current.lease_token_hash
                    or step != expected_step or step.status != AgentRunStepStatus.RUNNING
                    or step.lease_token != worker_lease_token or step.updated_at != current.step_revision
                    or run.updated_at != current.run_revision or run.pause_requested or run.cancel_requested
                ):
                    raise PostgresTrustRepositoryConflict("Pull request claim is stale, expired, or already consumed")
                action = self._trust.get_proposed_action(conn, current.action_id, current.workspace_id, for_update=True)
                decision = self._trust.get_latest_policy_decision_for_action(conn, current.action_id, current.workspace_id, for_update=True)
                lease = self._trust.get_capability_lease(conn, current.lease_id, current.workspace_id, for_update=True)
                if action is None or decision is None or lease is None or decision.decision.value != "ALLOW":
                    raise PostgresTrustRepositoryConflict("Pull request authorization was revoked or is missing")
                if (
                    action.action_type != CapabilityActionType.CREATE_PULL_REQUEST
                    or action.workspace_id != current.workspace_id or action.run_id != current.run_id
                    or action.task_id != current.task_id or action.lease_id != current.lease_id
                    or action.repository != current.repository or action.branch != current.head_branch
                    or any(action.metadata.get(key) != value for key, value in {
                        "base_branch": current.base_branch, "title_sha256": current.title_sha256,
                        "body_sha256": current.body_sha256, "intent_marker": current.intent_marker,
                        "idempotency_key": current.idempotency_key, "step_id": current.step_id,
                        "task_id": current.task_id, "approval_id": current.approval_id,
                        "lease_id": current.lease_id, "repository": current.repository,
                        "head_branch": current.head_branch,
                    }.items())
                    or lease.status != CapabilityLeaseStatus.ACTIVE or lease.expires_at <= db_now
                    or lease.approval_request_id != current.approval_id
                    or lease.approved_payload_hash != approval.approved_payload_hash
                    or lease.provider != task.provider or lease.agent_id != task.external_agent_id
                    or lease.scope.repository != current.repository or lease.scope.branch != current.head_branch
                    or not verify_payload_signature(lease_receipt_payload(lease), lease.receipt_signature)
                    or CapabilityActionType.CREATE_PULL_REQUEST not in lease.scope.allowed_actions
                ):
                    raise PostgresTrustRepositoryConflict("Pull request action or lease does not match intent")
                claimed = current.model_copy(update={"status": PullRequestIntentStatus.POST_CLAIMED,
                                                     "reason": "Single-use POST claim committed; recovery is GET-only.",
                                                     "updated_at": db_now})
                changed = conn.execute("UPDATE agent_run_pull_request_intents SET status=%s,reason=%s,updated_at=%s,payload=%s WHERE intent_id=%s AND workspace_id=%s AND status='INTENT'", (claimed.status.value, claimed.reason, db_now, Jsonb(_payload(claimed)), claimed.id, claimed.workspace_id))
                if changed.rowcount != 1:
                    raise PostgresTrustRepositoryConflict("Pull request POST claim lost its one-shot fence")
                return claimed

    def record_post_outcome(
        self, *, intent_id: str, workspace_id: str, outcome: str,
        observed_repository: str | None = None,
        observed_head_branch: str | None = None,
        observed_base_branch: str | None = None,
        marker_matches: bool | None = None,
        pull_number: int | None = None, pull_url: str | None = None,
        head_sha: str | None = None, base_sha: str | None = None,
        response_sha256: str | None = None, reason: str = "",
    ) -> AgentRunPullRequestIntent:
        if outcome not in {"VERIFIED", "AMBIGUOUS"}:
            raise ValueError("POST outcome must be VERIFIED or AMBIGUOUS")
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(f"SELECT {_COLUMNS} FROM agent_run_pull_request_intents WHERE intent_id=%s AND workspace_id=%s FOR UPDATE", (intent_id, workspace_id)).fetchone()
                if not row:
                    raise PostgresTrustRepositoryConflict("Pull request intent is not visible")
                current = _decode(row)
                if current.status != PullRequestIntentStatus.POST_CLAIMED:
                    raise PostgresTrustRepositoryConflict("Only a durable POST_CLAIMED intent can record POST outcome")
                db_now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                verified = outcome == "VERIFIED"
                if verified and not (
                                     observed_repository == current.repository
                                     and observed_head_branch == current.head_branch
                                     and observed_base_branch == current.base_branch
                                     and marker_matches is True
                                     and pull_number and _valid_pull_url(current.repository, pull_url, pull_number)
                                     and head_sha and _GIT_SHA.fullmatch(head_sha)
                                     and base_sha and _GIT_SHA.fullmatch(base_sha)
                                     and head_sha == current.expected_head_sha
                                     and base_sha == current.expected_base_sha
                                     and response_sha256 and _DIGEST.fullmatch(response_sha256)):
                    raise ValueError("Verified POST response metadata is incomplete or invalid")
                updated = current.model_copy(update={
                    "status": PullRequestIntentStatus.REMOTE_VERIFIED if verified else PullRequestIntentStatus.RECONCILIATION_REQUIRED,
                    "pull_number": pull_number if verified else None, "pull_url": pull_url if verified else None,
                    "observed_repository": observed_repository if verified else None,
                    "observed_head_branch": observed_head_branch if verified else None,
                    "observed_base_branch": observed_base_branch if verified else None,
                    "marker_matches": marker_matches if verified else None,
                    "observed_head_sha": head_sha if verified else None, "observed_base_sha": base_sha if verified else None,
                    "response_sha256": response_sha256 if verified else None,
                    "reason": reason or ("POST response verified" if verified else "POST outcome ambiguous; GET-only recovery required."),
                    "updated_at": db_now,
                })
                self._update(conn, updated, expected=PullRequestIntentStatus.POST_CLAIMED)
                return updated

    def record_get_reconciliation(
        self, *, intent_id: str, workspace_id: str, observation_status: str,
        matching_count: int, observed_repository: str | None = None,
        observed_head_branch: str | None = None, observed_base_branch: str | None = None,
        marker_matches: bool | None = None, pull_number: int | None = None,
        pull_url: str | None = None, head_sha: str | None = None,
        base_sha: str | None = None, response_sha256: str | None = None,
    ) -> AgentRunPullRequestIntent:
        if observation_status not in {"CERTAIN", "UNCERTAIN"} or type(matching_count) is not int or matching_count < 0:
            raise ValueError("GET reconciliation observation is invalid")
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(f"SELECT {_COLUMNS} FROM agent_run_pull_request_intents WHERE intent_id=%s AND workspace_id=%s FOR UPDATE", (intent_id, workspace_id)).fetchone()
                if not row:
                    raise PostgresTrustRepositoryConflict("Pull request intent is not visible")
                current = _decode(row)
                if current.status not in {PullRequestIntentStatus.POST_CLAIMED, PullRequestIntentStatus.RECONCILIATION_REQUIRED}:
                    raise PostgresTrustRepositoryConflict("GET recovery is allowed only after the one-shot POST claim")
                exact = _exact_get_match(
                    current, observation_status=observation_status, matching_count=matching_count,
                    observed_repository=observed_repository, observed_head_branch=observed_head_branch,
                    observed_base_branch=observed_base_branch, marker_matches=marker_matches,
                    pull_number=pull_number, pull_url=pull_url, head_sha=head_sha,
                    base_sha=base_sha, response_sha256=response_sha256,
                )
                db_now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                if exact:
                    updated = current.model_copy(update={
                        "status": PullRequestIntentStatus.REMOTE_VERIFIED, "pull_number": pull_number,
                        "pull_url": pull_url, "observed_repository": observed_repository,
                        "observed_head_branch": observed_head_branch,
                        "observed_base_branch": observed_base_branch, "marker_matches": True,
                        "observed_head_sha": head_sha, "observed_base_sha": base_sha,
                        "response_sha256": response_sha256,
                        "last_get_status": observation_status,
                        "get_match_count": matching_count,
                        "get_response_sha256": response_sha256,
                        "reason": "Exactly one exact-identity pull request recovered by GET.", "updated_at": db_now,
                    })
                else:
                    updated = current.model_copy(update={
                        "status": PullRequestIntentStatus.RECONCILIATION_REQUIRED,
                        "last_get_status": observation_status,
                        "get_match_count": matching_count,
                        "get_response_sha256": response_sha256 if isinstance(response_sha256, str) and _DIGEST.fullmatch(response_sha256) else None,
                        "reason": "GET result was zero, multiple, uncertain, or did not match exact intent identity; POST remains permanently fenced.",
                        "updated_at": db_now,
                    })
                self._update(conn, updated, expected=current.status)
                return updated

    @staticmethod
    def _update(conn: Connection, record: AgentRunPullRequestIntent, *, expected: PullRequestIntentStatus) -> None:
        changed = conn.execute(
            "UPDATE agent_run_pull_request_intents SET status=%s,pull_number=%s,pull_url=%s,observed_repository=%s,observed_head_branch=%s,observed_base_branch=%s,marker_matches=%s,observed_head_sha=%s,observed_base_sha=%s,response_sha256=%s,last_get_status=%s,get_match_count=%s,get_response_sha256=%s,reason=%s,updated_at=%s,payload=%s WHERE intent_id=%s AND workspace_id=%s AND status=%s",
            (record.status.value, record.pull_number, record.pull_url, record.observed_repository,
             record.observed_head_branch, record.observed_base_branch, record.marker_matches,
             record.observed_head_sha, record.observed_base_sha, record.response_sha256,
             record.last_get_status, record.get_match_count, record.get_response_sha256,
             record.reason, record.updated_at, Jsonb(_payload(record)), record.id,
             record.workspace_id, expected.value),
        )
        if changed.rowcount != 1:
            raise PostgresTrustRepositoryConflict("Pull request intent state transition lost its fence")

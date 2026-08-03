from __future__ import annotations

from .agent_run_models import AgentRun
from .agent_run_trust import attach_capability_lease
from .models import AuthUser
from .settings import get_settings
from .store import store
from .trust_models import (
    CapabilityActionType,
    CapabilityLease,
    CapabilityLeaseCreate,
    CapabilityLeaseMode,
    CapabilityScope,
)
from .trust_service import TrustControlConflict, issue_capability_lease


class RunCapabilityConflict(RuntimeError):
    pass


def ensure_run_capability(
    run: AgentRun,
    *,
    user: AuthUser,
) -> CapabilityLease | None:
    """Attach one exact, strict capability lease before the first side effect.

    Non-production profiles retain the existing explicit/manual behavior. Production
    trust enforcement derives scope only from the sealed approval and linked AgentTask;
    no paths or commands are invented by the scheduler.
    """
    if not get_settings().trust_enforcement:
        return None

    if run.capability_lease_id:
        lease = store.capability_leases.get(run.capability_lease_id)
        if lease is None:
            raise RunCapabilityConflict("Run references a missing capability lease.")
        return lease

    task = store.agent_tasks.get(run.task_id)
    approval = store.approval_requests.get(run.approval_request_id or "")
    if task is None:
        raise RunCapabilityConflict("Run cannot receive a capability without its AgentTask.")
    if approval is None:
        raise RunCapabilityConflict("Run cannot receive a capability without its approval.")
    if not approval.approved_payload_hash:
        raise RunCapabilityConflict(
            "Run cannot receive a capability until the approved payload is sealed."
        )
    if not task.repository or not task.branch_preference:
        raise RunCapabilityConflict(
            "Run capability requires an approved repository and feature branch."
        )

    allowed_actions = [
        CapabilityActionType.CREATE_BRANCH,
        CapabilityActionType.MODIFY_FILES,
        CapabilityActionType.RUN_COMMAND,
        CapabilityActionType.CREATE_PULL_REQUEST,
    ]
    scope = CapabilityScope(
        repository=task.repository,
        branch=task.branch_preference,
        allowed_actions=allowed_actions,
        allowed_path_prefixes=[item.path for item in approval.files],
        allowed_commands=list(approval.test_plan),
        allowed_network_domains=[],
        max_runtime_seconds=3600,
        max_cost_usd=10.0,
        max_retries=max(0, min(3, run.max_attempts_per_step - 1)),
        max_pull_requests=1,
        allow_auto_merge=False,
        metadata={
            "derived_from": "sealed_approval",
            "run_id": run.id,
            "task_id": task.id,
            "correlation_id": run.correlation_id,
        },
    )
    payload = CapabilityLeaseCreate(
        approval_request_id=approval.id,
        agent_id=task.external_agent_id,
        provider=task.provider,
        mode=CapabilityLeaseMode.STRICT,
        scope=scope,
        expires_in_seconds=3600,
        required_reviewer_count=1,
        prevent_self_approval=True,
    )
    try:
        lease = issue_capability_lease(payload, user=user)
        attach_capability_lease(run, lease.id, actor=user.email)
    except (ValueError, TrustControlConflict) as error:
        raise RunCapabilityConflict(str(error)) from error
    return lease

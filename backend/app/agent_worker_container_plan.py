from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType
from .agent_worker_claims import claim_agent_task_for_worker, claim_first_approved_agent_task_for_worker
from .models import AuditEvent
from .store import store


@dataclass(frozen=True)
class ContainerExecutionPolicy:
    policy_version: str = "aixion-container-execution-policy-v1"
    image: str = "python:3.12-slim"
    allowed_images: tuple[str, ...] = ("python:3.12-slim", "node:20-bookworm-slim", "gradle:8.7-jdk17")
    workdir: str = "/workspace/repo"
    network_mode: str = "none"
    allowed_network_modes: tuple[str, ...] = ("none",)
    memory_limit_mb: int = 1024
    min_memory_limit_mb: int = 128
    max_memory_limit_mb: int = 4096
    cpu_limit: float = 1.0
    min_cpu_limit: float = 0.25
    max_cpu_limit: float = 4.0
    timeout_seconds: int = 300
    min_timeout_seconds: int = 30
    max_timeout_seconds: int = 1800
    read_only_rootfs: bool = True
    drop_all_capabilities: bool = True
    no_new_privileges: bool = True
    workspace_mount_mode: str = "rw"
    docker_socket_mount_allowed: bool = False
    host_network_allowed: bool = False
    privileged_allowed: bool = False


@dataclass
class ContainerExecutionPlanResult:
    success: bool
    task_id: str | None = None
    worker_id: str = "agent-worker-container-plan"
    lease_token: str | None = None
    policy: ContainerExecutionPolicy | None = None
    event_id: str | None = None
    final_status: str | None = None
    reason: str = ""
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["generated_at"] = self.generated_at.isoformat()
        return payload


def get_default_container_execution_policy() -> ContainerExecutionPolicy:
    return ContainerExecutionPolicy()


def container_policy_metadata(policy: ContainerExecutionPolicy | None = None) -> dict[str, Any]:
    return asdict(policy or get_default_container_execution_policy())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(event_type: str, entity_id: str, details: dict[str, Any], actor: str) -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def _release_task(task: AgentTask, lease_token: str | None, worker_id: str) -> None:
    if lease_token and task.worker_lease_token != lease_token:
        _audit(
            "agent_worker.lease_release_skipped",
            task.id,
            {"worker_id": worker_id, "lease_token": lease_token, "current_lease_token": task.worker_lease_token},
            actor=worker_id,
        )
        return
    task.worker_lease_owner = None
    task.worker_lease_expires_at = None
    task.worker_lease_token = None
    task.updated_at = _now()
    _audit("agent_worker.task_released", task.id, {"worker_id": worker_id, "lease_token": lease_token}, actor=worker_id)


def validate_container_execution_policy(policy: ContainerExecutionPolicy | None = None) -> str | None:
    active_policy = policy or get_default_container_execution_policy()
    if active_policy.image not in active_policy.allowed_images:
        return f"Container image is not allowlisted: {active_policy.image}"
    if not active_policy.workdir.startswith("/workspace/"):
        return f"Container workdir must stay under /workspace: {active_policy.workdir}"
    if active_policy.network_mode not in active_policy.allowed_network_modes:
        return f"Container network mode is not allowlisted: {active_policy.network_mode}"
    if active_policy.memory_limit_mb < active_policy.min_memory_limit_mb:
        return f"Container memory limit too low: {active_policy.memory_limit_mb}"
    if active_policy.memory_limit_mb > active_policy.max_memory_limit_mb:
        return f"Container memory limit too high: {active_policy.memory_limit_mb}"
    if active_policy.cpu_limit < active_policy.min_cpu_limit:
        return f"Container CPU limit too low: {active_policy.cpu_limit}"
    if active_policy.cpu_limit > active_policy.max_cpu_limit:
        return f"Container CPU limit too high: {active_policy.cpu_limit}"
    if active_policy.timeout_seconds < active_policy.min_timeout_seconds:
        return f"Container timeout too low: {active_policy.timeout_seconds}"
    if active_policy.timeout_seconds > active_policy.max_timeout_seconds:
        return f"Container timeout too high: {active_policy.timeout_seconds}"
    if not active_policy.read_only_rootfs:
        return "Container root filesystem must be read-only."
    if not active_policy.drop_all_capabilities:
        return "Container must drop all Linux capabilities."
    if not active_policy.no_new_privileges:
        return "Container must enforce no-new-privileges."
    if active_policy.workspace_mount_mode not in {"rw", "ro"}:
        return f"Container workspace mount mode is invalid: {active_policy.workspace_mount_mode}"
    if active_policy.docker_socket_mount_allowed:
        return "Container policy must not allow Docker socket mounts."
    if active_policy.host_network_allowed:
        return "Container policy must not allow host networking."
    if active_policy.privileged_allowed:
        return "Container policy must not allow privileged mode."
    return None


def _append_container_plan_event(
    task: AgentTask,
    worker_id: str,
    lease_token: str | None,
    policy: ContainerExecutionPolicy,
) -> AgentTaskEvent:
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.NOTE,
        message="Worker planned container execution policy. No container was started.",
        status=task.status,
        actor=worker_id,
        metadata={
            "dry_run": True,
            "worker_id": worker_id,
            "lease_token": lease_token,
            "container_execution_planned": True,
            "container_started": False,
            "container_policy": container_policy_metadata(policy),
            "plan_type": "container_execution_policy_dry_run",
        },
    )
    store.agent_task_events[event.id] = event
    return event


def run_agent_worker_container_execution_plan_dry_run(
    *,
    task_id: str | None = None,
    first_approved: bool = False,
    worker_id: str = "agent-worker-container-plan",
    lease_seconds: int = 300,
    policy: ContainerExecutionPolicy | None = None,
) -> ContainerExecutionPlanResult:
    if not task_id and not first_approved:
        return ContainerExecutionPlanResult(success=False, worker_id=worker_id, reason="Provide task_id or set first_approved=true.")

    active_policy = policy or get_default_container_execution_policy()
    policy_error = validate_container_execution_policy(active_policy)
    if policy_error:
        return ContainerExecutionPlanResult(success=False, worker_id=worker_id, policy=active_policy, reason=policy_error)

    claim = (
        claim_first_approved_agent_task_for_worker(worker_id=worker_id, lease_seconds=lease_seconds)
        if first_approved and not task_id
        else claim_agent_task_for_worker(task_id=str(task_id), worker_id=worker_id, lease_seconds=lease_seconds)
    )
    if not claim.success or claim.task is None:
        return ContainerExecutionPlanResult(
            success=False,
            task_id=claim.task_id,
            worker_id=worker_id,
            lease_token=claim.lease_token,
            policy=active_policy,
            final_status=claim.task.status if claim.task else None,
            reason=claim.reason,
        )

    task = claim.task
    lease_token = claim.lease_token
    event = _append_container_plan_event(task, worker_id, lease_token, active_policy)
    _audit(
        "agent_worker.container_execution_plan_completed",
        task.id,
        {
            "worker_id": worker_id,
            "lease_token": lease_token,
            "container_started": False,
            "container_policy": container_policy_metadata(active_policy),
        },
        actor=worker_id,
    )
    _release_task(task, lease_token, worker_id)
    store.persist()

    return ContainerExecutionPlanResult(
        success=True,
        task_id=task.id,
        worker_id=worker_id,
        lease_token=lease_token,
        policy=active_policy,
        event_id=event.id,
        final_status=task.status,
        reason="Container execution dry-run plan completed. No container started.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan containerized AgentTask execution without starting a container.")
    parser.add_argument("--task-id", default=None, help="Specific AgentTask id to plan against.")
    parser.add_argument("--first-approved", action="store_true", help="Use the first approved task with a linked approval.")
    parser.add_argument("--worker-id", default="agent-worker-container-plan", help="Worker id written into task events.")
    parser.add_argument("--lease-seconds", type=int, default=300, help="Claim lease duration in seconds.")
    parser.add_argument("--json", action="store_true", help="Print JSON result.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = run_agent_worker_container_execution_plan_dry_run(
        task_id=args.task_id,
        first_approved=args.first_approved,
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        decision = "PASS" if result.success else "REFUSED"
        print(f"Agent worker container execution plan: {decision}")
        print(f"Task: {result.task_id or 'n/a'}")
        print(f"Reason: {result.reason}")
    return 0 if result.success else 1

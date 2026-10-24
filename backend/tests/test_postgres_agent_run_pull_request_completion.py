from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import RaiseException
from psycopg.types.json import Jsonb

from app import main
from app.agent_run_models import AgentRunStatus, AgentRunStepStatus, AgentRunStepType
from app.agent_run_supervisor import (
    PostgresPullRequestRequest,
    build_postgres_pull_request_completion,
)
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.approval_integrity import compute_approval_payload_hash
from app.models import ApprovalRequest, now_utc
from app.postgres_agent_run_apply_changes import AgentRunApplyChangeIntent, ApplyChangeIntentStatus
from app.postgres_agent_run_branch import AgentRunBranchIntent, BranchIntentStatus
from app.postgres_agent_run_pull_request import PullRequestIntentStatus
from app.postgres_agent_run_pull_request_route import (
    PostgresCreatePullRequestCoordinator,
    PullRequestRemote,
)
from app.postgres_agent_run_validation import AgentRunValidationIntent, RunValidationIntentStatus
from app.postgres_trust_repository import PostgresTrustRepository
from app.trust_crypto import sha256_hex
from app.trust_models import CapabilityActionType, ProposedAction

from test_postgres_agent_run_routes import _create_run_with_postgres_capability_lease

pytest_plugins = ("test_postgres_agent_run_routes",)


HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
TREE_SHA = "c" * 40
CONTENT_SHA = "d" * 64
VALIDATION_COMMAND = "python -m pytest"


class FakePullRequestProvider:
    """Deterministic provider; this test never contacts GitHub."""

    def observe_branch(self, _repository: str, branch: str, *, timeout_seconds: int) -> str:
        assert timeout_seconds == 40
        return HEAD_SHA if branch.startswith("feature/") else BASE_SHA

    def create_pull_request(
        self, repository: str, title: str, body: str, head_branch: str,
        base_branch: str, *, timeout_seconds: int,
    ) -> PullRequestRemote:
        assert timeout_seconds == 40
        return PullRequestRemote(
            repository=repository, number=23, title=title, body=body,
            head_branch=head_branch, base_branch=base_branch,
            html_url=f"https://github.com/{repository}/pull/23",
            head_sha=HEAD_SHA, base_sha=BASE_SHA,
        )

    def find_by_intent(
        self, _repository: str, _head_branch: str, _base_branch: str,
        _intent_marker: str, *, timeout_seconds: int,
    ) -> list[PullRequestRemote]:
        assert timeout_seconds == 40
        return []


def _insert_predecessor_intents(*, connect, task, approval, lease, run, steps) -> None:
    branch_step = next(item for item in steps if item.step_type == AgentRunStepType.CREATE_BRANCH)
    apply_step = next(item for item in steps if item.step_type == AgentRunStepType.APPLY_CHANGES)
    validation_step = next(item for item in steps if item.step_type == AgentRunStepType.RUN_VALIDATION)
    stamp = run.updated_at
    branch_action = ProposedAction(
        id=f"pr_test_branch_action_{uuid.uuid4().hex}", lease_id=lease.id,
        provider=task.provider, agent_id=task.external_agent_id, run_id=run.id,
        task_id=task.id, project_id=task.project_id, workspace_id=task.workspace_id,
        action_type=CapabilityActionType.CREATE_BRANCH, repository=task.repository,
        branch=task.branch_preference, metadata={},
    )
    apply_action = ProposedAction(
        id=f"pr_test_apply_action_{uuid.uuid4().hex}", lease_id=lease.id,
        provider=task.provider, agent_id=task.external_agent_id, run_id=run.id,
        task_id=task.id, project_id=task.project_id, workspace_id=task.workspace_id,
        action_type=CapabilityActionType.MODIFY_FILES, repository=task.repository,
        branch=task.branch_preference, paths=["docs/run.md"], metadata={},
    )
    lease_hash = sha256_hex({"lease_token": run.lease_token})
    branch = AgentRunBranchIntent(
        id=f"pr_test_branch_{uuid.uuid4().hex}", workspace_id=task.workspace_id,
        run_id=run.id, task_id=task.id, approval_id=approval.id, step_id=branch_step.id,
        action_id=branch_action.id, lease_id=lease.id,
        idempotency_key=branch_step.idempotency_key, repository=task.repository,
        source_branch="main", target_ref=task.branch_preference, source_sha=BASE_SHA,
        lease_token_hash=lease_hash, initial_lease_token_hash=lease_hash,
        run_revision=stamp, step_revision=branch_step.updated_at,
        status=BranchIntentStatus.COMPLETED, branch_url=None,
        created_at=stamp, updated_at=stamp,
    )
    apply = AgentRunApplyChangeIntent(
        id=f"pr_test_apply_{uuid.uuid4().hex}", workspace_id=task.workspace_id,
        run_id=run.id, task_id=task.id, approval_id=approval.id, step_id=apply_step.id,
        action_id=apply_action.id, lease_id=lease.id,
        idempotency_key=apply_step.idempotency_key, repository=task.repository,
        branch=task.branch_preference, path="docs/run.md", change_type="update",
        before_sha=BASE_SHA, desired_content_sha256=CONTENT_SHA,
        lease_token_hash=lease_hash, run_revision=stamp, step_revision=apply_step.updated_at,
        status=ApplyChangeIntentStatus.COMPLETED, commit_sha=HEAD_SHA,
        created_at=stamp, updated_at=stamp,
    )
    validation = AgentRunValidationIntent(
        id=f"pr_test_validation_{uuid.uuid4().hex}", workspace_id=task.workspace_id,
        run_id=run.id, task_id=task.id, approval_id=approval.id, step_id=validation_step.id,
        lease_id=lease.id, idempotency_key=validation_step.idempotency_key,
        approval_hash=approval.approved_payload_hash or "",
        test_plan_hash=sha256_hex([VALIDATION_COMMAND]), lease_token_hash=lease_hash,
        bundle_revision=HEAD_SHA, bundle_git_tree_sha=TREE_SHA,
        run_revision=stamp, step_revision=validation_step.updated_at,
        status=RunValidationIntentStatus.COMPLETED, created_at=stamp, updated_at=stamp,
    )
    trust = PostgresTrustRepository()
    with connect() as conn:
        trust.insert_proposed_action(conn, branch_action)
        trust.insert_proposed_action(conn, apply_action)
        conn.execute(
            "INSERT INTO agent_run_branch_intents(intent_id,workspace_id,run_id,task_id,approval_id,step_id,"
            "action_id,lease_id,idempotency_key,repository,source_branch,target_ref,source_sha,lease_token_hash,"
            "initial_lease_token_hash,run_revision,step_revision,status,branch_url,reason,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (branch.id, branch.workspace_id, branch.run_id, branch.task_id, branch.approval_id,
             branch.step_id, branch.action_id, branch.lease_id, branch.idempotency_key,
             branch.repository, branch.source_branch, branch.target_ref, branch.source_sha,
             branch.lease_token_hash, branch.initial_lease_token_hash, branch.run_revision,
             branch.step_revision, branch.status.value, branch.branch_url, branch.reason,
             branch.created_at, branch.updated_at, Jsonb(branch.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO agent_run_apply_change_intents(intent_id,workspace_id,run_id,task_id,approval_id,step_id,"
            "action_id,lease_id,idempotency_key,repository,branch,path,change_type,before_sha,desired_content_sha256,"
            "lease_token_hash,run_revision,step_revision,status,commit_sha,reason,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (apply.id, apply.workspace_id, apply.run_id, apply.task_id, apply.approval_id,
             apply.step_id, apply.action_id, apply.lease_id, apply.idempotency_key,
             apply.repository, apply.branch, apply.path, apply.change_type, apply.before_sha,
             apply.desired_content_sha256, apply.lease_token_hash, apply.run_revision,
             apply.step_revision, apply.status.value, apply.commit_sha, apply.reason,
             apply.created_at, apply.updated_at, Jsonb(apply.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO agent_run_validation_intents(intent_id,workspace_id,run_id,task_id,approval_id,step_id,"
            "lease_id,idempotency_key,approval_hash,test_plan_hash,lease_token_hash,bundle_revision,"
            "bundle_git_tree_sha,run_revision,step_revision,status,reason,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (validation.id, validation.workspace_id, validation.run_id, validation.task_id,
             validation.approval_id, validation.step_id, validation.lease_id,
             validation.idempotency_key, validation.approval_hash, validation.test_plan_hash,
             validation.lease_token_hash, validation.bundle_revision,
             validation.bundle_git_tree_sha, validation.run_revision, validation.step_revision,
             validation.status.value, validation.reason, validation.created_at,
             validation.updated_at, Jsonb(validation.model_dump(mode="json"))),
        )


def _prepare_verified_pr(*, postgres_agent_run_route_runtime):
    connect, workspace, _project, _other_workspace, _other_project, state = postgres_agent_run_route_runtime
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    # The shared fixture's capability helper binds the task to this branch.
    head_branch = "feature/approved-branch"
    with connect() as conn:
        task = AgentTask.model_validate(conn.execute(
            "SELECT payload FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
            (task_id, workspace.id),
        ).fetchone()[0])
        task.repository = "owner/approved-repository"
        task.branch_preference = head_branch
        conn.execute("UPDATE agent_tasks SET payload=%s WHERE task_id=%s AND workspace_id=%s",
                     (Jsonb(task.model_dump(mode="json")), task.id, workspace.id))
        approval = ApprovalRequest.model_validate(conn.execute(
            "SELECT payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
            (task.approval_request_id, workspace.id),
        ).fetchone()[0])
        approval.target_branch = head_branch
        approval.test_plan = [VALIDATION_COMMAND]
        approval.approved_payload_hash = compute_approval_payload_hash(approval)
        conn.execute("UPDATE approval_requests SET payload=%s WHERE approval_id=%s AND workspace_id=%s",
                     (Jsonb(approval.model_dump(mode="json")), approval.id, workspace.id))

    client = TestClient(main.app)
    run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect, workspace=workspace, task_id=task_id, state=state, client=client,
        lease_actions=[CapabilityActionType.CREATE_BRANCH, CapabilityActionType.MODIFY_FILES,
                       CapabilityActionType.RUN_COMMAND, CapabilityActionType.CREATE_PULL_REQUEST],
        allowed_commands=[VALIDATION_COMMAND], allowed_path_prefixes=["docs"],
    )
    attached = client.post(f"/agent/runs/{run_id}/capability-lease",
                           json={"capability_lease_id": lease.id})
    assert attached.status_code == 200, attached.text
    worker = "postgres-pr-completion-worker"
    run, _event, _audit = repositories.agent_runs.acquire_lease(
        run_id=run_id, worker_id=worker, lease_seconds=300, now=now_utc())
    steps = repositories.agent_runs.list_steps_for_run(run_id)
    pr_step = next(item for item in steps if item.step_type == AgentRunStepType.CREATE_PULL_REQUEST)
    stamp = now_utc()
    run.status = AgentRunStatus.RUNNING
    run.started_at = run.started_at or stamp
    run.current_step_id = pr_step.id
    run.current_step_index = pr_step.sequence
    run.updated_at = stamp
    for step in steps:
        step.updated_at = stamp
        if step.sequence < pr_step.sequence:
            step.status = AgentRunStepStatus.SUCCEEDED
            step.started_at = stamp
            step.completed_at = stamp
            step.lease_owner = None
            step.lease_token = None
            step.lease_expires_at = None
        elif step.id == pr_step.id:
            step.status = AgentRunStepStatus.RUNNING
            step.attempt_count = 1
            step.lease_owner = worker
            step.lease_token = run.lease_token
            step.lease_expires_at = run.lease_expires_at
            step.started_at = stamp
        else:
            step.status = AgentRunStepStatus.PENDING
    with connect() as conn:
        conn.execute("UPDATE agent_runs SET status=%s,current_step_id=%s,updated_at=%s,payload=%s "
                     "WHERE run_id=%s AND workspace_id=%s",
                     (run.status.value, run.current_step_id, stamp,
                      Jsonb(run.model_dump(mode="json")), run.id, workspace.id))
        for step in steps:
            conn.execute("UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
                          "WHERE step_id=%s AND workspace_id=%s",
                          (step.status.value, step.updated_at, Jsonb(step.model_dump(mode="json")),
                           step.id, workspace.id))
    run = repositories.agent_runs.get(run_id)
    steps = repositories.agent_runs.list_steps_for_run(run_id)
    task = repositories.agent_tasks.get(task_id)
    approval = repositories.approval_decisions.get(task.approval_request_id)
    assert run and run.lease_token and task and approval
    assert task.status == AgentTaskStatus.APPROVED
    assert task.repository == "owner/approved-repository"
    assert task.branch_preference == head_branch
    assert approval.target_branch == head_branch
    assert approval.approved_payload_hash == compute_approval_payload_hash(approval)
    _insert_predecessor_intents(connect=connect, task=task, approval=approval,
                                lease=lease, run=run, steps=steps)
    coordinator = PostgresCreatePullRequestCoordinator()
    request = PostgresPullRequestRequest(
        expected_run=run, expected_step=next(item for item in steps if item.id == run.current_step_id),
        task=task, approval=approval, lease=lease, worker_id=worker,
        worker_lease_token=run.lease_token, actor=worker, timeout_seconds=40,
        repository=task.repository, head_branch=task.branch_preference,
        base_branch="main", expected_head_sha=HEAD_SHA, provider=FakePullRequestProvider(),
        intent_repository=repositories.agent_run_pull_request_intents,
        completion_factory=lambda intent, title, body: build_postgres_pull_request_completion(
            run=run, steps=steps, intent=intent, title=title, body=body,
            actor=worker, worker_lease_token=run.lease_token or ""),
    )
    completion = coordinator.execute(request)
    assert completion.intent_id
    assert repositories.agent_run_pull_request_intents.get(
        intent_id=completion.intent_id, workspace_id=workspace.id).status == PullRequestIntentStatus.REMOTE_VERIFIED
    return connect, repositories, workspace, task, run, worker, completion


def _commit_completion(*, repositories, run, worker, completion):
    return repositories.agent_runs.commit_authoritative_task_step(
        expected_run=completion.expected_run,
        expected_steps=completion.expected_steps,
        worker_id=worker,
        lease_token=run.lease_token or "",
        step_type=AgentRunStepType.CREATE_PULL_REQUEST,
        pull_request_intent_id=completion.intent_id,
        pull_request_task_event=completion.task_event,
        pull_request_task_audit=completion.task_audit,
        build_records=lambda _task, _approval, _events, _run, _steps: (
            completion.updated_run, completion.updated_steps, completion.events, completion.audit,
        ),
    )


@pytest.mark.parametrize("force_failure", [False, True], ids=["commit", "rollback"])
def test_postgres_pull_request_completion_is_atomic(
    postgres_agent_run_route_runtime, force_failure: bool,
) -> None:
    connect, repositories, workspace, task, run, worker, completion = _prepare_verified_pr(
        postgres_agent_run_route_runtime=postgres_agent_run_route_runtime,
    )
    before_run = repositories.agent_runs.get(run.id)
    before_steps = repositories.agent_runs.list_steps_for_run(run.id)
    intent_repository = repositories.agent_run_pull_request_intents
    before_intent = intent_repository.get(intent_id=completion.intent_id, workspace_id=workspace.id)
    assert before_intent and before_intent.status == PullRequestIntentStatus.REMOTE_VERIFIED
    with connect() as conn:
        before_run_event_count = conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id=%s", (run.id,),
        ).fetchone()[0]
        before_audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s", (workspace.id,),
        ).fetchone()[0]
    if force_failure:
        with connect() as conn:
            conn.execute("""
                CREATE FUNCTION fail_pr_task_audit() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.entity_type='agent_task' AND NEW.event_type='agent_task.pull_request_created' THEN
                        RAISE EXCEPTION 'forced PR audit failure';
                    END IF;
                    RETURN NEW;
                END $$
            """)
            conn.execute("CREATE TRIGGER fail_pr_task_audit BEFORE INSERT ON audit_events "
                         "FOR EACH ROW EXECUTE FUNCTION fail_pr_task_audit()")
        with pytest.raises(RaiseException, match="forced PR audit failure"):
            _commit_completion(repositories=repositories, run=run, worker=worker,
                               completion=completion)
        assert repositories.agent_runs.get(run.id) == before_run
        assert repositories.agent_runs.list_steps_for_run(run.id) == before_steps
        assert repositories.agent_tasks.get(task.id).status == AgentTaskStatus.APPROVED
        after_intent = intent_repository.get(intent_id=completion.intent_id, workspace_id=workspace.id)
        assert after_intent == before_intent
        with connect() as conn:
            assert conn.execute("SELECT count(*) FROM agent_run_events WHERE run_id=%s",
                                (run.id,)).fetchone()[0] == before_run_event_count
            assert conn.execute("SELECT count(*) FROM audit_events WHERE workspace_id=%s",
                                (workspace.id,)).fetchone()[0] == before_audit_count
            assert conn.execute("SELECT count(*) FROM agent_task_events WHERE event_id=%s",
                                (completion.task_event.id,)).fetchone()[0] == 0
        return

    updated_run, updated_steps, events, audit = _commit_completion(
        repositories=repositories, run=run, worker=worker, completion=completion,
    )
    assert updated_run.lease_token is None
    assert next(item for item in updated_steps if item.id == run.current_step_id).status == AgentRunStepStatus.SUCCEEDED
    assert events and audit.event_type == "agent_run.step_completed"
    assert repositories.agent_tasks.get(task.id).status == AgentTaskStatus.READY_FOR_PR
    completed_intent = intent_repository.get(intent_id=completion.intent_id, workspace_id=workspace.id)
    assert completed_intent and completed_intent.status == PullRequestIntentStatus.COMPLETED
    with connect() as conn:
        task_event = conn.execute(
            "SELECT event_type,payload FROM agent_task_events WHERE event_id=%s AND workspace_id=%s",
            (completion.task_event.id, workspace.id),
        ).fetchone()
        assert task_event is not None and task_event[0] == "PR_CREATED"
        assert task_event[1]["metadata"]["pull_url"] == completed_intent.pull_url
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_type='agent_task' "
            "AND entity_id=%s AND event_type='agent_task.pull_request_created'",
            (workspace.id, task.id),
        ).fetchone()[0] == 1

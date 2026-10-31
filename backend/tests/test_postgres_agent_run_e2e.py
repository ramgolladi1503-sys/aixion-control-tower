from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest_plugins = ("test_postgres_agent_run_routes",)


def test_postgres_agent_run_executes_canonical_seven_steps_route_to_route(
    postgres_agent_run_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise the complete durable PostgreSQL run using only local fake providers."""
    from app import main
    from app.agent_run_models import AgentRunEventType, AgentRunStatus, AgentRunStepStatus, AgentRunStepType
    from app.agent_run_supervisor import AgentRunValidationBundle
    from app.agent_task_models import AgentTaskStatus
    from app.agent_worker_github_branch import GitHubBranchObservation
    from app.agent_worker_validation_runner import CommandExecutionResult
    from app.approval_integrity import compute_approval_payload_hash
    from app.models import ApprovalRequest, FileChange
    from app.postgres_runtime import PostgresRuntime
    from app.trust_models import CapabilityActionType
    from psycopg.types.json import Jsonb
    from test_postgres_agent_run_routes import _create_run_with_postgres_capability_lease

    connect, workspace, _project, _other_workspace, _other_project, fixture_state = (
        postgres_agent_run_route_runtime
    )
    repositories = fixture_state["repositories"]
    task_id = fixture_state["task_ids_by_workspace"][workspace.id]
    client = TestClient(main.app)
    repository = "owner/approved-repository"
    branch = "feature/approved-branch"
    path = "docs/run.md"
    content = "approved content from the seven step PostgreSQL run\n"
    command = "python -m pytest -q"

    # Prepare a real, clean Git checkout. Its source revision is the durable
    # branch pin; the fake apply provider makes one local commit and records
    # the durable intent marker in that commit message.
    git_path = tmp_path / "provider-repository"
    git_path.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(git_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    subprocess.run(["git", "init", "--initial-branch=main", str(git_path)], check=True, capture_output=True)
    git("config", "user.name", "Aixion test provider")
    git("config", "user.email", "aixion-test@example.test")
    (git_path / "README.md").write_text("fixture source\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "fixture source")
    source_sha = git("rev-parse", "HEAD")

    with connect() as conn:
        task_row = conn.execute(
            "SELECT payload FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
            (task_id, workspace.id),
        ).fetchone()
        task = __import__("app.agent_task_models", fromlist=["AgentTask"]).AgentTask.model_validate(task_row[0])
        task.repository = repository
        task.branch_preference = branch
        conn.execute(
            "UPDATE agent_tasks SET payload=%s WHERE task_id=%s AND workspace_id=%s",
            (Jsonb(task.model_dump(mode="json")), task_id, workspace.id),
        )
        approval_row = conn.execute(
            "SELECT payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
            (task.approval_request_id, workspace.id),
        ).fetchone()
        approval = ApprovalRequest.model_validate(approval_row[0])
        approval.target_branch = branch
        approval.files = [FileChange(path=path, change_type="create", diff="+approved\n", new_content=content)]
        approval.test_plan = [command]
        approval.approved_payload_hash = compute_approval_payload_hash(approval)
        conn.execute(
            "UPDATE approval_requests SET payload=%s WHERE approval_id=%s AND workspace_id=%s",
            (Jsonb(approval.model_dump(mode="json")), approval.id, workspace.id),
        )

    action_types = [
        CapabilityActionType.CREATE_BRANCH,
        CapabilityActionType.MODIFY_FILES,
        CapabilityActionType.RUN_COMMAND,
        CapabilityActionType.CREATE_PULL_REQUEST,
    ]
    run_id, lease = _create_run_with_postgres_capability_lease(
        connect=connect,
        workspace=workspace,
        task_id=task_id,
        state=fixture_state,
        client=client,
        lease_actions=action_types,
        allowed_path_prefixes=["docs"],
        allowed_commands=[command],
    )
    attached = client.post(
        f"/agent/runs/{run_id}/capability-lease",
        json={"capability_lease_id": lease.id},
    )
    assert attached.status_code == 200, attached.text

    runtime = PostgresRuntime(_dsn="postgresql://test-runtime/agent-run-e2e", profile="test", _connector=connect)
    bundle = runtime.repositories
    for state_name, attr in {
        "postgres_runtime": None,
        "postgres_repositories": None,
        "agent_run_repository": "agent_runs",
        "agent_task_repository": "agent_tasks",
        "approval_decision_repository": "approval_decisions",
        "trust_repository": "trust",
        "trust_event_repository": "trust_events",
        "agent_run_branch_intent_repository": "agent_run_branch_intents",
        "agent_run_apply_change_intent_repository": "agent_run_apply_change_intents",
        "agent_run_validation_intent_repository": "agent_run_validation_intents",
        "agent_run_pull_request_intent_repository": "agent_run_pull_request_intents",
    }.items():
        value = runtime if state_name == "postgres_runtime" else bundle if state_name == "postgres_repositories" else getattr(bundle, attr)
        monkeypatch.setattr(main.app.state, state_name, value, raising=False)

    for gate in (
        "AIXION_POSTGRES_CREATE_BRANCH_ENABLED",
        "AIXION_POSTGRES_APPLY_CHANGES_ENABLED",
        "AIXION_POSTGRES_RUN_VALIDATION_ENABLED",
        "AIXION_POSTGRES_CREATE_PULL_REQUEST_ENABLED",
    ):
        monkeypatch.setenv(gate, "true")
    monkeypatch.setenv("GITHUB_TOKEN", "test-only-token")
    monkeypatch.setenv("AIXION_VALIDATION_CONTAINER_IMAGE", "python:3.12-slim@sha256:" + "a" * 64)

    class FakeBranchProvider:
        create_calls = 0

        def observe_branch(self, repo: str, ref: str):
            assert repo == repository
            try:
                sha = git("rev-parse", "--verify", f"refs/heads/{ref}")
            except subprocess.CalledProcessError:
                return GitHubBranchObservation(repository=repo, branch=ref, found=False)
            return GitHubBranchObservation(
                repository=repo, branch=ref, found=True, sha=sha,
                html_url=f"https://github.com/{repo}/tree/{ref}",
            )

        def create_branch(self, repo: str, ref: str, pinned_sha: str):
            assert repo == repository and pinned_sha == source_sha
            self.create_calls += 1
            git("branch", ref, pinned_sha)

    branch_provider = FakeBranchProvider()
    monkeypatch.setattr("app.postgres_agent_run_branch_route._default_client", lambda: branch_provider)

    class FakeApplyProvider:
        supports_atomic_base_commit = True
        write_calls = 0
        last_commit_message = ""

        def __init__(self, _token: str) -> None:
            pass

        def observe_ref_sha(self, repo: str, ref: str) -> str:
            assert repo == repository
            return git("rev-parse", ref)

        def observe_file(self, repo: str, file_path: str, ref: str):
            assert repo == repository
            try:
                blob_sha = git("rev-parse", f"{ref}:{file_path}")
            except subprocess.CalledProcessError:
                from app.postgres_agent_run_apply_changes_executor import FileObservation
                return FileObservation(exists=False)
            raw = subprocess.run(
                ["git", "-C", str(git_path), "show", f"{ref}:{file_path}"],
                check=True,
                capture_output=True,
            ).stdout
            commit_sha = git("log", "-1", "--format=%H", ref, "--", file_path)
            message = git("show", "-s", "--format=%B", commit_sha)
            from app.postgres_agent_run_apply_changes_executor import FileObservation
            return FileObservation(
                exists=True,
                blob_sha=blob_sha,
                content_sha256=hashlib.sha256(raw).hexdigest(),
                commit_sha=commit_sha,
                commit_message=message,
            )

        def write_file(self, repo, file_path, ref, file_content, message, before_sha, expected_base_sha):
            assert repo == repository and ref == branch and expected_base_sha == source_sha
            assert before_sha is None and file_content == content
            self.write_calls += 1
            self.last_commit_message = message
            git("checkout", ref)
            target = git_path / file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file_content, encoding="utf-8")
            git("add", file_path)
            git("commit", "-m", message)
            return git("rev-parse", "HEAD")

    apply_provider = FakeApplyProvider("test-only-token")
    def fake_apply_provider_factory(_token: str) -> FakeApplyProvider:
        return apply_provider

    fake_apply_provider_factory.supports_atomic_base_commit = True
    monkeypatch.setattr(
        "app.postgres_agent_run_apply_changes_route.GitHubApplyChangeProvider",
        fake_apply_provider_factory,
    )

    class FakeContainer:
        calls: list[str] = []

        def available(self) -> bool:
            return True

        def run_validation(self, *, command: str, cwd, policy, timeout_seconds: int):
            self.calls.append(command)
            return CommandExecutionResult(
                command=command,
                exit_code=0,
                stdout_sha256=hashlib.sha256(b"ok").hexdigest(),
                stdout_bytes=2,
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                stderr_bytes=0,
                stream_evidence_complete=True,
            )

    class FakeBundleProvider:
        def resolve_bundle(self, *, run, step, task, approval):
            assert run.id == run_id and task.repository == repository
            assert approval.test_plan == [command]
            return AgentRunValidationBundle(
                path=git_path,
                repository=repository,
                branch=branch,
                revision=git("rev-parse", branch),
            )

    container = FakeContainer()
    monkeypatch.setattr(main.app.state, "agent_run_validation_bundle_provider", FakeBundleProvider(), raising=False)
    monkeypatch.setattr(main.app.state, "agent_run_validation_container_runtime", container, raising=False)

    class FakePullRequestProvider:
        create_calls = 0
        remote = None

        def observe_branch(self, repo: str, ref: str, *, timeout_seconds: int):
            assert repo == repository and timeout_seconds == 30
            return git("rev-parse", ref)

        def find_by_intent(self, *_args, **_kwargs):
            return [] if self.remote is None else [self.remote]

        def create_pull_request(self, repo, title, body, head_branch, base_branch, *, timeout_seconds):
            from app.postgres_agent_run_pull_request_route import PullRequestRemote
            assert repo == repository and head_branch == branch and base_branch == "main"
            self.create_calls += 1
            self.remote = PullRequestRemote(
                repository=repo,
                number=11,
                title=title,
                body=body,
                head_branch=head_branch,
                base_branch=base_branch,
                html_url=f"https://github.com/{repo}/pull/11",
                head_sha=git("rev-parse", head_branch),
                base_sha=git("rev-parse", "main"),
            )
            return self.remote

    pr_provider = FakePullRequestProvider()
    monkeypatch.setattr(main.app.state, "agent_run_pull_request_provider", pr_provider, raising=False)

    from app.store import store
    task_sqlite_projection_before = store.agent_tasks.get(task_id)
    approval_sqlite_projection_before = store.approval_requests.get(approval.id)
    expected_order = [
        AgentRunStepType.VALIDATE_SCOPE,
        AgentRunStepType.CREATE_BRANCH,
        AgentRunStepType.APPLY_CHANGES,
        AgentRunStepType.RUN_VALIDATION,
        AgentRunStepType.CREATE_PULL_REQUEST,
        AgentRunStepType.VERIFY_RESULT,
        AgentRunStepType.SEAL_EVIDENCE,
    ]
    completed_in_order = []
    for expected_type in expected_order:
        worker_id = (
            f"mission-control:{run_id}:pr"
            if expected_type == AgentRunStepType.CREATE_PULL_REQUEST
            else "postgres-seven-step-e2e"
        )
        response = client.post(
            f"/agent/runs/{run_id}/execute-next",
            json={"worker_id": worker_id, "timeout_seconds": 30},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        current = repositories.agent_runs.list_steps_for_run(run_id)
        completed = next(step for step in current if step.step_type == expected_type)
        assert completed.status == AgentRunStepStatus.SUCCEEDED, (
            f"{expected_type.value} ended {completed.status.value}: {completed.reason}; "
            f"response={body['steps']}"
        )
        completed_in_order.append(completed.step_type)
        assert body["run"]["id"] == run_id

    assert completed_in_order == expected_order
    durable_run = repositories.agent_runs.get(run_id)
    assert durable_run is not None and durable_run.status == AgentRunStatus.SUCCEEDED
    assert repositories.agent_tasks.get(task_id).status == AgentTaskStatus.READY_FOR_PR
    durable_events = repositories.agent_runs.list_events_for_run(run_id)
    assert [event.step_id for event in durable_events if event.event_type == AgentRunEventType.STEP_SUCCEEDED] == [
        step.id for step in repositories.agent_runs.list_steps_for_run(run_id)
    ]
    with connect() as conn:
        audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_type='agent_run' "
            "AND entity_id=%s AND event_type='agent_run.step_completed'",
            (workspace.id, run_id),
        ).fetchone()[0]
        assert audit_count == 7

    steps = repositories.agent_runs.list_steps_for_run(run_id)
    branch_step = next(step for step in steps if step.step_type == AgentRunStepType.CREATE_BRANCH)
    apply_step = next(step for step in steps if step.step_type == AgentRunStepType.APPLY_CHANGES)
    validation_step = next(step for step in steps if step.step_type == AgentRunStepType.RUN_VALIDATION)
    pr_step = next(step for step in steps if step.step_type == AgentRunStepType.CREATE_PULL_REQUEST)
    assert repositories.agent_run_branch_intents.get_for_step(
        workspace_id=workspace.id, run_id=run_id, step_id=branch_step.id,
        idempotency_key=branch_step.idempotency_key,
    ).status.value == "COMPLETED"
    apply_intent = repositories.agent_run_apply_change_intents.get_for_step(
        workspace_id=workspace.id, run_id=run_id, step_id=apply_step.id,
        idempotency_key=apply_step.idempotency_key,
    )
    assert apply_intent is not None and apply_intent.status.value == "COMPLETED"
    assert f"aixion-apply-intent:{apply_intent.id}" in apply_provider.last_commit_message
    assert repositories.agent_run_validation_intents.get_for_step(
        workspace_id=workspace.id, run_id=run_id, step_id=validation_step.id,
        idempotency_key=validation_step.idempotency_key,
    ).status.value == "COMPLETED"
    assert repositories.agent_run_pull_request_intents.get_for_step(
        workspace_id=workspace.id, run_id=run_id, step_id=pr_step.id,
        idempotency_key=pr_step.idempotency_key,
    ).status.value == "COMPLETED"
    pr_task_event = next(
        event for event in repositories.agent_tasks.list_events(task_id, workspace_id=workspace.id)
        if event.event_type.value == "PR_CREATED"
    )
    assert pr_task_event.metadata["worker_id"] == f"mission-control:{run_id}:pr"
    assert pr_task_event.metadata["approval_request_id"] == durable_run.approval_request_id
    assert pr_task_event.metadata["pull_request_number"] == pr_provider.remote.number
    assert pr_task_event.metadata["pull_request_url"] == pr_provider.remote.html_url
    assert pr_task_event.metadata["pull_request_opened"] is True
    assert pr_task_event.metadata["merged"] is False
    assert pr_task_event.metadata["operation_type"] == "github_pull_request_creation"
    assert branch_provider.create_calls == 1
    assert apply_provider.write_calls == 1
    assert container.calls == [command]
    assert pr_provider.create_calls == 1

    replay = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-seven-step-e2e", "timeout_seconds": 30},
    )
    assert replay.status_code == 409
    assert repositories.agent_runs.get(run_id) == durable_run
    assert store.agent_tasks.get(task_id) == task_sqlite_projection_before
    assert store.approval_requests.get(approval.id) == approval_sqlite_projection_before
    assert branch_provider.create_calls == apply_provider.write_calls == pr_provider.create_calls == 1
    assert container.calls == [command]

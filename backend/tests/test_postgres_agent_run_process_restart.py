from __future__ import annotations

import multiprocessing
import os
import queue as queue_module
import uuid
from datetime import timedelta
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql

pytest_plugins = ("test_postgres_agent_run_routes",)


def _recover_stale_run_in_restarted_api_process(
    dsn: str,
    schema: str,
    user_data: dict[str, object],
    run_id: str,
    results,
) -> None:
    """Start a fresh app lifespan and recover durable state in a new OS process."""
    from fastapi.testclient import TestClient as ProcessTestClient

    from app import agent_run_routes, application_repository_access, main as process_main
    from app.agent_run_routes import MaintainerDependency
    from app.auth import require_maintainer
    from app.models import AuthUser
    from app.postgres_runtime import PostgresRuntime

    def process_connect(
        _runtime_dsn: str, **connect_kwargs: object
    ) -> Connection:
        connection = psycopg.connect(dsn, **connect_kwargs)
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        return connection

    runtime = PostgresRuntime(
        _dsn="postgresql://disposable-agent-run-restart-test",
        profile="test",
        _connector=process_connect,
    )
    settings = SimpleNamespace(
        profile="test",
        auth_enabled=False,
        is_production=False,
        postgres_runtime_probe_enabled=True,
        postgres_auth_rate_limits_enabled=False,
        postgres_auth_enabled=True,
        approval_recovery_poll_seconds=300,
        outbox_dispatch_mode="scheduled",
        push_dispatch_enabled=False,
        email_dispatch_enabled=False,
    )

    # The test schema is selected by the disposable connector. Repositories,
    # schema probe, runtime identity, and app bindings are otherwise real.
    PostgresRuntime.from_environment = classmethod(lambda _cls: runtime)
    process_main.get_settings = lambda: settings
    agent_run_routes.get_settings = lambda: settings
    application_repository_access.get_settings = lambda: settings
    process_main.app.state.notification_outbox_repository = (
        runtime.repositories.notification_outbox
    )
    user = AuthUser.model_validate(user_data)
    process_main.app.dependency_overrides[require_maintainer] = lambda: user
    process_main.app.dependency_overrides[MaintainerDependency.dependency] = lambda: user

    try:
        with ProcessTestClient(process_main.app):
            lifespan_bound = (
                process_main.app.state.postgres_runtime is runtime
                and process_main.app.state.postgres_repositories is runtime.repositories
            )
            recovery_task = getattr(
                process_main.app.state, "agent_run_lease_recovery_task", None
            )
            recovery_task_running = (
                recovery_task is not None and not recovery_task.done()
            )
            recovered = runtime.repositories.agent_runs.get(run_id)
            events = runtime.repositories.agent_runs.list_events_for_run(run_id)
            results.put(
                (
                    lifespan_bound,
                    recovery_task_running,
                    process_main.app.state.agent_run_lease_recovery_status,
                    recovered.status.value if recovered is not None else None,
                    recovered.lease_token if recovered is not None else "missing-run",
                    sum(event.event_type.value == "STALE_LEASE_RECOVERED" for event in events),
                )
            )
    except BaseException as error:
        results.put((False, None, f"{type(error).__name__}: {error}"))


def test_postgres_agent_run_stale_lease_recovers_after_os_process_restart(
    postgres_agent_run_route_runtime,
) -> None:
    """The real app lifespan in a new OS process recovers the durable lease."""
    from app import main
    from app.agent_run_models import AgentRunStatus, AgentRunStepStatus
    from app.models import User, now_utc
    from psycopg.types.json import Jsonb

    connect, workspace, _project, _other_workspace, _other_project, state = (
        postgres_agent_run_route_runtime
    )
    repositories = state["repositories"]
    task_id = state["task_ids_by_workspace"][workspace.id]
    state["current_user"]["value"] = state["users"][0]
    owner = User(
        id=state["users"][0].id,
        email=state["users"][0].email,
        display_name=state["users"][0].display_name,
        role=state["users"][0].role,
        password_hash="test-only-password-hash",
        password_salt="test-only-password-salt",
        workspace_id=workspace.id,
        email_verified=True,
    )
    with connect() as connection:
        connection.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,"
            "email_verified,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id) DO NOTHING",
            (
                owner.id,
                owner.workspace_id,
                owner.email,
                owner.email.lower(),
                owner.role.value,
                owner.disabled,
                owner.email_verified,
                Jsonb(owner.model_dump(mode="json")),
            ),
        )
    client = TestClient(main.app)
    created = client.post("/agent/runs", json={"task_id": task_id})
    assert created.status_code == 200, created.text
    run_id = created.json()["run"]["id"]
    run = repositories.agent_runs.get(run_id)
    assert run is not None and run.current_step_id is not None
    step = next(
        item
        for item in repositories.agent_runs.list_steps_for_run(run_id)
        if item.id == run.current_step_id
    )
    stale_at = now_utc() - timedelta(minutes=5)
    stale_run = run.model_copy(
        update={
            "status": AgentRunStatus.RUNNING,
            "lease_owner": "terminated-api-worker",
            "lease_token": f"terminated-{uuid.uuid4().hex}",
            "lease_expires_at": stale_at,
            "heartbeat_at": stale_at,
            "updated_at": stale_at,
        }
    )
    stale_step = step.model_copy(
        update={
            "status": AgentRunStepStatus.RUNNING,
            "lease_owner": stale_run.lease_owner,
            "lease_token": stale_run.lease_token,
            "lease_expires_at": stale_at,
            "updated_at": stale_at,
        }
    )
    with connect() as connection:
        connection.execute(
            "UPDATE agent_runs SET status=%s,lease_owner=%s,lease_token=%s,"
            "lease_expires_at=%s,updated_at=%s,payload=%s "
            "WHERE run_id=%s AND workspace_id=%s",
            (
                stale_run.status.value,
                stale_run.lease_owner,
                stale_run.lease_token,
                stale_run.lease_expires_at,
                stale_run.updated_at,
                Jsonb(stale_run.model_dump(mode="json")),
                run_id,
                workspace.id,
            ),
        )
        connection.execute(
            "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
            "WHERE step_id=%s AND workspace_id=%s",
            (
                stale_step.status.value,
                stale_step.updated_at,
                Jsonb(stale_step.model_dump(mode="json")),
                stale_step.id,
                workspace.id,
            ),
        )
    client.close()

    with connect() as connection:
        schema = connection.execute("SELECT current_schema()").fetchone()[0]
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    process = context.Process(
        target=_recover_stale_run_in_restarted_api_process,
        args=(
            os.environ["AIXION_TEST_POSTGRES_DSN"],
            schema,
            state["users"][0].model_dump(mode="json"),
            run_id,
            results,
        ),
    )
    process.start()
    try:
        try:
            outcome = results.get(timeout=60)
        except queue_module.Empty:
            pytest.fail("Timed out waiting for fresh API process lease recovery")
        process.join(timeout=15)
        if process.is_alive():
            pytest.fail("Fresh API process did not exit after lease recovery")
        assert process.exitcode == 0
        assert outcome == (True, True, "healthy", "RETRY_WAIT", None, 1), outcome
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        results.close()
        results.join_thread()

    recovered = repositories.agent_runs.get(run_id)
    assert recovered is not None
    assert recovered.lease_token is None
    assert recovered.status == AgentRunStatus.RETRY_WAIT
    with connect() as connection:
        events = connection.execute(
            "SELECT count(*) FROM agent_run_events WHERE workspace_id=%s "
            "AND run_id=%s AND event_type='STALE_LEASE_RECOVERED'",
            (workspace.id, run_id),
        ).fetchone()[0]
        assert events == 1
        audit_events = connection.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_id=%s "
            "AND event_type='agent_run.stale_lease_recovered'",
            (workspace.id, run_id),
        ).fetchone()[0]
        assert audit_events == 1

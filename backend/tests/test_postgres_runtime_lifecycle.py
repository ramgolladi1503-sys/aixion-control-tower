from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("AIXION_PROFILE", "test")
os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app import main
from app.notification_outbox_repository import PostgresNotificationOutboxRepository


class FakeRuntime:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.opened = False
        self.closed = False
        self.email_outbox = object()
        self.notification_outbox = object.__new__(PostgresNotificationOutboxRepository)
        self.repositories = SimpleNamespace(
            email_outbox=self.email_outbox,
            notification_outbox=self.notification_outbox,
            approval_decisions=object(),
            agent_tasks=object(),
            agent_runs=object(),
            projects=object(),
            workspace_membership=object(),
            external_agent_auth=object(),
            work_orders=object(),
            ideas=object(),
            audit_events=object(),
            test_runs=object(),
            connectors=object(),
            mcp_child_servers=object(),
            mcp_pending_requests=object(),
            trust=object(),
            trust_events=object(),
        )

    def open(self, *, wait: bool = False) -> None:
        assert wait is True
        self.opened = True

    def probe(self) -> SimpleNamespace:
        return SimpleNamespace(ready=self.ready)

    def close(self) -> None:
        self.closed = True


def test_lifespan_opens_verifies_and_closes_opt_in_postgres_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime()
    sqlite_recovery_repositories = {
        "approval_decision_repository": object(),
        "agent_task_repository": object(),
        "agent_run_repository": object(),
    }
    recovery_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            outbox_dispatch_mode="inline",
            push_dispatch_enabled=False,
            email_dispatch_enabled=False,
            postgres_runtime_probe_enabled=True,
            postgres_auth_rate_limits_enabled=False,
        ),
    )
    monkeypatch.setattr(
        main.PostgresRuntime,
        "from_environment",
        staticmethod(lambda: runtime),
    )
    monkeypatch.setattr(
        main,
        "reconcile_linked_approval_tasks",
        lambda **kwargs: recovery_calls.append(kwargs) or 0,
    )
    application = SimpleNamespace(state=SimpleNamespace(**sqlite_recovery_repositories))

    async def exercise() -> None:
        async with main.lifespan(application):
            await asyncio.sleep(0)
            assert runtime.opened is True
            assert runtime.closed is False
            assert application.state.postgres_runtime is runtime
            assert application.state.postgres_repositories is runtime.repositories
        assert runtime.closed is True
        assert application.state.postgres_runtime is None
        assert application.state.postgres_repositories is None
        assert len(recovery_calls) == 1
        assert recovery_calls[0]["approval_repository"] is sqlite_recovery_repositories[
            "approval_decision_repository"
        ]
        assert recovery_calls[0]["task_repository"] is sqlite_recovery_repositories[
            "agent_task_repository"
        ]
        assert recovery_calls[0]["run_repository"] is sqlite_recovery_repositories[
            "agent_run_repository"
        ]

    asyncio.run(exercise())


def test_auth_rate_limit_binding_opens_runtime_without_probe_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime()
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            outbox_dispatch_mode="inline",
            push_dispatch_enabled=False,
            email_dispatch_enabled=False,
            postgres_runtime_probe_enabled=False,
            postgres_auth_rate_limits_enabled=True,
        ),
    )
    monkeypatch.setattr(
        main.PostgresRuntime,
        "from_environment",
        staticmethod(lambda: runtime),
    )
    application = SimpleNamespace(state=SimpleNamespace())

    async def exercise() -> None:
        async with main.lifespan(application):
            assert runtime.opened is True
            assert application.state.postgres_runtime is runtime
            assert application.state.postgres_repositories is runtime.repositories
        assert runtime.closed is True
        assert application.state.postgres_repositories is None

    asyncio.run(exercise())


def test_postgres_auth_lifecycle_uses_runtime_email_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime()
    captured: list[object] = []
    recovery_calls: list[dict[str, object]] = []
    dispatched = asyncio.Event()
    settings = SimpleNamespace(
        outbox_dispatch_mode="inline",
        push_dispatch_enabled=False,
        postgres_runtime_probe_enabled=False,
        postgres_auth_rate_limits_enabled=False,
        postgres_auth_enabled=True,
        email_dispatch_enabled=True,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main.PostgresRuntime, "from_environment", staticmethod(lambda: runtime))
    monkeypatch.setattr(
        main,
        "reconcile_linked_approval_tasks",
        lambda **kwargs: recovery_calls.append(kwargs) or 0,
    )

    async def dispatch_loop(*, postgres_repository=None) -> None:
        captured.append(postgres_repository)
        dispatched.set()

    monkeypatch.setattr(main, "email_dispatch_loop", dispatch_loop)
    application = SimpleNamespace(state=SimpleNamespace())

    async def exercise() -> None:
        async with main.lifespan(application):
            await asyncio.wait_for(dispatched.wait(), timeout=1)
            assert runtime.opened is True
            assert captured == [runtime.email_outbox]
            assert application.state.postgres_runtime is runtime
            assert application.state.postgres_repositories is runtime.repositories
            for state_name, repository_name in main._POSTGRES_APP_STATE_BINDINGS.items():
                assert getattr(application.state, state_name) is getattr(
                    runtime.repositories, repository_name
                )
            assert len(recovery_calls) == 1
            assert recovery_calls[0]["approval_repository"] is runtime.repositories.approval_decisions
            assert recovery_calls[0]["task_repository"] is runtime.repositories.agent_tasks
            assert recovery_calls[0]["run_repository"] is runtime.repositories.agent_runs
        assert runtime.closed is True
        assert application.state.postgres_runtime is None
        assert application.state.postgres_repositories is None
        for state_name in main._POSTGRES_APP_STATE_BINDINGS:
            assert not hasattr(application.state, state_name)

    asyncio.run(exercise())


def test_lifespan_retries_approval_reconciliation_and_cancels_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            outbox_dispatch_mode="scheduled",
            push_dispatch_enabled=False,
            email_dispatch_enabled=False,
            postgres_runtime_probe_enabled=False,
            postgres_auth_rate_limits_enabled=False,
            postgres_auth_enabled=False,
            approval_recovery_poll_seconds=0.001,
        ),
    )

    def reconcile(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(main, "reconcile_linked_approval_tasks", reconcile)
    application = SimpleNamespace(
        state=SimpleNamespace(
            approval_decision_repository=object(),
            agent_task_repository=object(),
            agent_run_repository=object(),
        )
    )

    async def exercise() -> None:
        async with main.lifespan(application):
            await asyncio.sleep(0.01)
            assert len(calls) >= 2
            assert application.state.approval_recovery_status == "healthy"
            assert application.state.approval_recovery_failure_count == 0
            assert application.state.approval_recovery_last_completed_at is not None
        assert application.state.approval_recovery_status == "healthy"

    asyncio.run(exercise())


def test_lifespan_fails_closed_and_closes_pool_on_schema_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime(ready=False)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            outbox_dispatch_mode="inline",
            push_dispatch_enabled=False,
            email_dispatch_enabled=False,
            postgres_runtime_probe_enabled=True,
        ),
    )
    monkeypatch.setattr(
        main.PostgresRuntime,
        "from_environment",
        staticmethod(lambda: runtime),
    )
    application = SimpleNamespace(state=SimpleNamespace())

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="migration schema is incomplete"):
            async with main.lifespan(application):
                pytest.fail("Lifespan must not start with an invalid PostgreSQL schema")

    asyncio.run(exercise())
    assert runtime.opened is True
    assert runtime.closed is True


def test_lifespan_closes_postgres_runtime_if_selected_recovery_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime()
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            outbox_dispatch_mode="inline",
            push_dispatch_enabled=False,
            email_dispatch_enabled=False,
            postgres_runtime_probe_enabled=True,
            postgres_auth_rate_limits_enabled=False,
        ),
    )
    monkeypatch.setattr(
        main.PostgresRuntime,
        "from_environment",
        staticmethod(lambda: runtime),
    )
    application = SimpleNamespace(
        state=SimpleNamespace(
            approval_decision_repository=object(),
            agent_task_repository=object(),
            agent_run_repository=object(),
        )
    )

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="supported repository profile"):
            async with main.lifespan(application):
                pytest.fail("Lifespan must not run SQLite recovery for other repositories")

    asyncio.run(exercise())
    assert runtime.closed is True

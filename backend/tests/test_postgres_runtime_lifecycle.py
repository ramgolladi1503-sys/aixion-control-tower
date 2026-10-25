from __future__ import annotations

import asyncio
import os
from dataclasses import fields, replace
from types import SimpleNamespace
from typing import get_type_hints

import pytest

os.environ.setdefault("AIXION_PROFILE", "test")
os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app import main
from app.application_repositories import ApplicationRepositories


class FakeRuntime:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.opened = False
        self.closed = False

        def unexpected_connect():
            raise AssertionError("Lifecycle setup must not connect through a repository")

        self.repositories = ApplicationRepositories.from_connector(unexpected_connect)
        self.email_outbox = self.repositories.email_outbox
        self.notification_outbox = self.repositories.notification_outbox

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
    class SQLiteTestRepository:
        pass

    prior_bindings = {
        state_name: (
            None
            if state_name == "notification_outbox_repository"
            else SQLiteTestRepository()
        )
        for state_name in main._POSTGRES_APP_STATE_BINDINGS
    }
    application = SimpleNamespace(state=SimpleNamespace(**prior_bindings))

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
        for state_name, previous_repository in prior_bindings.items():
            assert getattr(application.state, state_name) is previous_repository

    asyncio.run(exercise())


def test_application_repository_bundle_shares_trust_adapters_with_branch_intents() -> None:
    def unexpected_connect():
        raise AssertionError("Constructing the repository bundle must not connect")

    repositories = ApplicationRepositories.from_connector(unexpected_connect)

    assert repositories.agent_run_branch_intents._trust is repositories.trust
    assert repositories.agent_run_branch_intents._trust_events is repositories.trust_events


def test_postgres_auth_lifecycle_fails_closed_for_missing_repository_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime()
    runtime.repositories = replace(runtime.repositories, trust_events=None)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            outbox_dispatch_mode="scheduled",
            push_dispatch_enabled=False,
            email_dispatch_enabled=False,
            postgres_runtime_probe_enabled=False,
            postgres_auth_rate_limits_enabled=False,
            postgres_auth_enabled=True,
        ),
    )
    monkeypatch.setattr(main.PostgresRuntime, "from_environment", staticmethod(lambda: runtime))
    monkeypatch.setattr(main, "reconcile_linked_approval_tasks", lambda **_kwargs: 0)
    application = SimpleNamespace(state=SimpleNamespace())

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="missing or mismatched adapters"):
            async with main.lifespan(application):
                pytest.fail("Lifespan must reject an incomplete PostgreSQL repository bundle")

    asyncio.run(exercise())

    assert runtime.opened is True
    assert runtime.closed is True
    assert application.state.postgres_runtime is None
    assert application.state.postgres_repositories is None
    for state_name in main._POSTGRES_APP_STATE_BINDINGS:
        assert not hasattr(application.state, state_name)


def test_postgres_application_repository_binding_contract_tracks_typed_bundle() -> None:
    bundle_fields = {field.name for field in fields(ApplicationRepositories)}
    bundle_types = get_type_hints(ApplicationRepositories)
    mapped_fields = set(main._POSTGRES_APP_STATE_BINDINGS.values())

    assert set(bundle_types) == bundle_fields
    assert mapped_fields <= bundle_fields
    # These adapters are consumed directly by request and worker paths rather
    # than through `_POSTGRES_APP_STATE_BINDINGS`.
    assert {"auth_rate_limits", "email_outbox"} <= bundle_fields


@pytest.mark.parametrize(
    "repository_name",
    [field.name for field in fields(ApplicationRepositories)],
)
@pytest.mark.parametrize("invalid_adapter", [None, object()])
def test_postgres_auth_lifecycle_rejects_every_missing_or_mismatched_adapter(
    repository_name: str,
    invalid_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime()
    runtime.repositories = replace(
        runtime.repositories,
        **{repository_name: invalid_adapter},
    )
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            outbox_dispatch_mode="scheduled",
            push_dispatch_enabled=False,
            email_dispatch_enabled=False,
            postgres_runtime_probe_enabled=False,
            postgres_auth_rate_limits_enabled=False,
            postgres_auth_enabled=True,
        ),
    )
    monkeypatch.setattr(
        main.PostgresRuntime, "from_environment", staticmethod(lambda: runtime)
    )
    application = SimpleNamespace(state=SimpleNamespace())

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="missing or mismatched adapters"):
            async with main.lifespan(application):
                pytest.fail("Lifespan must reject an invalid PostgreSQL adapter")

    asyncio.run(exercise())
    assert runtime.opened is True
    assert runtime.closed is True
    assert application.state.postgres_runtime is None
    assert application.state.postgres_repositories is None
    for state_name in main._POSTGRES_APP_STATE_BINDINGS:
        assert not hasattr(application.state, state_name)


def test_postgres_auth_lifecycle_fails_closed_for_foreign_required_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime()
    foreign_approval_repository = object()
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            outbox_dispatch_mode="scheduled",
            push_dispatch_enabled=False,
            email_dispatch_enabled=False,
            postgres_runtime_probe_enabled=False,
            postgres_auth_rate_limits_enabled=False,
            postgres_auth_enabled=True,
        ),
    )
    monkeypatch.setattr(main.PostgresRuntime, "from_environment", staticmethod(lambda: runtime))
    application = SimpleNamespace(
        state=SimpleNamespace(approval_decision_repository=foreign_approval_repository)
    )

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="foreign binding"):
            async with main.lifespan(application):
                pytest.fail("Lifespan must reject a foreign PostgreSQL repository binding")

    asyncio.run(exercise())

    assert runtime.opened is True
    assert runtime.closed is True
    assert application.state.postgres_runtime is None
    assert application.state.postgres_repositories is None
    assert application.state.approval_decision_repository is foreign_approval_repository
    for state_name in list(main._POSTGRES_APP_STATE_BINDINGS)[1:]:
        assert not hasattr(application.state, state_name)


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
    monkeypatch.setattr(main, "expire_due_approvals", lambda **_kwargs: 0)
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


def test_failed_background_worker_does_not_skip_postgres_runtime_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime()
    worker_failed = asyncio.Event()

    class SQLiteApprovalRepository:
        pass

    previous_approval_repository = SQLiteApprovalRepository()
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            outbox_dispatch_mode="scheduled",
            push_dispatch_enabled=False,
            email_dispatch_enabled=False,
            postgres_runtime_probe_enabled=False,
            postgres_auth_rate_limits_enabled=False,
            postgres_auth_enabled=True,
            approval_recovery_poll_seconds=60,
        ),
    )
    monkeypatch.setattr(
        main.PostgresRuntime,
        "from_environment",
        staticmethod(lambda: runtime),
    )
    monkeypatch.setattr(main, "reconcile_linked_approval_tasks", lambda **_kwargs: 0)

    async def fail_recovery_worker(*_args, **_kwargs) -> None:
        worker_failed.set()
        raise RuntimeError("simulated recovery worker failure")

    monkeypatch.setattr(main, "_approval_recovery_loop", fail_recovery_worker)
    application = SimpleNamespace(
        state=SimpleNamespace(
            approval_decision_repository=previous_approval_repository,
        )
    )

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="simulated recovery worker failure"):
            async with main.lifespan(application):
                await asyncio.wait_for(worker_failed.wait(), timeout=1)
                # Give the scheduled task a chance to publish its failure
                # before lifespan shutdown awaits it.
                await asyncio.sleep(0)
        assert runtime.closed is True
        assert application.state.postgres_runtime is None
        assert application.state.postgres_repositories is None
        assert application.state.approval_decision_repository is previous_approval_repository
        for state_name in list(main._POSTGRES_APP_STATE_BINDINGS)[1:]:
            assert not hasattr(application.state, state_name)

    asyncio.run(exercise())

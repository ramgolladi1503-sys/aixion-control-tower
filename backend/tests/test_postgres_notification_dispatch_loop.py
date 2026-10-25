from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app import main
from app import postgres_notification_dispatcher as postgres_dispatcher
from app.application_repositories import ApplicationRepositories
from app.models import DeviceRegistration, Notification, now_utc
from app.notification_outbox_repository import PostgresNotificationOutboxRepository


class FakeRuntime:
    def __init__(self) -> None:
        # The lifespan validates the complete typed bundle, while this worker
        # test deliberately avoids opening a database connection.
        self.repositories = ApplicationRepositories.from_connector(lambda: None)
        self.closed = False

    def open(self, *, wait: bool = False) -> None:
        assert wait is True

    def probe(self) -> SimpleNamespace:
        return SimpleNamespace(ready=True)

    def close(self) -> None:
        self.closed = True


def _bind_notification_runtime(application: FastAPI, repositories: SimpleNamespace) -> None:
    application.state.approval_decision_repository = repositories.approval_decisions
    application.state.agent_task_repository = repositories.agent_tasks
    application.state.agent_run_repository = repositories.agent_runs
    application.state.agent_run_fault_repository = repositories.agent_run_faults
    application.state.project_repository = repositories.projects
    application.state.workspace_membership_repository = repositories.workspace_membership
    application.state.external_agent_auth_repository = repositories.external_agent_auth
    application.state.audit_event_repository = repositories.audit_events


def test_lifespan_rejects_postgres_notification_worker_without_runtime(monkeypatch) -> None:
    async def exercise() -> None:
        monkeypatch.setattr(
            main,
            "get_settings",
            lambda: SimpleNamespace(
                outbox_dispatch_mode="inline",
                push_dispatch_enabled=True,
                email_dispatch_enabled=False,
                postgres_runtime_probe_enabled=False,
                postgres_auth_rate_limits_enabled=False,
                postgres_auth_enabled=False,
            ),
        )
        monkeypatch.setattr(main, "reconcile_linked_approval_tasks", lambda **_kwargs: 0)
        application = FastAPI()
        application.state.notification_outbox_repository = object.__new__(
            PostgresNotificationOutboxRepository
        )
        with pytest.raises(RuntimeError, match="lifespan-owned PostgreSQL runtime"):
            async with main.lifespan(application):
                pytest.fail("A standalone PostgreSQL push worker must not start")

    asyncio.run(exercise())


def test_lifespan_selects_postgres_dispatcher_only_for_coherent_runtime(monkeypatch) -> None:
    async def exercise() -> None:
        runtime = FakeRuntime()
        settings = SimpleNamespace(
            outbox_dispatch_mode="inline",
            push_dispatch_enabled=True,
            email_dispatch_enabled=True,
            postgres_runtime_probe_enabled=False,
            postgres_auth_rate_limits_enabled=False,
            postgres_auth_enabled=True,
        )
        selected: list[object] = []
        dispatched = asyncio.Event()

        async def postgres_worker(repository) -> None:
            selected.append(repository)
            dispatched.set()
            await asyncio.Event().wait()

        async def email_worker(**_kwargs) -> None:
            return None

        monkeypatch.setattr(main, "get_settings", lambda: settings)
        monkeypatch.setattr(main.PostgresRuntime, "from_environment", lambda: runtime)
        monkeypatch.setattr(main, "reconcile_linked_approval_tasks", lambda **_kwargs: 0)
        monkeypatch.setattr(main, "postgres_notification_dispatch_loop", postgres_worker)
        monkeypatch.setattr(main, "email_dispatch_loop", email_worker)

        application = FastAPI()
        _bind_notification_runtime(application, runtime.repositories)
        async with main.lifespan(application):
            await asyncio.wait_for(dispatched.wait(), timeout=1)
            assert selected == [runtime.repositories.notification_outbox]
            assert application.state.postgres_repositories is runtime.repositories
            assert (
                application.state.notification_outbox_repository
                is runtime.repositories.notification_outbox
            )
        assert runtime.closed is True

    asyncio.run(exercise())


def test_lifespan_refuses_mixed_notification_producer_repository(monkeypatch) -> None:
    async def exercise() -> None:
        runtime = FakeRuntime()
        settings = SimpleNamespace(
            outbox_dispatch_mode="inline",
            push_dispatch_enabled=True,
            email_dispatch_enabled=False,
            postgres_runtime_probe_enabled=False,
            postgres_auth_rate_limits_enabled=False,
            postgres_auth_enabled=True,
        )
        monkeypatch.setattr(main, "get_settings", lambda: settings)
        monkeypatch.setattr(main.PostgresRuntime, "from_environment", lambda: runtime)
        monkeypatch.setattr(main, "reconcile_linked_approval_tasks", lambda **_kwargs: 0)
        application = FastAPI()
        _bind_notification_runtime(application, runtime.repositories)
        application.state.approval_decision_repository = object()

        with pytest.raises(RuntimeError, match="foreign binding"):
            async with main.lifespan(application):
                pytest.fail("Mixed SQLite/PostgreSQL notification authority must fail closed")
        assert runtime.closed is True

    asyncio.run(exercise())


def test_scheduled_dispatch_mode_does_not_start_in_process_workers(monkeypatch) -> None:
    async def exercise() -> None:
        settings = SimpleNamespace(
            outbox_dispatch_mode="scheduled",
            push_dispatch_enabled=True,
            email_dispatch_enabled=True,
            postgres_runtime_probe_enabled=False,
            postgres_auth_rate_limits_enabled=False,
            postgres_auth_enabled=False,
        )
        started: list[str] = []

        async def push_worker(*_args, **_kwargs) -> None:
            started.append("push")
            await asyncio.Event().wait()

        async def email_worker(*_args, **_kwargs) -> None:
            started.append("email")
            await asyncio.Event().wait()

        monkeypatch.setattr(main, "get_settings", lambda: settings)
        monkeypatch.setattr(main, "reconcile_linked_approval_tasks", lambda **_kwargs: 0)
        monkeypatch.setattr(main, "notification_dispatch_loop", push_worker)
        monkeypatch.setattr(main, "email_dispatch_loop", email_worker)

        async with main.lifespan(FastAPI()):
            await asyncio.sleep(0)
            assert started == []

    asyncio.run(exercise())


def test_lifespan_rejects_invalid_notification_repository_before_startup() -> None:
    async def exercise() -> None:
        application = FastAPI()
        application.state.notification_outbox_repository = object()
        try:
            async with main.lifespan(application):
                raise AssertionError("Invalid notification repository unexpectedly started")
        except RuntimeError as error:
            assert str(error) == "Application notification repository has an unsupported adapter"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "postgres_mode",
    ["postgres_auth_enabled"],
)
def test_local_push_loop_refuses_postgres_modes(monkeypatch, postgres_mode) -> None:
    monkeypatch.setattr(
        "app.notifications.get_settings",
        lambda: SimpleNamespace(**{postgres_mode: True, "push_dispatch_enabled": True}),
    )

    async def exercise() -> None:
        from app.notifications import notification_dispatch_loop

        with pytest.raises(RuntimeError, match="PostgreSQL notification dispatcher"):
            await notification_dispatch_loop()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "postgres_mode",
    ["postgres_auth_enabled"],
)
def test_local_notification_creation_refuses_postgres_modes_without_mutating_store(
    monkeypatch, postgres_mode
) -> None:
    from app.notifications import create_notification
    from app.store import store

    monkeypatch.setattr(
        "app.notifications.get_settings",
        lambda: SimpleNamespace(**{postgres_mode: True}),
    )
    before = set(store.notifications)
    with pytest.raises(RuntimeError, match="PostgreSQL notification dispatcher"):
        create_notification(
            title="Approval needed",
            body="Open the app to review.",
            entity_type="approval_request",
            entity_id="approval_guard_test",
            workspace_id="workspace_guard_test",
        )
    assert set(store.notifications) == before


def test_probe_only_mode_keeps_local_notification_helpers_available(monkeypatch) -> None:
    from app.notifications import _require_sqlite_notification_runtime, create_notification
    from app.store import store

    monkeypatch.setattr(
        "app.notifications.get_settings",
        lambda: SimpleNamespace(
            is_production=False,
            postgres_auth_enabled=False,
            postgres_runtime_probe_enabled=True,
        ),
    )
    _require_sqlite_notification_runtime()
    notification = create_notification(
        title="Probe-only local notice",
        body="SQLite remains the active store.",
        entity_type="approval_request",
        entity_id="probe_only_approval",
        workspace_id="probe_only_workspace",
    )

    assert store.notifications[notification.id] == notification


def test_probe_only_mode_keeps_local_notification_dispatch_loop_available(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.notifications.get_settings",
        lambda: SimpleNamespace(
            is_production=False,
            postgres_auth_enabled=False,
            postgres_runtime_probe_enabled=True,
            push_dispatch_enabled=False,
        ),
    )

    async def exercise() -> None:
        from app.notifications import notification_dispatch_loop

        await notification_dispatch_loop()

    asyncio.run(exercise())


def test_postgres_dispatch_loop_drains_inflight_thread_on_cancellation(monkeypatch) -> None:
    started = Event()
    release = Event()
    finished = Event()

    def dispatch(_repository) -> None:
        started.set()
        release.wait(timeout=2)
        finished.set()

    monkeypatch.setattr(postgres_dispatcher, "dispatch_one_postgres_push", dispatch)
    monkeypatch.setattr(
        postgres_dispatcher,
        "get_settings",
        lambda: SimpleNamespace(push_dispatch_enabled=True, push_dispatch_poll_seconds=30),
    )

    async def exercise() -> None:
        pool_closed = Event()

        async def lifespan_owned_worker() -> None:
            try:
                await postgres_dispatcher.postgres_notification_dispatch_loop(object())
            finally:
                # Models lifespan closing the shared PostgreSQL runtime after
                # the worker coroutine has drained its executor thread.
                pool_closed.set()

        task = asyncio.create_task(lifespan_owned_worker())
        started_ready = await asyncio.to_thread(started.wait, 2)
        assert started_ready
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not pool_closed.is_set()
        assert not finished.is_set()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Cancelled PostgreSQL worker did not propagate cancellation")
        assert finished.is_set()
        assert pool_closed.is_set()

    asyncio.run(exercise())


@pytest.mark.parametrize("failed_target_index", [0, 1])
def test_postgres_dispatcher_isolates_sender_exception_per_target(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failed_target_index: int,
) -> None:
    caplog.set_level("INFO", logger="app.postgres_notification_dispatcher")
    notification = Notification(
        id="notification_dispatch_unit",
        title="Approval needed",
        body="Open the app to review.",
        entity_type="approval_request",
        entity_id="approval_dispatch_unit",
        workspace_id="workspace_dispatch_unit",
        push_status="SENDING",
        push_attempts=1,
    )
    devices = [
        DeviceRegistration(
            id="device_a",
            user_id="owner_a",
            workspace_id=notification.workspace_id,
            token="token_a",
        ),
        DeviceRegistration(
            id="device_b",
            user_id="owner_b",
            workspace_id=notification.workspace_id,
            token="token_b",
        ),
    ]
    recorded: list[dict[str, object]] = []

    class Repository:
        def claim_next_due(self, **_: object) -> Notification:
            return notification

        def list_device_targets(self, _notification: Notification) -> list[DeviceRegistration]:
            return devices

        def record_outcome(self, **kwargs: object) -> Notification:
            recorded.append(kwargs)
            return notification

    monkeypatch.setattr(
        postgres_dispatcher,
        "get_settings",
        lambda: SimpleNamespace(
            push_dispatch_enabled=True,
            push_max_attempts=5,
            push_lease_seconds=30,
            push_retry_base_seconds=15,
            push_retry_max_seconds=900,
        ),
    )
    attempts: list[str] = []

    def sender(*, token: str, **_: object) -> None:
        attempt_index = len(attempts)
        attempts.append(token)
        if attempt_index == failed_target_index:
            raise RuntimeError("provider exception may contain sensitive details")
        return None

    postgres_dispatcher.dispatch_one_postgres_push(
        Repository(), now=now_utc(), sender=sender
    )

    assert attempts == ["token_a", "token_b"]
    assert len(recorded) == 1
    assert recorded[0]["push_status"] == "SENT"
    assert recorded[0]["push_error"] == "PARTIAL_DELIVERY"
    assert recorded[0]["next_attempt_at"] is None
    assert recorded[0]["invalid_devices"] == ()
    assert "provider exception" not in repr(recorded[0])
    delivery_log = caplog.text
    assert f"notification_id={notification.id}" in delivery_log
    assert "attempt=1" in delivery_log
    assert "state=SENT" in delivery_log
    assert "accepted_targets=1" in delivery_log
    assert "failed_targets=1" in delivery_log
    assert "token_a" not in delivery_log
    assert "token_b" not in delivery_log
    assert notification.entity_id not in delivery_log
